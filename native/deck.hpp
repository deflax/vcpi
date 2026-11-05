#pragma once
#include <juce_audio_basics/juce_audio_basics.h>
#include <juce_audio_formats/juce_audio_formats.h>
#include "ownership.hpp"
#include <array>
#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace vcpi {
static_assert(std::atomic<double>::is_always_lock_free && std::atomic<std::uint64_t>::is_always_lock_free,
              "Deck rendering requires lock-free scalar snapshots");
struct DeckStatus {
    std::string bankId, trackId, trackTitle, requestedTrackId, state, error, syncError;
    int trackSelect = 1, requestedTrackSelect = 1;
    std::uint64_t requestId = 0;
    bool loading = false, ready = true, buffering = false, ended = false, play = false;
    double position = 0, duration = 0, cue = 0, pitch = 0, nudge = 0;
    bool sync = true, syncActive = false;
    double bpm = 0, beatOffset = 0, beatConfidence = 0, playbackSpeed = 1, beatPhaseError = 0;
};

// Readers, filters and ring storage belong to this object until its worker joins.
// render() neither owns nor releases dynamically allocated resources.
// Callers serialize control operations with each other; Plugin uses a control
// mutex. Render-state mutations use its short ownership guard. Catalog edits
// join/prepare off-render and acquire that guard only to publish the change.
class Deck {
public:
    explicit Deck(const std::string& catalog, double outputRate);
    ~Deck();
    Deck(const Deck&) = delete;
    Deck& operator=(const Deck&) = delete;
    void render(float* left, float* right, int frames) noexcept;
    DeckStatus status() const;
    double parameter(const std::string& key) const;
    double setParameter(const std::string& key, double value);
    void command(const std::string& action, double value = 0);
    void updateCatalog(const std::string& catalog, Ownership& ownership);
    int trackCount() const noexcept { return static_cast<int>(tracks.size()); }
    const std::string& bankId() const noexcept { return bank; }
    const std::string& bankPath() const noexcept { return locator; }
private:
    static constexpr std::size_t capacity = 262144, mirror = 512;
    static constexpr int decodeFrames = 4096, filterStages = 8, history = 200, preRoll = 2048;
    enum State { free, preparing, ready, held, active, retired };
    enum Mode { selection, seek, recall, restore };
    struct BeatGrid { double bpm = 0, offset = 0, confidence = 0; bool analyzed = false; };
    struct Track { std::string id, path, title; BeatGrid grid; };
    struct Catalog { std::string bank, locator; std::vector<Track> tracks; };
    static Catalog parseCatalog(const juce::var& catalog);
    struct Request { std::uint64_t id = 0; int track = 1; double seconds = 0; Mode mode = selection; };
    struct Stream {
        std::atomic<int> state{free};
        Request request;
        std::unique_ptr<juce::AudioFormatReader> reader;
        std::array<std::vector<float>, 2> ring;
        std::array<std::array<juce::IIRFilter, filterStages>, 2> filters;
        std::array<juce::WindowedSincInterpolator, 2> sinc;
        std::atomic<std::uint64_t> read{0}, written{0};
        double rate = 0, target = 0;
        BeatGrid grid;
        juce::int64 length = 0, nextSource = 0;
        int discard = 0;
        std::atomic<bool> failed{false};
    };
    void request(int track, double seconds, Mode mode);
    bool prepare(Stream& stream, const Request& request, Track& track);
    bool fill(Stream& stream);
    bool analyze(Stream& stream, BeatGrid& grid);
    void publishGrid(const Stream& stream) noexcept;
    void work() noexcept;
    void commit(int index) noexcept;
    void retirePending() noexcept;
    void setError(std::uint64_t id, const std::string& error);
    bool cancelled(std::uint64_t id) const noexcept;
    const double outputRate, rampStep, pitchStep, nudgeStep;
    std::string bank, locator;
    std::vector<Track> tracks;
    std::array<Stream, 3> streams;
    juce::AudioBuffer<float> decode{2, decodeFrames};
    juce::AudioFormatManager formats;
    mutable std::mutex controlMutex, errorMutex;
    Request requested;
    std::string lastError;
    std::atomic<std::uint64_t> latestRequest{0}, finishedRequest{0}, commitSequence{0};
    std::atomic<int> requestedTrack{1}, appliedTrack{1};
    std::atomic<bool> stopping{false}, playing{false}, buffering{false}, ended{false};
    std::atomic<double> positionSeconds{0}, durationSeconds{0}, cueSeconds{0}, pitchTarget{0}, nudgeTarget{0};
    std::atomic<bool> syncEnabled{true}, syncActive{false};
    std::atomic<double> detectedBpm{0}, detectedOffset{0}, detectedConfidence{0};
    std::atomic<double> playbackSpeed{1}, beatPhaseError{0}, targetTempo{0};
    std::atomic<bool> clockReceived{false};
    std::thread worker;
    // Render-thread-only state, initialized before the worker is started.
    int current = 0, pending = -1;
    double cursor = 0, pitch = 0, nudge = 0, gain = 0;
    // Commands share Plugin's render ownership, never the reader-worker mutex.
    double clockAnchorBeat = 0, clockAnchorTime = 0, nextClockTime = 0, syncSpeed = 1;
    bool clockReset = false;
    float lastLeft = 0, lastRight = 0;
};
} // namespace vcpi
