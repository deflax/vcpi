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

In server mode, vcpi automatically attempts to start audio on boot. If audio
initialization fails, the server continues running and logs the failure.

If you are on Linux, you may also need:

```bash
sudo apt install libasound2-dev libjack-dev libportaudio2
```

## Repository Layout

```text
.
  core/               # Main package
    host.py           # vcpi core orchestration
  controllers/        # Hardware controller modules (BSP, MIDI Mix)
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

Inside the CLI, a typical first run is:

```text
deps
midi_ports
devices
midi_seq <index>
midi_mix <index>
audio_start
link 120
status
```

## Controller Setup

### Arturia BeatStep Pro (sequencer)

- BeatStep Pro is the note/sequence source; its MIDI channels are fully configurable.
- Route whichever channels your BSP sends on into vcpi slots with `route <ch> <slot>`.
- There are no hard-coded channel assumptions in vcpi.

```text
vcpi> midi_ports
vcpi> midi_seq 0
vcpi> route 1 1
vcpi> route 2 2
vcpi> route 10 3
vcpi> routing
```

### Akai MIDI Mix (hardware mixer)

- MIDI Mix uses its own dedicated input port (separate from BeatStep Pro).
- Connect it with `midi_mix <port_index>`.
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

Load a synth and test note:

```text
vcpi> load 1 /path/to/Synth.vst3 Lead
vcpi> audio_start
vcpi> note 1 60 100 500
```

Typical multi-instrument setup with BSP + MIDI Mix:

```text
vcpi> load 1 /path/to/Lead.vst3 Lead
vcpi> load 2 /path/to/Bass.vst3 Bass
vcpi> load 3 /path/to/Drums.vst3 Drums

vcpi> route 1 1
vcpi> route 2 2
vcpi> route 10 3

vcpi> midi_seq 0
vcpi> midi_mix 1
vcpi> load_fx /path/to/Delay.vst3 1 Delay
vcpi> load_fx /path/to/Reverb.vst3 master Reverb
vcpi> audio_start
vcpi> link 120
vcpi> status
```

Headless server + remote CLI:

```bash
# Terminal 1
./vcsrv

# Terminal 2
./vcli
```

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
