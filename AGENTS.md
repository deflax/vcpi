# vcpi Agent Guide

Read this file first in new sessions. It is a durable map of the repo, not a full spec.

## Overview

`vcpi` has a Python control plane and C++ audio engine with Ableton Link tempo sync for hardware-centric live setups. Start the daemon with `./vcpid` (or `python -m vcpi serve`) and connect with `./vcpi` (or `python -m vcpi cli`). Direct Python startup requires an editable package installation.

## Source of truth

- `README.md` — top-level repo map, quick start, and typical workflows
- `USAGE.md` — startup flags, CLI command reference, and important runtime conventions
- `requirements.txt` — Python dependency set
- `pyproject.toml` — Python package metadata and `src` package discovery
- `rpi-build/README.md` — Raspberry Pi image build and first-boot provisioning

## Top-level structure

```text
.
├── src/vcpi/      Python application package
├── src/engine/    C++ audio engine and Python bindings
├── assets/        Bundled samples and Cardinal/Pure Data patches
├── sessions/      Saved session files
├── rpi-build/     Raspberry Pi image build/provisioning tools
├── main.py        Top-level Python entry point
├── pyproject.toml Python package installation metadata
├── build_native.py Native engine build entry point
├── vcpid          Server launcher
├── vcpi           Client launcher
├── README.md      Project overview
└── USAGE.md       Command and flag reference
```

Hidden directories present in the workspace:

- `.git/` — git metadata
- `.venv/` — local Python environment
- `.opencode/` — workspace-local OpenCode tooling

## Where to look

| Need | Start here |
|---|---|
| Startup flow and repo overview | `README.md` |
| CLI flags and command syntax | `USAGE.md` |
| Main orchestration | `src/vcpi/host.py` |
| Real-time audio engine | `src/engine/runtime.cpp`, `src/engine/track_runner.cpp` |
| Python audio control | `src/vcpi/audio/control.py` |
| Audio process ownership and IPC | `src/vcpi/audio/` |
| CLI implementation | `src/vcpi/commands/` |
| Server/client split | `src/vcpi/server.py`, `src/vcpi/client.py` |
| Ableton Link integration | `src/vcpi/link.py` |
| Session save/restore | `src/vcpi/sessions/` |
| Generic MIDI input | `src/vcpi/controllers/midi_input.py` |
| Akai MIDI Mix mapping | `src/vcpi/controllers/akai_midimix.py` |
| WAV sampler | `src/engine/plugin.cpp`, `assets/samples/` |
| Signal/status/terminal rendering | `src/vcpi/presentation/` |
| Plugin installation and discovery | `USAGE.md`, `src/vcpi/commands/assets.py`, `src/vcpi/plugin_host.py` |
| Raspberry Pi deployment | `rpi-build/README.md`, `rpi-build/services/` |

## Runtime model

- `serve` starts the headless host and exposes a Unix socket control interface.
- `cli` connects to an already-running server.
- Server mode automatically reopens a saved audio output; `--output` overrides it and starts that device. Without a saved or explicit output, use `audio start [device]`. `--no-restore` skips saved connections.
- Only one server instance per user is allowed.
- Default socket path is `$XDG_RUNTIME_DIR/vcpi/vcpi.sock` with fallbacks documented in `USAGE.md`.

## Core repo conventions

- Tracks are `1-8`.
- MIDI channels are `1-16`.
- `fx_index` values in CLI commands are `1-based`.
- `master` refers to the global master effects bus.
- `midi link <ch> <track>` only changes internal routing; it does **not** open hardware ports.
- Use `midi ports input` / `midi ports output` to discover current indexes before opening devices.
- Port indexes can change after reboot or replug.

## Plugin and asset behavior

- `track <n> vst` and `track <n> fx` accept either a plugin name or a full path.
- VST3 name search checks `VST3_PATH` and `VST_PATH` directories, then the working directory, repository root, `~/.vst3`, `/usr/lib/vst3`, `/usr/local/lib/vst3`, and documented macOS paths. The former `vst3/` subdirectory is not searched automatically.
- `track <n> vcv` and `track <n> pd` load from `assets/patches/` by default; `VCPI_PATCHES_DIR` overrides that directory. Loading does not auto-route MIDI channels.
- `track <n> wav <pack> <sample>` resolves to `assets/samples/<pack>/<sample>.wav`. Asset discovery checks working-directory assets first, then bundled checkout assets.
- Built-in sample packs documented in `USAGE.md` are: `808`, `909`, `piano`, `organ`, `strings`, `synth-pads`, `synth-leads`.

## Dependency stack

From `requirements.txt`:

- `aalink`
- `python-rtmidi`
- `mido`
- `numpy`
- `prompt_toolkit`
- `rich`
- `psutil`

## Common commands

```bash
# First-time local setup
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python build_native.py

# Run server / client
./vcpid
./vcpi

# Direct Python equivalents
python -m vcpi serve
python -m vcpi cli
```

## Raspberry Pi notes

- `rpi-build/` is a deployment subtree, not part of the normal desktop runtime loop.
- The Pi image flow is documented in `rpi-build/README.md`.
- `payload.service` starts `python -m vcpi serve` on boot.
- Local Pi credential and Wi-Fi files are meant to stay inside `rpi-build/` and are gitignored.

## Good first reads for a fresh session

1. This file
2. `README.md`
3. `USAGE.md`
4. The specific module you plan to change under `src/vcpi/`, `src/engine/`, or `rpi-build/`

## Recent findings / current priorities

This section is intentionally short and may go stale. Prefer the sections above for durable repo facts.

- The application uses an installed `vcpi` namespace; no root-level Python package or `sys.path` adjustment is needed.
- Existing regressions run with `.venv/bin/python -m unittest discover -s tests`.
- The native extension builds into `src/vcpi`; per-platform build caches live under `build/engine/`.
- Separate transient session state lives in `HANDOFF.md` if that file exists.
- No active implementation task was in progress when this note was written.
