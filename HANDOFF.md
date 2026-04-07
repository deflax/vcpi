# Session Handoff

Updated: 2026-04-07 UTC

This file is for transient session state. Durable repo facts live in `AGENTS.md`.

## Current state

- Exploration-only session; no feature or bugfix implementation was underway.
- The main durable artifact created from exploration is `AGENTS.md`.
- Current repo knowledge was distilled from `README.md`, `USAGE.md`, `requirements.txt`, `vst3/README.md`, and `rpi-build/README.md`.

## Key findings

- `vcpi` is a Python VST3 host with Ableton Link tempo sync for hardware-centric live setups.
- Normal runtime is split daemon/client: `./vcsrv` or `python main.py serve`, then `./vcli` or `python main.py cli`.
- Main code areas are `core/`, `controllers/`, `graph/`, `sampler/`, `vst3/`, and `rpi-build/`.
- Most useful code entrypoints are `core/host.py`, `core/engine.py`, `core/cli.py`, `core/server.py`, `core/client.py`, `core/link.py`, and `core/session.py`.

## Tooling findings

- Runtime dependencies from `requirements.txt`: `pedalboard`, `aalink`, `python-rtmidi`, `mido`, `numpy`, `sounddevice`.
- No repo-local automated tests, CI workflows, lint/format config, Docker setup, or Python packaging config (`pyproject.toml`, `setup.py`) were found during exploration.

## Suggested startup for a new session

1. Read `AGENTS.md` first for the durable repo map.
2. Read this file for transient context and most recent findings.
3. Read the specific module you plan to change.
4. Use `README.md` and `USAGE.md` as command/reference docs.

## Working tree note

- This session added `AGENTS.md` and `HANDOFF.md` only.
