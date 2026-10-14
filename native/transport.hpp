#pragma once
#include "plugin.hpp"
#include "realtime.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <cmath>
#include <cstring>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>

namespace vcpi {
constexpr std::uint32_t transportVersion = 3;
namespace detail {
static_assert(sizeof(float) == sizeof(std::uint32_t) && std::numeric_limits<float>::is_iec559,
              "Audio peaks require 32-bit IEEE-754 floats");
static_assert(std::atomic<std::uint32_t>::is_always_lock_free, "Audio peak atomic must be lock free");
inline float peakSample(float peak, float sample) noexcept {
    return std::isfinite(sample) ? std::max(peak, std::abs(sample)) : peak;
}
inline float loadPeak(const std::atomic<std::uint32_t>& storage) noexcept {
    const auto bits = storage.load(std::memory_order_relaxed);
    float peak;
    std::memcpy(&peak, &bits, sizeof(peak));
    return peak;
}
inline void storePeak(std::atomic<std::uint32_t>& storage, float peak) noexcept {
    // One renderer owns each peak. Finite nonnegative IEEE-754 bits sort by value.
    std::uint32_t bits;
    std::memcpy(&bits, &peak, sizeof(bits));
    if (bits > storage.load(std::memory_order_relaxed)) storage.store(bits, std::memory_order_relaxed);
}
} // namespace detail
constexpr std::size_t handoffCells = 3;
// States: 0 free, 1 producer-owned, 2 ready, 3 consumer-owned.
// All non-atomic payload accesses occur while one process owns the cell.
struct alignas(64) RequestCell {
    std::atomic<std::uint32_t> state{0};
    std::uint64_t sequence = 0;
    std::uint32_t midiEnd = 0;
};
struct alignas(64) AudioCell {
    std::atomic<std::uint32_t> state{0};
    std::uint64_t sequence = 0;
};
struct MidiCell {
    std::uint32_t size = 0;
    std::array<std::uint8_t, midiMaxBytes> bytes{};
};
struct alignas(64) SharedHeader {
    std::uint64_t magic = 0;
    std::uint32_t version = 0, frames = 0;
    std::uint64_t mappingBytes = 0;
    alignas(64) std::atomic<std::uint64_t> sequence{0};
    alignas(64) std::atomic<std::uint64_t> submitted{0};
    alignas(64) std::atomic<std::uint64_t> progress{0};
    alignas(64) std::atomic<std::uint64_t> misses{0};
    alignas(64) std::atomic<std::uint32_t> audioPeakBits{0};
    alignas(64) std::atomic<std::uint32_t> midiWrite{0};
    alignas(64) std::atomic<std::uint32_t> midiRead{0};
    alignas(64) std::atomic<std::uint32_t> rendererPid{0};
    alignas(64) RealtimeRecord realtime;
    alignas(64) std::atomic<std::uint32_t> failureState{0};
    std::array<char, 512> failure{};
    std::array<RequestCell, handoffCells> requests{};
    std::array<AudioCell, handoffCells> audio{};
    std::array<MidiCell, midiCapacity> midi{};
};
struct TransportStats {
    std::uint64_t sequence = 0, submitted = 0, progress = 0, misses = 0;
    std::uint32_t midiSent = 0, midiConsumed = 0, midiPending = 0;
    float audioPeak = 0.0f;
    std::string failure;
    RealtimeStats realtime;
};
class Transport {
public:
    Transport(const std::string& path, int frames, bool create);
    ~Transport();
    Transport(const Transport&) = delete;
    Transport& operator=(const Transport&) = delete;
    void sendMidi(const std::string& message);
    TransportStats stats() const;
    int frameCount() const noexcept { return frames; }
    const std::string& filePath() const noexcept { return path; }
    SharedHeader& shared() noexcept { return *header; }
    const SharedHeader& shared() const noexcept { return *header; }
    bool consumePrevious(float* left, float* right) noexcept;
    void request() noexcept;
    bool takeRequest(std::uint64_t& sequence, std::uint32_t& midiEnd) noexcept;
    bool submit(std::uint64_t sequence, const float* left, const float* right) noexcept;
    void setFailure(const char* error) noexcept;
private:
    float* cellAudio(std::size_t index) noexcept;
    std::string path;
    int frames;
    std::size_t mappedBytes = 0;
    void* mapping = nullptr;
    SharedHeader* header = nullptr;
    std::size_t requestCursor = 0;
    std::mutex midiProducer;
};
class MidiFull : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};
class NativeTimeout : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};
class NativeOwnershipFailure : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};
} // namespace vcpi
