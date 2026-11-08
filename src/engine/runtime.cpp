#include "runtime.hpp"
#include "plugin_ownership.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <exception>
#include <limits>
#include <stdexcept>

namespace vcpi {
namespace {
using Clock = std::chrono::steady_clock;
std::atomic<std::uint64_t> nextRealtimeStart{0};
class PortAudioRuntime {
public:
    PortAudioRuntime() {
        const auto error = Pa_Initialize();
        if (error != paNoError) throw std::runtime_error(std::string("PortAudio initialization failed: ") + Pa_GetErrorText(error));
    }
    ~PortAudioRuntime() { Pa_Terminate(); }
};
void initialisePortAudio() { static PortAudioRuntime runtime; (void) runtime; }
void checkPa(PaError error, const char* operation) {
    if (error != paNoError) throw std::runtime_error(std::string(operation) + ": " + Pa_GetErrorText(error));
}
}
std::vector<DeviceInfo> devices() {
    initialisePortAudio();
    const auto count = Pa_GetDeviceCount();
    if (count < 0) checkPa(count, "PortAudio device enumeration");
    std::vector<DeviceInfo> result;
    result.reserve(static_cast<std::size_t>(count));
    for (int i = 0; i < count; ++i) {
        const auto* info = Pa_GetDeviceInfo(i);
        if (info) result.push_back({info->name, info->maxOutputChannels, info->hostApi, i, info->maxInputChannels});
    }
    return result;
}
std::vector<std::string> hostapis() {
    initialisePortAudio();
    const auto count = Pa_GetHostApiCount();
    if (count < 0) checkPa(count, "PortAudio backend enumeration");
    std::vector<std::string> result;
    for (int i = 0; i < count; ++i) {
        const auto* info = Pa_GetHostApiInfo(i);
        result.emplace_back(info ? info->name : "");
    }
    return result;
}
Engine::Engine(double rate, int size)
    : sampleRate(rate), frames((validateAudio(rate, size), size)), left(frames), right(frames), trackLeft(frames), trackRight(frames) {
    masterMidi.ensureSize(midiStorageBytes);
}
Engine::~Engine() {
    // Unknown callback ownership is a terminal process failure. Never destroy
    // this object underneath a backend thread whose exit was not established.
    if (failure.load(std::memory_order_acquire) == Failure::Ownership) std::terminate();
    try { shutdown(); }
    catch (...) { if (stream) std::terminate(); }
}
void Engine::checkOperational() const {
    switch (failure.load(std::memory_order_acquire)) {
        case Failure::Timeout: throw NativeTimeout("Native output failed after control timeout; owner restart required");
        case Failure::Ownership: throw NativeOwnershipFailure("Output callback ownership is unproven; terminate the audio owner");
        case Failure::None: break;
    }
    if (closed) throw std::runtime_error("Engine is shut down");
}
[[noreturn]] void Engine::ownershipFailure(PaError error, const char* operation) {
    failure.store(Failure::Ownership, std::memory_order_release);
    throw NativeOwnershipFailure(std::string(operation) + ": " + Pa_GetErrorText(error)
                                 + "; callback ownership unproven, terminate the audio owner");
}
void Engine::validateTrack(int index) {
    if (index < 0 || index >= static_cast<int>(trackCount)) throw std::invalid_argument("Track index must be 0..15");
}
float Engine::validateGain(double gain) {
    if (!std::isfinite(gain) || gain < 0.0 || gain > static_cast<double>(std::numeric_limits<float>::max()))
        throw std::invalid_argument("Gain must be a finite nonnegative float");
    return static_cast<float>(gain);
}
void Engine::submitControl(const Command& command) {
    checkOperational();
    const auto write = commandWrite.load(std::memory_order_relaxed);
    const auto read = commandRead.load(std::memory_order_acquire);
    if (static_cast<std::uint32_t>(write - read) >= commandCapacity) throw MidiFull("Native engine control FIFO is full");
    const auto ticket = ++nextTicket;
    commands[write % commandCapacity] = command;
    commands[write % commandCapacity].ticket = ticket;
    commandWrite.store(write + 1, std::memory_order_release);
    if (!running.load(std::memory_order_acquire)) applyControls();
    const auto deadline = Clock::now() + std::chrono::seconds(5);
    while (acknowledged.load(std::memory_order_acquire) < ticket) {
        if (Clock::now() >= deadline) {
            failure.store(Failure::Timeout, std::memory_order_release);
            throw NativeTimeout("Native output control acknowledgement timed out");
        }
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
    reconcileRetained();
}
void Engine::applyControls() noexcept {
    auto read = commandRead.load(std::memory_order_relaxed);
    const auto write = commandWrite.load(std::memory_order_acquire);
    for (std::size_t processed = 0; read != write && processed < commandCapacity; ++processed, ++read) {
        const auto& command = commands[read % commandCapacity];
        if (failure.load(std::memory_order_acquire) != Failure::None) {
            // Once failed, pending controls are discarded, never applied late.
            // Keep their transport lifetimes until successful stream teardown.
            acknowledged.store(command.ticket, std::memory_order_release);
            continue;
        }
        switch (command.type) {
            case CommandType::Attach:
                tracks[command.index].transport = command.transport;
                tracks[command.index].failureReported = false;
                [[fallthrough]];
            case CommandType::Track:
                tracks[command.index].gain = command.gain;
                tracks[command.index].muted = command.muted;
                tracks[command.index].solo = command.solo;
                break;
            case CommandType::MasterGain: masterGain = command.gain; break;
            case CommandType::Mixer:
                for (std::size_t i = 0; i < tracks.size(); ++i) {
                    tracks[i].gain = command.mixerTracks[i].gain;
                    tracks[i].muted = command.mixerTracks[i].muted;
                    tracks[i].solo = command.mixerTracks[i].solo;
                }
                masterGain = command.gain;
                break;
            case CommandType::Barrier: break;
        }
        // Release publishes completed raw-pointer replacement; no old callback
        // access remains when the control thread releases its retired mapping.
        acknowledged.store(command.ticket, std::memory_order_release);
    }
    commandRead.store(read, std::memory_order_release);
}
void Engine::reconcileRetained() {
    // Only called after ACK of the newest command, or after stream close. No
    // callback can modify track pointers until a subsequent control is published.
    std::array<std::shared_ptr<Transport>, trackCount> active{};
    for (std::size_t i = 0; i < tracks.size(); ++i) {
        const auto* pointer = tracks[i].transport;
        if (!pointer) continue;
        for (const auto& item : retained) if (item.get() == pointer) { active[i] = item; break; }
        if (!active[i])
            for (const auto& item : pendingRetirement) if (item.get() == pointer) { active[i] = item; break; }
    }
    retained.swap(active);
    pendingRetirement.clear();
}
void Engine::attach(int index, std::shared_ptr<Transport> transport, double gain, bool muted, bool solo) {
    validateTrack(index);
    const auto scalar = validateGain(gain);
    if (transport && transport->frameCount() != frames) throw std::invalid_argument("Transport block size differs from engine");
    std::lock_guard<std::mutex> control(controls);
    checkOperational();
    if (transport) {
        for (std::size_t i = 0; i < retained.size(); ++i)
            if (i != static_cast<std::size_t>(index) && retained[i] && retained[i]->filePath() == transport->filePath())
                throw std::invalid_argument("A transport can be attached to only one output track");
        pendingRetirement.push_back(transport);
    }
    submitControl({CommandType::Attach, index, transport.get(), scalar, muted, solo, 0});
}
void Engine::setTrack(int index, double gain, bool muted, bool solo) {
    validateTrack(index);
    const auto scalar = validateGain(gain);
    std::lock_guard<std::mutex> control(controls);
    submitControl({CommandType::Track, index, nullptr, scalar, muted, solo, 0});
}
void Engine::setMasterGain(double gain) {
    const auto scalar = validateGain(gain);
    std::lock_guard<std::mutex> control(controls);
    submitControl({CommandType::MasterGain, 0, nullptr, scalar, false, false, 0});
}
void Engine::setMixer(const std::array<std::tuple<double, bool, bool>, trackCount>& mixing, double gain) {
    Command command;
    command.type = CommandType::Mixer;
    command.gain = validateGain(gain);
    for (std::size_t i = 0; i < mixing.size(); ++i) {
        const auto& [trackGain, muted, solo] = mixing[i];
        command.mixerTracks[i] = {validateGain(trackGain), muted, solo};
    }
    std::lock_guard<std::mutex> control(controls);
    submitControl(command);
}
void Engine::setMasterEffects(const std::vector<std::shared_ptr<Plugin>>& chain) {
    validateEffects(chain, sampleRate, frames);
    std::lock_guard<std::mutex> control(controls);
    checkOperational();
    if (running.load(std::memory_order_acquire)) throw std::runtime_error("Stop output before changing master effects");
    masterEffects = chain;
}
void Engine::barrier() {
    std::lock_guard<std::mutex> control(controls);
    submitControl({CommandType::Barrier});
}
void Engine::start(int deviceIndex, const std::string& deviceName) {
    std::lock_guard<std::mutex> control(controls);
    checkOperational();
    if (running.load(std::memory_order_acquire)) return;
    initialisePortAudio();
    auto selected = deviceIndex;
    if (!deviceName.empty()) {
        selected = paNoDevice;
        const auto available = devices();
        for (const auto& device : available)
            if (device.maxOutputChannels >= 2 && device.name == deviceName) { selected = device.index; break; }
        if (selected == paNoDevice) {
            const auto wanted = juce::String(deviceName).toLowerCase();
            for (const auto& device : available) {
                if (device.maxOutputChannels < 2 || !juce::String(device.name).toLowerCase().contains(wanted)) continue;
                if (selected != paNoDevice) throw std::invalid_argument("Ambiguous output device name: " + deviceName);
                selected = device.index;
            }
        }
    } else if (selected == -1) selected = Pa_GetDefaultOutputDevice();
    const auto* device = Pa_GetDeviceInfo(selected);
    if (!device || device->maxOutputChannels < 2) throw std::invalid_argument("No compatible stereo output device");
    const auto* host = Pa_GetHostApiInfo(device->hostApi);
    PaStreamParameters output{};
    output.device = selected;
    output.channelCount = 2;
    output.sampleFormat = paFloat32;
    output.suggestedLatency = device->defaultLowOutputLatency;
    checkPa(Pa_IsFormatSupported(nullptr, &output, sampleRate), "Unsupported output format");
    checkPa(Pa_OpenStream(&stream, nullptr, &output, sampleRate, static_cast<unsigned long>(frames), paDitherOff | paClipOff, &Engine::callback, this), "Open output stream");
    realtime.reset(true);
    realtimeStart = nextRealtimeStart.fetch_add(1, std::memory_order_relaxed) + 1;
    running.store(true, std::memory_order_release);
    const auto error = Pa_StartStream(stream);
    if (error != paNoError) {
        stopUnlocked();
        checkPa(error, "Start output stream");
    }
    outputIndex = selected;
    outputName = device->name;
    backendLabel = host ? host->name : "PortAudio";
}
void Engine::stopUnlocked() {
    if (failure.load(std::memory_order_acquire) == Failure::Ownership)
        throw NativeOwnershipFailure("Cannot retry a stream with unproven ownership; terminate the audio owner");
    PaError stopError = paNoError;
    if (stream) {
        stopError = Pa_StopStream(stream);
        if (stopError != paNoError && stopError != paStreamIsStopped) {
            const auto abortError = Pa_AbortStream(stream);
            if (abortError != paNoError && abortError != paStreamIsStopped)
                ownershipFailure(abortError, "Abort output stream after failed stop");
        }
        const auto closeError = Pa_CloseStream(stream);
        // PortAudio may remove a stream from its list even when Close fails.
        // Preserve all callback state and never retry this possibly invalid handle.
        if (closeError != paNoError) ownershipFailure(closeError, "Close output stream");
        stream = nullptr;
    }
    running.store(false, std::memory_order_release);
    realtime.reset();
    applyControls();
    if (failure.load(std::memory_order_acquire) == Failure::None) reconcileRetained();
    if (stopError != paNoError && stopError != paStreamIsStopped)
        checkPa(stopError, "Stop output stream (abort and close succeeded)");
}
void Engine::stop() { std::lock_guard<std::mutex> control(controls); stopUnlocked(); }
void Engine::shutdown() {
    std::lock_guard<std::mutex> control(controls);
    if (closed) return;
    stopUnlocked();
    for (auto& track : tracks) track.transport = nullptr;
    for (auto& item : retained) item.reset();
    pendingRetirement.clear();
    masterEffects.clear();
    closed = true;
}
int Engine::callback(const void*, void* output, unsigned long count, const PaStreamCallbackTimeInfo*, PaStreamCallbackFlags flags, void* context) noexcept {
    auto& engine = *static_cast<Engine*>(context);
#if defined(__linux__)
    // POD TLS avoids a syscall on subsequent callbacks. A backend replacement
    // thread still gets its own attempt; a stream restart gets a fresh token.
    static thread_local std::uint64_t promotedStart = 0;
    if (promotedStart != engine.realtimeStart) {
        engine.realtime.promote(outputRealtimePriority);
        promotedStart = engine.realtimeStart;
    }
#endif
    if (flags & paOutputUnderflow) engine.underruns.fetch_add(1, std::memory_order_relaxed);
    if (count != static_cast<unsigned long>(engine.frames)) {
        engine.applyControls();
        std::fill_n(static_cast<float*>(output), count * 2, 0.0f);
        engine.renderErrors.fetch_add(1, std::memory_order_relaxed);
        return paContinue;
    }
    engine.render(static_cast<float*>(output));
    return paContinue;
}
void Engine::render(float* output) noexcept {
    const auto started = Clock::now();
    applyControls();
    if (failure.load(std::memory_order_acquire) != Failure::None) {
        std::fill_n(output, static_cast<std::size_t>(frames) * 2, 0.0f);
        eligibleTracks.store(0, std::memory_order_relaxed);
        recordTiming(static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - started).count()));
        return;
    }
    std::fill(left.begin(), left.end(), 0.0f);
    std::fill(right.begin(), right.end(), 0.0f);
    bool hasSolo = false;
    for (const auto& track : tracks) if (track.transport && track.solo) hasSolo = true;
    int eligible = 0;
    for (auto& track : tracks) {
        if (!track.transport) continue;
        if (!track.failureReported && track.transport->shared().failureState.load(std::memory_order_acquire) == 2) {
            renderErrors.fetch_add(1, std::memory_order_relaxed);
            track.failureReported = true;
        }
        track.transport->consumePrevious(trackLeft.data(), trackRight.data());
        track.transport->request();
        if (track.muted || (hasSolo && !track.solo)) continue;
        ++eligible;
        for (int frame = 0; frame < frames; ++frame) {
            left[frame] += trackLeft[frame] * track.gain;
            right[frame] += trackRight[frame] * track.gain;
        }
    }
    eligibleTracks.store(eligible, std::memory_order_relaxed);
    masterMidi.clear();
    if (!masterEffects.empty()) {
        PluginOwnership ownership;
        if (!ownership.tryAcquire(nullptr, masterEffects)) {
            std::fill(left.begin(), left.end(), 0.0f);
            std::fill(right.begin(), right.end(), 0.0f);
        } else {
            try { for (const auto& effect : masterEffects) effect->process(left.data(), right.data(), masterMidi); }
            catch (...) {
                renderErrors.fetch_add(1, std::memory_order_relaxed);
                std::fill(left.begin(), left.end(), 0.0f);
                std::fill(right.begin(), right.end(), 0.0f);
            }
        }
    }
    float peak = 0.0f;
    for (int frame = 0; frame < frames; ++frame) {
        const auto l = left[frame] * masterGain, r = right[frame] * masterGain;
        const auto finalLeft = std::isfinite(l) ? std::clamp(l, -1.0f, 1.0f) : 0.0f;
        const auto finalRight = std::isfinite(r) ? std::clamp(r, -1.0f, 1.0f) : 0.0f;
        output[static_cast<std::size_t>(frame) * 2] = finalLeft;
        output[static_cast<std::size_t>(frame) * 2 + 1] = finalRight;
        peak = std::max(peak, std::max(std::abs(finalLeft), std::abs(finalRight)));
    }
    // A control timeout may arrive while third-party master DSP is executing.
    // Its result must never be published as a late audible control success.
    if (failure.load(std::memory_order_acquire) != Failure::None)
        std::fill_n(output, static_cast<std::size_t>(frames) * 2, 0.0f);
    else
        detail::storePeak(outputPeakBits, peak);
    recordTiming(static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - started).count()));
}
void Engine::recordTiming(std::uint64_t elapsed) noexcept {
    callbackCount.fetch_add(1, std::memory_order_relaxed);
    lastCallbackNs.store(elapsed, std::memory_order_relaxed);
    const auto peak = peakCallbackNs.load(std::memory_order_relaxed);
    if (elapsed > peak) peakCallbackNs.store(elapsed, std::memory_order_relaxed);
    if (static_cast<double>(elapsed) > frames / sampleRate * 1.0e9) deadlineMisses.fetch_add(1, std::memory_order_relaxed);
}
std::vector<float> Engine::renderBlock() {
    std::lock_guard<std::mutex> control(controls);
    checkOperational();
    if (running.load(std::memory_order_acquire)) throw std::runtime_error("Offline rendering requires stopped output");
    std::vector<float> result(static_cast<std::size_t>(frames) * 2);
    render(result.data());
    return result;
}
EngineStats Engine::stats() {
    std::lock_guard<std::mutex> control(controls);
    EngineStats result;
    result.running = running.load(std::memory_order_acquire);
    result.eligibleTracks = eligibleTracks.load(std::memory_order_relaxed);
    result.underruns = underruns.load(std::memory_order_relaxed);
    result.renderErrors = renderErrors.load(std::memory_order_relaxed);
    result.callbackCount = callbackCount.load(std::memory_order_relaxed);
    result.deadlineMisses = deadlineMisses.load(std::memory_order_relaxed);
    result.lastCallbackMs = lastCallbackNs.load(std::memory_order_relaxed) / 1.0e6;
    result.peakCallbackMs = peakCallbackNs.load(std::memory_order_relaxed) / 1.0e6;
    result.blockPeriodMs = frames / sampleRate * 1000.0;
    result.outputPeak = detail::loadPeak(outputPeakBits);
    result.backendLabel = backendLabel;
    result.outputName = outputName;
    result.outputIndex = outputIndex;
    result.realtime = realtime.stats(outputRealtimePriority);
    return result;
}
} // namespace vcpi
