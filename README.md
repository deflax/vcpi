# LinkVST

LinkVST is a Python VST3 host with Ableton Link tempo sync, designed for
hardware-centric live setups (for example Arturia BeatStep Pro + Akai MIDI Mix).

This repository is now structured with LinkVST as the main project at the repo
root. Raspberry Pi image tooling lives under `rpi-build/`.

## Project Split

- Main development/runtime project is at repo root (`linkvst/`, `vst_host.py`).
- Raspberry Pi image provisioning is isolated under `rpi-build/`.
- Local startup is optimized for repo-root use via `./start.sh`.

## Quick Start (from repo root)

Fastest path:

```bash
./start.sh
```

Manual setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start local interactive mode
python vst_host.py
# or (script is executable)
./vst_host.py
# or
python -m linkvst
```

If you are on Linux, you may also need:

```bash
sudo apt install libasound2-dev libjack-dev libportaudio2
```

## Repository Layout

```text
.
  linkvst/            # Main package
  docs/               # Extended documentation (CLI reference, etc.)
  start.sh            # One-command local launcher (creates .venv on first run)
  vst_host.py         # Thin entry point
  requirements.txt    # Python dependencies
  rpi-build/          # Raspberry Pi image build/provisioning tools only
```

## Common Commands

```bash
# One-command launcher (bootstraps .venv on first run)
./start.sh

# Local interactive host
python vst_host.py
./vst_host.py

# Headless daemon with Unix socket API
python vst_host.py serve --sock /run/linkvst/linkvst.sock

# Connect CLI client to a running daemon
python -m linkvst cli --sock /run/linkvst/linkvst.sock
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
- Route whichever channels your BSP sends on into LinkVST slots with `route <ch> <slot>`.
- There are no hard-coded channel assumptions in LinkVST.

```text
linkvst> midi_ports
linkvst> midi_seq 0
linkvst> route 1 1
linkvst> route 2 2
linkvst> route 10 3
linkvst> routing
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
linkvst> load 1 /path/to/Synth.vst3 Lead
linkvst> audio_start
linkvst> note 1 60 100 500
```

Typical multi-instrument setup with BSP + MIDI Mix:

```text
linkvst> load 1 /path/to/Lead.vst3 Lead
linkvst> load 2 /path/to/Bass.vst3 Bass
linkvst> load 3 /path/to/Drums.vst3 Drums

linkvst> route 1 1
linkvst> route 2 2
linkvst> route 10 3

linkvst> midi_seq 0
linkvst> midi_mix 1
linkvst> load_fx /path/to/Delay.vst3 1 Delay
linkvst> load_fx /path/to/Reverb.vst3 master Reverb
linkvst> audio_start
linkvst> link 120
linkvst> status
```

Headless server + remote CLI:

```bash
./start.sh serve --sock /run/linkvst/linkvst.sock
python -m linkvst cli --sock /run/linkvst/linkvst.sock
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

LinkVST relies on libraries with different licenses, including GPL components
(`pedalboard`, `aalink`). If you distribute bundled binaries, review dependency
licenses carefully.
