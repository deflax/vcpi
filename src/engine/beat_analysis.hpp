#pragma once
#include <atomic>
namespace juce { class AudioFormatReader; }

namespace vcpi {
struct BeatAnalysis { double bpm = 0, offset = 0, confidence = 0; };
inline constexpr const char* beatAnalyzerVersion = "qm-dsp-e34a3cc188332ed7c33cd9257ef164de5b587191-grid-v2";

// Non-real-time, bounded-memory analysis. Cancellation is observed while decoding
// and between bounded Queen Mary tracker windows; an interrupted result is unused.
BeatAnalysis analyzeBeats(juce::AudioFormatReader& reader, const std::atomic<bool>& cancel);
} // namespace vcpi
