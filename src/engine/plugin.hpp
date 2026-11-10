#pragma once
#include <juce_audio_processors/juce_audio_processors.h>
#include "deck.hpp"
#include "ownership.hpp"
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
    juce::MemoryBlock getState();
    void loadState(const std::string& data);
    void loadSavedState(const std::string& data);
    bool isEffect() const noexcept { return effect; }
    DeckStatus deckStatus() const;
    void deckCommand(const std::string& action, double value = 0);
    void setDeckClock(double bpm, double beat, double sampledAtSeconds);
    void updateDeckCatalog(const std::string& catalog);
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
    ParameterSnapshot snapshot(const ParameterInfo& info) const;
    Scalar parameterValue(const ParameterInfo& info) const;
    ParameterInfo& parameter(const std::string& key);
    void discoverParameters();
    void applyState(const std::string& data);
    ParameterSnapshot deckParameter(const std::string& key) const;
    // Declared first, destroyed last. JUCE's own scoped reference count keeps
    // the message manager alive until every plugin instance has been destroyed,
    // then shuts it down on the owner's control thread, not during C++ atexit.
    juce::ScopedJuceInitialiser_GUI juceLifetime;
    Ownership ownership;
    // Control-only serialization; never acquired by the audio callback.
    mutable std::mutex deckMutex;
    const double sampleRate;
    const int frames;
    bool effect = false;
    bool parametersPending = false;
    PluginMetadata description;
    std::unique_ptr<juce::AudioPluginInstance> instance;
    std::unique_ptr<Deck> deck;
    juce::AudioBuffer<float> audio;
    std::vector<ParameterInfo> parameterInfos;
    std::map<std::string, std::size_t> parameterIndex;
};
void validateAudio(double sampleRate, int frames);
void validateEffects(const std::vector<std::shared_ptr<Plugin>>& effects, double sampleRate, int frames, const Plugin* instrument = nullptr);
} // namespace vcpi
