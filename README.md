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
