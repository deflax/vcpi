#include "plugin.hpp"
#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>
#include <thread>

namespace vcpi {
static_assert(std::atomic<std::uint32_t>::is_always_lock_free, "Native ownership requires lock-free atomics");
void Ownership::acquire() {
    while (!tryAcquire()) std::this_thread::sleep_for(std::chrono::microseconds(100));
}
void validateAudio(double rate, int frames) {
    if (!std::isfinite(rate) || rate < 8000.0 || rate > 768000.0 || frames < 1 || frames > 65536)
        throw std::invalid_argument("Invalid audio sample rate or block size");
}
void validateEffects(const std::vector<std::shared_ptr<Plugin>>& effects, double rate, int frames, const Plugin* instrument) {
    if (effects.size() > maxEffects) throw std::invalid_argument("Too many insert effects (maximum 64)");
    std::set<const Plugin*> unique;
    if (instrument) unique.insert(instrument);
    for (const auto& item : effects) {
        if (!item || !item->isEffect() || !item->compatible(rate, frames))
            throw std::invalid_argument("Insert must be an effect prepared at the same sample rate and block size");
        if (!unique.insert(item.get()).second) throw std::invalid_argument("A plugin instance cannot occur twice in a chain");
    }
}
namespace {
std::string parameterKey(const std::string& label) {
    std::string result;
    const auto compatible = juce::String::fromUTF8(label.c_str())
        .replace("#", "_sharp").replace(juce::String::fromUTF8("\xe2\x99\xaf"), "_sharp")
        .replace(juce::String::fromUTF8("\xe2\x99\xad"), "_flat").toStdString();
    for (const auto character : compatible) {
        const auto c = static_cast<unsigned char>(character);
        if (std::isalnum(c) && c < 128) result.push_back(static_cast<char>(std::tolower(c)));
        else if (!result.empty() && result.back() != '_') result.push_back('_');
    }
    while (!result.empty() && result.back() == '_') result.pop_back();
    return result.empty() ? "parameter" : result;
}
// Preserve the former control API's display units and typed choices, even
// when a controller (such as Surge XT) does not advertise VST3 stepCount.
bool numericText(const std::string& text, const std::string&, double& result) {
    try {
        std::size_t count = 0;
        result = std::stod(text, &count);
        if (std::isnan(result)) return false;
        const auto suffix = juce::String(text.substr(count)).trim().toLowerCase();
        if (suffix == "khz") { result *= 1000.0; return true; }
        return suffix.isEmpty() || suffix == "hz" || suffix == "ms" || suffix == "db"
            || suffix == "%" || suffix == "sec" || suffix == "seconds" || suffix == "dbtp"
            || suffix == "x" || suffix == "*" || suffix == "," || suffix == ".";
    } catch (...) { return false; }
}
bool trueText(const std::string& text) {
    const auto value = juce::String(text).trim().toLowerCase();
    return value == "on" || value == "yes" || value == "true" || value == "enabled";
}
void validateSavedState(const void* data, std::size_t size) {
    if (size == 0 || size > static_cast<std::size_t>(std::numeric_limits<int>::max()))
        throw std::invalid_argument("Invalid VST3 saved state size");
    const auto xml = juce::AudioProcessor::getXmlFromBinary(data, static_cast<int>(size));
    if (!xml || !xml->hasTagName("VST3PluginState"))
        throw std::invalid_argument("Invalid JUCE VST3 saved state");
    const auto* component = xml->getChildByName("IComponent");
    juce::MemoryBlock componentState;
    if (!component || !componentState.fromBase64Encoding(component->getAllSubText()) || componentState.getSize() == 0)
        throw std::invalid_argument("VST3 saved state has no valid component state");
    if (const auto* controller = xml->getChildByName("IEditController")) {
        juce::MemoryBlock controllerState;
        if (!controllerState.fromBase64Encoding(controller->getAllSubText()))
            throw std::invalid_argument("VST3 saved state has invalid controller state");
    }
}
}
Plugin::Plugin(const std::string& path, double rate, int size, const std::string& kind)
    : sampleRate(rate), frames(size) {
   #if JUCE_MAC
    // Keep Cocoa/message handling available without making this audio owner a Dock app.
    juce::Process::setDockIconVisible(false);
   #endif
    validateAudio(rate, size);
    if (kind != "wav" && kind != "instrument" && kind != "effect")
        throw std::invalid_argument("Plugin kind must be instrument, effect or wav");
    const juce::File file(juce::String::fromUTF8(path.c_str()));
    if (!file.exists()) throw std::runtime_error("Plugin file not found: " + path);
    description.path = file.getFullPathName().toStdString();
    effect = kind == "effect";
    wav = kind == "wav";
    if (wav) {
        loadWav(file);
        description.name = file.getFileNameWithoutExtension().toStdString();
        description.descriptiveName = description.name;
        description.manufacturer = "vcpi";
        description.category = "Sampler";
        description.version = "1";
        description.identifier = "wav:" + description.path;
        description.infoType = "Sample";
        return;
    }
    juce::VST3PluginFormat format;
    juce::OwnedArray<juce::PluginDescription> types;
    format.findAllTypesForFile(types, file.getFullPathName());
    const juce::PluginDescription* selected = nullptr;
    for (const auto* type : types)
        if (type->isInstrument != effect) { selected = type; break; }
    if (!selected) throw std::invalid_argument("VST3 contains no compatible " + kind + ": " + path);
    juce::String error;
    instance = format.createInstanceFromDescription(*selected, rate, size, error);
    if (!instance) throw std::runtime_error("VST3 loading failed: " + error.toStdString());
    // Disable sidechains/auxiliary buses. Prefer stereo but retain a plugin's
    // supported default mono/multichannel layout if stereo is unavailable.
    auto layout = instance->getBusesLayout();
    for (int i = 1; i < layout.inputBuses.size(); ++i) layout.inputBuses.set(i, juce::AudioChannelSet::disabled());
    for (int i = 1; i < layout.outputBuses.size(); ++i) layout.outputBuses.set(i, juce::AudioChannelSet::disabled());
    if (!layout.outputBuses.isEmpty()) layout.outputBuses.set(0, juce::AudioChannelSet::stereo());
    if (!layout.inputBuses.isEmpty()) layout.inputBuses.set(0, effect ? juce::AudioChannelSet::stereo() : juce::AudioChannelSet::disabled());
    if (instance->checkBusesLayoutSupported(layout)) instance->setBusesLayout(layout);
    else {
        layout = instance->getBusesLayout();
        for (int i = 1; i < layout.inputBuses.size(); ++i) layout.inputBuses.set(i, juce::AudioChannelSet::disabled());
        for (int i = 1; i < layout.outputBuses.size(); ++i) layout.outputBuses.set(i, juce::AudioChannelSet::disabled());
        if (instance->checkBusesLayoutSupported(layout)) instance->setBusesLayout(layout);
    }
    if (instance->getTotalNumOutputChannels() < 1)
        throw std::invalid_argument("VST3 has no audio output bus");
    const auto channels = std::max({2, instance->getTotalNumInputChannels(), instance->getTotalNumOutputChannels()});
    if (channels > 64) throw std::invalid_argument("VST3 exceeds 64 audio channels");
    instance->setProcessingPrecision(juce::AudioProcessor::singlePrecision);
    instance->setPlayConfigDetails(instance->getTotalNumInputChannels(), instance->getTotalNumOutputChannels(), rate, size);
    instance->setNonRealtime(false);
    instance->prepareToPlay(rate, size);
    audio.setSize(channels, size, false, true, false);
    juce::MidiBuffer midi;
    midi.ensureSize(midiStorageBytes);
    for (int block = 0; block < 2; ++block) {
        audio.clear();
        midi.clear();
        instance->processBlock(audio, midi);
    }
    description.name = selected->name.toStdString();
    description.descriptiveName = selected->descriptiveName.toStdString();
    description.manufacturer = selected->manufacturerName.toStdString();
    description.category = selected->category.toStdString();
    description.version = selected->version.toStdString();
    description.identifier = selected->createIdentifierString().toStdString();
    description.infoType = effect ? "Effect" : "Instrument";
    discoverParameters();
}
Plugin::~Plugin() = default;
void Plugin::loadWav(const juce::File& file) {
    juce::WavAudioFormat format;
    std::unique_ptr<juce::AudioFormatReader> reader(format.createReaderFor(file.createInputStream().release(), true));
    if (!reader) throw std::runtime_error("Unable to decode WAV: " + file.getFullPathName().toStdString());
    if (reader->sampleRate <= 0.0 || reader->lengthInSamples < 1 || reader->lengthInSamples > std::numeric_limits<int>::max())
        throw std::invalid_argument("WAV sample length or rate is unsupported");
    const auto sourceFrames = static_cast<int>(reader->lengthInSamples);
    juce::AudioBuffer<float> source(2, sourceFrames);
    source.clear();
    if (!reader->read(&source, 0, sourceFrames, 0, true, true)) throw std::runtime_error("WAV decoding failed");
    if (reader->numChannels == 1) source.copyFrom(1, 0, source, 0, 0, sourceFrames);
    const double resampled = static_cast<double>(sourceFrames) * sampleRate / reader->sampleRate;
    if (resampled > std::numeric_limits<int>::max()) throw std::invalid_argument("Resampled WAV is too large");
    const auto count = sourceFrames == 1 ? 1 : std::max(1, static_cast<int>(std::nearbyint(resampled)));
    sampleLeft.resize(static_cast<std::size_t>(count));
    sampleRight.resize(static_cast<std::size_t>(count));
    for (int i = 0; i < count; ++i) {
        // Match sampler.wav.resample_linear's endpoint-aligned linspace, not
        // the subtly different i * source_rate / destination_rate grid.
        const auto position = count > 1 ? static_cast<double>(i) * (sourceFrames - 1) / (count - 1) : 0.0;
        const auto first = static_cast<int>(position), next = std::min(first + 1, sourceFrames - 1);
        const auto fraction = static_cast<float>(position - first);
        sampleLeft[static_cast<std::size_t>(i)] = source.getSample(0, first) * (1.0f - fraction) + source.getSample(0, next) * fraction;
        sampleRight[static_cast<std::size_t>(i)] = source.getSample(1, first) * (1.0f - fraction) + source.getSample(1, next) * fraction;
    }
}
void Plugin::discoverParameters() {
    parameterInfos.clear();
    parameterIndex.clear();
    for (auto* item : instance->getParameters()) {
        ParameterInfo info;
        info.parameter = item;
        auto& descriptor = info.descriptor;
        descriptor.label = item->getName(256).toStdString();
        descriptor.units = item->getLabel().trim().toStdString();
        const auto base = parameterKey(descriptor.label +
            (descriptor.units.empty() || descriptor.units.front() == ':' ? "" : " " + descriptor.units));
        descriptor.key = base;
        for (int suffix = 2; parameterIndex.count(descriptor.key); ++suffix) descriptor.key = base + "_" + std::to_string(suffix);
        descriptor.discrete = item->isDiscrete();
        descriptor.automatable = item->isAutomatable();
        descriptor.low = std::numeric_limits<double>::infinity();
        descriptor.high = -std::numeric_limits<double>::infinity();
        bool allNumeric = true;
        // Numeric display curves use endpoint/midpoint probes. Exhaustively
        // querying thousands of parameters stalls real controllers at startup.
        for (int step = 0; step <= 2; ++step) {
            double value = 0.0;
            if (numericText(item->getText(static_cast<float>(step) / 2.0f, 256).toStdString(), descriptor.units, value)) {
                descriptor.low = std::min(descriptor.low, value);
                descriptor.high = std::max(descriptor.high, value);
            } else {
                allNumeric = false;
                break;
            }
        }
        if (allNumeric) {
            descriptor.kind = "float";
            info.physical = true;
        } else {
            std::map<std::string, std::size_t> choices;
            std::string previous;
            float rangeStart = 0.0f;
            auto finishRange = [&](const std::string& text) {
                // Use an actually observed point, not the midpoint between
                // probes: continuous text controls can change inside that gap.
                const float raw = rangeStart;
                const auto inserted = choices.emplace(text, descriptor.validValues.size());
                if (inserted.second) {
                    descriptor.validValues.push_back(text);
                    info.enumRawValues.push_back(raw);
                } else info.enumRawValues[inserted.first->second] = raw;
            };
            for (int step = 0; step <= 1000; ++step) {
                const float raw = static_cast<float>(step) / 1000.0f;
                const auto text = item->getText(raw, 256).toStdString();
                if (step == 0) previous = text;
                else if (text != previous) {
                    finishRange(previous);
                    previous = text;
                    rangeStart = raw;
                }
            }
            finishRange(previous);
            descriptor.low = 0.0;
            descriptor.high = 1.0;
            descriptor.discrete = true;
            if (descriptor.validValues.size() == 2 &&
                (trueText(descriptor.validValues[0]) || trueText(descriptor.validValues[1]))) {
                descriptor.kind = "bool";
                descriptor.boolean = true;
                const std::size_t truth = trueText(descriptor.validValues[0]) ? 0 : 1;
                info.trueRaw = info.enumRawValues[truth];
                info.falseRaw = info.enumRawValues[1 - truth];
            } else descriptor.kind = "enum";
        }
        parameterIndex.emplace(descriptor.key, parameterInfos.size());
        parameterInfos.push_back(std::move(info));
    }
}
ParameterSnapshot Plugin::snapshot(const ParameterInfo& info) const {
    auto result = info.descriptor;
    result.rawValue = info.parameter->getValue();
    result.stringValue = info.parameter->getText(static_cast<float>(result.rawValue), 256).toStdString();
    if (result.boolean) result.value = trueText(result.stringValue);
    else if (result.kind == "enum") result.value = result.stringValue;
    else {
        double physical = 0.0;
        result.value = info.physical && numericText(result.stringValue, result.units, physical) ? physical : result.rawValue;
    }
    return result;
}
Plugin::ParameterInfo& Plugin::parameter(const std::string& key) {
    const auto found = parameterIndex.find(key);
    if (found == parameterIndex.end()) throw std::invalid_argument("Unknown parameter: " + key);
    return parameterInfos[found->second];
}
PluginMetadata Plugin::metadata() {
    ControlOwnership lock(ownership);
    auto result = description;
    if (instance) result.latency = instance->getLatencySamples();
    return result;
}
std::vector<ParameterSnapshot> Plugin::parameters() {
    ControlOwnership lock(ownership);
    std::vector<ParameterSnapshot> result;
    result.reserve(parameterInfos.size());
    for (const auto& info : parameterInfos) result.push_back(snapshot(info));
    return result;
}
ParameterSnapshot Plugin::parameterSnapshot(const std::string& key) {
    ControlOwnership lock(ownership);
    return snapshot(parameter(key));
}
Scalar Plugin::parameterValue(const ParameterInfo& info) const {
    const double raw = info.parameter->getValue();
    if (info.descriptor.boolean) return trueText(info.parameter->getText(static_cast<float>(raw), 256).toStdString());
    if (info.descriptor.kind != "enum" && !info.physical) return raw;
    const auto text = info.parameter->getText(static_cast<float>(raw), 256).toStdString();
    if (info.descriptor.kind == "enum") return text;
    double physical = 0.0;
    return numericText(text, info.descriptor.units, physical) ? physical : raw;
}
Scalar Plugin::getParameter(const std::string& key) {
    ControlOwnership lock(ownership);
    return parameterValue(parameter(key));
}
Scalar Plugin::setParameter(const std::string& key, const Scalar& value) {
    ControlOwnership lock(ownership);
    auto& info = parameter(key);
    float normalized = 0.0f;
    if (info.descriptor.boolean) {
        if (const auto* boolean = std::get_if<bool>(&value)) normalized = *boolean ? info.trueRaw : info.falseRaw;
        else if (const auto* number = std::get_if<double>(&value)) normalized = *number != 0.0 ? info.trueRaw : info.falseRaw;
        else throw std::invalid_argument("Boolean parameter requires bool or numeric value");
    } else if (info.descriptor.kind == "enum") {
        const auto* text = std::get_if<std::string>(&value);
        if (!text) throw std::invalid_argument("Enum parameter requires a string");
        normalized = info.parameter->getValueForText(juce::String(*text));
        if (!std::isfinite(normalized) ||
            info.parameter->getText(normalized, 256).toStdString() != *text) {
            // Display sampling cannot enumerate every continuously formatted
            // string. The live value is also an exact, controller-owned mapping.
            const float current = info.parameter->getValue();
            if (info.parameter->getText(current, 256).toStdString() == *text)
                normalized = current;
            else {
                const auto found = std::find(info.descriptor.validValues.begin(), info.descriptor.validValues.end(), *text);
                if (found == info.descriptor.validValues.end())
                    throw std::invalid_argument("Invalid enum value for " + key);
                normalized = info.enumRawValues[static_cast<std::size_t>(found - info.descriptor.validValues.begin())];
            }
        }
    } else {
        const auto* number = std::get_if<double>(&value);
        if (!number || std::isnan(*number)) throw std::invalid_argument("Numeric parameter requires a number");
        if (info.physical) {
            const auto requested = std::clamp(*number, info.descriptor.low, info.descriptor.high);
            normalized = info.parameter->getValueForText(juce::String(requested, 12));
            auto displayed = [&](float raw) {
                double actual = 0.0;
                numericText(info.parameter->getText(raw, 256).toStdString(), info.descriptor.units, actual);
                return actual;
            };
            const double actual = std::isfinite(normalized) ? displayed(normalized) : std::numeric_limits<double>::quiet_NaN();
            const bool converted = actual == requested ||
                (std::isfinite(actual) && std::isfinite(requested) &&
                 std::abs(actual - requested) <= std::max(1e-6, std::abs(requested) * 1e-6));
            if (!converted) {
                float low = 0.0f, high = 1.0f;
                const double first = displayed(low), last = displayed(high);
                const bool ascending = first <= last;
                normalized = requested == last ? 1.0f : 0.0f;
                if (requested != first && requested != last) {
                    double bestError = std::numeric_limits<double>::infinity();
                    for (int iteration = 0; iteration < 24; ++iteration) {
                        const float raw = (low + high) * 0.5f;
                        const double current = displayed(raw);
                        if (current == requested) { normalized = raw; break; }
                        const double error = std::abs(current - requested);
                        if (error < bestError) { bestError = error; normalized = raw; }
                        if ((current < requested) == ascending) low = raw;
                        else high = raw;
                    }
                }
            }
        } else normalized = static_cast<float>(*number);
    }
    if (!std::isfinite(normalized)) throw std::invalid_argument("Plugin could not parse parameter value");
    info.parameter->setValueNotifyingHost(std::clamp(normalized, 0.0f, 1.0f));
    parametersPending = true;
    return parameterValue(info);
}
void Plugin::loadPreset(const std::string& path) {
    ControlOwnership lock(ownership);
    if (!instance) throw std::invalid_argument("WAV instruments do not support presets");
    juce::MemoryBlock data;
    if (!juce::File(juce::String(path)).loadFileAsData(data)) throw std::runtime_error("Cannot read preset: " + path);
    if (data.getSize() == 0) throw std::invalid_argument("Empty preset file");
    if (juce::File(juce::String(path)).hasFileExtension("vstpreset")) {
        struct PresetVisitor final : juce::ExtensionsVisitor {
            explicit PresetVisitor(const juce::MemoryBlock& bytes) : data(bytes) {}
            void visitVST3Client(const VST3Client& client) override { accepted = client.setPreset(data); }
            const juce::MemoryBlock& data;
            bool accepted = false;
        } visitor(data);
        instance->getExtensions(visitor);
        if (!visitor.accepted)
            throw std::runtime_error("VST3 rejected preset: " + path);
    } else if (juce::File(juce::String(path)).hasFileExtension("vcv")) {
        // Cardinal stores standard-base64 project data in DPF's "patch"
        // field. Raw .vcv bytes are not a JUCE VST3 state envelope.
        juce::MemoryBlock current;
        instance->getStateInformation(current);
        auto xml = juce::AudioProcessor::getXmlFromBinary(
            current.getData(), static_cast<int>(current.getSize()));
        auto* component = xml ? xml->getChildByName("IComponent") : nullptr;
        juce::MemoryBlock componentData;
        if (!component || !componentData.fromBase64Encoding(component->getAllSubText()))
            throw std::runtime_error("Cardinal has no readable VST3 component state");
        std::string state(static_cast<const char*>(componentData.getData()), componentData.getSize());
        std::size_t patchStart = std::string::npos, patchEnd = std::string::npos;
        bool inStates = false;
        // Preserve the other fields, including host parameter values.
        for (std::size_t pos = 0; pos < state.size();) {
            const auto keyEnd = state.find('\0', pos);
            if (keyEnd == std::string::npos) break;
            const auto key = state.substr(pos, keyEnd - pos);
            pos = keyEnd + 1;
            if (key == "__dpf_state_begin__") { inStates = true; continue; }
            if (key == "__dpf_state_end__") break;
            const auto valueEnd = state.find('\0', pos);
            if (valueEnd == std::string::npos) break;
            if (inStates && key == "patch") {
                patchStart = pos;
                patchEnd = valueEnd;
                break;
            }
            pos = valueEnd + 1;
        }
        if (patchStart == std::string::npos)
            throw std::runtime_error("Plugin does not expose Cardinal's DPF patch state");
        const auto encoded = juce::Base64::toBase64(data.getData(), data.getSize()).toStdString();
        state.replace(patchStart, patchEnd - patchStart, encoded);
        component->deleteAllTextElements();
        component->addTextElement(juce::MemoryBlock(state.data(), state.size()).toBase64Encoding());
        // Do not replay stale controller state over the updated component.
        if (auto* controller = xml->getChildByName("IEditController"))
            xml->removeChildElement(controller, true);
        juce::AudioProcessor::copyXmlToBinary(*xml, current);
        if (current.getSize() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
            throw std::invalid_argument("Preset is too large");
        applyState(std::string(static_cast<const char*>(current.getData()), current.getSize()));
        return; // applyState already refreshes parameter metadata.
    } else {
        if (data.getSize() > static_cast<std::size_t>(std::numeric_limits<int>::max())) throw std::invalid_argument("Preset is too large");
        instance->setStateInformation(data.getData(), static_cast<int>(data.getSize()));
    }
    discoverParameters();
}
juce::MemoryBlock Plugin::getState() {
    ControlOwnership lock(ownership);
    if (!instance) throw std::invalid_argument("WAV instruments do not support state chunks");
    // Commit acknowledged edits before serialization. Some VST3s ignore
    // parameter flushes with zero samples, so pending edits need one block.
    // Ordinary captures don't advance DSP; WAV captures never render.
    if (parametersPending) {
        audio.clear();
        juce::MidiBuffer midi;
        instance->processBlock(audio, midi);
        parametersPending = false;
    }
    juce::MemoryBlock data;
    instance->getStateInformation(data);
    // JUCE always emits an XML envelope, even if the VST3 returned no state.
    // A nonempty envelope alone is not a restorable instrument.
    validateSavedState(data.getData(), data.getSize());
    return data;
}
void Plugin::loadState(const std::string& data) {
    ControlOwnership lock(ownership);
    if (!instance) throw std::invalid_argument("WAV instruments do not support state chunks");
    if (data.empty() || data.size() > static_cast<std::size_t>(std::numeric_limits<int>::max())) throw std::invalid_argument("Invalid state chunk size");
    applyState(data);
}
void Plugin::loadSavedState(const std::string& data) {
    ControlOwnership lock(ownership);
    if (!instance) throw std::invalid_argument("WAV instruments do not support state chunks");
    validateSavedState(data.data(), data.size());
    applyState(data);
}
void Plugin::applyState(const std::string& data) {
    // Some instruments defer streamed state while processing is active.
    // Deactivate under ownership so their state is applied before restarting.
    instance->releaseResources();
    try {
        instance->setStateInformation(data.data(), static_cast<int>(data.size()));
    } catch (...) {
        instance->prepareToPlay(sampleRate, frames);
        throw;
    }
    instance->prepareToPlay(sampleRate, frames);
    // Instruments may apply an enqueued patch only on their next audio block.
    // Preroll the new state without MIDI, then deliver deferred VST3 restart
    // notifications before capturing parameter metadata.
    auto preroll = [&] {
        juce::MidiBuffer midi;
        for (int block = 0; block < 2; ++block) {
            audio.clear();
            midi.clear();
            instance->processBlock(audio, midi);
        }
        juce::MessageManager::getInstance()->runDispatchLoopUntil(1);
    };
    preroll();
    // JUCE resets its host parameter cache before a deferred processor load
    // has landed. Its public state API must synchronize the controller again
    // after preroll; otherwise getters keep the pre-load values.
    instance->setStateInformation(data.data(), static_cast<int>(data.size()));
    preroll();
    parametersPending = false;
    discoverParameters();
}
void Plugin::renderWav(float* left, float* right, const juce::MidiBuffer& midi) noexcept {
    std::fill_n(left, frames, 0.0f);
    std::fill_n(right, frames, 0.0f);
    // Queue order and voice stealing match WavSamplerPlugin: note-off is ignored.
    for (const auto event : midi) {
        if (event.numBytes >= 3 && (event.data[0] & 0xf0) == 0x90 && event.data[2] > 0) {
            if (voiceCount == voices.size()) {
                std::move(voices.begin() + 1, voices.end(), voices.begin());
                --voiceCount;
            }
            voices[voiceCount++] = {0.0, std::pow(2.0, (static_cast<int>(event.data[1]) - 60) / 12.0), event.data[2] / 127.0};
        }
    }
    std::size_t alive = 0;
    for (std::size_t voiceIndex = 0; voiceIndex < voiceCount; ++voiceIndex) {
        auto voice = voices[voiceIndex];
        for (int i = 0; i < frames; ++i) {
            const auto position = voice.position + i * voice.rate;
            if (position >= sampleLeft.size()) break;
            const auto first = static_cast<std::size_t>(position), next = std::min(first + 1, sampleLeft.size() - 1);
            const auto fraction = position - static_cast<double>(first);
            left[i] += static_cast<float>((sampleLeft[first] * (1.0 - fraction) + sampleLeft[next] * fraction) * voice.gain);
            right[i] += static_cast<float>((sampleRight[first] * (1.0 - fraction) + sampleRight[next] * fraction) * voice.gain);
        }
        voice.position += frames * voice.rate;
        if (voice.position < sampleLeft.size()) voices[alive++] = voice;
    }
    voiceCount = alive;
}
void Plugin::process(float* left, float* right, juce::MidiBuffer& midi) {
    if (wav) { renderWav(left, right, midi); return; }
    audio.clear();
    if (effect) {
        if (instance->getTotalNumInputChannels() == 1) {
            auto* mono = audio.getWritePointer(0);
            for (int i = 0; i < frames; ++i) mono[i] = 0.5f * (left[i] + right[i]);
        } else {
            audio.copyFrom(0, 0, left, frames);
            audio.copyFrom(1, 0, right, frames);
        }
    }
    instance->processBlock(audio, midi);
    parametersPending = false;
    std::copy_n(audio.getReadPointer(0), frames, left);
    std::copy_n(audio.getReadPointer(instance->getTotalNumOutputChannels() == 1 ? 0 : 1), frames, right);
}
} // namespace vcpi
