#include "pd_state.hpp"
#include <juce_audio_processors/juce_audio_processors.h>
#include <charconv>
#include <cmath>
#include <cstring>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string_view>

namespace vcpi::pd_state {
namespace {
constexpr std::size_t maxPdStateBytes = 128 * 1024 * 1024;
std::unique_ptr<juce::XmlElement> plugdataXml(const void* data, std::size_t size) {
    // JUCE's decoder tolerates truncated XML lengths. Do not pass a truncated
    // component back into plugdata's unchecked legacy stream reader.
    if (size < 10 || size > maxPdStateBytes || juce::ByteOrder::littleEndianInt(data) != 0x21324356)
        throw std::invalid_argument("Unsupported plugdata state: invalid binary XML");
    const auto* bytes = static_cast<const char*>(data);
    const auto length = juce::ByteOrder::littleEndianInt(bytes + 4);
    if (length != size - 9 || bytes[size - 1] != 0 || std::memchr(bytes + 8, 0, length)
        || !juce::CharPointer_UTF8::isValidString(bytes + 8, static_cast<int>(length)))
        throw std::invalid_argument("Unsupported plugdata state: invalid XML size or encoding");
    auto xml = juce::AudioProcessor::getXmlFromBinary(data, static_cast<int>(size));
    if (!xml) throw std::invalid_argument("Unsupported plugdata state: unreadable XML");
    return xml;
}
} // namespace

juce::String pdPatchText(const juce::MemoryBlock& data) {
    const auto* bytes = static_cast<const char*>(data.getData());
    const auto size = data.getSize();
    if (size == 0 || size > maxPdPatchBytes || std::memchr(bytes, 0, size)
        || !juce::CharPointer_UTF8::isValidString(bytes, static_cast<int>(size)))
        throw std::invalid_argument("Invalid Pure Data patch: expected bounded UTF-8 text without NUL bytes");

    // Pd records end at an unescaped semicolon, not necessarily at a newline.
    // Check the file framing, not object names: vanilla abstractions and data
    // records must remain usable without opening an editor or resolving them here.
    const std::string_view text(bytes, size);
    std::size_t start = 0;
    int canvases = 0;
    bool haveCanvas = false, escaped = false;
    for (std::size_t pos = 0; pos < size; ++pos) {
        const auto c = static_cast<unsigned char>(bytes[pos]);
        if (c < 32 && c != '\n' && c != '\r' && c != '\t')
            throw std::invalid_argument("Invalid Pure Data patch: control character");
        if (escaped) { escaped = false; continue; }
        if (c == '\\') { escaped = true; continue; }
        if (c != ';') continue;
        const auto begin = text.find_first_not_of(" \t\r\n", start);
        if (begin == std::string_view::npos || begin >= pos)
            throw std::invalid_argument("Invalid Pure Data patch: empty record");
        const auto record = text.substr(begin, pos - begin);
        const bool canvas = record.substr(0, 10) == "#N canvas ";
        const bool structure = record.substr(0, 10) == "#N struct ";
        if ((!haveCanvas && !canvas && !structure) || record.size() < 4 || record[0] != '#'
            || (record[1] != 'N' && record[1] != 'X' && record[1] != 'A') || record[2] != ' ')
            throw std::invalid_argument("Invalid Pure Data patch: expected #N canvas and Pd records");
        if (canvas) {
            auto token = record.find_first_not_of(" \t\r\n", 10);
            for (int coordinate = 0; coordinate < 4; ++coordinate) {
                if (token == std::string_view::npos)
                    throw std::invalid_argument("Invalid Pure Data patch: incomplete canvas header");
                const auto end = record.find_first_of(" \t\r\n", token);
                const auto stop = end == std::string_view::npos ? record.size() : end;
                int value = 0;
                const auto parsed = std::from_chars(record.data() + token, record.data() + stop, value);
                if (parsed.ec != std::errc() || parsed.ptr != record.data() + stop)
                    throw std::invalid_argument("Invalid Pure Data patch: invalid canvas geometry");
                token = end == std::string_view::npos ? end : record.find_first_not_of(" \t\r\n", end);
            }
            if (token == std::string_view::npos)
                throw std::invalid_argument("Invalid Pure Data patch: incomplete canvas header");
            ++canvases;
            haveCanvas = true;
        }
        if (record.substr(0, 11) == "#X restore " && --canvases < 1)
            throw std::invalid_argument("Invalid Pure Data patch: unmatched subpatch restore");
        start = pos + 1;
    }
    if (!haveCanvas || canvases != 1 || text.find_first_not_of(" \t\r\n", start) != std::string_view::npos)
        throw std::invalid_argument("Invalid Pure Data patch: unterminated record or subpatch");
    auto content = juce::String::fromUTF8(bytes, static_cast<int>(size));
    if (content.getNumBytesAsUTF8() != size || std::memcmp(content.toRawUTF8(), bytes, size) != 0)
        throw std::invalid_argument("Invalid Pure Data patch: malformed UTF-8");
    return content;
}
juce::MemoryBlock importPdState(const juce::MemoryBlock& current, const juce::String& content,
                               const juce::String& location) {
    auto outer = plugdataXml(current.getData(), current.getSize());
    auto* component = outer->getChildByName("IComponent");
    juce::MemoryBlock state;
    if (!outer->hasTagName("VST3PluginState") || !component
        || component->getNextElementWithTagName("IComponent")
        || !state.fromBase64Encoding(component->getAllSubText())
        || state.getSize() < 20 || state.getSize() > maxPdStateBytes)
        throw std::invalid_argument("Unsupported plugdata state: missing VST3 component");

    juce::MemoryInputStream input(state, false);
    const auto count = input.readInt();
    if (count < 0 || count > 4096 || static_cast<std::size_t>(count) > (state.getSize() - 20) / 2)
        throw std::invalid_argument("Unsupported plugdata state: invalid patch count");
    const auto* bytes = static_cast<const char*>(state.getData());
    for (int patch = 0; patch < count; ++patch) {
        for (int field = 0; field < 2; ++field) {
            const auto offset = static_cast<std::size_t>(input.getPosition());
            const auto* end = static_cast<const char*>(std::memchr(bytes + offset, 0, state.getSize() - offset));
            if (!end || !juce::CharPointer_UTF8::isValidString(bytes + offset, static_cast<int>(end - bytes - offset)))
                throw std::invalid_argument("Unsupported plugdata state: unterminated or invalid patch record");
            input.setPosition(end - bytes + 1);
        }
    }
    const auto settingsOffset = static_cast<std::size_t>(input.getPosition());
    if (state.getSize() - settingsOffset < 16)
        throw std::invalid_argument("Unsupported plugdata state: truncated settings");
    const auto latency = input.readInt();
    const auto oversampling = input.readInt();
    const auto tail = input.readFloat();
    const auto xmlSize = input.readInt();
    const auto xmlOffset = static_cast<std::size_t>(input.getPosition());
    if (latency < 0 || latency > std::numeric_limits<int>::max() - 64
        || oversampling < 0 || oversampling > 3 || !std::isfinite(tail) || tail < 0.0f
        || xmlSize < 10 || static_cast<std::size_t>(xmlSize) > state.getSize() - xmlOffset)
        throw std::invalid_argument("Unsupported plugdata state: invalid settings or XML bounds");
    auto xml = plugdataXml(bytes + xmlOffset, static_cast<std::size_t>(xmlSize));
    if (!xml->hasTagName("plugdata_save") || xml->getStringAttribute("Version") != "0.9.3"
        || xml->getStringAttribute("Legacy") != "0" || !xml->hasAttribute("Latency")
        || !xml->hasAttribute("Oversampling") || !xml->hasAttribute("TailLength"))
        throw std::invalid_argument("Unsupported plugdata state: requires plugdata 0.9.3 non-legacy layout");
    const auto setting = [&](const char* name) {
        const auto text = xml->getStringAttribute(name).toStdString();
        try {
            std::size_t consumed = 0;
            const auto value = std::stod(text, &consumed);
            if (consumed == text.size() && std::isfinite(value)) return value;
        } catch (const std::exception&) {}
        throw std::invalid_argument("Unsupported plugdata state: invalid numeric XML setting");
    };
    const auto xmlTail = setting("TailLength");
    if (setting("Latency") != latency || setting("Oversampling") != oversampling
        || xmlTail < 0.0 || xmlTail > std::numeric_limits<float>::max())
        throw std::invalid_argument("Unsupported plugdata state: inconsistent XML settings");
    auto* patches = xml->getChildByName("Patches");
    if (!patches || patches->getNextElementWithTagName("Patches")
        || patches->getNumChildElements() != count)
        throw std::invalid_argument("Unsupported plugdata state: inconsistent Patches layout");
    for (const auto* patch : patches->getChildIterator()) {
        if (!patch->hasTagName("Patch") || !patch->hasAttribute("Content") || !patch->hasAttribute("Location"))
            throw std::invalid_argument("Unsupported plugdata state: invalid XML patch record");
    }

    // JUCE appends its own bypass/private state after the processor's XML.
    // Recognize its framing and preserve it verbatim, rather than dropping it
    // or allowing a bogus private-data length to reach the wrapper.
    const auto suffixOffset = xmlOffset + static_cast<std::size_t>(xmlSize);
    const auto suffixSize = state.getSize() - suffixOffset;
    constexpr std::string_view marker = "JUCEPrivateData";
    constexpr std::size_t suffixOverhead = 16 + marker.size();
    if (suffixSize != 0
        && (suffixSize < suffixOverhead
            || juce::ByteOrder::littleEndianInt64(bytes + suffixOffset) != 0
            || std::memcmp(bytes + state.getSize() - marker.size(), marker.data(), marker.size()) != 0
            || juce::ByteOrder::littleEndianInt64(bytes + state.getSize() - marker.size() - 8) != suffixSize - suffixOverhead))
        throw std::invalid_argument("Unsupported plugdata state: invalid JUCE private suffix");

    patches->deleteAllChildElements();
    auto* patch = patches->createNewChildElement("Patch");
    patch->setAttribute("Content", content);
    patch->setAttribute("Location", location);
    patch->setAttribute("SplitIndex", 0);
    patch->setAttribute("PluginMode", 0);
    patch->setAttribute("PluginModeScale", 100);
    juce::MemoryBlock xmlData;
    juce::AudioProcessor::copyXmlToBinary(*xml, xmlData);
    juce::MemoryBlock replacement;
    {
        juce::MemoryOutputStream output(replacement, false);
        output.writeInt(1);
        output.writeString(content);
        output.writeString(location);
        output.write(bytes + settingsOffset, 12);
        output.writeInt(static_cast<int>(xmlData.getSize()));
        output.write(xmlData.getData(), xmlData.getSize());
        output.write(bytes + suffixOffset, suffixSize);
    }
    if (replacement.getSize() > maxPdStateBytes)
        throw std::invalid_argument("Pure Data patch produces an oversized plugdata state");
    component->deleteAllTextElements();
    component->addTextElement(replacement.toBase64Encoding());
    if (auto* controller = outer->getChildByName("IEditController"))
        outer->removeChildElement(controller, true);
    juce::MemoryBlock result;
    juce::AudioProcessor::copyXmlToBinary(*outer, result);
    if (result.getSize() > maxPdStateBytes)
        throw std::invalid_argument("Pure Data patch produces an oversized VST3 state");
    return result;
}
} // namespace vcpi::pd_state
