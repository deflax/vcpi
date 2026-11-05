#include "runtime.hpp"
#include "plugin_ownership.hpp"
#include <algorithm>
#include <chrono>
#include <cstring>
#include <exception>
#include <stdexcept>
#include <unistd.h>

namespace vcpi {
namespace {
using Clock = std::chrono::steady_clock;
void checkFailure(const Transport& transport) {
    const auto& shared = transport.shared();
    if (shared.failureState.load(std::memory_order_acquire) == 2)
        throw std::runtime_error(shared.failure.data());
}
void appendMidi(juce::MidiBuffer& buffer, const MidiCell& cell) {
    // JUCE 8.0.6 MidiBuffer's documented-public raw storage: native int32 time,
    // uint16 length, bytes. Every event is time zero and already FIFO ordered.
    // Appending avoids MidiBuffer::addEvent's quadratic ordered-insertion scan.
    static_assert(JUCE_MAJOR_VERSION == 8 && JUCE_MINOR_VERSION == 0 && JUCE_BUILDNUMBER == 6,
                  "Recheck MidiBuffer raw encoding when upgrading JUCE");
    const std::int32_t time = 0;
    const auto length = static_cast<std::uint16_t>(cell.size);
    std::array<juce::uint8, sizeof(time) + sizeof(length)> prefix{};
    std::memcpy(prefix.data(), &time, sizeof(time));
    std::memcpy(prefix.data() + sizeof(time), &length, sizeof(length));
    buffer.data.addArray(prefix.data(), static_cast<int>(prefix.size()));
    buffer.data.addArray(cell.bytes.data(), static_cast<int>(cell.size));
}
} // namespace

TrackRunner::TrackRunner(std::shared_ptr<Transport> blocks, std::shared_ptr<Plugin> plugin, double rate, int size)
    : transport(std::move(blocks)), instrument(std::move(plugin)), sampleRate(rate),
      frames((validateAudio(rate, size), size)), left(frames), right(frames) {
    if (!transport || transport->frameCount() != size || !instrument || instrument->isEffect() || !instrument->compatible(rate, size))
        throw std::invalid_argument("TrackRunner requires a compatible instrument and transport");
    midi.ensureSize(midiStorageBytes);
    observedMidi = transport->shared().midiRead.load(std::memory_order_acquire);
    std::uint32_t empty = 0;
    if (!transport->shared().rendererPid.compare_exchange_strong(empty, static_cast<std::uint32_t>(::getpid()), std::memory_order_acq_rel))
        throw std::invalid_argument("Transport already has a native renderer");
    transport->shared().realtime.reset(true);
    try { thread = std::thread(&TrackRunner::run, this); }
    catch (...) {
        transport->shared().realtime.reset();
        transport->shared().rendererPid.store(0, std::memory_order_release);
        throw;
    }
}
TrackRunner::~TrackRunner() { shutdown(); }
void TrackRunner::collectMidi(std::uint32_t end) {
    const auto count = static_cast<std::uint32_t>(end - observedMidi);
    // A barrier may have observed newer MIDI than the last output request.
    if (count > midiCapacity) return;
    for (std::uint32_t i = 0; i < count; ++i) {
        const auto& cell = transport->shared().midi[observedMidi % midiCapacity];
        appendMidi(midi, cell);
        ++observedMidi;
    }
}
void TrackRunner::process() {
    std::fill(left.begin(), left.end(), 0.0f);
    std::fill(right.begin(), right.end(), 0.0f);
    instrument->process(left.data(), right.data(), midi);
    for (const auto& effect : effects) effect->process(left.data(), right.data(), midi);
    midi.clear();
    // Keep FIFO cells reserved while a stopped barrier has buffered messages;
    // producer admission thus bounds both buffered and unbuffered MIDI to 4096.
    transport->shared().midiRead.store(observedMidi, std::memory_order_release);
}
void TrackRunner::run() noexcept {
    // Complete promotion (including a nonterminal denial) before any barrier ACK.
    transport->shared().realtime.promote(trackRealtimePriority);
    while (!stopping.load(std::memory_order_acquire)) {
        if (!gate.tryAcquire()) {
            std::this_thread::sleep_for(std::chrono::microseconds(100));
            continue;
        }
        bool didWork = false;
        try {
            const auto ticket = flushTicket.load(std::memory_order_acquire);
            if (ticket != flushedTicket.load(std::memory_order_relaxed)) {
                collectMidi(flushEnd.load(std::memory_order_acquire));
                flushedTicket.store(ticket, std::memory_order_release);
                didWork = true;
            }
            PluginOwnership pluginLocks;
            if (pluginLocks.tryAcquire(instrument.get(), effects)) {
                std::uint64_t sequence = 0;
                std::uint32_t end = 0;
                if (transport->takeRequest(sequence, end) && sequence > renderedSequence) {
                    collectMidi(end);
                    process();
                    transport->submit(sequence, left.data(), right.data());
                    renderedSequence = sequence;
                    didWork = true;
                }
            }
        } catch (const std::exception& error) {
            transport->setFailure(error.what());
            stopping.store(true, std::memory_order_release);
        } catch (...) {
            transport->setFailure("Unknown native track render failure");
            stopping.store(true, std::memory_order_release);
        }
        gate.release();
        if (!didWork) std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
    transport->shared().realtime.exited();
}
void TrackRunner::barrier() {
    std::lock_guard<std::mutex> control(controls);
    checkFailure(*transport);
    if (stopping.load(std::memory_order_acquire)) throw std::runtime_error("TrackRunner is shut down");
    const auto sequence = transport->shared().sequence.load(std::memory_order_acquire);
    const auto end = transport->shared().midiWrite.load(std::memory_order_acquire);
    flushEnd.store(end, std::memory_order_release);
    const auto ticket = flushTicket.fetch_add(1, std::memory_order_release) + 1;
    const auto deadline = Clock::now() + std::chrono::seconds(5);
    while (flushedTicket.load(std::memory_order_acquire) < ticket || transport->shared().submitted.load(std::memory_order_acquire) < sequence) {
        checkFailure(*transport);
        if (stopping.load(std::memory_order_acquire)) throw std::runtime_error("TrackRunner stopped before barrier");
        if (Clock::now() >= deadline) {
            transport->setFailure("Native track barrier timed out");
            stopping.store(true, std::memory_order_release);
            throw NativeTimeout("Native track barrier timed out");
        }
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
}
void TrackRunner::setEffects(const std::vector<std::shared_ptr<Plugin>>& chain) {
    validateEffects(chain, sampleRate, frames, instrument.get());
    std::lock_guard<std::mutex> control(controls);
    checkFailure(*transport);
    if (stopping.load(std::memory_order_acquire)) throw std::runtime_error("TrackRunner is shut down");
    ControlOwnership ownership(gate);
    effects = chain;
}
std::vector<float> TrackRunner::renderOnce() {
    std::lock_guard<std::mutex> control(controls);
    checkFailure(*transport);
    if (stopping.load(std::memory_order_acquire)) throw std::runtime_error("TrackRunner is shut down");
    ControlOwnership ownership(gate);
    PluginOwnership pluginLocks;
    pluginLocks.acquire(instrument.get(), effects);
    collectMidi(transport->shared().midiWrite.load(std::memory_order_acquire));
    process();
    std::vector<float> result(static_cast<std::size_t>(frames) * 2);
    float peak = 0.0f;
    for (int frame = 0; frame < frames; ++frame) {
        result[static_cast<std::size_t>(frame) * 2] = left[frame];
        result[static_cast<std::size_t>(frame) * 2 + 1] = right[frame];
        peak = detail::peakSample(detail::peakSample(peak, left[frame]), right[frame]);
    }
    detail::storePeak(transport->shared().audioPeakBits, peak);
    transport->shared().progress.fetch_add(1, std::memory_order_relaxed);
    return result;
}
void TrackRunner::shutdown() {
    std::lock_guard<std::mutex> control(controls);
    stopping.store(true, std::memory_order_release);
    if (thread.joinable()) thread.join();
    transport->shared().rendererPid.store(0, std::memory_order_release);
}
} // namespace vcpi
