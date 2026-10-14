#pragma once
#include "transport.hpp"
#include <portaudio.h>
#include <array>
#include <atomic>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

namespace vcpi {
class SlotRunner {
public:
    SlotRunner(std::shared_ptr<Transport> transport, std::shared_ptr<Plugin> plugin, double sampleRate, int frames);
    ~SlotRunner();
    void setEffects(const std::vector<std::shared_ptr<Plugin>>& effects);
    void barrier();
    void shutdown();
    std::vector<float> renderOnce();
private:
    void run() noexcept;
    void collectMidi(std::uint32_t end);
    void process();
    std::shared_ptr<Transport> transport;
    std::shared_ptr<Plugin> instrument;
    std::vector<std::shared_ptr<Plugin>> effects;
    double sampleRate;
    int frames;
    Ownership gate;
    std::mutex controls;
    std::atomic<bool> stopping{false};
    std::atomic<std::uint32_t> flushEnd{0};
    std::atomic<std::uint64_t> flushTicket{0}, flushedTicket{0};
    std::uint32_t observedMidi = 0;
    std::uint64_t renderedSequence = 0;
    juce::MidiBuffer midi;
    std::vector<float> left, right;
    std::thread thread;
};
struct DeviceInfo {
    std::string name;
    int maxOutputChannels = 0, hostapi = 0, index = 0, maxInputChannels = 0;
};
std::vector<DeviceInfo> devices();
std::vector<std::string> hostapis();
struct EngineStats {
    bool running = false;
    int eligibleSlots = 0;
    std::uint64_t underruns = 0, renderErrors = 0, callbackCount = 0, deadlineMisses = 0;
    double lastCallbackMs = 0.0, peakCallbackMs = 0.0, blockPeriodMs = 0.0;
    float outputPeak = 0.0f;
    std::string backendLabel, outputName;
};
class Engine {
public:
    Engine(double sampleRate, int frames);
    ~Engine();
    void attach(int index, std::shared_ptr<Transport> transport, double gain, bool muted, bool solo);
    void setSlot(int index, double gain, bool muted, bool solo);
    void setMasterGain(double gain);
    void setMasterEffects(const std::vector<std::shared_ptr<Plugin>>& effects);
    void start(int deviceIndex = -1, const std::string& deviceName = {});
    void stop();
    void shutdown();
    void barrier();
    EngineStats stats();
    std::vector<float> renderBlock();
private:
    static constexpr std::size_t slotCount = 8, commandCapacity = 128;
    enum class CommandType { Attach, Slot, MasterGain, Barrier };
    struct Command {
        CommandType type = CommandType::Barrier;
        int index = 0;
        Transport* transport = nullptr;
        float gain = 1.0f;
        bool muted = false, solo = false;
        std::uint64_t ticket = 0;
    };
    struct Slot {
        Transport* transport = nullptr;
        float gain = 1.0f;
        bool muted = false, solo = false;
        bool failureReported = false;
    };
    void submitControl(Command command);
    void applyControls() noexcept;
    void reconcileRetained();
    void render(float* output) noexcept;
    void stopUnlocked();
    void checkOperational() const;
    void recordTiming(std::uint64_t elapsed) noexcept;
    [[noreturn]] void ownershipFailure(PaError error, const char* operation);
    static int callback(const void*, void*, unsigned long, const PaStreamCallbackTimeInfo*, PaStreamCallbackFlags, void*) noexcept;
    static void validateSlot(int index);
    static float validateGain(double gain);
    const double sampleRate;
    const int frames;
    std::mutex controls;
    std::array<Slot, slotCount> slots{};
    // Lifetimes retained/reclaimed only by the control thread after callback ACK.
    std::array<std::shared_ptr<Transport>, slotCount> retained{};
    std::vector<std::shared_ptr<Transport>> pendingRetirement;
    std::vector<std::shared_ptr<Plugin>> masterEffects;
    std::array<Command, commandCapacity> commands{};
    std::atomic<std::uint32_t> commandWrite{0}, commandRead{0};
    std::atomic<std::uint64_t> acknowledged{0};
    std::uint64_t nextTicket = 0;
    float masterGain = 1.0f;
    std::vector<float> left, right, slotLeft, slotRight;
    juce::MidiBuffer masterMidi;
    PaStream* stream = nullptr;
    std::atomic<bool> running{false};
    enum class Failure : std::uint32_t { None, Timeout, Ownership };
    static_assert(std::atomic<Failure>::is_always_lock_free, "Output failure latch must be lock free");
    std::atomic<Failure> failure{Failure::None};
    bool closed = false;
    std::atomic<int> eligibleSlots{0};
    std::atomic<std::uint64_t> underruns{0}, renderErrors{0}, callbackCount{0}, deadlineMisses{0}, lastCallbackNs{0}, peakCallbackNs{0};
    std::atomic<std::uint32_t> outputPeakBits{0};
    std::string backendLabel, outputName;
};
} // namespace vcpi
