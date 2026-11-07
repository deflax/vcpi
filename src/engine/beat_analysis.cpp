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

// Only used by the non-real-time analysis worker. Raw stderr follows the
// daemon's existing debug routing; never emit per-beat or render-thread logs.
struct Diagnostics {
    const char* reason = "cancelled";
    std::size_t windows = 0, silentWindows = 0, invalidWindows = 0;
    std::size_t predicted = 0, boundary = 0, weak = 0, supported = 0;
    std::size_t gaps = 0, unstable = 0, regions = 0, matched = 0, total = 0;
    double bpm = 0, coverage = -1, rms = -1, confidence = -1;

    void log(const juce::AudioFormatReader& reader) const {
        const auto* file = dynamic_cast<const juce::FileInputStream*>(reader.input);
        const auto source = file ? file->getFile().getFullPathName() : juce::String("<stream>");
        std::ostringstream message;
        message << std::fixed << std::setprecision(4)
                << "beat-analysis source=" << juce::JSON::toString(juce::var(source), true).toStdString()
                << " reason=" << reason
                << " windows=" << windows << " silent_windows=" << silentWindows
                << " invalid_tracker_windows=" << invalidWindows
                << " predicted_beats=" << predicted << " boundary_beats=" << boundary
                << " weak_beats=" << weak << " supported_beats=" << supported
                << " gap_candidates=" << gaps << " unstable_candidates=" << unstable
                << " stable_regions=" << regions << " matched_beats=" << matched
                << " region_beats=" << total;
        if (bpm > 0) message << " candidate_bpm=" << bpm;
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

BeatAnalysis fitGrid(const std::vector<double>& novelty, const std::atomic<bool>& cancel, Diagnostics& diagnostic) {
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
    // Refine using compatible stable regions, including ones separated by an
    // intro/breakdown. Centered regression avoids cancellation in hour-long files.
    for (int pass = 0; pass < 3; ++pass) {
        Fit fit;
        for (const auto& region : regions) {
            if (cancel.load(std::memory_order_relaxed)) return {};
            if (std::abs(region.period - period) > tolerance * 2 / region.points.size()) continue;
            for (const auto& point : region.points) {
                const double beat = std::round((point.time - offset) / period);
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
    std::size_t matched = 0, total = 0;
    for (const auto& region : regions) {
        for (const auto& point : region.points) {
            ++total;
            const double error = std::abs(std::remainder(point.time - offset, period));
            if (error <= tolerance && std::abs(region.period - period) <= tolerance * 2 / region.points.size()) {
                ++matched; squaredError += error * error;
            }
        }
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
    const auto result = fitGrid(novelty, cancel, diagnostic);
    diagnostic.log(reader);
    return result;
}
} // namespace vcpi
