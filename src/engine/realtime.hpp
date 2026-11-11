#pragma once
#include <atomic>
#include <cerrno>
#include <cstdint>
#if defined(__APPLE__)
#include <cmath>
#include <limits>
#include <mach/mach.h>
#include <mach/mach_error.h>
#include <mach/mach_time.h>
#include <mach/thread_policy.h>
#include <pthread.h>
#endif
#if defined(__linux__)
#include <sched.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

namespace vcpi {
constexpr int outputRealtimePriority = 20;
constexpr int trackRealtimePriority = 10;
struct RealtimeStats {
    const char* state = "inactive";
    const char* policy = "unknown";
    const char* requestedPolicy = "SCHED_FIFO";
    const char* errorDomain = "errno";
    const char* errorMessage = "";
    int priority = 0;
    int requestedPriority = 0;
    int error = 0;
    std::uint64_t threadId = 0;
};
// Shared by the renderer and control readers (including other processes).
// Only startup performs a scheduling operation; stats queries stay off render threads.
struct RealtimeRecord {
    enum class Phase : std::uint32_t { Inactive, Pending, Attempted, Exited, Disabled };
    static_assert(std::atomic<Phase>::is_always_lock_free, "Realtime phase must be lock free");
    static_assert(std::atomic<std::int32_t>::is_always_lock_free, "Realtime diagnostics must be lock free");
    static_assert(std::atomic<std::uint64_t>::is_always_lock_free, "Thread IDs must be lock free");
    std::atomic<Phase> phase{Phase::Inactive};
    std::atomic<std::uint64_t> threadId{0};
    std::atomic<std::int32_t> promotionError{0};

    void reset(bool pending = false) noexcept {
        phase.store(Phase::Inactive, std::memory_order_release);
        threadId.store(0, std::memory_order_relaxed);
        promotionError.store(0, std::memory_order_relaxed);
        if (pending) phase.store(Phase::Pending, std::memory_order_release);
    }
    void promote(int priority, double periodSeconds = 0.0) noexcept {
#if defined(__linux__)
        (void) periodSeconds;
        const auto tid = static_cast<std::int32_t>(::syscall(SYS_gettid));
        sched_param requested{};
        requested.sched_priority = priority;
        const auto error = ::sched_setscheduler(0, SCHED_FIFO, &requested) == 0 ? 0 : errno;
        threadId.store(tid, std::memory_order_relaxed);
        promotionError.store(error, std::memory_order_relaxed);
        phase.store(Phase::Attempted, std::memory_order_release);
#elif defined(__APPLE__)
        (void) priority;
        std::uint64_t tid = 0;
        auto error = pthread_threadid_np(nullptr, &tid) == 0 ? KERN_SUCCESS : KERN_FAILURE;
        mach_timebase_info_data_t timebase{};
        if (error == KERN_SUCCESS) error = mach_timebase_info(&timebase);
        if (error == KERN_SUCCESS) {
            // Mach uses absolute ticks, not nanoseconds. Its scheduler enforces
            // computation >= constraint/2. This is not a CPU reservation.
            const auto ticksPerSecond = 1.0e9 * timebase.denom / timebase.numer;
            const auto periodTicks = periodSeconds * ticksPerSecond;
            const auto computationTicks = periodTicks * 0.5;
            if (!std::isfinite(periodTicks) || periodTicks < 1.0
                || periodTicks > std::numeric_limits<std::uint32_t>::max()
                || computationTicks < 1.0) {
                error = KERN_INVALID_ARGUMENT;
            } else {
                thread_time_constraint_policy_data_t policy{};
                policy.period = static_cast<std::uint32_t>(periodTicks);
                policy.computation = static_cast<std::uint32_t>(computationTicks);
                policy.constraint = policy.period;
                policy.preemptible = true;
                error = thread_policy_set(pthread_mach_thread_np(pthread_self()),
                                          THREAD_TIME_CONSTRAINT_POLICY,
                                          reinterpret_cast<thread_policy_t>(&policy),
                                          THREAD_TIME_CONSTRAINT_POLICY_COUNT);
            }
        }
        threadId.store(tid, std::memory_order_relaxed);
        promotionError.store(error, std::memory_order_relaxed);
        phase.store(Phase::Attempted, std::memory_order_release);
#else
        (void) priority;
        (void) periodSeconds;
#endif
    }
#if defined(__APPLE__)
    void leaveUnchanged() noexcept {
        std::uint64_t tid = 0;
        (void) pthread_threadid_np(nullptr, &tid);
        threadId.store(tid, std::memory_order_relaxed);
        promotionError.store(0, std::memory_order_relaxed);
        phase.store(Phase::Disabled, std::memory_order_release);
    }
#endif
    void exited() noexcept { phase.store(Phase::Exited, std::memory_order_release); }

    RealtimeStats stats(int requestedPriority) const noexcept {
        RealtimeStats result;
        result.requestedPriority = requestedPriority;
#if defined(__linux__)
        const auto current = phase.load(std::memory_order_acquire);
        if (current == Phase::Inactive) return result;
        if (current == Phase::Pending) { result.state = "pending"; return result; }
        result.threadId = threadId.load(std::memory_order_relaxed);
        result.error = promotionError.load(std::memory_order_relaxed);
        if (current == Phase::Exited || result.threadId <= 0) {
            result.state = "unavailable";
            result.error = ESRCH;
            return result;
        }
        const auto tid = static_cast<pid_t>(result.threadId);
        const auto policy = ::sched_getscheduler(tid);
        sched_param actual{};
        if (policy < 0 || ::sched_getparam(tid, &actual) != 0) {
            result.state = "unavailable";
            result.error = errno;
            return result;
        }
        result.policy = policyName(policy);
        result.priority = actual.sched_priority;
        result.state = result.error != 0 ? "denied"
            : policy == SCHED_FIFO && actual.sched_priority == requestedPriority ? "active" : "degraded";
#elif defined(__APPLE__)
        result.requestedPriority = 0;
        result.errorDomain = "mach";
        const auto current = phase.load(std::memory_order_acquire);
        if (requestedPriority == outputRealtimePriority) {
            // CoreAudio owns its callback's scheduling. Do not overwrite it.
            result.policy = result.requestedPolicy = "CoreAudio";
            result.state = current == Phase::Inactive ? "inactive" : "backend-managed";
            return result;
        }
        if (current == Phase::Disabled) {
            result.state = "disabled";
            result.policy = "unchanged";
            result.requestedPolicy = "none";
            result.errorDomain = "none";
            result.threadId = threadId.load(std::memory_order_relaxed);
            return result;
        }
        result.policy = result.requestedPolicy = "THREAD_TIME_CONSTRAINT";
        if (current == Phase::Inactive) return result;
        if (current == Phase::Pending) { result.state = "pending"; return result; }
        result.threadId = threadId.load(std::memory_order_relaxed);
        result.error = promotionError.load(std::memory_order_relaxed);
        if (current == Phase::Exited) {
            result.state = "unavailable";
        } else {
            // A Mach port is task-local: another process cannot query this
            // thread using its port name. Report startup acceptance honestly;
            // the kernel may subsequently demote an over-budget thread.
            result.state = result.error == KERN_SUCCESS ? "configured" : "denied";
        }
        if (result.error != KERN_SUCCESS) result.errorMessage = mach_error_string(result.error);
#else
        result.state = "unsupported";
#endif
        return result;
    }
private:
#if defined(__linux__)
    static const char* policyName(int policy) noexcept {
        switch (policy) {
            case SCHED_OTHER: return "SCHED_OTHER";
            case SCHED_FIFO: return "SCHED_FIFO";
            case SCHED_RR: return "SCHED_RR";
#ifdef SCHED_BATCH
            case SCHED_BATCH: return "SCHED_BATCH";
#endif
#ifdef SCHED_IDLE
            case SCHED_IDLE: return "SCHED_IDLE";
#endif
#ifdef SCHED_DEADLINE
            case SCHED_DEADLINE: return "SCHED_DEADLINE";
#endif
            default: return "unknown";
        }
    }
#endif
};
} // namespace vcpi
