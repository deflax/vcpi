#include "transport.hpp"
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <new>
#include <stdexcept>
#include <sys/mman.h>
#include <thread>
#include <sys/stat.h>
#include <unistd.h>
#include <time.h>

namespace vcpi {
static_assert(std::atomic<std::uint32_t>::is_always_lock_free, "Cross-process 32-bit atomic must be lock free");
static_assert(std::atomic<std::uint64_t>::is_always_lock_free, "Cross-process 64-bit atomic must be lock free");
namespace {
constexpr std::uint64_t transportMagic = 0x5643504941554431ULL;
std::runtime_error systemError(const char* operation) {
    return std::runtime_error(std::string(operation) + ": " + std::strerror(errno));
}
WakeupMode startupWakeupMode() {
    const auto* value = std::getenv("VCPI_TRACK_WAKEUP");
    if (value == nullptr || std::strcmp(value, "poll") == 0) return WakeupMode::poll;
    if (std::strcmp(value, "notify") == 0) return WakeupMode::notify;
    throw std::invalid_argument("VCPI_TRACK_WAKEUP must be notify or poll");
}
std::size_t timingBucket(std::uint64_t ns) noexcept {
    // Both supported toolchains lower this to a bounded integer bit-scan.
    return 63U - static_cast<unsigned>(__builtin_clzll(ns | 1ULL));
}
}
std::uint64_t monotonicNanoseconds() noexcept {
    timespec now{};
    if (::clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return static_cast<std::uint64_t>(now.tv_sec) * 1000000000ULL
        + static_cast<std::uint64_t>(now.tv_nsec);
}
TimingStats TimingRecord::stats() const noexcept {
    TimingStats result;
    // Only the control caller retries. A busy renderer cannot block stats forever.
    for (unsigned attempt = 0; attempt < 8; ++attempt) {
        const auto before = publication.load(std::memory_order_acquire);
        if (before & 1) continue;
        result.count = count.load(std::memory_order_relaxed);
        result.wakeupNsSum = wakeupNsSum.load(std::memory_order_relaxed);
        result.renderNsSum = renderNsSum.load(std::memory_order_relaxed);
        result.renderCpuNsSum = renderCpuNsSum.load(std::memory_order_relaxed);
        result.cpuClockErrors = cpuClockErrors.load(std::memory_order_relaxed);
        for (std::size_t i = 0; i < timingBuckets; ++i) {
            result.wakeupHistogram[i] = wakeupHistogram[i].load(std::memory_order_relaxed);
            result.renderHistogram[i] = renderHistogram[i].load(std::memory_order_relaxed);
            result.renderCpuHistogram[i] = renderCpuHistogram[i].load(std::memory_order_relaxed);
        }
        std::atomic_thread_fence(std::memory_order_acquire);
        if (publication.load(std::memory_order_relaxed) == before) {
            result.coherent = true;
            break;
        }
    }
    return result;
}
Transport::Transport(const std::string& filePath, int size, bool create) : path(filePath), frames(size) {
    if (frames < 1 || frames > 65536) throw std::invalid_argument("Invalid transport block size");
    // Validate before opening or touching the transport. Joiners use only the
    // creator's immutable shared selection, never their own environment.
    const auto mode = create ? startupWakeupMode() : WakeupMode::poll;
    mappedBytes = sizeof(SharedHeader) + handoffCells * static_cast<std::size_t>(frames) * 2 * sizeof(float);
    const auto descriptor = ::open(path.c_str(), O_RDWR | (create ? O_CREAT : 0), S_IRUSR | S_IWUSR);
    if (descriptor < 0) throw systemError("open audio transport");
    if (create && ::ftruncate(descriptor, static_cast<off_t>(mappedBytes)) != 0) {
        const auto error = systemError("size audio transport");
        ::close(descriptor);
        throw error;
    }
    struct stat status{};
    if (::fstat(descriptor, &status) != 0 || status.st_size != static_cast<off_t>(mappedBytes)) {
        ::close(descriptor);
        throw std::invalid_argument("Audio transport layout/size mismatch");
    }
    mapping = ::mmap(nullptr, mappedBytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0);
    const auto savedErrno = errno;
    ::close(descriptor);
    if (mapping == MAP_FAILED) { mapping = nullptr; errno = savedErrno; throw systemError("map audio transport"); }
    if (create) {
        // Construction and page touching happen before either native audio thread.
        std::memset(mapping, 0, mappedBytes);
        header = new (mapping) SharedHeader(mode);
        header->version = transportVersion;
        header->frames = static_cast<std::uint32_t>(frames);
        header->mappingBytes = mappedBytes;
        header->magic = transportMagic;
    } else {
        header = static_cast<SharedHeader*>(mapping);
        if (header->magic != transportMagic || header->version != transportVersion || header->frames != static_cast<std::uint32_t>(frames) || header->mappingBytes != mappedBytes
            || (header->wakeupMode != WakeupMode::notify && header->wakeupMode != WakeupMode::poll)) {
            ::munmap(mapping, mappedBytes);
            mapping = nullptr;
            header = nullptr;
            throw std::invalid_argument("Incompatible audio transport header");
        }
    }
    try {
        if (header->wakeupMode == WakeupMode::notify)
            wakeup = std::make_unique<Wakeup>(header->wakeup, create);
    } catch (...) {
        ::munmap(mapping, mappedBytes);
        mapping = nullptr;
        header = nullptr;
        throw;
    }
    // Fault in the already-initialized shared pages before callback access.
    volatile const auto* pages = static_cast<const unsigned char*>(mapping);
    for (std::size_t i = 0; i < mappedBytes; i += 4096) (void) pages[i];
}
Transport::~Transport() {
    // The wake object uses shared header storage during teardown.
    wakeup.reset();
    if (mapping) ::munmap(mapping, mappedBytes);
}
void Transport::notify() noexcept {
    if (wakeup) wakeup->notify();
}
void Transport::waitForWork() {
    if (wakeup) wakeup->wait();
    else std::this_thread::sleep_for(std::chrono::microseconds(100));
}
float* Transport::cellAudio(std::size_t index) noexcept {
    auto* data = reinterpret_cast<float*>(static_cast<unsigned char*>(mapping) + sizeof(SharedHeader));
    return data + index * static_cast<std::size_t>(frames) * 2;
}
void Transport::sendMidi(const std::string& message) {
    if (message.empty() || message.size() > midiMaxBytes) throw std::invalid_argument("MIDI message must contain 1..1024 bytes");
    std::lock_guard<std::mutex> lock(midiProducer);
    const auto write = header->midiWrite.load(std::memory_order_relaxed);
    const auto read = header->midiRead.load(std::memory_order_acquire);
    if (static_cast<std::uint32_t>(write - read) >= midiCapacity) throw MidiFull("Track MIDI FIFO is full");
    auto& cell = header->midi[write % midiCapacity];
    cell.size = static_cast<std::uint32_t>(message.size());
    std::memcpy(cell.bytes.data(), message.data(), message.size());
    header->midiWrite.store(write + 1, std::memory_order_release);
}
TransportStats Transport::stats() const {
    TransportStats result;
    result.wakeupMode = header->wakeupMode;
    result.sequence = header->sequence.load(std::memory_order_acquire);
    result.submitted = header->submitted.load(std::memory_order_acquire);
    result.progress = header->progress.load(std::memory_order_acquire);
    result.misses = header->misses.load(std::memory_order_relaxed);
    result.audioPeak = detail::loadPeak(header->audioPeakBits);
    result.realtime = header->realtime.stats(trackRealtimePriority);
    result.rendererPid = header->rendererPid.load(std::memory_order_acquire);
    result.timingEnabled = (header->timing.state.load(std::memory_order_acquire) & 1) != 0;
    result.timing = header->timing.stats();
    // Read consumer first, so a racing dequeue can't make pending underflow.
    const auto read = header->midiRead.load(std::memory_order_acquire);
    const auto write = header->midiWrite.load(std::memory_order_acquire);
    result.midiSent = write;
    result.midiConsumed = read;
    result.midiPending = std::min<std::uint32_t>(write - read, midiCapacity);
    if (header->failureState.load(std::memory_order_acquire) == 2) result.failure = header->failure.data();
    return result;
}
void Transport::setTimingEnabled(bool enabled) noexcept {
    auto& timing = header->timing;
    const auto revision = timing.revision.fetch_add(1, std::memory_order_relaxed) + 1;
    timing.state.store((revision << 1) | static_cast<std::uint64_t>(enabled), std::memory_order_release);
}
bool Transport::timingActive(std::uint64_t state) const noexcept {
    return (state & 1) != 0 && header->timing.state.load(std::memory_order_acquire) == state;
}
void Transport::recordTiming(std::uint64_t state, std::uint64_t requestedNs,
                             std::uint64_t startedNs, std::uint64_t finishedNs,
                             bool cpuValid, std::uint64_t cpuNs) noexcept {
    // A request from a previous enabled interval must not leak into a later one.
    if (!timingActive(state) || requestedNs == 0 || startedNs < requestedNs || finishedNs < startedNs) return;
    const auto wakeupNs = startedNs - requestedNs;
    const auto renderNs = finishedNs - startedNs;
    auto& timing = header->timing;
    // The native renderer is the sole writer. No publication retry or lock.
    timing.publication.fetch_add(1, std::memory_order_relaxed);
    std::atomic_thread_fence(std::memory_order_release);
    timing.wakeupNsSum.fetch_add(wakeupNs, std::memory_order_relaxed);
    timing.renderNsSum.fetch_add(renderNs, std::memory_order_relaxed);
    timing.wakeupHistogram[timingBucket(wakeupNs)].fetch_add(1, std::memory_order_relaxed);
    timing.renderHistogram[timingBucket(renderNs)].fetch_add(1, std::memory_order_relaxed);
    if (cpuValid) {
        timing.renderCpuNsSum.fetch_add(cpuNs, std::memory_order_relaxed);
        timing.renderCpuHistogram[timingBucket(cpuNs)].fetch_add(1, std::memory_order_relaxed);
    } else {
        timing.cpuClockErrors.fetch_add(1, std::memory_order_relaxed);
    }
    timing.count.fetch_add(1, std::memory_order_relaxed);
    timing.publication.fetch_add(1, std::memory_order_release);
}
bool Transport::consumePrevious(float* left, float* right) noexcept {
    const auto expectedSequence = header->sequence.load(std::memory_order_relaxed);
    bool found = false;
    for (std::size_t i = 0; i < handoffCells; ++i) {
        auto& cell = header->audio[i];
        std::uint32_t ready = 2;
        if (!cell.state.compare_exchange_strong(ready, 3, std::memory_order_acquire, std::memory_order_relaxed)) continue;
        if (expectedSequence != 0 && cell.sequence == expectedSequence && !found) {
            const auto* samples = cellAudio(i);
            std::copy_n(samples, frames, left);
            std::copy_n(samples + frames, frames, right);
            found = true;
        }
        // A completed old block is discarded, never replayed on later callbacks.
        cell.state.store(0, std::memory_order_release);
    }
    if (!found) {
        std::fill_n(left, frames, 0.0f);
        std::fill_n(right, frames, 0.0f);
        if (expectedSequence != 0) header->misses.fetch_add(1, std::memory_order_relaxed);
    }
    return found;
}
void Transport::request() noexcept {
    const auto sequence = header->sequence.fetch_add(1, std::memory_order_relaxed) + 1;
    const auto midiEnd = header->midiWrite.load(std::memory_order_acquire);
    // The worker owns at most one request cell. The producer may replace any
    // unread ready cell, so three bounded attempts always find available storage.
    for (std::size_t attempt = 0; attempt < handoffCells; ++attempt) {
        const auto index = (requestCursor + attempt) % handoffCells;
        auto& cell = header->requests[index];
        auto state = cell.state.load(std::memory_order_relaxed);
        if (state != 0 && state != 2) continue;
        if (!cell.state.compare_exchange_strong(state, 1, std::memory_order_acquire, std::memory_order_relaxed)) continue;
        cell.sequence = sequence;
        cell.midiEnd = midiEnd;
        cell.timingState = header->timing.state.load(std::memory_order_acquire);
        cell.requestedNs = (cell.timingState & 1) ? monotonicNanoseconds() : 0;
        cell.state.store(2, std::memory_order_release);
        requestCursor = (index + 1) % handoffCells;
        notify();
        return;
    }
}
bool Transport::takeRequest(std::uint64_t& sequence, std::uint32_t& midiEnd,
                            std::uint64_t& timingState, std::uint64_t& requestedNs) noexcept {
    bool found = false;
    sequence = 0;
    timingState = requestedNs = 0;
    for (auto& cell : header->requests) {
        std::uint32_t ready = 2;
        if (!cell.state.compare_exchange_strong(ready, 3, std::memory_order_acquire, std::memory_order_relaxed)) continue;
        if (cell.sequence > sequence) {
            sequence = cell.sequence;
            midiEnd = cell.midiEnd;
            timingState = cell.timingState;
            requestedNs = cell.requestedNs;
            found = true;
        }
        cell.state.store(0, std::memory_order_release);
    }
    return found;
}
bool Transport::submit(std::uint64_t sequence, const float* left, const float* right) noexcept {
    bool published = false;
    float peak = 0.0f;
    for (std::size_t i = 0; i < handoffCells; ++i) {
        auto& cell = header->audio[i];
        std::uint32_t free = 0;
        if (!cell.state.compare_exchange_strong(free, 1, std::memory_order_acquire, std::memory_order_relaxed)) continue;
        auto* samples = cellAudio(i);
        for (int frame = 0; frame < frames; ++frame) {
            const auto l = left[frame], r = right[frame];
            samples[frame] = l;
            samples[frames + frame] = r;
            peak = detail::peakSample(detail::peakSample(peak, l), r);
        }
        cell.sequence = sequence;
        cell.state.store(2, std::memory_order_release);
        published = true;
        break;
    }
    if (!published) {
        // A rendered block still supplies evidence even when the handoff is full.
        for (int frame = 0; frame < frames; ++frame)
            peak = detail::peakSample(detail::peakSample(peak, left[frame]), right[frame]);
    }
    detail::storePeak(header->audioPeakBits, peak);
    header->progress.fetch_add(1, std::memory_order_relaxed);
    header->submitted.store(sequence, std::memory_order_release);
    return published;
}
void Transport::setFailure(const char* error) noexcept {
    std::uint32_t empty = 0;
    if (!header->failureState.compare_exchange_strong(empty, 1, std::memory_order_acquire, std::memory_order_relaxed)) return;
    const auto length = std::min(std::strlen(error), header->failure.size() - 1);
    std::memcpy(header->failure.data(), error, length);
    header->failure[length] = '\0';
    header->failureState.store(2, std::memory_order_release);
}
} // namespace vcpi
