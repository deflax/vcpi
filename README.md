# vcpi

vcpi is a Python VST3 host with Ableton Link tempo sync, designed for
hardware-centric live setups (for example Arturia BeatStep Pro + Akai MIDI Mix).

## Quick Start (from repo root)

Fastest path:

```bash
# Terminal 1
./vcsrv

# Terminal 2
./vcli
```

Manual setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1: start server
python main.py serve

# Terminal 2: connect interactive client
python main.py cli
```

vcpi now always runs with separate server (`serve`) and client (`cli`) processes.

In server mode, vcpi does not start audio automatically. Start audio manually
from `vcli` with `audio_start [device]`.

If you are on Linux, you may also need:

```bash
sudo apt install libasound2-dev libjack-dev libportaudio2
```

## Repository Layout

```text
.
  core/               # Main package
    host.py           # vcpi core orchestration
  controllers/        # Hardware controller modules (generic MIDI input, MIDI Mix)
  graph/              # ASCII visualization renderers (route graph, signal flow, plugin info, knobs)
  samples/            # Built-in WAV sample packs (for `load wav`); see samples/README.md
  vst3/               # Open-license VST3 plugins (run arch-specific fetch scripts)
  docs/               # Extended documentation (CLI reference, etc.)
  main.py             # Top-level Python entry point
  vcsrv               # Server launcher (creates .venv on first run)
  vcli                # Client launcher (creates .venv on first run)
  requirements.txt    # Python dependencies
  rpi-build/          # Raspberry Pi image build/provisioning tools only
```

## Common Commands

```bash
# Start headless daemon with Unix socket API
./vcsrv

# Connect CLI client to a running daemon
./vcli

# Direct python equivalents
python main.py serve
python main.py cli

# Logging defaults:
# - python main.py serve -> WARNING
# - ./vcsrv -> DEBUG
LOG_LEVEL=INFO ./vcsrv
```

Full CLI and startup flag reference: `docs/cli-reference.md`

Inside `./vcli`, press `Tab` to autocomplete command names. The `load`
command has context-aware completion: `load vst` and `load fx` complete
detected VST3 plugin names, `load wav` completes pack and sample names,
and `load vcv` completes patch names. You do not need to type full paths.

Inside the CLI, a typical first run is:

```text
deps
midi_ports_in
audio_devices
load vcv <slot> <patch_name>
midi_in <index>
midi_mix <index>
midi_ports_out
midi_mix_out <index>
route <ch> <slot>
mixer
audio_start
link 120
status
info 1
knobs 1
```

### Finding MIDI port indexes

Use these commands to discover indexes used by `midi_in`,
`midi_mix`, and `midi_mix_out`:

```text
vcpi> midi_ports_in
  [0] Arturia BeatStep Pro MIDI 1
  [1] Novation 25 LE
  [2] MIDI Mix MIDI 1

vcpi> midi_ports_out
  [0] MIDI Mix MIDI 1
```

Use the number in brackets:

```text
vcpi> midi_in 0
vcpi> midi_in 1
vcpi> midi_mix 2
vcpi> midi_mix_out 0
```

You can open any number of MIDI inputs. Use `midi_ins` to list them and
`midi_in_close <index>` to close one.

Port indexes can change after reboot/replug, so always re-check with
`midi_ports_in` and `midi_ports_out`.

## Controller Setup

### MIDI inputs (any keyboard, sequencer, etc.)

All MIDI input devices are handled identically. Open any device with
`midi_in <port_index>` and route its MIDI channels to slots:

```text
vcpi> midi_ports_in
vcpi> midi_in 0          # e.g. BeatStep Pro
vcpi> midi_in 1          # e.g. keyboard
vcpi> route 1 1          # BSP ch 1 -> slot 1
vcpi> route 2 2          # BSP ch 2 -> slot 2
vcpi> route 10 3         # BSP ch 10 -> slot 3
vcpi> route 5 1          # keyboard ch 5 -> slot 1
```

- There are no device-specific commands; every MIDI input is a peer.
- Routing is channel-based: `route <ch> <slot>` maps a MIDI channel to a slot.
- Each device sends on its own channel(s); configure channels on the hardware.
- Use `midi_ins` to list open inputs, `midi_in_close <index>` to close one.

### Akai MIDI Mix (hardware mixer)

- MIDI Mix uses its own dedicated input port (separate from note routing).
- Connect control input with `midi_mix <port_index>` (from `midi_ports_in`).
- Optional LED feedback is via MIDI output with `midi_mix_out <port_index>` (from `midi_ports_out`).
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
vcpi> load vcv 1 ambient
vcpi> route 1 1
```

`load vcv` loads a fresh Cardinal instance into the slot you specify.
Routing remains explicit via `route <ch> <slot>`.

Fetch bundled open-license VST3 plugins and load one:

```bash
./vst3/fetch-vsts-amd64    # desktop/laptop Linux amd64
# or
./vst3/fetch-vsts-aarch64  # Raspberry Pi / Linux aarch64
```

Bundled plugins currently include Dexed, Surge XT, Odin 2, OB-Xf, Geonkick,
JC-303, Firefly Synth 2 (+FX), and Dragonfly Reverb.

```text
vcpi> load vst 1 Dexed         # Tab completes installed VST names
vcpi> audio_start
vcpi> note 1 60 100 500
```

Full paths also work:

```text
vcpi> load vst 1 /path/to/Synth.vst3 Lead
```

Load a WAV sample into a slot and trigger it from MIDI routing:

```text
vcpi> load wav 2 909 bassdrum
vcpi> route 10 2
vcpi> midi_in 0
```

`load wav` resolves to `samples/<pack>/<sample>.wav` (for example,
`samples/909/bassdrum.wav`) and plays one-shot sample voices on note-on.
You can include or omit the `.wav` extension in `<sample>`.

Built-in packs include drums (`808`, `909`) plus melodic/synth packs:

- `piano`
- `organ`
- `strings`
- `synth-pads`
- `synth-leads`

Examples:

```text
vcpi> load wav 1 piano c4-soft
vcpi> load wav 2 organ c4-drawbar
vcpi> load wav 3 strings c4-ensemble
vcpi> load wav 4 synth-pads c4-warm
vcpi> load wav 5 synth-leads c4-mono-saw
```

Typical multi-instrument setup:

```text
vcpi> load vst 1 Dexed Lead
vcpi> load vst 2 OB-Xf Bass
vcpi> load vst 3 Geonkick Drums

vcpi> route 1 1
vcpi> route 2 2
vcpi> route 10 3

vcpi> midi_in 0
vcpi> midi_in 1
vcpi> midi_mix 2
vcpi> load fx DragonflyRoomReverb 1 Reverb
vcpi> audio_start
vcpi> link 120
vcpi> status

vcpi> mixer             # signal flow: all slots, FX chains, master bus
vcpi> info 1            # plugin metadata: vendor, category, version, etc.
vcpi> info 1 fx 1       # info for slot 1's first effect
vcpi> knobs 1           # ASCII slider view of all parameters
vcpi> knobs master 1    # knobs for first master effect
```

Headless server + remote CLI:

```bash
# Terminal 1
./vcsrv

# Terminal 2
./vcli
```

From `vcli`, use `shutdown` to terminate the daemon process (for example, to
let systemd restart it).

## Raspberry Pi Builds

All Raspberry Pi image build files are in `rpi-build/`.

- Full instructions: `rpi-build/README.md`
- Build script: `rpi-build/prepare-image.sh`
- First-boot provisioning script: `rpi-build/setup.sh`
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

## License Notes

vcpi relies on libraries with different licenses, including GPL components
(`pedalboard`, `aalink`). If you distribute bundled binaries, review dependency
licenses carefully.
