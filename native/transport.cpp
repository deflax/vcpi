#include "transport.hpp"
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <new>
#include <stdexcept>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace vcpi {
static_assert(std::atomic<std::uint32_t>::is_always_lock_free, "Cross-process 32-bit atomic must be lock free");
static_assert(std::atomic<std::uint64_t>::is_always_lock_free, "Cross-process 64-bit atomic must be lock free");
namespace {
constexpr std::uint64_t transportMagic = 0x5643504941554431ULL;
constexpr std::uint32_t transportVersion = 2;
std::runtime_error systemError(const char* operation) {
    return std::runtime_error(std::string(operation) + ": " + std::strerror(errno));
}
}
Transport::Transport(const std::string& filePath, int size, bool create) : path(filePath), frames(size) {
    if (frames < 1 || frames > 65536) throw std::invalid_argument("Invalid transport block size");
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
        header = new (mapping) SharedHeader;
        header->version = transportVersion;
        header->frames = static_cast<std::uint32_t>(frames);
        header->mappingBytes = mappedBytes;
        header->magic = transportMagic;
    } else {
        header = static_cast<SharedHeader*>(mapping);
        if (header->magic != transportMagic || header->version != transportVersion || header->frames != static_cast<std::uint32_t>(frames) || header->mappingBytes != mappedBytes) {
            ::munmap(mapping, mappedBytes);
            mapping = nullptr;
            header = nullptr;
            throw std::invalid_argument("Incompatible audio transport header");
        }
    }
    // Fault in the already-initialized shared pages before callback access.
    volatile const auto* pages = static_cast<const unsigned char*>(mapping);
    for (std::size_t i = 0; i < mappedBytes; i += 4096) (void) pages[i];
}
Transport::~Transport() { if (mapping) ::munmap(mapping, mappedBytes); }
float* Transport::cellAudio(std::size_t index) noexcept {
    auto* data = reinterpret_cast<float*>(static_cast<unsigned char*>(mapping) + sizeof(SharedHeader));
    return data + index * static_cast<std::size_t>(frames) * 2;
}
void Transport::sendMidi(const std::string& message) {
    if (message.empty() || message.size() > midiMaxBytes) throw std::invalid_argument("MIDI message must contain 1..1024 bytes");
    std::lock_guard<std::mutex> lock(midiProducer);
    const auto write = header->midiWrite.load(std::memory_order_relaxed);
    const auto read = header->midiRead.load(std::memory_order_acquire);
    if (static_cast<std::uint32_t>(write - read) >= midiCapacity) throw MidiFull("Slot MIDI FIFO is full");
    auto& cell = header->midi[write % midiCapacity];
    cell.size = static_cast<std::uint32_t>(message.size());
    std::memcpy(cell.bytes.data(), message.data(), message.size());
    header->midiWrite.store(write + 1, std::memory_order_release);
}
TransportStats Transport::stats() const {
    TransportStats result;
    result.sequence = header->sequence.load(std::memory_order_acquire);
    result.submitted = header->submitted.load(std::memory_order_acquire);
    result.progress = header->progress.load(std::memory_order_acquire);
    result.misses = header->misses.load(std::memory_order_relaxed);
    result.audioPeak = detail::loadPeak(header->audioPeakBits);
    // Read consumer first, so a racing dequeue can't make pending underflow.
    const auto read = header->midiRead.load(std::memory_order_acquire);
    const auto write = header->midiWrite.load(std::memory_order_acquire);
    result.midiSent = write;
    result.midiConsumed = read;
    result.midiPending = std::min<std::uint32_t>(write - read, midiCapacity);
    if (header->failureState.load(std::memory_order_acquire) == 2) result.failure = header->failure.data();
    return result;
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
        cell.state.store(2, std::memory_order_release);
        requestCursor = (index + 1) % handoffCells;
        return;
    }
}
bool Transport::takeRequest(std::uint64_t& sequence, std::uint32_t& midiEnd) noexcept {
    bool found = false;
    sequence = 0;
    for (auto& cell : header->requests) {
        std::uint32_t ready = 2;
        if (!cell.state.compare_exchange_strong(ready, 3, std::memory_order_acquire, std::memory_order_relaxed)) continue;
        if (cell.sequence > sequence) { sequence = cell.sequence; midiEnd = cell.midiEnd; found = true; }
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
