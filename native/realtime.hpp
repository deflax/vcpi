#pragma once
#include <atomic>
#include <cerrno>
#include <cstdint>
#if defined(__linux__)
#include <sched.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

namespace vcpi {
constexpr int outputRealtimePriority = 20;
constexpr int slotRealtimePriority = 10;
struct RealtimeStats {
    const char* state = "inactive";
    const char* policy = "unknown";
    int priority = 0;
    int requestedPriority = 0;
    int error = 0;
    std::int32_t threadId = 0;
};
// Shared by the renderer and control readers (including other processes).
// Only startup performs a scheduling operation; stats queries stay off render threads.
struct RealtimeRecord {
    enum class Phase : std::uint32_t { Inactive, Pending, Attempted, Exited };
    static_assert(std::atomic<Phase>::is_always_lock_free, "Realtime phase must be lock free");
    static_assert(std::atomic<std::int32_t>::is_always_lock_free, "Realtime diagnostics must be lock free");
    std::atomic<Phase> phase{Phase::Inactive};
    std::atomic<std::int32_t> threadId{0}, promotionError{0};

    void reset(bool pending = false) noexcept {
        phase.store(Phase::Inactive, std::memory_order_release);
        threadId.store(0, std::memory_order_relaxed);
        promotionError.store(0, std::memory_order_relaxed);
        if (pending) phase.store(Phase::Pending, std::memory_order_release);
    }
    void promote(int priority) noexcept {
#if defined(__linux__)
        const auto tid = static_cast<std::int32_t>(::syscall(SYS_gettid));
        sched_param requested{};
        requested.sched_priority = priority;
        const auto error = ::sched_setscheduler(0, SCHED_FIFO, &requested) == 0 ? 0 : errno;
        threadId.store(tid, std::memory_order_relaxed);
        promotionError.store(error, std::memory_order_relaxed);
        phase.store(Phase::Attempted, std::memory_order_release);
#else
        (void) priority;
#endif
    }
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
        const auto policy = ::sched_getscheduler(result.threadId);
        sched_param actual{};
        if (policy < 0 || ::sched_getparam(result.threadId, &actual) != 0) {
            result.state = "unavailable";
            result.error = errno;
            return result;
        }
        result.policy = policyName(policy);
        result.priority = actual.sched_priority;
        result.state = result.error != 0 ? "denied"
            : policy == SCHED_FIFO && actual.sched_priority == requestedPriority ? "active" : "degraded";
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
