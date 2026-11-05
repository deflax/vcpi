# Native deck track implementation plan

## Goal and scope

Add `deck` as a native JUCE-backed track source for manual DJ beatmatching.
Each deck independently loads a track from a music bank, plays stereo audio,
and supports vinyl-style pitch, momentary nudge, pause, seek and one cue point.
Existing track mixing, inserts, mute/solo, MIDI routing and process isolation stay
in place. Playback is independent of Link and global musical transport.

No automatic sync, beat detection, time stretching/key lock, crossfader,
scratching/reverse playback, waveform UI or looping is required by this design.
The implementation below is now present; the original design contract is retained
for reference. See implementation verification at the end for exercised gates
and the remaining physical-hardware acceptance limits.

## Verified repository foundation

- `src/engine/plugin.hpp` and `src/engine/plugin.cpp`: `Plugin` already wraps VST3 and a
  native WAV sampler. The sampler reads the entire file, resamples it into memory
  and plays MIDI-triggered one-shot voices. It is not a deck transport.
- `src/engine/track_runner.cpp`: `TrackRunner` renders a native `Plugin`, then its inserts,
  into shared-memory transport. Output mixes those blocks with one block of
  buffering. Neither track DSP nor the output callback enters Python.
- `src/plugin_host.py`, `src/audio/track_client.py`, `src/audio/owner.py`: new track
  sources are prepared in staged workers before publication. Existing track
  controls operate through bounded IPC and generation-aware updates.
- `src/audio_state.py`, `src/models.py`: daemon-side snapshots currently have
  no deck transport/status fields. Completion and read-only commands use caches.
- `src/sessions/persistence.py`: session format 3 saves native VST3 chunks and path-based WAV
  tracks; versions 1, 2 and 3 are readable.
- `src/engine/CMakeLists.txt`: JUCE 8.0.6 audio basics/formats/processors are available;
  audio-utils is not currently compiled. FLAC, Ogg and MP3 decoding are disabled.
  Do not assume AudioTransportSource is available without build changes.

## Behaviour contract

| Operation | Proposed behaviour |
|---|---|
| Create deck | Load bank, prepare entry 1, remain paused at position 0 |
| Track selection | One integer parameter, 1-based like other user-facing VCPI indexes |
| Successful track change | Fade old playback out, commit prepared track, reset cue and position to 0, remain paused; retain pitch, mixer, routing and inserts; clear nudge |
| Selection failure | Keep existing track and its transport; expose failed request and error; never silently substitute another entry |
| Play/pause | Pause preserves position; play resumes; short gain ramps avoid discontinuities |
| Pitch | Percent, -16 to +16; 0 means original speed and pitch |
| Nudge | -1 to +1, temporary speed offset of up to -2% to +2%; release returns to 0 |
| Seek | Absolute source-track seconds, bounded to [0, duration]; preserve playing/paused state once target data is ready |
| Set cue | Store current source position, without moving playback |
| Recall cue | Move to stored cue and pause |
| End of file | Stop at duration, no automatic wrap; Play from ended restarts at cue |
| Track mute | Continue advancing transport through the existing mixer behaviour |
| Output audio stop | Freeze deck position when rendering stops; starting output must not implicitly change the deck play flag |
| Session restore | Restore selected track, pitch and cue, positioned at cue and paused; nudge is 0 |

Pitch/nudge are smoothed. Source position advances by
`source_sample_rate / output_sample_rate * (1 + pitch / 100) * (1 + 0.02 * nudge)`
per output frame, using a double-precision source-frame cursor. Report position
and duration in original-track seconds, not speed-adjusted elapsed wall time.

During track preparation, the old track remains usable. The new track takes
effect only after successful preparation and an audio-side commit. Play commands
while selection is pending affect the old track; the newly committed track still
loads paused. The newest selection wins: stale preparation results cannot commit.
An explicit selection of the already active entry is a no-op unless another
selection is pending, in which case it cancels that pending selection.

## Music bank

Use a small JSON manifest, not directory enumeration or numbered filenames:

```json
{
  "version": 1,
  "id": "live-set",
  "tracks": [
    {"id": "opening", "path": "music/opening.wav", "title": "Opening"},
    {"id": "second", "path": "music/second.wav", "title": "Second"}
  ]
}
```

- Manifest order defines `track_select` values 1..N. Stable track IDs, not these
  ordinal values, identify saved selections. No fixed 128-track limit.
- Relative asset paths resolve against the manifest directory, never the daemon
  working directory. Explicit absolute paths may also be used.
- Reject duplicate/empty IDs and empty banks. Opening a bank need not decode
  every asset; prepare only the selected track. Report missing/unreadable assets
  when selected, without damaging an already loaded deck.
- Parse/validate the manifest on the control side and give native code a resolved
  bank catalog once at deck construction. Track changes pass an ordinal/request
  ID, not the entire catalog. Keep catalog transfers within existing framed IPC
  limits or use its multipart pattern; do not raise limits indiscriminately.
- WAV is the initial required format, supporting mono (duplicated to stereo) and
  stereo, varying source sample rates, and supported PCM/float WAV encodings.
  Reject unsupported layouts/formats clearly. MP3/FLAC support requires a separate
  explicit codec/build decision; it is not implied by using JUCE.
- No automatic bank rescanning or authoring command is needed. Reopening the
  bank reloads the manifest. Keep track banks distinct from MIDI sequence banks.

## Native architecture and real-time rules

Introduce `src/engine/deck.hpp` and `src/engine/deck.cpp` for a focused `Deck` component.
Integrate it as a `deck` branch of the existing `Plugin` source wrapper rather
than refactoring all sources into a new inheritance hierarchy. Keep the WAV
sampler unchanged. Branch deck metadata, parameters, processing and persistence
explicitly; never dereference the absent VST3 instance for a deck.

Each deck owns:

- transport state and a source-frame cursor on the native render thread;
- one non-real-time file preparation/read-ahead worker;
- bounded, preallocated stereo streaming buffers and resampler scratch storage;
- bounded control handoff plus small applied-state snapshots.

The worker uses JUCE audio-format readers to open/decode files and refill
read-ahead buffers. Memory consumption must be bounded by configured buffering,
not track duration. Use JUCE resampling primitives with retained filter/history
state and measured anti-aliasing behaviour; do not reuse the sampler's full-file
linear resampling or reset interpolation history on each audio block.

Loading and seeking prepare data outside the render thread. Prepared buffers
carry selection/seek request IDs, so abandoned results cannot affect the active
cursor. Preserve resampler continuity for pitch changes; reset/re-prime history
on a committed track change or seek. Prepare against the actual source rate and
maximum allowed playback rate, not an assumed 44.1 kHz source.

Render-side requirements:

- no file operations, decoding, allocation, waiting, Python/GIL or thread joining;
- bounded command consumption and fixed-cost buffer lookup;
- read-ahead shortages produce a ramp to silence, retain the logical position
  until matching data is ready, then resume; publish buffering state rather than
  reading synchronously or silently skipping source content;
- EOF is distinct from temporary starvation;
- swapping buffers never destroys readers or releases the last owning reference
  on the render thread; retire resources to the worker/control side;
- worker teardown and cancellation complete before deck destruction returns.

Existing `Plugin` ownership can exclude rendering while control holds it. Never
hold it across track loading, decoding, file seeks or worker joins. Ordinary
control writes should enqueue bounded native commands and return promptly.

## Control and headless interface

Proposed CLI, following existing track and parameter conventions:

```text
track 1 deck /music/live-set.json Deck-A
track 2 deck /music/live-set.json Deck-B
get 1
set 1 track_select 2
set 1 pitch -1.25
set 1 play true
set 1 nudge 1
set 1 nudge 0
track 1 deck seek 90
track 1 deck cue set
track 1 deck cue
track 1 deck status
```

Expose `track_select`, `play`, `pitch`, and `nudge` through the existing typed
parameter descriptor/get/set/controller machinery. Cue and seek are explicit
commands, not fake persistent automation booleans. A native deck parameter
adapter must provide physical ranges/values without depending on VST3 parameter
objects. Track selection acknowledgement means accepted, not loaded.

Add bounded deck status updates for active/requested track identity, request ID,
loading/ready/buffering/ended state, play flag, position, duration, cue, pitch,
nudge and last load error. Report requested and applied selection separately.
Publish transitions promptly and progress at a capped rate (for example 10 Hz),
not once per sample/block. Preserve snapshot epoch and track-generation checks.
`get`, status and completion remain cache-only, including manifest track titles.

Within-bank track changes do not replace the track worker, increment the source
replacement generation, or rebuild inserts. Initial deck creation still uses
staged track publication so a failed load cannot destroy the previous track.

Define routed MIDI transport bindings in one documented mapping table, alongside
CLI controls: press/release nudge, play/pause, cue and continuous pitch/selection.
Keep these local to deck interpretation rather than changing other source types.
Generic MIDI input already forwards releases; MIDI Mix currently treats its
supported buttons as toggles and ignores releases. Do not hijack its mute/solo
buttons for nudge. Any controller path assigned to nudge must preserve release,
including velocity-zero note-on, and clear held state on input disconnect,
channel-route removal and deck replacement. Bindings and channel filtering need
behavioural tests; a broader controller mapping redesign is not part of this work.

## Persistence

Extend session format 3 while retaining readers for formats 1 and 2. Existing
sources keep their established restore behaviour. A deck track gets an explicit
`deck` payload containing bank manifest locator, bank ID, stable selected track
ID, cue time and pitch, alongside ordinary name/mixer/routing/effect fields.
Do not serialize a VST3 chunk for the deck itself. Its inserts still save theirs.

Resolve the selected track by ID on restore, regardless of manifest reordering.
Do not silently fall back to a different ordinal or a stale asset path when an
ID is missing. Use existing session error reporting for missing banks/tracks and
continue restoring unaffected tracks. Playback always restores paused at cue,
with zero nudge, even if restoring the session restarts the output device.
Persist only committed track selection if a load is in flight.

## Implementation sequence and acceptance gates

1. **Deck source and native playback.** Implement bounded streaming, resampling,
   transport, fades, seek/cue, EOF and native status; integrate the source wrapper,
   pybind API and native build. Prove generated mono/stereo WAV playback at
   different source/output rates through the real offline `TrackRunner` path.
2. **Bank and owner integration.** Implement manifest validation, staged deck
   creation, in-worker selection, cancellation and status publication through
   host/audio/track owners. Prove rapid A/B/C selection commits only C, failed
   selection preserves the old track, inserts survive changes, and another track
   keeps rendering while loading or seeking.
3. **Headless controls.** Add CLI grammar/help, typed parameters, completion,
   status/labels and the documented MIDI bindings. Exercise commands through the
   real daemon/client interface, not just direct native calls. Demonstrate pitch
   and nudge-release behaviour with actual routed MIDI messages.
4. **Sessions and complete acceptance.** Add format-3 deck persistence and legacy
   compatibility coverage. Prove save/restore after bank reordering selects the
   same track ID and never autoplays. Document the feature in README.md and
   USAGE.md after smoke proof. Run existing affected suites and final live smoke.

Each stage is an implementation dependency, not a separately delivered reduced
feature. The deliverable includes all four stages.

Permanent regression coverage should focus on observable behaviour: pause
freezes position; pitch/nudge alter known tone frequency and cursor rate; release
returns to base speed; cue/seek land correctly; EOF does not wrap; late requests
cannot replace newer selections; missing files preserve current playback;
streaming shortages recover without position corruption; decks remain isolated;
track effects/routing survive selection; session restoration is paused and uses
stable identity; repeated create/change/destroy leaves no workers/resources.

Use generated WAV fixtures and existing hardware-free patterns in
`tests/test_audio.py`, `tests/test_track_process.py` and
`tests/test_audio_process.py`. Streaming-worker tests must explicitly await
readiness with deadlines rather than use arbitrary sleeps. Instrument the render
path in a throwaway smoke harness to verify no render-thread allocation or file
I/O. Verify bounded memory with a long track, not only tiny fixtures.

Final audible acceptance: two decks, manual beatmatching with pitch/nudge, seek
and cue, bank switching while the other deck plays, and session restore. Repeat
on Linux and macOS; measure read-ahead/deadline behaviour on the intended Pi
hardware before claiming suitability there. Offline output is not a substitute
for this audible and hardware-specific check.

## Expected change surface

- Native: new deck files; `src/engine/plugin.hpp`, `src/engine/plugin.cpp`,
  `src/engine/bindings.cpp`, `src/engine/CMakeLists.txt`; runtime only if needed for
  bounded status/control handoff, not a new mixer or output path.
- Ownership/control: `src/plugin_host.py`, `src/host.py`, `src/audio/control.py`,
  `src/audio/` client, owner and protocol modules.
- Snapshots/interface: `src/models.py`, `src/audio_state.py`, `src/commands/`,
  affected presentation/status renderers and controller handlers.
- Bank parsing: a focused control-side module such as `src/deck_bank.py`.
- Persistence/tests/docs: `src/sessions/`, focused behavioural tests alongside
  the existing native/process tests, README.md and USAGE.md.

## Research verification

An actual native WAV source was created and rendered through `TrackRunner`
without opening an audio device. A generated stereo fixture produced native
transport `progress=1`, `audio_peak=0.125`, and an empty failure string. NumPy is
unavailable in the current Python environment, so rendered array conversion
could not be inspected; native transport statistics verified the render. This
checks the existing integration foundation, not any proposed deck behaviour.
No deck implementation or automated test suite was run as part of this plan.

## Implementation verification

Implemented the native streaming deck, owner/control integration, headless
commands and routed MIDI bindings, and format-3 stable-identity persistence.
User-facing examples and bank/MIDI details are in README.md and USAGE.md.

Verified on Linux/aarch64:

- `python build_native.py` builds the native extension successfully.
- Full suite with a real DragonflyRoomReverb insert enabled:
  `DECK_TEST_EFFECT=/workspaces/vcpi-305d644615af/vst3/DragonflyRoomReverb.vst3 /tmp/vcpi-deck-venv/bin/python -m pytest -q tests`
  produced **195 passed, 10 skipped**. Deck cases cover generated mono/stereo,
  different source rates, PCM24/float WAV, pitch/nudge tone and cursor changes,
  pause, seek/cue/EOF (including cue restored at duration), cancellation/failure,
  starvation recovery, isolated decks, insert preservation, large catalogs,
  actual routed MIDI releases/lifecycle, and paused stable-ID session restore.
- A separate real daemon process and Unix-socket client exercised two deck
  creations, cached get/status, typed play/pitch/nudge controls, seek/cue,
  save, reordered-manifest restore and clean shutdown. With output stopped,
  prepared seek/selection commits correctly remain pending until rendering.
- A throwaway standalone C++ render harness instrumented allocation/deallocation
  and file-operation calls while rendering and issuing pitch/nudge/seek/cue and
  selection controls: **0 render allocations, 0 frees, 0 file-I/O calls**.
- A sparse two-hour stereo 48 kHz WAV (1,382,400,044 logical bytes) reported the
  correct 7200-second duration. Adding its deck alongside a short-file deck
  increased resident memory by **6272 KiB**, rather than loading track duration.

Physical audible/manual-beatmatching acceptance is **not verified**: this
workspace has no audio device. macOS listening and intended-Pi read-ahead/CPU/
deadline measurements also remain unperformed. No live-hardware suitability
claim follows from the hardware-free results above.
