#include "deck.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <stdexcept>
#include <unordered_set>

namespace vcpi {
namespace {
constexpr double minimumSyncSpeed = 0.5, maximumSyncSpeed = 2.0;
constexpr double maximumSpeed = maximumSyncSpeed * 1.08;
double monotonicSeconds() noexcept {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
double number(const juce::var& object, const char* key, double fallback) {
    return object.hasProperty(key) ? static_cast<double>(object[key]) : fallback;
}
}
Deck::Deck(const std::string& catalog, double rate)
    : outputRate(rate), rampStep(1.0 / std::max(1.0, rate * 0.005)),
      pitchStep(1.0 - std::exp(-1.0 / (rate * 0.020))),
      nudgeStep(1.0 - std::exp(-1.0 / (rate * 0.010))) {
    if (!std::isfinite(rate) || rate < 8000 || rate > 768000)
        throw std::invalid_argument("Invalid deck output sample rate");
    const auto parsed = juce::JSON::parse(juce::String::fromUTF8(catalog.c_str()));
    bank = parsed["id"].toString().toStdString();
    locator = parsed["bank_path"].toString().toStdString();
    const auto* entries = parsed["tracks"].getArray();
    if (bank.empty() || entries == nullptr || entries->isEmpty())
        throw std::invalid_argument("Deck requires a resolved nonempty bank catalog");
    std::unordered_set<std::string> ids;
    for (const auto& entry : *entries) {
        Track track{entry["id"].toString().toStdString(), entry["path"].toString().toStdString(),
                    entry["title"].toString().toStdString()};
        if (track.id.empty() || !ids.insert(track.id).second ||
            !juce::File::isAbsolutePath(juce::String(track.path)))
            throw std::invalid_argument("Invalid resolved deck track ID or absolute path");
        if (track.title.empty()) track.title = track.id;
        tracks.push_back(std::move(track));
    }
    const auto initial = number(parsed, "initial_track", 1);
    const auto initialCue = number(parsed, "cue", 0);
    const auto initialPitch = number(parsed, "pitch", 0);
    if (!std::isfinite(initial) || initial != std::floor(initial) || initial < 1 || initial > trackCount() ||
        !std::isfinite(initialCue) || initialCue < 0 || !std::isfinite(initialPitch) || initialPitch < -16 || initialPitch > 16)
        throw std::invalid_argument("Invalid initial deck selection, cue or pitch");
    for (auto& stream : streams)
        for (auto& channel : stream.ring) channel.resize(capacity + mirror);
    requested = {0, static_cast<int>(initial), initialCue, restore};
    requestedTrack.store(requested.track);
    appliedTrack.store(requested.track);
    pitchTarget.store(initialPitch);
    pitch = initialPitch;
    syncEnabled.store(number(parsed, "sync", 1) != 0);
    formats.registerFormat(new juce::WavAudioFormat(), true);
    formats.registerFormat(new juce::AiffAudioFormat(), false);
    formats.registerFormat(new juce::MP3AudioFormat(), false);
    formats.registerFormat(new juce::FlacAudioFormat(), false);
    // Nothing is published and no thread exists until the first reader and its
    // bounded read-ahead have succeeded. A throwing constructor cannot leak a worker.
    prepare(streams[0], requested);
    streams[0].state.store(active);
    cursor = streams[0].target;
    cueSeconds.store(cursor / streams[0].rate);
    positionSeconds.store(cursor / streams[0].rate);
    durationSeconds.store(static_cast<double>(streams[0].length) / streams[0].rate);
    ended.store(cursor >= streams[0].length);
    publishGrid(streams[0]);
    worker = std::thread([this] { work(); });
}
Deck::~Deck() {
    stopping.store(true, std::memory_order_release);
    if (worker.joinable()) worker.join();
}
bool Deck::cancelled(std::uint64_t id) const noexcept {
    return stopping.load(std::memory_order_acquire) || latestRequest.load(std::memory_order_acquire) != id;
}
void Deck::request(int track, double seconds, Mode mode) {
    std::lock_guard<std::mutex> lock(controlMutex);
    const auto previous = latestRequest.load(std::memory_order_relaxed);
    if (mode == selection && track == appliedTrack.load(std::memory_order_acquire) &&
        previous == finishedRequest.load(std::memory_order_acquire)) return;
    requested = {previous + 1, track, seconds, mode};
    requestedTrack.store(track, std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> errorLock(errorMutex);
        lastError.clear();
    }
    // Re-selecting the active entry invalidates any pending preparation/commit.
    if (mode == selection && track == appliedTrack.load(std::memory_order_acquire))
        finishedRequest.store(requested.id, std::memory_order_release);
    latestRequest.store(requested.id, std::memory_order_release);
}
void Deck::setError(std::uint64_t id, const std::string& text) {
    std::lock_guard<std::mutex> lock(controlMutex);
    if (latestRequest.load(std::memory_order_acquire) != id) return;
    {
        std::lock_guard<std::mutex> errorLock(errorMutex);
        lastError = text;
    }
    finishedRequest.store(id, std::memory_order_release);
}
bool Deck::fill(Stream& stream) {
    if (stream.failed.load(std::memory_order_relaxed)) return false;
    const auto written = stream.written.load(std::memory_order_relaxed);
    const auto consumed = stream.read.load(std::memory_order_acquire);
    const auto available = capacity - static_cast<std::size_t>(written - consumed);
    if (available < decodeFrames) return false;
    // A short, zero-padded tail supplies the sinc look-ahead. EOF is determined
    // by the logical source cursor, never by read-ahead exhaustion.
    if (stream.nextSource > stream.length + decodeFrames && stream.discard == 0) return false;
    decode.clear();
    const auto begin = std::max<juce::int64>(0, stream.nextSource);
    const auto end = std::min<juce::int64>(stream.length, stream.nextSource + decodeFrames);
    if (end > begin) {
        const auto offset = static_cast<int>(begin - stream.nextSource);
        if (!stream.reader->read(&decode, offset, static_cast<int>(end - begin), begin, true, true))
            throw std::runtime_error("Failed reading deck audio");
        if (stream.reader->numChannels == 1)
            decode.copyFrom(1, offset, decode, 0, offset, static_cast<int>(end - begin));
    }
    stream.nextSource += decodeFrames;
    auto next = written;
    for (int frame = 0; frame < decodeFrames; ++frame) {
        float values[2];
        for (int channel = 0; channel < 2; ++channel) {
            values[channel] = decode.getSample(channel, frame);
            for (auto& filter : stream.filters[channel]) values[channel] = filter.processSingleSampleRaw(values[channel]);
        }
        if (stream.discard > 0) { --stream.discard; continue; }
        const auto index = static_cast<std::size_t>(next) & (capacity - 1);
        for (int channel = 0; channel < 2; ++channel) {
            stream.ring[channel][index] = values[channel];
            if (index < mirror) stream.ring[channel][capacity + index] = values[channel];
        }
        ++next;
    }
    stream.written.store(next, std::memory_order_release);
    return true;
}
bool Deck::analyze(Stream& stream, BeatGrid& grid) {
    // Bounded 60-second onset-envelope scan; only the preparation thread touches
    // the per-catalog cache. Published streams carry an immutable grid copy.
    constexpr double envelopeRate = 200.0;
    const int hop = std::max(1, static_cast<int>(std::lround(stream.rate / envelopeRate)));
    const double step = hop / stream.rate;
    const auto length = std::min(stream.length, static_cast<juce::int64>(stream.rate * 60));
    std::vector<double> envelope(static_cast<std::size_t>((length + hop - 1) / hop), 0);
    juce::AudioBuffer<float> audio(2, decodeFrames);
    for (juce::int64 start = 0; start < length; start += decodeFrames) {
        if (cancelled(stream.request.id)) return false;
        const auto count = static_cast<int>(std::min<juce::int64>(decodeFrames, length - start));
        audio.clear();
        if (!stream.reader->read(&audio, 0, count, start, true, true))
            throw std::runtime_error("Failed reading deck beat analysis");
        for (int frame = 0; frame < count; ++frame) {
            const double a = audio.getSample(0, frame), b = audio.getSample(1, frame);
            envelope[static_cast<std::size_t>((start + frame) / hop)] += a * a + b * b;
        }
        // Selection analysis must not starve the still-playing old stream.
        for (auto& other : streams) {
            if (&other == &stream || other.state.load(std::memory_order_acquire) != active) continue;
            try { fill(other); }
            catch (const std::exception& error) {
                other.failed.store(true, std::memory_order_release);
                setError(other.request.id, error.what());
            }
        }
    }
    BeatGrid result;
    result.analyzed = true;
    double peak = 0, previous = 0;
    for (auto& energy : envelope) {
        const auto level = std::sqrt(energy / hop);
        energy = std::max(0.0, level - previous);
        previous = level;
        peak = std::max(peak, energy);
    }
    if (peak < 0.0001) { grid = result; return true; }
    // Pick the first transient edge, not the decaying waveform's individual
    // cycles. Local contrast rejects sustained tones and unstructured noise.
    std::vector<double> onsets;
    for (std::size_t i = 0; i < envelope.size(); ++i) {
        if ((i & 1023) == 0 && cancelled(stream.request.id)) return false;
        double background = 0;
        const auto begin = i > 40 ? i - 40 : 0;
        for (auto j = begin; j < i; ++j) background += envelope[j];
        background /= std::max<std::size_t>(1, i - begin);
        if (envelope[i] < peak * 0.12 || envelope[i] < background * 3 ||
            (i > 0 && envelope[i] < envelope[i - 1]) ||
            (i + 1 < envelope.size() && envelope[i] < envelope[i + 1])) continue;
        const double time = i * step;
        if (onsets.empty() || time - onsets.back() >= 0.18) onsets.push_back(time);
    }
    if (onsets.size() < 8) { grid = result; return true; }
    // Evaluate candidate periods against actual transient pairs, allowing a
    // missed beat. Strongest fundamental wins over its half-tempo multiple.
    double bestPeriod = 0, bestScore = 0;
    for (double period = 60.0 / 200; period <= 60.0 / 60 + step * 0.25; period += step) {
        if (cancelled(stream.request.id)) return false;
        double score = 0;
        for (std::size_t i = 1; i < onsets.size(); ++i) {
            const auto distance = (onsets[i] - onsets[i - 1]) / period;
            const auto beats = std::round(distance);
            if (beats < 1 || beats > 4) continue;
            const auto error = std::abs(distance - beats) * period;
            score += std::max(0.0, 1.0 - error / (2.5 * step)) / beats;
        }
        score /= onsets.size() - 1;
        if (score > bestScore) { bestScore = score; bestPeriod = period; }
    }
    if (bestScore < 0.65) { grid = result; return true; }
    double intervalSum = 0, beatSum = 0;
    for (std::size_t i = 1; i < onsets.size(); ++i) {
        const auto interval = onsets[i] - onsets[i - 1];
        const auto beats = std::round(interval / bestPeriod);
        if (beats >= 1 && beats <= 4 && std::abs(interval - beats * bestPeriod) < 2.5 * step) {
            intervalSum += interval;
            beatSum += beats;
        }
    }
    bestPeriod = intervalSum / beatSum;
    // Fit the grid over the whole scan, eliminating hop-size tempo drift.
    double sumX = 0, sumY = 0, sumXX = 0, sumXY = 0, count = 0;
    for (const auto time : onsets) {
        const auto beat = std::round((time - onsets.front()) / bestPeriod);
        if (std::abs(time - onsets.front() - beat * bestPeriod) > bestPeriod * 0.12) continue;
        sumX += beat; sumY += time; sumXX += beat * beat; sumXY += beat * time; ++count;
    }
    const auto denominator = count * sumXX - sumX * sumX;
    if (count < 8 || count < onsets.size() * 0.65 || denominator <= 0) { grid = result; return true; }
    const auto period = (count * sumXY - sumX * sumY) / denominator;
    const auto offset = (sumY - period * sumX) / count;
    if (period < 60.0 / 201 || period > 60.0 / 59) { grid = result; return true; }
    std::size_t matched = 0;
    for (const auto time : onsets)
        if (std::abs(std::remainder(time - offset, period)) < std::max(0.015, period * 0.06)) ++matched;
    const double confidence = static_cast<double>(matched) / onsets.size();
    if (confidence >= 0.75) {
        result.bpm = 60 / period;
        result.offset = std::fmod(std::fmod(offset, period) + period, period);
        result.confidence = confidence * bestScore;
    }
    if (cancelled(stream.request.id)) return false;
    grid = result;
    return true;
}
void Deck::publishGrid(const Stream& stream) noexcept {
    detectedBpm.store(stream.grid.bpm, std::memory_order_relaxed);
    detectedOffset.store(stream.grid.offset, std::memory_order_relaxed);
    detectedConfidence.store(stream.grid.confidence, std::memory_order_relaxed);
    syncActive.store(false, std::memory_order_relaxed);
    beatPhaseError.store(0, std::memory_order_relaxed);
}
bool Deck::prepare(Stream& stream, const Request& wanted) {
    stream.request = wanted;
    stream.failed.store(false);
    stream.read.store(0);
    stream.written.store(0);
    const juce::File file{juce::String(tracks[static_cast<std::size_t>(wanted.track - 1)].path)};
    if (!file.hasFileExtension("wav;wave;aif;aiff;aifc;mp3;flac"))
        throw std::invalid_argument("Deck supports AIFF, WAV, MP3 and FLAC files");
    if (!std::filesystem::is_regular_file(file.getFullPathName().toStdString()))
        throw std::invalid_argument("Deck audio must be a regular file");
    stream.reader.reset(formats.createReaderFor(file));
    if (!stream.reader) throw std::runtime_error("Unable to decode deck audio: " + file.getFullPathName().toStdString());
    if (stream.reader->numChannels < 1 || stream.reader->numChannels > 2)
        throw std::invalid_argument("Deck audio must be mono or stereo");
    stream.rate = stream.reader->sampleRate;
    stream.length = stream.reader->lengthInSamples;
    if (!std::isfinite(stream.rate) || stream.rate < 8000 || stream.rate > 768000 || stream.length <= 0)
        throw std::invalid_argument("Invalid deck audio rate or empty audio");
    auto& grid = tracks[static_cast<std::size_t>(wanted.track - 1)].grid;
    if (!grid.analyzed && !analyze(stream, grid)) return false;
    stream.grid = grid;
    stream.target = std::clamp(wanted.seconds * stream.rate, 0.0, static_cast<double>(stream.length));
    const auto targetFrame = static_cast<juce::int64>(std::floor(stream.target));
    stream.nextSource = targetFrame - history / 2 - preRoll;
    stream.discard = preRoll;
    // WindowedSincInterpolator has a fixed source-rate cutoff. A retained
    // Butterworth prefilter supplies anti-aliasing at every supported sync speed.
    // It is deliberately conservative, independent of live speed, and runs on
    // decoded source samples on the worker, not on the real-time thread.
    const auto cutoff = std::min(stream.rate * 0.45, outputRate * 0.40 / maximumSpeed);
    for (auto& channel : stream.filters)
        for (int stage = 0; stage < filterStages; ++stage) {
            const auto q = 1.0 / (2.0 * std::cos(juce::MathConstants<double>::pi * (2 * stage + 1) / (4 * filterStages)));
            channel[stage].setCoefficients(juce::IIRCoefficients::makeLowPass(stream.rate, cutoff, q));
            channel[stage].reset();
        }
    const auto goal = static_cast<std::uint64_t>(std::clamp(stream.rate * 0.25, 8192.0, static_cast<double>(capacity / 2)));
    while (stream.written.load() < goal) {
        if (cancelled(wanted.id)) return false;
        if (!fill(stream)) break;
    }
    if (cancelled(wanted.id)) return false;
    // Prime all 200 retained sinc history samples around the requested cursor;
    // the final priming step preserves a fractional source-frame seek target.
    float ignored = 0;
    for (int channel = 0; channel < 2; ++channel) {
        stream.sinc[channel].reset();
        for (int i = 0; i < history; ++i)
            stream.sinc[channel].process(i == history - 1 ? 1.0 + stream.target - targetFrame : 1.0,
                                        stream.ring[channel].data() + i, &ignored, 1);
    }
    stream.read.store(history, std::memory_order_release);
    return true;
}
void Deck::work() noexcept {
    std::uint64_t handled = 0;
    while (!stopping.load(std::memory_order_acquire)) {
        bool didWork = false;
        for (auto& stream : streams) {
            auto state = stream.state.load(std::memory_order_acquire);
            if (state == ready && cancelled(stream.request.id)) {
                int expected = ready;
                if (stream.state.compare_exchange_strong(expected, preparing, std::memory_order_acq_rel)) state = preparing;
                else state = expected;
            }
            if (state == retired || state == preparing) {
                stream.reader.reset();
                stream.state.store(free, std::memory_order_release);
            }
        }
        const auto newest = latestRequest.load(std::memory_order_acquire);
        if (newest != handled) {
            Request wanted;
            { std::lock_guard<std::mutex> lock(controlMutex); wanted = requested; }
            if (wanted.id == finishedRequest.load(std::memory_order_acquire)) handled = wanted.id;
            else for (auto& stream : streams) {
                if (stream.state.load(std::memory_order_acquire) != free) continue;
                stream.state.store(preparing, std::memory_order_release);
                try {
                    if (prepare(stream, wanted)) stream.state.store(ready, std::memory_order_release);
                    else stream.state.store(retired, std::memory_order_release);
                } catch (const std::exception& error) {
                    setError(wanted.id, error.what());
                    stream.state.store(retired, std::memory_order_release);
                } catch (...) {
                    setError(wanted.id, "Unknown deck preparation failure");
                    stream.state.store(retired, std::memory_order_release);
                }
                handled = wanted.id;
                didWork = true;
                break;
            }
        }
        for (auto& stream : streams) {
            const auto state = stream.state.load(std::memory_order_acquire);
            if (state != active && state != ready && state != held) continue;
            try { didWork = fill(stream) || didWork; }
            catch (const std::exception& error) {
                stream.failed.store(true, std::memory_order_release);
                setError(stream.request.id, error.what());
            } catch (...) {
                stream.failed.store(true, std::memory_order_release);
                setError(stream.request.id, "Unknown deck read failure");
            }
        }
        if (!didWork) std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}
void Deck::retirePending() noexcept {
    if (pending < 0) return;
    streams[pending].state.store(retired, std::memory_order_release);
    pending = -1;
}
void Deck::commit(int index) noexcept {
    commitSequence.fetch_add(1, std::memory_order_acq_rel);
    auto& stream = streams[index];
    streams[current].state.store(retired, std::memory_order_release);
    current = index;
    pending = -1;
    cursor = stream.target;
    if (stream.request.mode == selection) {
        playing.store(false, std::memory_order_release);
        cueSeconds.store(0, std::memory_order_release);
        nudgeTarget.store(0, std::memory_order_release);
        nudge = 0;
    } else if (stream.request.mode == recall || stream.request.mode == restore) {
        playing.store(false, std::memory_order_release);
        if (stream.request.mode == restore) cueSeconds.store(cursor / stream.rate, std::memory_order_release);
        nudgeTarget.store(0, std::memory_order_release);
        nudge = 0;
    }
    ended.store(cursor >= stream.length, std::memory_order_release);
    if (cursor >= stream.length) playing.store(false, std::memory_order_release);
    buffering.store(false, std::memory_order_release);
    appliedTrack.store(stream.request.track, std::memory_order_release);
    durationSeconds.store(static_cast<double>(stream.length) / stream.rate, std::memory_order_release);
    positionSeconds.store(cursor / stream.rate, std::memory_order_release);
    publishGrid(stream);
    finishedRequest.store(stream.request.id, std::memory_order_release);
    stream.state.store(active, std::memory_order_release);
    lastLeft = lastRight = 0;
    commitSequence.fetch_add(1, std::memory_order_release);
}
void Deck::render(float* left, float* right, int frames) noexcept {
    if (pending >= 0 && cancelled(streams[pending].request.id)) retirePending();
    for (int index = 0; index < static_cast<int>(streams.size()); ++index) {
        int expected = ready;
        if (!streams[index].state.compare_exchange_strong(expected, held, std::memory_order_acq_rel)) continue;
        if (cancelled(streams[index].request.id)) streams[index].state.store(retired, std::memory_order_release);
        else { retirePending(); pending = index; }
    }
    const auto now = monotonicSeconds();
    // Sample-time continuity supports both hardware callbacks and offline
    // rendering. Re-anchor after a stopped device; periodic clock commands
    // re-project their reception time, without assuming Python's clock epoch.
    if (clockReset || nextClockTime == 0 || now > nextClockTime + 0.1) nextClockTime = now;
    clockReset = false;
    const auto tempo = targetTempo.load(std::memory_order_relaxed);
    const auto beatStep = tempo / (60 * outputRate);
    double globalBeat = clockAnchorBeat + (nextClockTime - clockAnchorTime) * tempo / 60;
    nextClockTime += frames / outputRate;
    const bool clockValid = clockReceived.load(std::memory_order_relaxed) && tempo > 0;
    const bool requestedSync = syncEnabled.load(std::memory_order_relaxed);
    bool activeSync = false;
    double speed = 1, phaseError = 0;
    for (int frame = 0; frame < frames; ++frame) {
        if (pending >= 0 && cancelled(streams[pending].request.id)) retirePending();
        if (pending >= 0 && gain <= 0) commit(pending);
        auto& stream = streams[current];
        pitch += (pitchTarget.load(std::memory_order_relaxed) - pitch) * pitchStep;
        nudge += (nudgeTarget.load(std::memory_order_relaxed) - nudge) * nudgeStep;
        speed = (1.0 + pitch / 100.0) * (1.0 + 0.02 * nudge);
        const auto baseSpeed = stream.grid.bpm > 0 ? tempo / stream.grid.bpm : 0;
        activeSync = requestedSync && clockValid && stream.grid.bpm > 0 &&
                     baseSpeed >= minimumSyncSpeed && baseSpeed <= maximumSyncSpeed;
        phaseError = 0;
        if (activeSync) {
            const auto sourceBeat = (cursor / stream.rate - stream.grid.offset) * stream.grid.bpm / 60;
            phaseError = std::remainder(globalBeat - sourceBeat, 1.0);
            const auto corrected = baseSpeed * (1 + std::clamp(phaseError * 1.5, -0.08, 0.08));
            syncSpeed += (corrected - syncSpeed) * pitchStep;
            speed = syncSpeed;
        } else syncSpeed = speed;
        const auto ratio = stream.rate / outputRate * speed;
        const auto consumed = stream.read.load(std::memory_order_relaxed);
        const auto available = stream.written.load(std::memory_order_acquire) - consumed;
        const bool wantsAudio = playing.load(std::memory_order_relaxed) && cursor < stream.length;
        // The sinc retains its own fractional phase; ceil(ratio)+1 covers the
        // exact next input consumption without mutating history on starvation.
        const bool hasData = available >= static_cast<std::uint64_t>(std::ceil(ratio) + 1);
        const bool audible = wantsAudio && hasData;
        buffering.store(wantsAudio && !hasData, std::memory_order_relaxed);
        const double targetGain = audible && pending < 0 ? 1.0 : 0.0;
        gain += std::clamp(targetGain - gain, -rampStep, rampStep);
        if (audible) {
            const auto offset = static_cast<std::size_t>(consumed) & (capacity - 1);
            const auto used = stream.sinc[0].process(ratio, stream.ring[0].data() + offset, &lastLeft, 1);
            stream.sinc[1].process(ratio, stream.ring[1].data() + offset, &lastRight, 1);
            stream.read.store(consumed + static_cast<std::uint64_t>(used), std::memory_order_release);
            cursor = std::min(cursor + ratio, static_cast<double>(stream.length));
            if (cursor >= stream.length) {
                playing.store(false, std::memory_order_release);
                ended.store(true, std::memory_order_release);
            }
        }
        left[frame] = lastLeft * static_cast<float>(gain);
        right[frame] = lastRight * static_cast<float>(gain);
        globalBeat += beatStep;
    }
    positionSeconds.store(cursor / streams[current].rate, std::memory_order_release);
    syncActive.store(activeSync, std::memory_order_release);
    playbackSpeed.store(speed, std::memory_order_release);
    beatPhaseError.store(phaseError, std::memory_order_release);
}
double Deck::parameter(const std::string& key) const {
    if (key == "track_select") return requestedTrack.load(std::memory_order_acquire);
    if (key == "play") return playing.load(std::memory_order_acquire) ? 1.0 : 0.0;
    if (key == "pitch") return pitchTarget.load(std::memory_order_acquire);
    if (key == "nudge") return nudgeTarget.load(std::memory_order_acquire);
    if (key == "sync") return syncEnabled.load(std::memory_order_acquire) ? 1.0 : 0.0;
    throw std::invalid_argument("Unknown deck parameter: " + key);
}
double Deck::setParameter(const std::string& key, double value) {
    if (!std::isfinite(value)) throw std::invalid_argument("Deck parameters must be finite");
    if (key == "track_select") {
        if (value != std::floor(value) || value < 1 || value > trackCount())
            throw std::invalid_argument("Deck track_select is a 1-based bank index");
        request(static_cast<int>(value), 0, selection);
    } else if (key == "play") {
        if (value != 0 && ended.load(std::memory_order_acquire))
            request(appliedTrack.load(std::memory_order_acquire), cueSeconds.load(std::memory_order_acquire), seek);
        playing.store(value != 0, std::memory_order_release);
    } else if (key == "pitch") {
        if (value < -16 || value > 16) throw std::invalid_argument("Deck pitch must be in [-16, 16]");
        pitchTarget.store(value, std::memory_order_release);
    } else if (key == "nudge") {
        if (value < -1 || value > 1) throw std::invalid_argument("Deck nudge must be in [-1, 1]");
        nudgeTarget.store(value, std::memory_order_release);
    } else if (key == "sync") {
        syncEnabled.store(value != 0, std::memory_order_release);
        if (value == 0) syncActive.store(false, std::memory_order_release);
    } else throw std::invalid_argument("Unknown deck parameter: " + key);
    return value;
}
void Deck::command(const std::string& action, double value) {
    if (!std::isfinite(value)) throw std::invalid_argument("Deck command value must be finite");
    if (action == "sync_tempo") {
        if (value <= 0) throw std::invalid_argument("Deck sync_tempo must be positive");
        const auto now = monotonicSeconds();
        if (clockReceived.load(std::memory_order_relaxed))
            clockAnchorBeat += (now - clockAnchorTime) * targetTempo.load(std::memory_order_relaxed) / 60;
        clockAnchorTime = now;
        targetTempo.store(value, std::memory_order_release);
    } else if (action == "sync_beat") {
        clockAnchorBeat = value;
        clockAnchorTime = monotonicSeconds();
        clockReset = true;
        clockReceived.store(true, std::memory_order_release);
    } else if (action == "cue_set") cueSeconds.store(positionSeconds.load(std::memory_order_acquire), std::memory_order_release);
    else if (action == "play_toggle") {
        // Controls are serialized by the native owner; CAS also makes rapid
        // direct callers lossless without a cached play-state round trip.
        auto previous = playing.load(std::memory_order_acquire);
        while (!playing.compare_exchange_weak(previous, !previous, std::memory_order_acq_rel)) {}
        if (!previous && ended.load(std::memory_order_acquire))
            request(appliedTrack.load(std::memory_order_acquire), cueSeconds.load(std::memory_order_acquire), seek);
    } else if (action == "seek" || action == "cue" || action == "restore") {
        const auto seconds = action == "cue" ? cueSeconds.load(std::memory_order_acquire) : value;
        const auto bounded = std::clamp(seconds, 0.0, durationSeconds.load(std::memory_order_acquire));
        if (action != "seek") playing.store(false, std::memory_order_release);
        if (action == "restore") { cueSeconds.store(bounded); nudgeTarget.store(0); }
        request(appliedTrack.load(std::memory_order_acquire), bounded,
                action == "seek" ? seek : action == "cue" ? recall : restore);
    } else throw std::invalid_argument("Unknown deck command: " + action);
}
DeckStatus Deck::status() const {
    DeckStatus result;
    result.bankId = bank;
    // Only a commit changes the identity/duration tuple. Readers may retry on
    // the control thread; the render thread never waits for snapshot readers.
    for (;;) {
        const auto sequence = commitSequence.load(std::memory_order_acquire);
        if (sequence & 1) { std::this_thread::yield(); continue; }
        result.trackSelect = appliedTrack.load(std::memory_order_acquire);
        result.buffering = buffering.load(std::memory_order_acquire);
        result.ended = ended.load(std::memory_order_acquire);
        result.play = playing.load(std::memory_order_acquire);
        result.position = positionSeconds.load(std::memory_order_acquire);
        result.duration = durationSeconds.load(std::memory_order_acquire);
        result.cue = cueSeconds.load(std::memory_order_acquire);
        result.pitch = pitchTarget.load(std::memory_order_acquire);
        result.nudge = nudgeTarget.load(std::memory_order_acquire);
        result.bpm = detectedBpm.load(std::memory_order_acquire);
        result.beatOffset = detectedOffset.load(std::memory_order_acquire);
        result.beatConfidence = detectedConfidence.load(std::memory_order_acquire);
        result.sync = syncEnabled.load(std::memory_order_acquire);
        result.syncActive = syncActive.load(std::memory_order_acquire);
        result.playbackSpeed = playbackSpeed.load(std::memory_order_acquire);
        result.beatPhaseError = beatPhaseError.load(std::memory_order_acquire);
        {
            std::lock_guard<std::mutex> lock(controlMutex);
            result.requestedTrackSelect = requestedTrack.load(std::memory_order_acquire);
            result.requestId = latestRequest.load(std::memory_order_acquire);
            result.loading = result.requestId != finishedRequest.load(std::memory_order_acquire);
        }
        std::atomic_thread_fence(std::memory_order_acquire);
        if (sequence == commitSequence.load(std::memory_order_acquire)) break;
    }
    const auto& selected = tracks[static_cast<std::size_t>(result.trackSelect - 1)];
    result.trackId = selected.id;
    result.trackTitle = selected.title;
    result.requestedTrackId = tracks[static_cast<std::size_t>(result.requestedTrackSelect - 1)].id;
    result.ready = !result.buffering;
    result.state = result.loading ? "loading" : result.buffering ? "buffering" : result.ended ? "ended" : "ready";
    { std::lock_guard<std::mutex> lock(errorMutex); result.error = lastError; }
    const auto tempo = targetTempo.load(std::memory_order_acquire);
    if (result.sync) {
        if (result.bpm <= 0) result.syncError = "No reliable beat grid";
        else if (!clockReceived.load(std::memory_order_acquire) || tempo <= 0) result.syncError = "Waiting for global clock";
        else if (tempo / result.bpm < minimumSyncSpeed || tempo / result.bpm > maximumSyncSpeed)
            result.syncError = "Unsupported sync speed (supported ratio 0.5 to 2.0)";
    }
    return result;
}
} // namespace vcpi
