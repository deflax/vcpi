#pragma once
#include <juce_core/juce_core.h>
#include <cstddef>

namespace vcpi::pd_state {
constexpr std::size_t maxPdPatchBytes = 16 * 1024 * 1024;

// Control-thread helpers only; the caller owns the plugin and state application.
juce::String pdPatchText(const juce::MemoryBlock& data);
juce::MemoryBlock importPdState(const juce::MemoryBlock& current, const juce::String& content,
                               const juce::String& location);
} // namespace vcpi::pd_state
