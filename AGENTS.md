# vcpi Agent Guide

Read this file first in new sessions. It is a durable map of the repo, not a full spec.

## Overview

`vcpi` is a Python VST3 host with Ableton Link tempo sync for hardware-centric live setups. The normal workflow is a split daemon/client model: start the server with `./vcsrv` (or `python main.py serve`) and connect with `./vcli` (or `python main.py cli`).

## Source of truth

- `README.md` — top-level repo map, quick start, and typical workflows
- `USAGE.md` — startup flags, CLI command reference, and important runtime conventions
- `requirements.txt` — Python dependency set
- `vst3/README.md` — bundled plugin fetch flow and catalog
- `rpi-build/README.md` — Raspberry Pi image build and first-boot provisioning

## Top-level structure

```text
.
├── core/          Main application package
├── controllers/   MIDI device integrations
├── graph/         ASCII renderers for flow/status/plugin info/knobs
├── sampler/       WAV sampler package and built-in sample packs
├── patches/       Cardinal/VCV `.vcv` patch files
├── sessions/      Saved session files
├── vst3/          Fetch scripts and docs for bundled VST3 plugins
├── rpi-build/     Raspberry Pi image build/provisioning tools
├── main.py        Top-level Python entry point
├── vcsrv          Server launcher
├── vcli           Client launcher
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
| Main orchestration | `core/host.py` |
| Real-time audio engine | `core/engine.py` |
| CLI implementation | `core/cli.py` |
| Server/client split | `core/server.py`, `core/client.py` |
| Ableton Link integration | `core/link.py` |
| Session save/restore | `core/session.py` |
| Generic MIDI input | `controllers/midi_input.py` |
| Akai MIDI Mix mapping | `controllers/akai_midimix.py` |
| WAV sampler | `sampler/plugin.py`, `sampler/wav.py` |
| Signal/status renderers | `graph/` |
| Bundled plugin fetch flow | `vst3/README.md`, `vst3/fetch-vsts-*` |
| Raspberry Pi deployment | `rpi-build/README.md`, `rpi-build/services/` |

## Runtime model

- `serve` starts the headless host and exposes a Unix socket control interface.
- `cli` connects to an already-running server.
- Server mode does **not** auto-start audio; start it manually from the client with `audio start [device]`.
- Only one server instance per user is allowed.
- Default socket path is `$XDG_RUNTIME_DIR/vcpi/vcpi.sock` with fallbacks documented in `USAGE.md`.

## Core repo conventions

- Slots are `1-8`.
- MIDI channels are `1-16`.
- `fx_index` values in CLI commands are `1-based`.
- `master` refers to the global master effects bus.
- `midi link <ch> <slot>` only changes internal routing; it does **not** open hardware ports.
- Use `midi ports input` / `midi ports output` to discover current indexes before opening devices.
- Port indexes can change after reboot or replug.

## Plugin and asset behavior

- `slot <n> vst` and `slot <n> fx` accept either a plugin name or a full path.
- VST3 search includes the repo `vst3/` directory, `~/.vst3`, `/usr/lib/vst3`, `/usr/local/lib/vst3`, and documented macOS paths.
- `slot <n> vcv` loads a Cardinal patch from `patches/` by default and does not auto-route channels.
- `slot <n> wav <pack> <sample>` resolves to `sampler/samples/<pack>/<sample>.wav`.
- Built-in sample packs documented in `USAGE.md` are: `808`, `909`, `piano`, `organ`, `strings`, `synth-pads`, `synth-leads`.

## Dependency stack

From `requirements.txt`:

- `pedalboard`
- `aalink`
- `python-rtmidi`
- `mido`
- `numpy`
- `sounddevice`

## Common commands

```bash
# First-time local setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run server / client
./vcsrv
./vcli

# Direct Python equivalents
python main.py serve
python main.py cli

# Fetch bundled VST3 plugins
./vst3/fetch-vsts-amd64
./vst3/fetch-vsts-aarch64
```

## Raspberry Pi notes

- `rpi-build/` is a deployment subtree, not part of the normal desktop runtime loop.
- The Pi image flow is documented in `rpi-build/README.md`.
- `payload.service` starts `main.py serve` on boot.
- Local Pi credential and Wi-Fi files are meant to stay inside `rpi-build/` and are gitignored.

## Good first reads for a fresh session

1. This file
2. `README.md`
3. `USAGE.md`
4. The specific module you plan to change under `core/`, `controllers/`, `sampler/`, `graph/`, `vst3/`, or `rpi-build/`

## Recent findings / current priorities

This section is intentionally short and may go stale. Prefer the sections above for durable repo facts.

- Exploration completed on `2026-04-07`.
- This is a Python audio/MIDI host repo, not a web or JS app.
- No repo-local automated tests, CI workflows, lint/format config, Docker setup, or Python packaging config (`pyproject.toml`, `setup.py`) were found during exploration.
- The most useful code entrypoints for follow-up exploration are `core/host.py`, `core/engine.py`, `core/cli.py`, `core/server.py`, `core/client.py`, `core/link.py`, and `core/session.py`.
- Separate transient session state lives in `HANDOFF.md` if that file exists.
- No active implementation task was in progress when this note was written.
