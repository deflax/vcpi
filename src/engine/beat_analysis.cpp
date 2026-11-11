#include "beat_analysis.hpp"
#include <dsp/onsets/DetectionFunction.h>
#include <dsp/tempotracking/TempoTrackV2.h>
#include <juce_audio_formats/juce_audio_formats.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <iomanip>
#include <limits>
#include <stdexcept>
#include <sstream>
#include <vector>

namespace vcpi {
namespace {
// TempoTrackV2 contains constants for 44100 / 512 novelty frames per second.
// Normalize audio, rather than changing that frame rate for high-rate files.
constexpr double analysisRate = 22050.0;
constexpr int hop = 256, window = 512, decodeSize = 16384;
constexpr double step = hop / analysisRate, tolerance = 0.025;
constexpr std::size_t trackerWindow = 4096, trackerOverlap = 1024;
constexpr std::size_t minimumBeats = 16;

std::string analysisSource(const juce::AudioFormatReader& reader) {
    const auto* file = dynamic_cast<const juce::FileInputStream*>(reader.input);
    const auto source = file ? file->getFile().getFullPathName() : juce::String("<stream>");
    return juce::JSON::toString(juce::var(source), true).toStdString();
}

// Only used by the non-real-time analysis worker. Raw stderr follows the
// daemon's existing debug routing; never emit per-beat or render-thread logs.
struct Diagnostics {
    const char* reason = "cancelled";
    std::size_t windows = 0, silentWindows = 0, invalidWindows = 0;
    std::size_t predicted = 0, boundary = 0, weak = 0, supported = 0;
    std::size_t gaps = 0, unstable = 0, regions = 0, matched = 0, total = 0;
    std::size_t halfBeatMatched = 0;
    bool twoStep = false;
    double bpm = 0, coverage = -1, rms = -1, confidence = -1;

    void log(const juce::AudioFormatReader& reader) const {
        const auto source = analysisSource(reader);
        std::ostringstream message;
        message << std::fixed << std::setprecision(4)
                << "beat-analysis source=" << source
                << " reason=" << reason
                << " windows=" << windows << " silent_windows=" << silentWindows
                << " invalid_tracker_windows=" << invalidWindows
                << " predicted_beats=" << predicted << " boundary_beats=" << boundary
                << " weak_beats=" << weak << " supported_beats=" << supported
                << " gap_candidates=" << gaps << " unstable_candidates=" << unstable
                << " stable_regions=" << regions << " matched_beats=" << matched
                << " region_beats=" << total
                << " aligned_beats=" << matched - halfBeatMatched
                << " half_beat_matches=" << halfBeatMatched;
        message << " two_step_double_time=" << (twoStep ? "true" : "false");
        if (bpm > 0) message << " candidate_bpm=" << bpm;
        if (twoStep) message << " selected_bpm=" << bpm * 2;
        if (coverage >= 0) message << " coverage=" << coverage;
        if (rms >= 0) message << " rms_error_ms=" << rms * 1000;
        if (confidence >= 0) message << " confidence=" << confidence;
        message << " required_confidence=0.65 tolerance_ms=25 minimum_beats=16";
        std::fprintf(stderr, "%s\n", message.str().c_str());
    }
};

struct Point { double beat, time; };
struct Fit {
    double x = 0, y = 0, xx = 0, xy = 0, count = 0;
    void add(double beat, double time) {
        x += beat; y += time; xx += beat * beat; xy += beat * time; ++count;
    }
    double period() const { return (count * xy - x * y) / (count * xx - x * x); }
    double offset() const { return (y - period() * x) / count; }
};
struct Region {
    std::vector<Point> points;
    double period = 0, offset = 0;
    double span() const { return points.back().time - points.front().time; }
};

// Choose one phase interpretation for the entire region, not for each point:
// a drifting region must not hop between subdivisions to manufacture a fit.
double regionPhase(const Region& region, double period, double offset) {
    const double midpointBeat = region.points.back().beat * 0.5;
    const double phase = region.offset - offset + midpointBeat * (region.period - period);
    return std::abs(std::remainder(phase, period)) > period * 0.25 ? 0.5 : 0.0;
}

void logRegion(const Region& region, std::size_t index, double period, double offset,
               std::size_t phaseRejected, std::size_t matched, double phase, const std::string& source) {
    const double periodLimit = tolerance * 2 / region.points.size();
    const bool tempoCompatible = std::abs(region.period - period) <= periodLimit;
    const char* disagreement = tempoCompatible ? (phaseRejected ? "phase" : "none")
                                              : (phaseRejected ? "both" : "tempo");
    // Compare fitted local-grid phase at both ends: a tempo mismatch can align
    // briefly, then drift. Signed phase wraps to half a global beat.
    const double phaseStart = std::remainder(region.offset - offset, period);
    const double phaseEnd = std::remainder(
        region.offset + region.points.back().beat * region.period - offset, period);
    std::ostringstream message;
    message << std::fixed << std::setprecision(4)
            << "beat-analysis-region source=" << source << " region=" << index
            << " start_seconds=" << region.points.front().time
            << " end_seconds=" << region.points.back().time
            << " bpm=" << 60 / region.period << " beats=" << region.points.size()
            << " candidate_bpm=" << 60 / period
            << " period_delta_ms=" << (region.period - period) * 1000
            << " allowed_period_delta_ms=" << periodLimit * 1000
            << " phase_start_ms=" << phaseStart * 1000
            << " phase_end_ms=" << phaseEnd * 1000
            << " phase_rejected_beats=" << phaseRejected
            << " matched_beats=" << matched
            << " aligned_beats=" << (phase == 0 ? matched : 0)
            << " half_beat_matches=" << (phase == 0.5 ? matched : 0)
            << " phase_group=" << (phase == 0.5 ? "half-beat" : "aligned")
            << " tempo_compatible=" << (tempoCompatible ? "true" : "false")
            << " disagreement=" << disagreement;
    std::fprintf(stderr, "%s\n", message.str().c_str());
}

// Fit consecutive, acoustically supported tracker beats, not an arbitrary grid
// through all the tracker's predictions (which also exist for white noise).
void appendRegions(const std::vector<Point>& points, std::vector<Region>& regions, Diagnostics& diagnostic) {
    std::size_t begin = 0;
    while (begin + minimumBeats <= points.size()) {
        Fit fit;
        std::size_t end = begin;
        for (; end < begin + minimumBeats; ++end) {
            if (end > begin && points[end].beat != points[end - 1].beat + 1) break;
            fit.add(points[end].beat - points[begin].beat, points[end].time - points[begin].time);
        }
        if (end != begin + minimumBeats) { ++diagnostic.gaps; ++begin; continue; }
        double period = fit.period(), offset = fit.offset();
        bool stable = period >= 60.0 / 200 && period <= 60.0 / 60;
        for (auto i = begin; stable && i < end; ++i)
            stable = std::abs(points[i].time - points[begin].time - offset -
                              (points[i].beat - points[begin].beat) * period) <= tolerance;
        if (!stable) { ++diagnostic.unstable; ++begin; continue; }
        for (; end < points.size(); ++end) {
            if (points[end].beat != points[end - 1].beat + 1 ||
                std::abs(points[end].time - points[begin].time - offset -
                         (points[end].beat - points[begin].beat) * period) > tolerance) break;
            auto extended = fit;
            extended.add(points[end].beat - points[begin].beat, points[end].time - points[begin].time);
            const double nextPeriod = extended.period(), nextOffset = extended.offset();
            // Bounding the change at both ends bounds the change everywhere in
            // this region without a quadratic rescan for every added beat.
            if (std::abs(nextOffset - offset) +
                std::abs(nextPeriod - period) * (points[end].beat - points[begin].beat) > tolerance / 2) break;
            fit = extended; period = nextPeriod; offset = nextOffset;
        }
        Region region;
        region.period = period;
        region.offset = points[begin].time + offset;
        region.points.reserve(end - begin);
        for (auto i = begin; i < end; ++i)
            region.points.push_back({points[i].beat - points[begin].beat, points[i].time});
        regions.push_back(std::move(region));
        begin = end;
    }
}

// A two-step backbeat can look like a perfectly stable half-time pulse. Look
// between those pulses for the alternating straight/displaced kick pattern:
// [backbeat, hat, hat, kick, backbeat, hat, kick, hat], or its bar rotation.
// Uniform eighth/sixteenth notes are not evidence for choosing double time.
// This is a rhythmic preference, not a genre classifier: identical audio can
// legitimately be counted in either octave.
bool twoStepDoubleTime(const std::vector<double>& novelty, const std::vector<Region>& regions,
                       double period, double offset, const std::atomic<bool>& cancel) {
    if (period < 60.0 / 100 || period > 60.0 / 70) return false;
    std::size_t bars = 0, supported = 0;
    auto onset = [&](double time) {
        // Invert the novelty-frame centroid correction used by supported beats.
        const auto center = static_cast<long long>(std::llround(
            (time - (window / 2.0 - hop / 2.0) / analysisRate) / step - 2.0));
        double peak = 0;
        for (int delta = -2; delta <= 2; ++delta) {
            const auto index = center + delta;
            if (index >= 0 && static_cast<std::size_t>(index) < novelty.size())
                peak = std::max(peak, novelty[static_cast<std::size_t>(index)]);
        }
        return peak;
    };
    for (const auto& region : regions) {
        if (cancel.load(std::memory_order_relaxed)) return false;
        if (std::abs(region.period - period) > tolerance * 2 / region.points.size()) continue;
        const double phase = regionPhase(region, period, offset);
        const double start = offset + (std::ceil((region.points.front().time - offset) / period - phase)
                                       + phase) * period;
        std::array<std::size_t, 2> patterns{};
        std::size_t count = 0;
        for (double time = start; time + 2 * period <= region.points.back().time; time += 2 * period) {
            if (cancel.load(std::memory_order_relaxed)) return false;
            std::array<double, 8> energy{};
            for (std::size_t i = 0; i < energy.size(); ++i)
                energy[i] = onset(time + i * period / 4);
            for (int rotation = 0; rotation < 2; ++rotation) {
                auto at = [&](int index) { return energy[static_cast<std::size_t>((index + rotation * 4) % 8)]; };
                const double background = std::max({at(1), at(2), at(5), at(7)});
                const double backbeat = std::min(at(0), at(4));
                const double kicks = std::min(at(3), at(6));
                if (backbeat > background * 1.5 && kicks > background * 1.5 &&
                    kicks > backbeat * 0.2)
                    ++patterns[static_cast<std::size_t>(rotation)];
            }
            ++count;
        }
        bars += count;
        supported += std::max(patterns[0], patterns[1]);
    }
    return supported >= 8 && supported >= bars * 0.6;
}

BeatAnalysis fitGrid(const std::vector<double>& novelty, const std::atomic<bool>& cancel,
                     Diagnostics& diagnostic, const juce::AudioFormatReader& reader) {
    std::vector<Region> regions;
    for (std::size_t start = 0; start < novelty.size(); start += trackerWindow - trackerOverlap) {
        if (cancel.load(std::memory_order_relaxed)) return {};
        const auto end = std::min(start + trackerWindow, novelty.size());
        if (end - start < 768) break; // QM needs at least two tempo-analysis frames.
        ++diagnostic.windows;
        const auto peak = *std::max_element(novelty.begin() + start, novelty.begin() + end);
        if (peak < 1e-4) { ++diagnostic.silentWindows; continue; }
        std::vector<double> df(novelty.begin() + start, novelty.begin() + end);
        // Unit scaling makes support tests independent of the recording's gain.
        for (auto& value : df) value /= peak;
        std::vector<double> periods(df.size(), 0), tempi, beats;
        TempoTrackV2 tracker(analysisRate, hop);
        tracker.calculateBeatPeriod(df, periods, tempi);
        if (cancel.load(std::memory_order_relaxed)) return {};
        if (std::any_of(periods.begin(), periods.end(), [](double p) { return p < 1 || !std::isfinite(p); })) {
            ++diagnostic.invalidWindows;
            continue;
        }
        tracker.calculateBeats(df, periods, beats);
        if (cancel.load(std::memory_order_relaxed)) return {};
        diagnostic.predicted += beats.size();
        std::vector<Point> supported;
        supported.reserve(beats.size());
        for (std::size_t b = 0; b < beats.size(); ++b) {
            const int center = static_cast<int>(beats[b]);
            // Discard the tracker's warmup/tail, except at the file boundaries.
            if ((start != 0 && center < 256) ||
                (end != novelty.size() && center >= static_cast<int>(df.size()) - 256) ||
                center < 4 || center + 4 >= static_cast<int>(df.size())) {
                ++diagnostic.boundary;
                continue;
            }
            double background = 0;
            int backgroundCount = 0;
            for (int i = std::max(0, center - 16); i <= std::min(static_cast<int>(df.size()) - 1, center + 16); ++i) {
                if (std::abs(i - center) <= 3) continue;
                background += df[static_cast<std::size_t>(i)]; ++backgroundCount;
            }
            background /= std::max(1, backgroundCount);
            double localPeak = 0, weight = 0, moment = 0;
            for (int i = center - 3; i <= center + 3; ++i) {
                const double value = df[static_cast<std::size_t>(i)];
                localPeak = std::max(localPeak, value);
                const double excess = std::max(0.0, value - background);
                weight += excess; moment += excess * i;
            }
            if (localPeak < 0.02 || localPeak <= background * 2.0 || weight <= 0) {
                ++diagnostic.weak;
                continue;
            }
            // Each retained DF index refers to original frame index +2. Windows
            // start at frame*hop; their centers are window/2 later. Complex
            // spectral difference compares successive windows, so its burst's
            // centroid has another half-hop delay, removed here. No source-rate
            // dependent magic offset, and no first-two-frame phase loss.
            const double time = (static_cast<double>(start) + moment / weight + 2.0) * step +
                                (window / 2.0 - hop / 2.0) / analysisRate;
            supported.push_back({static_cast<double>(b), time});
        }
        diagnostic.supported += supported.size();
        appendRegions(supported, regions, diagnostic);
        if (end == novelty.size()) break;
    }
    diagnostic.regions = regions.size();
    if (cancel.load(std::memory_order_relaxed)) return {};
    if (regions.empty()) { diagnostic.reason = "no-stable-regions"; return {}; }
    const auto anchor = std::max_element(regions.begin(), regions.end(),
        [](const Region& a, const Region& b) { return a.span() < b.span(); });
    double period = anchor->period, offset = anchor->offset;
    const double origin = offset;
    // Keep the longest region's phase as the anchor. Opposite-phase regions
    // contribute half-integer beat coordinates, never an averaged quarter-beat
    // offset or a doubled tempo. Confidence then measures agreement with this
    // two-phase interpretation, not certainty that the anchor is the kick.
    // Centered regression avoids cancellation in hour-long files.
    for (int pass = 0; pass < 3; ++pass) {
        Fit fit;
        for (const auto& region : regions) {
            if (cancel.load(std::memory_order_relaxed)) return {};
            if (std::abs(region.period - period) > tolerance * 2 / region.points.size()) continue;
            const double phase = regionPhase(region, period, offset);
            for (const auto& point : region.points) {
                const double beat = std::round((point.time - offset) / period - phase) + phase;
                if (std::abs(point.time - offset - beat * period) <= tolerance)
                    fit.add(beat, point.time - origin);
            }
        }
        if (fit.count < minimumBeats) {
            diagnostic.reason = "insufficient-fit-beats";
            return {};
        }
        period = fit.period(); offset = origin + fit.offset();
    }
    double squaredError = 0;
    std::size_t matched = 0, total = 0, regionIndex = 0;
    const auto source = analysisSource(reader);
    for (const auto& region : regions) {
        std::size_t regionMatched = 0, phaseRejected = 0;
        const bool tempoCompatible = std::abs(region.period - period) <= tolerance * 2 / region.points.size();
        const double phase = regionPhase(region, period, offset);
        for (const auto& point : region.points) {
            ++total;
            const double directError = std::abs(std::remainder(point.time - offset, period));
            const double error = std::abs(std::remainder(point.time - offset - phase * period, period));
            if (directError > tolerance) ++phaseRejected;
            if (error <= tolerance && tempoCompatible) {
                ++matched; ++regionMatched; squaredError += error * error;
                if (phase == 0.5) ++diagnostic.halfBeatMatched;
            }
        }
        logRegion(region, ++regionIndex, period, offset, phaseRejected, regionMatched, phase, source);
    }
    diagnostic.matched = matched;
    diagnostic.total = total;
    diagnostic.coverage = total ? static_cast<double>(matched) / total : 0;
    if (matched) diagnostic.rms = std::sqrt(squaredError / matched);
    if (std::isfinite(period) && period > 0) diagnostic.bpm = 60 / period;
    if (matched < minimumBeats) { diagnostic.reason = "insufficient-matched-beats"; return {}; }
    if (matched < total * 0.6) { diagnostic.reason = "coverage-below-0.60"; return {}; }
    if (!std::isfinite(period)) { diagnostic.reason = "invalid-period"; return {}; }
    if (period < 60.0 / 200 || period > 60.0 / 60) {
        diagnostic.reason = "tempo-outside-60-200";
        return {};
    }
    const double coverage = static_cast<double>(matched) / total;
    const double regularity = std::max(0.0, 1.0 - std::sqrt(squaredError / matched) / tolerance);
    const double confidence = coverage * regularity;
    diagnostic.confidence = confidence;
    if (confidence < 0.65) { diagnostic.reason = "confidence-below-0.65"; return {}; }
    diagnostic.twoStep = twoStepDoubleTime(novelty, regions, period, offset, cancel);
    if (cancel.load(std::memory_order_relaxed)) return {};
    if (diagnostic.twoStep) period *= 0.5;
    diagnostic.reason = "accepted";
    return {60 / period, std::fmod(std::fmod(offset, period) + period, period), confidence};
}
} // namespace

BeatAnalysis analyzeBeats(juce::AudioFormatReader& reader, const std::atomic<bool>& cancel) {
    Diagnostics diagnostic;
    DFConfig config{hop, window, DF_COMPLEXSD, 3, false, -1, -1};
    DetectionFunction detector(config);
    juce::AudioBuffer<float> audio(2, decodeSize);
    std::array<double, window> samples{};
    int buffered = 0, zeroWindows = 0;
    std::size_t frameIndex = 0;
    std::vector<double> novelty;
    const double expected = std::ceil(static_cast<double>(reader.lengthInSamples) / reader.sampleRate / step);
    if (expected < static_cast<double>(novelty.max_size())) novelty.reserve(static_cast<std::size_t>(expected));
    auto appendSample = [&](double sample) {
        samples[static_cast<std::size_t>(buffered++)] = sample;
        if (buffered != window) return;
        const bool silent = std::all_of(samples.begin(), samples.end(), [](double x) { return x == 0; });
        zeroWindows = silent ? std::min(3, zeroWindows + 1) : 0;
        // Two zero windows flush complex-spectrum history. Thereafter an exact
        // zero DF is equivalent to running the FFT, important for sparse WAVs.
        const double value = zeroWindows <= 2 ? detector.processTimeDomain(samples.data()) : 0;
        if (frameIndex++ >= 2) novelty.push_back(value);
        std::copy(samples.begin() + hop, samples.end(), samples.begin());
        buffered = window - hop;
    };
    // Area resampling integrates source samples into fixed-rate analysis bins.
    // It keeps just one accumulator, preserves timing at every source rate, and
    // suppresses high-rate aliasing without touching the playback reader/ring.
    const double ratio = reader.sampleRate / analysisRate;
    double remaining = ratio, accumulator = 0;
    for (juce::int64 start = 0; start < reader.lengthInSamples; start += decodeSize) {
        if (cancel.load(std::memory_order_relaxed)) { diagnostic.log(reader); return {}; }
        const int count = static_cast<int>(std::min<juce::int64>(decodeSize, reader.lengthInSamples - start));
        audio.clear();
        if (!reader.read(&audio, 0, count, start, true, true)) {
            diagnostic.reason = "read-error";
            diagnostic.log(reader);
            throw std::runtime_error("Failed reading deck beat analysis");
        }
        const float* left = audio.getReadPointer(0);
        const float* right = reader.numChannels > 1 ? audio.getReadPointer(1) : left;
        for (int i = 0; i < count; ++i) {
            const double value = (static_cast<double>(left[i]) + right[i]) * 0.5;
            double available = 1;
            while (available > 1e-10) {
                const double take = std::min(remaining, available);
                accumulator += value * take;
                remaining -= take; available -= take;
                if (remaining <= 1e-10) {
                    appendSample(accumulator / ratio);
                    accumulator = 0; remaining = ratio;
                }
            }
        }
    }
    if (remaining < ratio) appendSample(accumulator / ratio);
    // Flush one final partial analysis window, not extra source audio.
    if (buffered > window - hop) {
        const int padding = window - buffered;
        for (int i = 0; i < padding; ++i) appendSample(0);
    }
    const auto result = fitGrid(novelty, cancel, diagnostic, reader);
    diagnostic.log(reader);
    return result;
}
} // namespace vcpi
