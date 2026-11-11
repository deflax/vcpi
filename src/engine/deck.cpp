#include "deck.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <future>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <unordered_set>

namespace vcpi {
namespace {
constexpr double minimumSyncSpeed = 0.5, maximumSyncSpeed = 2.0;
double monotonicSeconds() noexcept {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
double number(const juce::var& object, const char* key, double fallback) {
    return object.hasProperty(key) ? static_cast<double>(object[key]) : fallback;
}
DeckSongState songState(const juce::var& object) {
    DeckSongState state;
    state.cue = number(object, "cue", 0);
    state.position = number(object, "position", state.cue);
    state.pitch = number(object, "pitch", 0);
    state.phaseOffset = number(object, "phase_offset", 0);
    state.sync = number(object, "sync", 1) != 0;
    state.play = number(object, "play", 0) != 0;
    if (!std::isfinite(state.phaseOffset) || state.phaseOffset < -0.5 || state.phaseOffset > 0.5)
        throw std::invalid_argument("Deck phase_offset must be finite and in [-0.5, 0.5]");
    if (!std::isfinite(state.position) || state.position < 0 ||
        !std::isfinite(state.cue) || state.cue < 0 ||
        !std::isfinite(state.pitch) || state.pitch < -16 || state.pitch > 16)
        throw std::invalid_argument("Invalid deck song position, cue or pitch");
    return state;
}
}
DeckSongState Deck::SongMemory::load() const noexcept {
    return {position.load(std::memory_order_acquire), cue.load(std::memory_order_acquire),
            pitch.load(std::memory_order_acquire), phaseOffset.load(std::memory_order_acquire),
            sync.load(std::memory_order_acquire), play.load(std::memory_order_acquire)};
}
void Deck::SongMemory::store(const DeckSongState& state) noexcept {
    position.store(state.position, std::memory_order_release);
    cue.store(state.cue, std::memory_order_release);
    pitch.store(state.pitch, std::memory_order_release);
    phaseOffset.store(state.phaseOffset, std::memory_order_release);
    sync.store(state.sync, std::memory_order_release);
    play.store(state.play, std::memory_order_release);
}
Deck::Catalog Deck::parseCatalog(const juce::var& parsed) {
    Catalog result{parsed["id"].toString().toStdString(), parsed["bank_path"].toString().toStdString(), {}};
    if (parsed.hasProperty("sync_filter")) {
        if (!parsed["sync_filter"].isBool())
            throw std::invalid_argument("Deck sync_filter must be a boolean");
        result.syncFilter = static_cast<bool>(parsed["sync_filter"]);
    }
    const auto* entries = parsed["tracks"].getArray();
    if (result.bank.empty() || entries == nullptr || entries->isEmpty())
        throw std::invalid_argument("Deck requires a resolved nonempty playlist catalog");
    result.tracks.reserve(static_cast<std::size_t>(entries->size()));
    std::unordered_set<std::string> ids;
    const auto saved = parsed["song_states"];
    if (!saved.isVoid() && !saved.isObject())
        throw std::invalid_argument("Deck song_states must be an object");
    for (const auto& entry : *entries) {
        Track track{entry["id"].toString().toStdString(), entry["path"].toString().toStdString(),
                    entry["title"].toString().toStdString()};
        if (track.id.empty() || !ids.insert(track.id).second ||
            !juce::File::isAbsolutePath(juce::String(track.path)))
            throw std::invalid_argument("Invalid resolved deck track ID or absolute path");
        if (entry.hasProperty("bpm_hint")) {
            const auto& hint = entry["bpm_hint"];
            if (!hint.isDouble() && !hint.isInt() && !hint.isInt64())
                throw std::invalid_argument("Deck bpm_hint must be a finite number in [60, 200]");
            const double bpm = static_cast<double>(hint);
            if (!std::isfinite(bpm) || bpm < 60 || bpm > 200)
                throw std::invalid_argument("Deck bpm_hint must be a finite number in [60, 200]");
            track.bpmHint = bpm;
        }
        if (track.title.empty()) track.title = track.id;
        const juce::Identifier key{juce::String(track.id)};
        if (saved.hasProperty(key)) {
            const auto state = saved[key];
            if (!state.isObject()) throw std::invalid_argument("Deck song state must be an object");
            track.memory->store(songState(state));
        }
        result.tracks.push_back(std::move(track));
    }
    const auto initial = number(parsed, "initial_track", 1);
    if (!std::isfinite(initial) || initial != std::floor(initial) ||
        initial < 1 || initial > static_cast<double>(result.tracks.size()))
        throw std::invalid_argument("Invalid initial deck selection");
    result.initialTrack = static_cast<int>(initial);
    auto& selected = result.tracks[static_cast<std::size_t>(result.initialTrack - 1)];
    if (!saved.hasProperty(juce::Identifier{juce::String(selected.id)}))
        selected.memory->store(songState(parsed));
    return result;
}
Deck::Deck(const std::string& catalog, double rate)
    : outputRate(rate), rampStep(1.0 / std::max(1.0, rate * 0.005)),
      pitchStep(1.0 - std::exp(-1.0 / (rate * 0.020))),
      nudgeStep(1.0 - std::exp(-1.0 / (rate * 0.010))) {
    if (!std::isfinite(rate) || rate < 8000 || rate > 768000)
        throw std::invalid_argument("Invalid deck output sample rate");
    const auto parsed = juce::JSON::parse(juce::String::fromUTF8(catalog.c_str()));
    auto resolved = parseCatalog(parsed);
    bank = std::move(resolved.bank);
    locator = std::move(resolved.locator);
    tracks = std::move(resolved.tracks);
    syncFilter = resolved.syncFilter;
    const auto initial = resolved.initialTrack;
    auto initialState = tracks[static_cast<std::size_t>(initial - 1)].memory->load();
    for (auto& stream : streams)
        for (auto& channel : stream.ring) channel.resize(capacity + mirror);
    requested = {0, static_cast<int>(initial), initialState.position, restore, initialState};
    formats.registerFormat(new juce::WavAudioFormat(), true);
    formats.registerFormat(new juce::AiffAudioFormat(), false);
    formats.registerFormat(new juce::MP3AudioFormat(), false);
    formats.registerFormat(new juce::FlacAudioFormat(), false);
    // Nothing is published and no thread exists until the first reader and its
    // bounded read-ahead have succeeded. A throwing constructor cannot leak a worker.
    prepareCatalog(streams[0], requested, tracks, syncFilter);
    requested = streams[0].request;
    initialState = requested.song;
    requestedTrack.store(requested.track);
    appliedTrack.store(requested.track);
    pitchTarget.store(initialState.pitch);
    phaseOffset.store(initialState.phaseOffset);
    pitch = initialState.pitch;
    syncEnabled.store(initialState.sync);
    playing.store(initialState.play);
    streams[0].state.store(active);
    cursor = streams[0].target;
    cueSeconds.store(std::min(initialState.cue, static_cast<double>(streams[0].length) / streams[0].rate));
    positionSeconds.store(cursor / streams[0].rate);
    durationSeconds.store(static_cast<double>(streams[0].length) / streams[0].rate);
    ended.store(cursor >= streams[0].length);
    if (ended.load()) playing.store(false);
    publishGrid(streams[0]);
    worker = std::thread([this] { work(); });
}
Deck::~Deck() {
    stopping.store(true, std::memory_order_release);
    if (worker.joinable()) worker.join();
}
void Deck::updateCatalog(const std::string& catalog, Ownership& ownership) {
    // Plugin serializes all control readers/writers, but audio keeps consuming
    // the active ring during parsing, cancellation, joining and preparation.
    auto replacement = parseCatalog(juce::JSON::parse(juce::String::fromUTF8(catalog.c_str())));
    std::unordered_map<std::string_view, std::size_t> oldEntries;
    oldEntries.reserve(tracks.size());
    if (replacement.bank == bank)
        for (std::size_t i = 0; i < tracks.size(); ++i) oldEntries.emplace(tracks[i].id, i);
    std::vector<int> remap(tracks.size(), 0);
    for (std::size_t i = 0; i < replacement.tracks.size(); ++i) {
        const auto old = oldEntries.find(replacement.tracks[i].id);
        if (old == oldEntries.end()) continue;
        if (tracks[old->second].path != replacement.tracks[i].path)
            throw std::invalid_argument("A deck entry ID cannot change its audio path");
        remap[old->second] = static_cast<int>(i + 1);
    }
    stopping.store(true, std::memory_order_release);
    if (worker.joinable()) worker.join();
    // The beat cache is worker-owned until the join, unlike immutable IDs/paths.
    for (std::size_t i = 0; i < tracks.size(); ++i) {
        if (remap[i] == 0) continue;
        auto& replacementTrack = replacement.tracks[static_cast<std::size_t>(remap[i] - 1)];
        replacementTrack.memory->store(tracks[i].memory->load());
        if (replacementTrack.bpmHint != tracks[i].bpmHint) continue;
        replacementTrack.analysis->force.store(tracks[i].analysis->force.load());
        if (!tracks[i].gridSource ||
            !BeatAnalysisCache(replacementTrack.path, replacementTrack.bpmHint).matches(*tracks[i].gridSource))
            continue;
        replacementTrack.grid = tracks[i].grid;
        replacementTrack.gridSource = tracks[i].gridSource;
        replacementTrack.analysis->bpm.store(tracks[i].analysis->bpm.load());
    }
    int oldTrack, newTrack, prepared = -1;
    bool refreshActive = false;
    DeckSongState activeSong;
    std::uint64_t id;
    {
        ControlOwnership lock(ownership);
        retirePending();
        for (int i = 0; i < static_cast<int>(streams.size()); ++i)
            if (i != current) streams[i].state.store(retired, std::memory_order_release);
        oldTrack = appliedTrack.load(std::memory_order_relaxed);
        newTrack = remap[static_cast<std::size_t>(oldTrack - 1)];
        refreshActive = newTrack != 0 &&
            (tracks[static_cast<std::size_t>(oldTrack - 1)].bpmHint !=
                 replacement.tracks[static_cast<std::size_t>(newTrack - 1)].bpmHint ||
             (replacement.syncFilter &&
              (!syncFilter || !replacement.tracks[static_cast<std::size_t>(newTrack - 1)].grid ||
               streams[current].grid.bpm <= 0)));
        if (refreshActive)
            activeSong = {cursor / streams[current].rate, cueSeconds.load(std::memory_order_relaxed),
                          pitchTarget.load(std::memory_order_relaxed), phaseOffset.load(std::memory_order_relaxed),
                          syncEnabled.load(std::memory_order_relaxed), playing.load(std::memory_order_relaxed)};
        id = latestRequest.load(std::memory_order_relaxed) + 1;
        requested = {id, oldTrack, 0, selection};
        requestedTrack.store(oldTrack, std::memory_order_relaxed);
        finishedRequest.store(id, std::memory_order_relaxed);
        latestRequest.store(id, std::memory_order_release);
        streams[current].request.id = id;
        if (newTrack == 0 || refreshActive) prepared = (current + 1) % static_cast<int>(streams.size());
    }
    { std::lock_guard<std::mutex> lock(errorMutex); lastError.clear(); }
    // No worker remains, and all pending streams are retired. Clearing this
    // flag lets synchronous replacement preparation use ordinary cancellation.
    stopping.store(false, std::memory_order_release);
    try {
        if (prepared >= 0) {
            if (newTrack == 0)
                newTrack = replacement.bank == bank
                    ? std::min(oldTrack, static_cast<int>(replacement.tracks.size())) : replacement.initialTrack;
            streams[prepared].state.store(preparing, std::memory_order_release);
            auto& next = replacement.tracks[static_cast<std::size_t>(newTrack - 1)];
            // Cancelled pending selections cannot mutate inactive memory.
            const auto old = oldEntries.find(next.id);
            auto song = refreshActive ? activeSong : old == oldEntries.end()
                ? next.memory->load() : tracks[old->second].memory->load();
            if (replacement.syncFilter && !refreshActive && replacement.bank == bank) song.play = false;
            // A hint change refreshes analysis, not the song's performance controls.
            const Request wanted{id, newTrack, song.position, refreshActive ? seek : selection, song};
            prepareCatalog(streams[prepared], wanted, replacement.tracks, replacement.syncFilter);
            newTrack = streams[prepared].request.track;
        }
        {
            ControlOwnership lock(ownership);
            // Capture using the OLD catalog index before swapping vectors.
            // Nothing above mutates remembered state if preparation fails.
            if (prepared >= 0) rememberSong(phaseOffset.load(std::memory_order_relaxed));
            for (std::size_t i = 0; i < tracks.size(); ++i)
                if (remap[i] != 0)
                    replacement.tracks[static_cast<std::size_t>(remap[i] - 1)].memory.swap(tracks[i].memory);
            bank.swap(replacement.bank);
            locator.swap(replacement.locator);
            tracks.swap(replacement.tracks);
            syncFilter = replacement.syncFilter;
            requested = {id, newTrack, 0, selection};
            requestedTrack.store(newTrack, std::memory_order_release);
            if (prepared >= 0) {
                auto phase = phaseOffset.load(std::memory_order_relaxed);
                commit(prepared, phase, false);
                gain = 0;
            } else {
                appliedTrack.store(newTrack, std::memory_order_release);
                streams[current].request.track = newTrack;
            }
        }
    } catch (...) {
        if (prepared >= 0) streams[prepared].state.store(retired, std::memory_order_release);
        worker = std::thread([this] { work(); });
        throw;
    }
    worker = std::thread([this] { work(); });
}
bool Deck::cancelled(std::uint64_t id) const noexcept {
    return stopping.load(std::memory_order_acquire) || latestRequest.load(std::memory_order_acquire) != id;
}
void Deck::request(int track, double seconds, Mode mode) {
    std::lock_guard<std::mutex> lock(controlMutex);
    const auto previous = latestRequest.load(std::memory_order_relaxed);
    if (mode == selection && track == appliedTrack.load(std::memory_order_acquire) &&
        previous == finishedRequest.load(std::memory_order_acquire)) return;
    const auto song = mode == selection
        ? tracks[static_cast<std::size_t>(track - 1)].memory->load() : DeckSongState{};
    if (mode == reanalysis) {
        auto& analysis = *tracks[static_cast<std::size_t>(track - 1)].analysis;
        analysis.force.store(true, std::memory_order_release);
        analysis.bpm.store(-1, std::memory_order_release);
    }
    requested = {previous + 1, track, mode == selection ? song.position : seconds, mode, song};
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
    if (stream.nextSource > stream.length + decodeFrames) return false;
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
        const auto index = static_cast<std::size_t>(next) & (capacity - 1);
        for (int channel = 0; channel < 2; ++channel) {
            const auto value = decode.getSample(channel, frame);
            stream.ring[channel][index] = value;
            if (index < mirror) stream.ring[channel][capacity + index] = value;
        }
        ++next;
    }
    stream.written.store(next, std::memory_order_release);
    return true;
}
bool Deck::feedStreams() {
    bool didWork = false;
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
    return didWork;
}
std::unique_ptr<juce::AudioFormatReader> Deck::openReader(const Track& track) {
    const juce::File file{juce::String(track.path)};
    if (!file.hasFileExtension("wav;wave;aif;aiff;aifc;mp3;flac"))
        throw std::invalid_argument("Deck supports AIFF, WAV, MP3 and FLAC files");
    if (!std::filesystem::is_regular_file(file.getFullPathName().toStdString()))
        throw std::invalid_argument("Deck audio must be a regular file");
    std::unique_ptr<juce::AudioFormatReader> reader(formats.createReaderFor(file));
    if (!reader) throw std::runtime_error("Unable to decode deck audio: " + file.getFullPathName().toStdString());
    if (reader->numChannels < 1 || reader->numChannels > 2)
        throw std::invalid_argument("Deck audio must be mono or stereo");
    if (!std::isfinite(reader->sampleRate) || reader->sampleRate < 8000 ||
        reader->sampleRate > 768000 || reader->lengthInSamples <= 0)
        throw std::invalid_argument("Invalid deck audio rate or empty audio");
    return reader;
}
bool Deck::analyze(Track& track, juce::AudioFormatReader& reader, std::uint64_t id, BeatAnalysis& grid) {
    {
        std::lock_guard<std::mutex> lock(controlMutex);
        if (cancelled(id)) return false;
        analyzingTrackId = track.id;
    }
    // The same bounded, cancellable QM task serves selected and unvisited
    // entries. Its reader is private; this worker keeps ALL playable rings fed.
    std::atomic<bool> cancel{false};
    try {
        auto result = std::async(std::launch::async, [&] { return analyzeBeats(reader, cancel, track.bpmHint); });
        while (result.wait_for(std::chrono::milliseconds(1)) != std::future_status::ready) {
            if (cancelled(id)) cancel.store(true, std::memory_order_relaxed);
            feedStreams();
        }
        grid = result.get();
    } catch (...) {
        std::lock_guard<std::mutex> lock(controlMutex);
        analyzingTrackId.clear();
        throw;
    }
    std::lock_guard<std::mutex> lock(controlMutex);
    analyzingTrackId.clear();
    return !cancelled(id);
}
bool Deck::ensureGrid(Track& track, std::uint64_t id, juce::AudioFormatReader* reader) {
    if (cancelled(id)) return false;
    auto source = std::make_shared<BeatAnalysisCache>(track.path, track.bpmHint);
    const bool forced = track.analysis->force.load(std::memory_order_acquire);
    if (forced) source->erase();
    if (forced || !track.gridSource || !source->matches(*track.gridSource)) {
        track.grid.reset();
        track.gridSource.reset();
        track.analysis->bpm.store(-1, std::memory_order_release);
    }
    if (!track.grid) {
        auto grid = forced ? std::optional<BeatAnalysis>{} : source->load();
        if (!grid) {
            // Background cache hits never open or decode the source audio.
            auto ownedReader = reader ? nullptr : openReader(track);
            BeatAnalysis analyzed;
            if (!analyze(track, reader ? *reader : *ownedReader, id, analyzed)) return false;
            source->store(analyzed);
            grid = analyzed;
        }
        track.grid = grid;
        track.gridSource = std::move(source);
    }
    // Serialize publication with reanalysis invalidation. A cancelled result
    // may remain a reusable grid, but cannot overwrite a newer pending display.
    std::lock_guard<std::mutex> lock(controlMutex);
    if (cancelled(id)) return false;
    track.analysis->bpm.store(track.grid->bpm, std::memory_order_release);
    track.analysis->force.store(false, std::memory_order_release);
    return true;
}
void Deck::unavailable(Track& track, std::uint64_t id) {
    std::lock_guard<std::mutex> lock(controlMutex);
    if (cancelled(id)) return;
    track.grid.reset();
    track.gridSource.reset();
    track.analysis->bpm.store(0, std::memory_order_release);
    track.analysis->force.store(false, std::memory_order_release);
}
void Deck::publishGrid(const Stream& stream) noexcept {
    detectedBpm.store(stream.grid.bpm, std::memory_order_relaxed);
    detectedOffset.store(stream.grid.offset, std::memory_order_relaxed);
    detectedConfidence.store(stream.grid.confidence, std::memory_order_relaxed);
    syncActive.store(false, std::memory_order_relaxed);
    beatPhaseError.store(0, std::memory_order_relaxed);
}
void Deck::prepareCatalog(Stream& stream, Request wanted, std::vector<Track>& entries, bool filter) {
    const int initial = wanted.track;
    const int count = static_cast<int>(entries.size());
    for (int offset = 0; offset < (filter ? count : 1); ++offset) {
        wanted.track = (initial - 1 + offset) % count + 1;
        auto& track = entries[static_cast<std::size_t>(wanted.track - 1)];
        if (offset != 0) {
            wanted.song = track.memory->load();
            wanted.song.play = false;
            wanted.seconds = wanted.song.position;
            wanted.mode = selection;
        }
        try {
            if (prepare(stream, wanted, track, filter)) return;
            throw std::runtime_error("Deck playlist preparation was cancelled");
        } catch (const std::exception&) {
            if (!filter || cancelled(wanted.id)) throw;
            unavailable(track, wanted.id);
        }
    }
    throw std::runtime_error("No sync-ready playlist entries: no readable audio with a reliable beat grid");
}
bool Deck::prepare(Stream& stream, const Request& wanted, Track& track, bool requireGrid) {
    stream.request = wanted;
    stream.failed.store(false);
    stream.read.store(0);
    stream.written.store(0);
    stream.reader = openReader(track);
    stream.rate = stream.reader->sampleRate;
    stream.length = stream.reader->lengthInSamples;
    if (!ensureGrid(track, wanted.id, stream.reader.get())) return false;
    stream.grid = *track.grid;
    if (requireGrid && stream.grid.bpm <= 0)
        throw std::runtime_error("Deck playlist entry is not sync-ready: no reliable beat grid");
    stream.target = std::clamp(wanted.seconds * stream.rate, 0.0, static_cast<double>(stream.length));
    const auto targetFrame = static_cast<juce::int64>(std::floor(stream.target));
    stream.nextSource = targetFrame - history / 2;
    const auto goal = static_cast<std::uint64_t>(std::clamp(stream.rate * 0.25, 8192.0, static_cast<double>(capacity / 2)));
    while (stream.written.load() < goal) {
        if (cancelled(wanted.id)) return false;
        if (!fill(stream)) break;
    }
    if (cancelled(wanted.id)) return false;
    // Prime the retained history directly; no discarded convolution is needed.
    // Preserve the fractional source-frame seek phase for the first output.
    stream.sinc.reset(stream.ring[0].data(), stream.ring[1].data(),
                      stream.target - targetFrame);
    stream.read.store(history, std::memory_order_release);
    return true;
}
void Deck::work() noexcept {
    std::uint64_t handled = 0;
    std::size_t background = 0;
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
            background = 0;
            Request wanted;
            { std::lock_guard<std::mutex> lock(controlMutex); wanted = requested; }
            if (wanted.id == finishedRequest.load(std::memory_order_acquire)) handled = wanted.id;
            else for (auto& stream : streams) {
                if (stream.state.load(std::memory_order_acquire) != free) continue;
                stream.state.store(preparing, std::memory_order_release);
                try {
                    if (prepare(stream, wanted, tracks[static_cast<std::size_t>(wanted.track - 1)],
                                syncFilter && wanted.mode == selection))
                        stream.state.store(ready, std::memory_order_release);
                    else stream.state.store(retired, std::memory_order_release);
                } catch (const std::exception& error) {
                    unavailable(tracks[static_cast<std::size_t>(wanted.track - 1)], wanted.id);
                    setError(wanted.id, error.what());
                    stream.state.store(retired, std::memory_order_release);
                } catch (...) {
                    unavailable(tracks[static_cast<std::size_t>(wanted.track - 1)], wanted.id);
                    setError(wanted.id, "Unknown deck preparation failure");
                    stream.state.store(retired, std::memory_order_release);
                }
                handled = wanted.id;
                didWork = true;
                break;
            }
        }
        didWork = feedStreams() || didWork;
        // First top up playback and prepare the newest selection/seek. A new
        // request cancels background QM at its existing bounded checkpoints.
        if (!didWork && latestRequest.load(std::memory_order_acquire) == handled) {
            while (background < tracks.size() &&
                   tracks[background].analysis->bpm.load(std::memory_order_acquire) >= 0)
                ++background;
            if (background < tracks.size()) {
                auto& track = tracks[background];
                try {
                    if (ensureGrid(track, handled)) ++background;
                } catch (...) {
                    unavailable(track, handled);
                    if (!cancelled(handled)) ++background;
                }
                didWork = true;
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
void Deck::rememberSong(double phase) noexcept {
    tracks[static_cast<std::size_t>(appliedTrack.load(std::memory_order_relaxed) - 1)].memory->store({
        cursor / streams[current].rate, cueSeconds.load(std::memory_order_relaxed),
        pitchTarget.load(std::memory_order_relaxed), phase,
        syncEnabled.load(std::memory_order_relaxed), playing.load(std::memory_order_relaxed)});
}
void Deck::commit(int index, double& phase, bool rememberOutgoing) noexcept {
    commitSequence.fetch_add(1, std::memory_order_acq_rel);
    auto& stream = streams[index];
    if (rememberOutgoing && stream.request.mode == selection) rememberSong(phase);
    streams[current].state.store(retired, std::memory_order_release);
    current = index;
    pending = -1;
    cursor = stream.target;
    if (stream.request.mode == selection) {
        const auto& song = stream.request.song;
        phase = song.phaseOffset;
        phaseOffset.store(phase, std::memory_order_release);
        playing.store(song.play, std::memory_order_release);
        cueSeconds.store(std::min(song.cue, static_cast<double>(stream.length) / stream.rate),
                         std::memory_order_release);
        pitchTarget.store(song.pitch, std::memory_order_release);
        pitch = song.pitch;
        syncEnabled.store(song.sync, std::memory_order_release);
        syncSpeed = 1 + pitch / 100;
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
    if (pending >= 0 && (cancelled(streams[pending].request.id) ||
                         streams[pending].failed.load(std::memory_order_acquire))) retirePending();
    for (int index = 0; index < static_cast<int>(streams.size()); ++index) {
        int expected = ready;
        if (!streams[index].state.compare_exchange_strong(expected, held, std::memory_order_acq_rel)) continue;
        if (cancelled(streams[index].request.id) || streams[index].failed.load(std::memory_order_acquire))
            streams[index].state.store(retired, std::memory_order_release);
        else { retirePending(); pending = index; }
    }
    const auto now = monotonicSeconds();
    // Sample-time continuity supports both hardware callbacks and offline
    // rendering. Clock snapshots retain their original sample time, so delayed
    // delivery does not shift the shared beat reference to the reception time.
    if (clockReset || nextClockTime == 0 || now > nextClockTime + 0.1) nextClockTime = now;
    clockReset = false;
    const auto tempo = targetTempo.load(std::memory_order_relaxed);
    const auto beatStep = tempo / (60 * outputRate);
    double globalBeat = clockAnchorBeat + (nextClockTime - clockAnchorTime) * tempo / 60;
    nextClockTime += frames / outputRate;
    const bool clockValid = clockReceived.load(std::memory_order_relaxed) && tempo > 0;
    bool activeSync = false;
    double speed = 1, phaseError = 0;
    // Controls share render ownership. Accumulate locally, publishing once per
    // block; raw nudge release freezes the retained target without a smoothing tail.
    auto phase = phaseOffset.load(std::memory_order_relaxed);
    for (int frame = 0; frame < frames; ++frame) {
        if (pending >= 0 && (cancelled(streams[pending].request.id) ||
                             streams[pending].failed.load(std::memory_order_acquire))) retirePending();
        if (pending >= 0 && gain <= 0) commit(pending, phase);
        const bool requestedSync = syncEnabled.load(std::memory_order_relaxed);
        auto& stream = streams[current];
        pitch += (pitchTarget.load(std::memory_order_relaxed) - pitch) * pitchStep;
        const auto targetNudge = nudgeTarget.load(std::memory_order_relaxed);
        nudge += (targetNudge - nudge) * nudgeStep;
        speed = (1.0 + pitch / 100.0) * (1.0 + 0.02 * nudge);
        const auto baseSpeed = stream.grid.bpm > 0 ? tempo / stream.grid.bpm : 0;
        activeSync = requestedSync && clockValid && stream.grid.bpm > 0 &&
                     baseSpeed >= minimumSyncSpeed && baseSpeed <= maximumSyncSpeed;
        phaseError = 0;
        if (activeSync) {
            const auto sourceBeat = (cursor / stream.rate - stream.grid.offset) * stream.grid.bpm / 60;
            phaseError = std::remainder(globalBeat + phase - sourceBeat, 1.0);
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
            if (activeSync && targetNudge != 0)
                phase = std::remainder(phase + 0.02 * targetNudge * beatStep, 1.0);
            const auto offset = static_cast<std::size_t>(consumed) & (capacity - 1);
            const auto used = stream.sinc.process(ratio, stream.ring[0].data() + offset,
                                                  stream.ring[1].data() + offset, lastLeft, lastRight);
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
    phaseOffset.store(phase, std::memory_order_release);
    positionSeconds.store(cursor / streams[current].rate, std::memory_order_release);
    syncActive.store(activeSync, std::memory_order_release);
    playbackSpeed.store(speed, std::memory_order_release);
    beatPhaseError.store(phaseError, std::memory_order_release);
}
double Deck::parameter(const std::string& key) const {
    if (key == "index") return requestedTrack.load(std::memory_order_acquire);
    if (key == "seek") return positionSeconds.load(std::memory_order_acquire);
    if (key == "play") return playing.load(std::memory_order_acquire) ? 1.0 : 0.0;
    if (key == "pitch") return pitchTarget.load(std::memory_order_acquire);
    if (key == "nudge") return nudgeTarget.load(std::memory_order_acquire);
    if (key == "sync") return syncEnabled.load(std::memory_order_acquire) ? 1.0 : 0.0;
    if (key == "phase_offset") return phaseOffset.load(std::memory_order_acquire);
    throw std::invalid_argument("Unknown deck parameter: " + key);
}
double Deck::setParameter(const std::string& key, double value) {
    if (!std::isfinite(value)) throw std::invalid_argument("Deck parameters must be finite");
    if (key == "index") {
        if (value != std::floor(value) || value < 1 || value > trackCount())
            throw std::invalid_argument("Deck index is a 1-based playlist index");
        request(static_cast<int>(value), 0, selection);
    } else if (key == "seek") {
        value = std::clamp(value, 0.0, durationSeconds.load(std::memory_order_acquire));
        command("seek", value);
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
    } else if (key == "phase_offset") {
        if (value < -0.5 || value > 0.5)
            throw std::invalid_argument("Deck phase_offset must be in [-0.5, 0.5]");
        phaseOffset.store(value, std::memory_order_release);
    } else if (key == "sync") {
        syncEnabled.store(value != 0, std::memory_order_release);
        if (value == 0) syncActive.store(false, std::memory_order_release);
    } else throw std::invalid_argument("Unknown deck parameter: " + key);
    return value;
}
void Deck::setClock(double bpm, double beat, double sampledAtSeconds) {
    if (!std::isfinite(bpm) || bpm <= 0 || !std::isfinite(beat) || !std::isfinite(sampledAtSeconds))
        throw std::invalid_argument("Deck clock requires positive finite tempo and finite beat/time");
    clockAnchorBeat = beat;
    clockAnchorTime = sampledAtSeconds;
    targetTempo.store(bpm, std::memory_order_release);
    clockReset = true;
    clockReceived.store(true, std::memory_order_release);
}
void Deck::command(const std::string& action, double value) {
    if (!std::isfinite(value)) throw std::invalid_argument("Deck command value must be finite");
    if (action == "phase_reset") {
        phaseOffset.store(0, std::memory_order_release);
    } else if (action == "reanalyze") {
        playing.store(false, std::memory_order_release);
        request(appliedTrack.load(std::memory_order_acquire),
                positionSeconds.load(std::memory_order_acquire), reanalysis);
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
    result.syncFilter = syncFilter;
    result.songStates.reserve(tracks.size());
    for (const auto& track : tracks) result.songStates.emplace(track.id, DeckSongState{});
    result.songBpms.reserve(tracks.size());
    {
        std::lock_guard<std::mutex> lock(controlMutex);
        result.analyzingTrackId = analyzingTrackId;
        for (const auto& track : tracks) {
            const auto bpm = track.analysis->bpm.load(std::memory_order_acquire);
            result.songBpms.emplace(track.id, bpm < 0 ? std::nullopt : std::optional<double>{bpm});
        }
    }
    // With catalog edits serialized by the control owner, only a render commit
    // can change this identity/duration tuple. The render thread never waits
    // for snapshot readers; readers retry on the control thread.
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
        result.phaseOffset = phaseOffset.load(std::memory_order_acquire);
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
        for (const auto& track : tracks) result.songStates.at(track.id) = track.memory->load();
        result.songStates.at(tracks[static_cast<std::size_t>(result.trackSelect - 1)].id) = {
            result.position, result.cue, result.pitch, result.phaseOffset, result.sync, result.play};
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
