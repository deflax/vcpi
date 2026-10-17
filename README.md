# vcpi

vcpi is a VST3 host with a native C++ audio engine, Python control plane, and
Ableton Link tempo sync, designed for hardware-centric live setups (for example
Arturia BeatStep Pro + Akai MIDI Mix).

## Setup

Linux and macOS are supported. Install the native build prerequisites before
starting the daemon. The first build requires network access to download pinned
JUCE, pybind11, and PortAudio sources, plus Git, CMake 3.22+, a C++17 compiler,
and Python development headers.

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

### Quick start (from repo root)

```bash
# Terminal 1: prepare the environment, build native audio, and start the daemon
./vcpid

# Terminal 2: connect the interactive client
./vcpi
```

Both launchers create `.venv` if needed and install Python dependencies on first
launch or when `requirements.txt` changes. `./vcpid` also builds the native
extension incrementally before starting the daemon.

Audio does **not** start automatically. In the client, try a bundled sample:

```text
slot 1 wav piano c4-soft
audio devices
audio start
note 1 60 100 500
```

Use `audio start <device>` to select an output from `audio devices`.
Use `quit` to disconnect the client; `shutdown` stops the daemon.
Only one daemon per user can run at a time.

### Manual setup

Direct Python startup does not install dependencies or build the extension:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python build_native.py

# Terminal 1
.venv/bin/python main.py serve

# Terminal 2
.venv/bin/python main.py cli
```

Build on each target machine using its own virtual environment. Linux uses
PortAudio's ALSA backend; macOS uses CoreAudio. VST3 bundles must match the target
OS and CPU architecture. Do not copy `.venv`, `build/`, or compiled extensions
between platforms. Set `CMAKE_BUILD_PARALLEL_LEVEL=1` on memory-constrained systems.

## Runtime Model

The daemon runs a Python control process, a spawned audio-output process, and a
persistent process for each loaded slot; the CLI is a separate client connected
through a local Unix socket. To use it from another machine, SSH into the host
and run `./vcpi` there.

Command parsing, session files, MIDI ports, sequencing, and Ableton Link stay in
the control process. Each slot owns its VST3/Cardinal or WAV source and insert
effects on a native render thread. The native PortAudio callback mixes slot
audio and runs master effects. Neither the callback nor slot DSP enters Python
or acquires the GIL. Shared-memory slot transport adds one block of buffering.
Loading or replacing a slot does not stop unrelated slots; read-only plugin
commands use cached snapshots.

Sessions save native VST3 state chunks for instruments, slot inserts, and master
effects; WAV slots remain path-based. See [session commands](USAGE.md#session-commands)
for save/restore behavior and legacy session compatibility.

## Repository Layout

```text
.
  core/               # Main package
    host.py           # Control-process orchestration
    audio_process.py  # Output owner, control IPC, staged slot publication
    slot_process.py   # Persistent slot owners and shared-memory audio/MIDI
    plugin_host.py    # Native lifecycle helpers for output and slot children
    audio_state.py    # Plain plugin/parameter snapshots for control and display
    audio_deps.py     # Native extension availability for audio owners
    engine.py         # Control-only facade for native audio
    cli.py            # Interactive command-line interface
    server.py         # Unix socket server for headless mode
    client.py         # CLI client (connects to server)
    link.py           # Ableton Link wrapper
    sequencer.py      # Internal step sequencer
    session.py        # Session save/restore
  native/             # C++ VST3 hosting, sampler, transport, mixer and callback
  build_native.py     # Per-platform/Python-ABI native build entrypoint
  sampler/            # WAV sample assets
    samples/          # Built-in WAV sample packs (808, 909, piano, etc.)
  controllers/        # Hardware controller modules (generic MIDI input, MIDI Mix)
  graph/              # ASCII visualization renderers (signal flow, plugin info)
  vst3/               # Open-license VST3 plugins (run arch-specific fetch scripts)
  patches/            # VCV/Cardinal .vcv patch files
  sessions/           # User-saved session files (save/load commands)
  main.py             # Top-level Python entry point
  vcpid               # Server launcher (venv setup and incremental native build)
  vcpi                # Client launcher (creates .venv on first run)
  requirements.txt    # Python dependencies
  USAGE.md            # Full CLI and startup flag reference
  rpi-build/          # Raspberry Pi image build/provisioning tools only
```

## Common Commands

```bash
# Start headless daemon with Unix socket API
./vcpid

# Connect CLI client to a running daemon
./vcpi

# Direct python equivalents
python main.py serve
python main.py cli

# Logging defaults:
# - python main.py serve -> INFO
# - ./vcpid -> INFO
LOG_LEVEL=INFO ./vcpid
```

Toggle logging without restarting from the connected client:
`debug on` enables DEBUG, `debug off` returns to INFO, and `debug` shows the
current level. The setting applies to the daemon and audio workers until the
daemon exits; logs appear in the daemon terminal or service journal.
Raw native/plugin stdout and stderr are discarded unless DEBUG is enabled, then
shown with `[daemon]`, `[audio]`, or `[slot N]` prefixes. vcpi creates no
per-process log files and cannot replay discarded output. Structured vcpi
activity messages, warnings, and errors remain visible. For startup diagnostics, use
`LOG_LEVEL=DEBUG ./vcpid`; this also streams build output with `[build]` prefixes.
Successful builds are otherwise quiet; failures show the final 64 KiB of
diagnostics.

Full CLI and startup flag reference: [USAGE.md](USAGE.md).

Inside `./vcpi`, press `Tab` for command and argument suggestions. Completion
comes from the daemon: audio/MIDI subcommands, routing channels and slots,
sequencer banks, mixer targets, help topics, and debug modes are supported.
`get`/`set` suggest cached plugin parameters and boolean/enum choices; effect
indexes reflect loaded chains. Plugin, patch, sample, and session names are
discovered on the daemon, including names containing spaces where the command
supports them. Free-form numeric values are left for you to enter.

The client keeps a scrolling `vcpi>` prompt with session history and uses
`prompt_toolkit` + Rich for enhanced terminal output. `help`, `get`, `status`, and
`info` display tables. `flow` adds routing, mapped-control, and audio-output tables;
`flow explore` opens a read-only full-screen explorer with slot/master drill-downs,
scrolling, and manual refresh. `get` includes parameter values, units, ranges,
and choices without slider bars.
The `help` overview combines its categories and descriptions into one compact
table; use `help <command>` for detailed syntax.
Piped input/output remains plain text. `./vcpi` and `./vcpid` install dependencies
on first launch and when `requirements.txt` changes, including for existing
environments. For direct Python startup, install them manually with
`.venv/bin/pip install -r requirements.txt`.

To route a hardware MIDI input to a bundled sample:

```text
slot 1 wav piano c4-soft
midi ports input
midi input <index>
midi link <ch> 1
audio devices
audio start
tempo 120
status
flow
```

Replace `<index>` with a port index and `<ch>` with the hardware's MIDI channel
(1–16). `midi link` changes internal routing only; `midi input` opens the port.
For VST instruments, fetch or install a compatible plugin first, then use
`slot 1 vst <plugin-name>`. MIDI Mix control and LED ports are optional; see
[Controller Setup](#controller-setup).

Read or change instrument, slot-effect, and master-effect parameters:

```text
get <slot> [name]
get <slot> fx <index> [name]
get master [index] [name]
set <slot> <name> <value>
set <slot> fx <index> <name> <value>
set master <index> <name> <value>
```

Slots are 1–8 and effect indexes are 1-based. Omit `name` to read all visible
parameters or supply one name to read just that parameter. For `get master`,
the effect index defaults to 1; a numeric token immediately after `master`
is always an index. `set master` requires an explicit index. Slot effects
require the `fx <index>` tokens. Use `help get` and `help set` for detailed
syntax and examples.

Plain-text `get` output prints `  name = value`, including units when available,
with `  (range low .. high)` or `  (choices choice1, choice2)` on the same line.
An empty parameter list prints `  No parameters`; an unknown name is an error.
JUCE-generated `MIDI CC <channel>|<controller>` helpers are hidden, while
ordinary controls, including non-automatable ones, remain visible.
`get` and `info` read cached metadata rather than calling live plugin getters;
plugin-side changes can remain stale until a full capture. `set` waits for
the owning process and reports the actual applied value, including clamping.
See [USAGE.md](USAGE.md#plugin-commands) for capture details and parameter examples.

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

All MIDI input devices are handled identically. Open any device with
`midi input <port_index>` and route its MIDI channels to slots:

```text
vcpi> midi ports input
vcpi> midi input 0       # e.g. BeatStep Pro
vcpi> midi input 1       # e.g. keyboard
vcpi> midi link 1 1      # BSP ch 1 -> slot 1
vcpi> midi link 2 2      # BSP ch 2 -> slot 2
vcpi> midi link 10 3     # BSP ch 10 -> slot 3
vcpi> midi link 5 1      # keyboard ch 5 -> slot 1
```

- Generic note inputs use the same commands; MIDI Mix has a separate control input.
- Routing is channel-based: `midi link <ch> <slot>` maps a MIDI channel to a slot.
- Each device sends on its own channel(s); configure channels on the hardware.
- Use `midi input close <index>` to close an open input.

### Akai MIDI Mix (hardware mixer)

- MIDI Mix uses its own dedicated input port (separate from note routing).
- Connect control input with `midimix input <port_index>` (from `midi ports input`).
- Optional LED feedback is via MIDI output with `midimix output <port_index>` (from `midi ports output`).
- Factory mapping is used by default:
  - Channel faders 1-8 -> slot gain
  - Master fader -> master gain
  - 3 knobs per strip -> first 3 parameters of that slot's instrument plugin
  - MUTE buttons -> mute toggle
  - REC ARM buttons -> solo toggle

Strip-to-slot mapping is fixed: strip 1 controls slot 1, ... strip 8 controls slot 8.

MIDI Mix quick mapping (factory defaults):

```text
Faders (slots 1-8):   CC 19,23,27,31,49,53,57,61
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
vcpi> slot 1 vcv ambient
vcpi> midi link 1 1
```

`slot <n> vcv` loads a fresh Cardinal instance into the slot you specify.
Routing remains explicit via `midi link <ch> <slot>`.

Fetch bundled open-license VST3 plugins and load one:

```bash
./vst3/fetch-vsts-amd64    # desktop/laptop Linux amd64
# or
./vst3/fetch-vsts-aarch64  # Raspberry Pi / Linux aarch64
```

The Linux x86_64 fetcher includes Dexed, Surge XT, Odin 2, OB-Xf, Geonkick,
JC-303, Firefly Synth 2 (+FX), and Dragonfly Reverb. The Linux aarch64 fetcher
includes Surge XT, OB-Xf, and Dragonfly Reverb. These scripts do not install
macOS plugins; install macOS-compatible VST3 bundles separately.
See the [plugin catalog](vst3/README.md#plugin-catalog) for architecture-specific details.

```text
vcpi> slot 1 vst Dexed         # Tab completes installed VST names
vcpi> audio start
vcpi> note 1 60 100 500
```

Full paths also work:

```text
vcpi> slot 1 vst /path/to/Synth.vst3 --name Lead
```

Load a WAV sample into a slot and trigger it from MIDI routing:

```text
vcpi> slot 2 wav 909 bassdrum
vcpi> midi link 10 2
vcpi> midi input 0
```

`slot <n> wav` resolves to `sampler/samples/<pack>/<sample>.wav` (for example,
`sampler/samples/909/bassdrum.wav`) and plays one-shot sample voices on note-on.
You can include or omit the `.wav` extension in `<sample>`.

Built-in packs include drums (`808`, `909`) plus melodic/synth packs:

- `piano`
- `organ`
- `strings`
- `synth-pads`
- `synth-leads`

Examples:

```text
vcpi> slot 1 wav piano c4-soft
vcpi> slot 2 wav organ c4-drawbar
vcpi> slot 3 wav strings c4-ensemble
vcpi> slot 4 wav synth-pads c4-warm
vcpi> slot 5 wav synth-leads c4-mono-saw
```

Internal sequencer (bar-aligned with Ableton Link when enabled):

```text
vcpi> seq 1 d c b a          # define 4-note pattern in bank 1
vcpi> seq 2 c                # single note in bank 2
vcpi> seq link 1 5           # play bank 1 through slot 5
vcpi> seq link 2 3           # play bank 2 through slot 3
vcpi> seq cut 5              # stop sequence on slot 5
vcpi> seq                    # show all banks
```

Typical multi-instrument setup:

```text
vcpi> slot 1 vst Dexed --name Lead
vcpi> slot 2 vst OB-Xf --name Bass
vcpi> slot 3 vst Geonkick --name Drums

vcpi> midi link 1 1
vcpi> midi link 2 2
vcpi> midi link 10 3

vcpi> midi input 0
vcpi> midi input 1
vcpi> midimix input 2
vcpi> slot 1 fx DragonflyRoomReverb --name Reverb
vcpi> audio start
vcpi> ableton link 120
vcpi> status

vcpi> flow              # signal flow: all slots, FX chains, master bus
vcpi> flow explore      # navigate MIDI, controls, audio, slots, and master
vcpi> flow controls     # exact MIDI Mix bindings and cached parameter targets
vcpi> info 1            # plugin metadata: vendor, category, version, etc.
vcpi> info 1 fx 1       # info for slot 1's first effect
vcpi> get 1             # all visible instrument parameters
vcpi> get 1 fx 1        # parameters for slot 1's first effect
vcpi> get master 1     # parameters for the first master effect
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

The build host installs OS/Python dependencies, Cardinal and the ARM64 native
extension into the image before flashing. The Pi needs no Internet access on
first boot; SSH and Ableton Link work over the local network. Build on Linux with
root/loop-mount access and, on x86_64, ARM64 QEMU/binfmt support. See the Pi guide
for prerequisites and verification limits.

## License

vcpi is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0),
including the native JUCE 8 audio engine. See [LICENSE](LICENSE) for the full text.

The native engine uses JUCE 8.0.6 under AGPL-3.0, pybind11 under BSD-3-Clause,
and PortAudio under its permissive license. Third-party licenses and copyright
notices remain unchanged; `aalink` and the bundled VST3 plugins retain their
GPL-3.0 licenses. GPLv3 text is retained in [LICENSE.GPL-3.0](LICENSE.GPL-3.0).

If you modify vcpi and let users interact with it remotely over a network,
AGPLv3 requires offering those users the corresponding source for the version
you run, including your modifications. An upstream source link alone does not
cover unpublished local changes.
