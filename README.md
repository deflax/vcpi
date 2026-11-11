# vcpi

vcpi hosts Cardinal/VCV and plugdata/Pure Data patches alongside audio decks,
with VST3 effects, a native C++ audio engine, Python control plane, and Ableton
Link tempo sync for hardware-centric live setups (for example Arturia BeatStep
Pro + Akai MIDI Mix).

## Setup

Linux and macOS are supported. Install the native build prerequisites before
starting the daemon. The first build requires network access to download pinned
JUCE, pybind11, PortAudio and Queen Mary DSP sources, plus Git, CMake 3.22+, a
C++17 compiler, and Python development headers.

On Debian/Ubuntu:

```bash
sudo apt install build-essential cmake pkg-config git python3-dev python3-venv \
  libasound2-dev libfreetype6-dev libfontconfig1-dev libx11-dev libxext-dev
```

On macOS, install Python 3, Git, Apple's command-line tools, and CMake
(for example with Homebrew):

```bash
xcode-select --install
brew install python cmake git
```

The native build resolves the macOS SDK with `xcrun` before initializing the
compilers, including when an existing build cache has an empty or obsolete
sysroot. A CMake `CMAKE_OSX_SYSROOT` containing the required headers takes
precedence, followed by `SDKROOT`, then SDK discovery through `xcrun`. SDK names
passed as `CMAKE_OSX_SYSROOT` are also resolved through `xcrun`.
If compilation reports missing SDK headers such as `TargetConditionals.h` or
`assert.h`, check `xcrun --sdk macosx --show-sdk-path` and that the reported SDK
still exists. After an Xcode/command-line-tools update, repair the active
developer directory with `xcode-select` if necessary, and unset an obsolete
`SDKROOT`. Cached SDK paths missing the required headers are refreshed automatically.
Then rerun `./vcpid`; deleting downloaded build dependencies is not required.

### Quick start (from repo root)

```bash
# Terminal 1: prepare the environment, build native audio, and start the daemon
./vcpid

# Terminal 2: connect the interactive client
./vcpi
```

Both launchers create `.venv` if needed and install the application in editable
mode on first launch, when `requirements.txt` or `pyproject.toml` changes, or when
the environment points to another checkout. `./vcpid` also builds the native
extension incrementally before starting the daemon.

On a fresh session, start audio manually. Later server launches automatically
reopen the saved audio output. With plugdata installed, try a bundled patch:

```text
track 1 pd sine
audio devices
audio start
note 1 60 --velocity 100 --duration-ms 500
```

Mixer channels are called **tracks** (1–16). Sessions read and write only the
current format, **3**, with track-based keys. Older or unknown formats are
rejected; no backward-compatibility migration is provided.

Use `audio start <device>` to select an output from `audio devices`.
Use `quit` to disconnect the client; `shutdown` stops the daemon.
Only one daemon per user can run at a time.

### Manual setup

Direct Python startup does not install dependencies or build the extension:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python build_native.py

# Terminal 1
.venv/bin/python -m vcpi serve

# Terminal 2
.venv/bin/python -m vcpi cli
```

Build on each target machine using its own virtual environment. Linux uses
PortAudio's ALSA backend; macOS uses CoreAudio. VST3 bundles must match the target
OS and CPU architecture. Do not copy `.venv`, `build/`, or compiled extensions
between platforms. Set `CMAKE_BUILD_PARALLEL_LEVEL=1` on memory-constrained systems.

## Runtime Model

The daemon runs a Python control process, a spawned audio-output process, and a
persistent process for each loaded track; the CLI is a separate client connected
through a local Unix socket. To use it from another machine, SSH into the host
and run `./vcpi` there.

Command parsing, session files, MIDI ports, sequencing, and Ableton Link stay in
the control process. Each track owns its Cardinal/plugdata or deck source and insert
effects on a native render thread. The native PortAudio callback mixes track
audio and runs master effects. Neither the callback nor track DSP enters Python
or acquires the GIL. Shared-memory track transport adds one block of buffering.
Loading or replacing a track does not stop unrelated tracks; read-only plugin
commands use cached snapshots.

Selective scheduling targets only native audio threads. Linux requests
`SCHED_FIFO` for output and track rendering. macOS requests Mach
`THREAD_TIME_CONSTRAINT_POLICY` for track threads at startup while leaving
output callback scheduling to CoreAudio (`backend-managed` in diagnostics).
A macOS track's `configured` state means the startup request was accepted, not
that its live policy is continuously verified; the kernel can later demote it.
By default, track workers check for work and sleep for 100 microseconds when idle;
one-block buffering is unchanged. `VCPI_TRACK_WAKEUP=notify` instead blocks on
shared futexes on Linux or Mach semaphores on macOS. Both modes use the same
optimized renderer; see [wakeup comparisons](USAGE.md#comparing-polling-and-notifications).
Dropout-free audio is
not guaranteed. Under desktop CPU contention, late track
blocks can be replaced by silence even with no callback underruns: inspect
missing track blocks as well as output timing and underruns. See
[selective realtime scheduling](USAGE.md#selective-realtime-scheduling) for
platform-specific states and Linux permissions.

To measure a running session before changing audio scheduling, use
`python -m vcpi.benchmark --output baseline.json` from the installed Python
environment. It samples CPU and audio-counter deltas and temporarily enables
native request-to-render/render-duration measurements without pausing playback.
See [audio benchmarking](USAGE.md#audio-benchmarking) for repeatable comparisons.

Sessions save native VST3 state chunks for instruments, track inserts, and master
effects. Sessions save track focus, while decks embed
their playlists and each song's position, play/pause, cue, pitch, sync and phase
correction. Playing decks resume when audio starts. See [session commands](USAGE.md#session-commands)
for save/restore behavior.

Native decks stream mono/stereo AIFF, WAV, MP3 and FLAC tracks from session-owned
playlists. Load an audio file with `track 1 deck song.wav`, or a directory with
`track 1 deck "/music/Live Set"` to load its supported audio files in filename
order without scanning subdirectories. Repeat either command to replace the
whole playlist and reset song memory; mixer, routing and inserts are retained.
Add `--bpm-hint 174` when loading a path to supply its expected median tempo.
The optional 60–200 BPM hint guides analysis and half/double-time selection,
without forcing every song to that exact BPM; it is saved with each playlist
entry and has a separate analysis cache from other hints or automatic analysis.
Use `track 1 clear` to remove the whole deck. Queen Mary DSP analyzes the whole selected file for tempo and a stable beat grid; decks
follow the shared musical timeline's tempo and beat phase by default.
Same-tempo regions that switch to offbeat accents can support analysis through
an explicit half-beat alternative without doubling BPM. The output retains one
anchored phase, not an average of opposite phases; this is not automatic kick
identification, and `phase_offset` remains available for musical correction.
Recurring syncopated two-step accents can select full-time tempo over a stable
half-time pulse (for example 166 rather than 83 BPM), without treating uniform
hi-hat subdivisions as grounds to double slower tracks. This is a conservative
rhythm heuristic, not a guarantee of genre or tempo-octave recognition.
Each has play/pause, vinyl-style speed/pitch, nudge, seek and one cue point.
While synced, nudge adjusts a persistent per-song phase correction without
changing shared BPM; releasing retains alignment. Read or set `phase_offset`
in beats, or clear it with `track 1 deck phase reset`. Unsynced nudge retains
its temporary ±2% speed behavior. The first three parameters—track selection,
seek and nudge—map automatically to MIDI Mix knobs.
Use `set 1 sync false` for manual beatmatching. See
[deck commands and MIDI bindings](USAGE.md#deck-commands) for playlist editing,
analysis limits and controls. There is no key lock, looping or crossfader.

Beat analysis is cached across loads and daemon restarts. On Linux the default
is `~/.cache/vcpi/analysis/`; an absolute `XDG_CACHE_HOME` overrides the cache root.
macOS defaults to `~/Library/Caches/vcpi/analysis/`. File identity and analyzer
version changes invalidate cached results. Use `track 1 deck reanalyze` to
discard the selected song's cached beats and detect again, paused at its current
position. The cache contains analysis metadata, not audio or session playlists;
see [deck commands](USAGE.md#deck-commands).

## Repository Layout

```text
.
  src/
    vcpi/               # Importable Python application package
      host.py           # Control-process orchestration
      commands/         # CLI handlers, help, completion and discovery
      audio/            # Process clients, owners, protocol and control.py facade
      controllers/      # Generic MIDI input and hardware integrations
      presentation/     # Status, signal-flow and terminal rendering
      sessions/         # Persistence, snapshots and restore phases
      plugin_host.py    # Native lifecycle helpers for audio children
      audio_state.py    # Cached plugin/parameter snapshots
      client.py         # Unix-socket CLI client
      server.py         # Headless Unix-socket server
      link.py           # Ableton Link wrapper
      timeline.py       # Shared clock authority, bar grid and transport
      sequencer.py      # Internal step sequencer
    engine/             # C++ hosting, decks, transport and mixer
      CMakeLists.txt    # Native extension build
      pd_state.cpp      # Control-thread plugdata/Pd state conversion
      track_runner.cpp  # Per-track rendering and MIDI consumption
      runtime.cpp       # Output device, mixing and master-effects callback
  patches/              # Cardinal .vcv and plugdata .pd patches
  sessions/             # User-saved session files, not Python source
  tests/                # Automated Python regression tests
  pyproject.toml        # Python package metadata and src discovery
  requirements.txt      # Python dependency set
  build_native.py       # Native build entrypoint
  main.py               # Direct-script entrypoint (after package installation)
  vcpi                  # Client launcher
  vcpid                 # Daemon launcher
  USAGE.md              # Command and flag reference
  rpi-build/            # Raspberry Pi image build/provisioning tools
```

`src/vcpi` is the Python package; `src/engine` is C++ source, not a Python package.
Import command entrypoints from `vcpi.commands.cli` and `vcpi.commands.completion`;
audio facades from `vcpi.audio.client`, `vcpi.audio.track_client`, and
`vcpi.audio.control`; and session file operations from `vcpi.sessions.persistence`.
Command execution and completion share `vcpi.commands.assets` for discovery.
Native backend imports remain deferred to the processes that own audio resources.
The build cache lives under
`build/engine/<platform-architecture-python-abi>/<checkout-id>/`, where the
checkout ID is derived from the resolved checkout path. Moving or copying a
checkout selects a fresh cache rather than reusing CMake's incompatible absolute
paths. Existing caches are left untouched; the first build at a new location may
download the pinned build dependencies again. CMake places the compiled
`_vcpi_native` extension inside `src/vcpi`.

The native `PluginOwnership` guard is shared by track rendering and master effects.
Pd-state conversion stays on the control thread, while deck streaming and callback
resource-lifetime rules remain within their existing ownership boundaries.

## Common Commands

```bash
# Start headless daemon with Unix socket API
./vcpid

# Connect CLI client to a running daemon
./vcpi

# Direct python equivalents
python -m vcpi serve
python -m vcpi cli

# Logging defaults:
# - python -m vcpi serve -> INFO
# - ./vcpid -> INFO
LOG_LEVEL=INFO ./vcpid
```

Startup options use descriptive names, for example:

```bash
./vcpid --sample-rate 48000 --buffer-size 512 --bpm 128 --no-link \
  --output "USB Audio" --socket /tmp/my-vcpi.sock
./vcpi --socket /tmp/my-vcpi.sock
```

Explicit tempo, Link, and device options override restored session settings.
Omitted options preserve saved settings; sample rate and buffer size are
startup-only. The server automatically reopens the saved audio output;
`--output` starts the explicitly selected output instead. With no saved or
explicit output, audio stays stopped until `audio start [device]`.
Use `--midi-input` repeatedly to select multiple MIDI inputs and
`--midimix-input` / `--midimix-output` for controller connections.

Toggle logging without restarting from the connected client:
`debug on` enables DEBUG, `debug off` returns to INFO, and `debug` shows the
current level. The setting applies to the daemon and audio workers until the
daemon exits; logs appear in the daemon terminal or service journal.
Raw native/plugin stdout and stderr are discarded unless DEBUG is enabled, then
shown with `[daemon]`, `[audio]`, or `[track N]` prefixes. vcpi creates no
per-process log files and cannot replay discarded output. Structured vcpi
activity messages, warnings, and errors remain visible. For startup diagnostics, use
`LOG_LEVEL=DEBUG ./vcpid`. Native build progress and diagnostics always stream
with `[build]` prefixes, regardless of `LOG_LEVEL`, including on the first launch.

Full CLI and startup flag reference: [USAGE.md](USAGE.md).

Inside `./vcpi`, press `Tab` for command and argument suggestions. Completion
comes from the daemon: audio/MIDI subcommands, routing channels and tracks,
sequencer banks, mixer targets, help topics, and debug modes are supported.
`get` suggests cached controls and listing flags; `set` suggests controls, plugin
settings, and boolean/enum choices. Effect indexes reflect loaded chains.
Plugin, patch, and session names are
discovered on the daemon, including names containing spaces where the command
supports them. Quote names containing spaces. Free-form numeric values are left for you to enter.

The client keeps a scrolling `vcpi>` prompt with session history and uses
`prompt_toolkit` + Rich for enhanced terminal output. `help`, `get`, `status`, and
`show` commands display tables. Bare `show` prints a compact sixteen-track overview;
focused views cover routing, controllers, audio, tracks, master and effects.
All views print at the prompt. `get` includes parameter values, units, ranges,
and choices without slider bars.
The `help` overview combines its categories and descriptions into one compact
table; use `help <command>` or a focused topic such as `help track deck`,
`help audio start`, or `help midi input close` for syntax, defaults, and examples.
Piped input/output remains plain text. The launchers synchronize editable package
installation using `.vcpi-requirements.txt` and `.vcpi-pyproject.toml` snapshots
inside the virtual environment, plus a check that imports resolve to this checkout.
For direct Python startup, install with `.venv/bin/python -m pip install -e .`.

To route a hardware MIDI input to a bundled patch (requires plugdata):

```text
track 1 pd sine
midi ports input
midi input <index>
midi link <ch> 1
audio devices
audio start
tempo 120
status
show
```

Replace `<index>` with a port index and `<ch>` with the hardware's MIDI channel
(1–16). `midi link` changes internal routing only; `midi input` opens the port.

Optional labels consistently use `--name`, including plugdata, Cardinal, and deck
tracks. For example: `track 1 pd sine --name "Sine Lead"`.
`gain 1` reads the track gain; `gain 1 0.7` sets it. Use `mute 1 on`,
`mute 1 off`, or `mute 1 toggle` for explicit state control; `solo` works the
same way. Bare `mute 1` and `solo 1` remain toggle shortcuts.
Commands reject unexpected arguments rather than silently ignoring them.
Direct VST instrument tracks are not supported; use `vcv`, `pd`, or `deck`.
VST3 effects remain available through `track <n|master> fx`. MIDI Mix control and LED ports are optional; see
[Controller Setup](#controller-setup).

Read or change instrument, track-effect, and master-effect parameters:

```text
get <track> [name | <listing flag>]
get <track> fx <index> [name | <listing flag>]
get master [index] [name | <listing flag>]
set <track> <name> <value>
set <track> fx <index> <name> <value>
set master <index> <name> <value>
```

Tracks are 1–16 and effect indexes are 1-based. Omit `name` to list musical
controls, or supply one exact name to inspect any cached parameter, including
MIDI and unused slots. Instead of a name, use one listing flag: `--controls`
(the default), `--settings`, `--midi`, `--unused`, or `--all`. Flags cannot be
combined with each other or with a name. For `get master`,
the effect index defaults to 1; a numeric token immediately after `master`
is always an index. `set master` requires an explicit index. Track effects
require the `fx <index>` tokens. Use `help get` and `help set` for detailed
syntax and examples.

Plain-text `get` output prints `  name = value`, including units when available,
with `  (range low .. high)` or `  (choices choice1, choice2)` on the same line.
An empty parameter list prints `  No parameters`; an unknown name is an error.
`--all` separates Controls, Plugin settings, MIDI parameters, and Unused
parameters, with section headings in both plain text and the interactive table.
JUCE MIDI helpers and Cardinal's MIDI CC bank belong to `midi`; plugdata's
disabled slots belong to `unused`. Known plugdata/Cardinal host and window
settings belong to `settings`. Ordinary controls remain visible even when zero
or non-automatable; Cardinal's 24 CV parameters remain controls.
The listing flags affect listing only: raw parameter snapshots, native plugin
state chunks, and MIDI input are unchanged. Explicit MIDI parameters can still
be set; unused slots are rejected before mutation.
`get` and `show` read cached metadata rather than calling live plugin getters;
plugin-side changes can remain stale until a full capture. `set` waits for
the owning process and reports the actual applied value, including clamping.
See [USAGE.md](USAGE.md#plugin-commands) for capture details and parameter examples.

`show <track>` summarizes the chain, routing, mixer state and relevant controller
bindings, plus compact deck title, transport, detected BPM, requested/active sync
and errors. Add `--details` for instrument metadata, paths and worker diagnostics,
including full deck diagnostics and indexed playlist paths.
Use `show <track> fx <index> --details` or `show master fx <index> --details`
for full effect metadata; both require an explicit 1-based effect index.

### Finding MIDI port indexes

Use these commands to discover indexes used by `midi input`,
`midimix input`, and `midimix output`:

```text
vcpi> midi ports input
  [0] Arturia BeatStep Pro MIDI 1
  [1] Novation 25 LE
  [2] MIDI Mix MIDI 1

vcpi> midi ports output
  [0] MIDI Mix MIDI 1
```

Use the number in brackets:

```text
vcpi> midi input 0
vcpi> midi input 1
vcpi> midimix input 2
vcpi> midimix output 0
```

You can open any number of MIDI inputs. Use `midi input close <index>` to
close one.

Port indexes can change after reboot/replug, so always re-check with
`midi ports input` and `midi ports output`.

## Controller Setup

### MIDI inputs (any keyboard, sequencer, etc.)

Generic MIDI inputs share a channel map. Open a keyboard with
`midi input <port_index>` and route its MIDI channels to tracks:

```text
vcpi> midi ports input
vcpi> midi input 1       # e.g. keyboard
vcpi> midi link 5 1      # keyboard ch 5 -> track 1
```

- Generic note inputs use the same commands; MIDI Mix and BeatStep Pro have dedicated integrations.
- Routing is channel-based: `midi link <ch> <track>` maps a MIDI channel to a track.
- Each device sends on its own channel(s); configure channels on the hardware.
- Use `midi input close <index>` to close an open input.

### Arturia BeatStep Pro

The dedicated integration automatically identifies one unambiguous BeatStep Pro
input after startup and reconnects it after replug. Use `beatstep input [port]`
to select a port explicitly; `beatstep close` disables automatic reconnection.
Do not open the same port as a generic MIDI input.

- SEQUENCER 1 -> track 3; SEQUENCER 2 -> track 4; DRUM -> track 5.
- CONTROL buttons 1–8 operate the deck on track 1; buttons 9–16 operate track 2.
- Rotaries 1–4 and 9–12 target track 1; 5–8 and 13–16 target track 2.
- Mappings live in `controllers/beatstep_pro.py`; vcpi never rewrites hardware
  templates or requires per-session MIDI learn.

The explicit application profile expects CONTROL channel 16, sequence channels
1/2/10, Gate button CCs 36–51 and absolute rotary CCs 20–35. **These are not
verified Arturia factory CC assignments.** Adapt the code profile to the
controller's existing template once; CONTROL and sequencer channels must be
distinct. See [BeatStep Pro setup](USAGE.md#beatstep-pro-controller) for details.

### Shared clock and transport

`clock` shows timing authority, BPM and position confidence. Select it with
`clock source auto|internal|link|beatstep`. Auto prefers stable received BeatStep
clock, then Link peers, then internal timing; USB presence alone never restarts
playback. Clock loss continues internally without resetting position.

Built-in sequences use a shared four-beat bar grid and launch at the next bar.
`transport start` resets to beat one; `transport stop` pauses sequences;
`transport continue` resumes; `transport align` declares the current downbeat.
Decks keep independent play/pause and a continuously advancing beat-phase clock.
Deck beat detection still does not identify the recording's musical downbeat.


### Akai MIDI Mix (hardware mixer)

- MIDI Mix uses its own dedicated input port (separate from note routing).
- Connect control input with `midimix input <port_index>` (from `midi ports input`).
- Optional LED feedback is via MIDI output with `midimix output <port_index>` (from `midi ports output`).
- Factory mapping is used by default:
  - Channel faders 1-8 -> track gain
  - Master fader -> master gain
  - 3 knobs per strip -> first 3 musical controls of that track's instrument plugin,
    skipping plugin settings, MIDI helpers, unused slots, and plugdata's global volume
  - MUTE buttons -> mute toggle
  - REC ARM buttons -> solo toggle

Strip-to-track mapping is fixed: strip 1 controls track 1, ... strip 8 controls track 8.
There is no banking: the eight physical strips always target tracks 1–8.
Tracks 9–16 are available through CLI commands and routed generic MIDI inputs
(for example, `midi link 16 16` routes MIDI channel 16 to track 16).

MIDI Mix quick mapping (factory defaults):

```text
Faders (tracks 1-8):   CC 19,23,27,31,49,53,57,61
Master fader:         CC 62
Knob 1 (per strip):   CC 16,20,24,28,46,50,54,58
Knob 2 (per strip):   CC 17,21,25,29,47,51,55,59
Knob 3 (per strip):   CC 18,22,26,30,48,52,56,60
MUTE buttons:         Notes 1,4,7,10,13,16,19,22
SOLO (REC ARM):       Notes 3,6,9,12,15,18,21,24
```

## Example Commands

Cardinal + VCV patch quick load:

```text
# place patch files under ./patches, e.g. ./patches/ambient.vcv
vcpi> track 1 vcv ambient
vcpi> midi link 1 1
```

`track <n> vcv` loads a fresh Cardinal instance into the track you specify.
Routing remains explicit via `midi link <ch> <track>`.

Pure Data patches run through the plugdata VST3 instrument, without opening an
editor. Install `plugdata.vst3` from
[plugdata v0.9.3-2](https://github.com/plugdata-team/plugdata/releases/tag/v0.9.3-2)
for your OS/architecture into a standard VST3 directory (such as `~/.vst3` or
`/usr/local/lib/vst3` on Linux), or set `PLUGDATA_VST3_PATH` to the bundle's full
path. On macOS, use `~/Library/Audio/Plug-Ins/VST3` or
`/Library/Audio/Plug-Ins/VST3`.

```text
vcpi> track 1 pd sine
vcpi> midi link 1 1
vcpi> audio start
vcpi> note 1 60 --velocity 100 --duration-ms 500
```

`track <n> pd <patch_name[.pd]> [--name <label>]` shares the `patches/` directory
and `VCPI_PATCHES_DIR` setting with Cardinal. Names can contain spaces and nested
paths; Tab completion quotes them as needed. Import supports plugdata's 0.9.3
state layout and rejects other layouts. Sessions embed the main patch, but its
relative abstractions, samples, and custom external dependencies must remain
available. See [plugdata usage](USAGE.md#host-startup-flags) for installation,
limits, and patch conventions.

Install VST3 effects for your OS and CPU architecture in a standard user/system
directory: `~/.vst3`, `/usr/lib/vst3`, or `/usr/local/lib/vst3` on Linux;
`~/Library/Audio/Plug-Ins/VST3` or `/Library/Audio/Plug-Ins/VST3` on macOS.
For other locations, set `VST3_PATH` or `VST_PATH` to a colon-separated list of
plugin directories before starting the daemon. The former repository `vst3/`
subdirectory is no longer searched automatically.

Load an installed effect by name on a populated track or on the master bus:

```text
vcpi> track 1 fx DragonflyRoomReverb --name Reverb
vcpi> track master fx DragonflyRoomReverb
```

Full paths also work:

```text
vcpi> track 1 fx /path/to/Effect.vst3 --name Reverb
```

For continuous deck playback:

```text
vcpi> track 1 deck "/music/Live Set" --name Deck-A
vcpi> track 2 deck /music/other.mp3 --name Deck-B
vcpi> tempo 120
vcpi> audio start
vcpi> set 1 play true
vcpi> set 2 play true
vcpi> show 2
```

Both decks initially load paused. Select playlist entries with
`set 1 index 2` (1-based playlist index); switching is asynchronous and restores each song's
remembered position, play/pause and calibration without replacing the worker or
inserts. Repeating `track 1 deck <path>` replaces the list and resets that memory.
Inspect `show 1` for compact operational info,
including errors. Detected BPM is the song's analyzed tempo, distinct from the
shared/global tempo; pending or missing analysis reads analyzing/unavailable,
not 0. Use `show 1 --details` for requested versus applied selection, confidence,
grid offsets, full diagnostics and indexed playlist paths. `get 1` remains the
list of deck controls; `get 1 seek` reads position and `set 1 seek <seconds>`
moves it. `track 1 deck seek <seconds>` is an equivalent convenience command.
Automatic Queen Mary analysis fits stable 60–200 BPM regions across the whole
track and persists results; uncertain grids remain manual rather than guessing.
Hardware-free Linux rendering and daemon command
coverage do not replace audible Linux/macOS acceptance or intended-Pi
read-ahead/deadline measurements before live use.

Internal sequencer (bar-aligned with Ableton Link when enabled):

```text
vcpi> seq 1 d c b a          # define 4-note pattern in bank 1
vcpi> seq 2 c                # single note in bank 2
vcpi> seq link 1 5           # play bank 1 through track 5
vcpi> seq link 2 3           # play bank 2 through track 3
vcpi> seq cut 5              # stop sequence on track 5
vcpi> seq                    # show all banks
```

Typical multi-instrument setup:

```text
vcpi> track 1 pd classic-subtractive-synth --name Lead
vcpi> track 2 pd sine --name Bass
vcpi> track 3 pd poly-pad --name Pad

vcpi> midi link 1 1
vcpi> midi link 2 2
vcpi> midi link 3 3

vcpi> midi input 0
vcpi> midi input 1
vcpi> midimix input 2
vcpi> track 1 fx DragonflyRoomReverb --name Reverb
vcpi> audio start
vcpi> ableton link 120
vcpi> status

vcpi> show                         # compact routing overview; stays at the prompt
vcpi> show controls                # controller connections and configured profiles
vcpi> show controls midimix        # mapped controls and cached values
vcpi> show controls beatstep --details # full expected profile, ranges and input modes
vcpi> show 1                       # chain, routing, mixer and relevant controls
vcpi> show 1 --details             # full cached instrument metadata and diagnostics
vcpi> show 1 fx 1 --details        # full metadata for track 1's first effect
vcpi> show master fx 1 --details   # full metadata for the first master effect
vcpi> get 1             # instrument controls
vcpi> get 1 --all       # controls, settings, MIDI, and unused slots, grouped
vcpi> get 1 fx 1        # controls for track 1's first effect
vcpi> get master 1     # controls for the first master effect
```

Headless server + local CLI (or run both commands on the host over SSH):

```bash
# Terminal 1
./vcpid

# Terminal 2
./vcpi
```

From `vcpi`, `quit` or `exit` disconnects without stopping audio. Use `shutdown`
to save the session and terminate the daemon. Whether a service restarts it
depends on that service's restart policy.

## Raspberry Pi Builds

All Raspberry Pi image build files are in `rpi-build/`.

- Full instructions: `rpi-build/README.md`
- Build script: `rpi-build/prepare-image.sh`
- Internal target provisioner: `rpi-build/internal/provision-target.sh` (not a user entry point)
- Systemd units: `rpi-build/services/`

Run image build from repo root:

```bash
sudo ./rpi-build/prepare-image.sh <raspios-image-url>
```

Or from inside `rpi-build/`:

```bash
cd rpi-build
sudo ./prepare-image.sh <raspios-image-url>
```

The build host installs OS/Python dependencies, Cardinal, pinned plugdata v0.9.3-2,
and the ARM64 native extension into the image before flashing. The Pi needs no Internet access on
first boot; SSH and Ableton Link work over the local network. Build on Linux with
root/loop-mount access and, on x86_64, ARM64 QEMU/binfmt support. See the Pi guide
for prerequisites and verification limits.

## License

vcpi is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0),
including the native JUCE 8 audio engine. See [LICENSE](LICENSE) for the full text.

The native engine uses JUCE 8.0.6 under AGPL-3.0, pybind11 under BSD-3-Clause,
PortAudio under its permissive license, and [Queen Mary DSP](https://github.com/c4dm/qm-dsp)
under GPL-2.0-or-later (pinned to `e34a3cc188332ed7c33cd9257ef164de5b587191`).
Queen Mary DSP's original notices and its bundled KissFFT license are retained in
the fetched sources; `aalink` retains its GPL-3.0 license. Separately installed
VST3 plugins retain their respective licenses. Both the AGPLv3 project license
and the GPLv3 text for dependencies are retained in [LICENSE](LICENSE).

If you modify vcpi and let users interact with it remotely over a network,
AGPLv3 requires offering those users the corresponding source for the version
you run, including your modifications. An upstream source link alone does not
cover unpublished local changes.
