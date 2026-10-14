#pragma once
#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_audio_formats/juce_audio_formats.h>
#include <array>
#include <atomic>
#include <map>
#include <memory>
#include <string>
#include <variant>
#include <vector>

namespace vcpi {
constexpr std::size_t midiCapacity = 4096;
constexpr std::size_t midiMaxBytes = 1024;
constexpr std::size_t midiStorageBytes = midiCapacity * (midiMaxBytes + 32);
constexpr std::size_t maxEffects = 64;
using Scalar = std::variant<double, bool, std::string>;

// A single bounded acquire attempt on the render side; only control threads wait.
class Ownership {
public:
    bool tryAcquire() noexcept {
        std::uint32_t expected = 0;
        return state.compare_exchange_strong(expected, 1, std::memory_order_acquire, std::memory_order_relaxed);
    }
    void acquire();
    void release() noexcept { state.store(0, std::memory_order_release); }
private:
    std::atomic<std::uint32_t> state{0};
};
class ControlOwnership {
public:
    explicit ControlOwnership(Ownership& value) : owner(value) { owner.acquire(); }
    ~ControlOwnership() { owner.release(); }
    ControlOwnership(const ControlOwnership&) = delete;
    ControlOwnership& operator=(const ControlOwnership&) = delete;
private:
    Ownership& owner;
};
struct ParameterSnapshot {
    std::string key, kind, label, units, stringValue;
    std::vector<std::string> validValues;
    double low = 0.0, high = 1.0, rawValue = 0.0;
    bool automatable = false, boolean = false, discrete = false;
    Scalar value = 0.0;
};
struct PluginMetadata {
    std::string name, descriptiveName, manufacturer, category, version, identifier, path, infoType;
    int latency = 0;
};
class Plugin {
public:
    Plugin(const std::string& path, double sampleRate, int frames, const std::string& kind);
    ~Plugin();
    PluginMetadata metadata();
    std::vector<ParameterSnapshot> parameters();
    ParameterSnapshot parameterSnapshot(const std::string& key);
    Scalar getParameter(const std::string& key);
    Scalar setParameter(const std::string& key, const Scalar& value);
    void loadPreset(const std::string& path);
    void loadState(const std::string& data);
    bool isEffect() const noexcept { return effect; }
    bool compatible(double rate, int size) const noexcept { return sampleRate == rate && frames == size; }
    bool tryAcquire() noexcept { return ownership.tryAcquire(); }
    void release() noexcept { ownership.release(); }
    // Caller owns this plugin; stereo is planar and exactly frames long.
    void process(float* left, float* right, juce::MidiBuffer& midi);
private:
    struct ParameterInfo {
        juce::AudioProcessorParameter* parameter = nullptr;
        ParameterSnapshot descriptor;
        bool physical = false;
        std::vector<float> enumRawValues;
        float falseRaw = 0.0f, trueRaw = 1.0f;
    };
    struct Voice { double position = 0.0, rate = 1.0, gain = 1.0; };
    ParameterSnapshot snapshot(const ParameterInfo& info) const;
    Scalar parameterValue(const ParameterInfo& info) const;
    ParameterInfo& parameter(const std::string& key);
    void discoverParameters();
    void loadWav(const juce::File& path);
    void renderWav(float* left, float* right, const juce::MidiBuffer& midi) noexcept;
    // Declared first, destroyed last. JUCE's own scoped reference count keeps
    // the message manager alive until every plugin instance has been destroyed,
    // then shuts it down on the owner's control thread, not during C++ atexit.
    juce::ScopedJuceInitialiser_GUI juceLifetime;
    Ownership ownership;
    const double sampleRate;
    const int frames;
    bool effect = false, wav = false;
    PluginMetadata description;
    std::unique_ptr<juce::AudioPluginInstance> instance;
    juce::AudioBuffer<float> audio;
    std::vector<ParameterInfo> parameterInfos;
    std::map<std::string, std::size_t> parameterIndex;
    std::vector<float> sampleLeft, sampleRight;
    std::array<Voice, 32> voices{};
    std::size_t voiceCount = 0;
};
void validateAudio(double sampleRate, int frames);
void validateEffects(const std::vector<std::shared_ptr<Plugin>>& effects, double sampleRate, int frames, const Plugin* instrument = nullptr);
} // namespace vcpi
