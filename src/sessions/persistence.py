"""Session persistence -- save and restore host state to a JSON file.

Saved state includes:
  - Per-track: source kind (plugin/wav/deck/vcv/pd), instrument path, name, gain,
    enabled, muted, solo, insert effect paths/names, and opaque native VST3 state
  - Master effects: paths, names, and opaque native VST3 state
  - Decks: full inline playlist, stable active ID, cue, pitch and sync; no VST chunk
  - Master gain
  - MIDI channel -> track routing
  - Link BPM and enabled state
  - Audio/MIDI device connection targets

The session file is human-readable JSON so it can be hand-edited if needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from vcpi.link import validate_bpm

from . import restore as _restore_phases
from .snapshot import snapshot as _snapshot

if TYPE_CHECKING:
    from vcpi.host import VcpiCore

DEFAULT_SESSION_PATH = Path("~/.config/vcpi/session.json").expanduser()

logger = logging.getLogger(__name__)


def save(host: VcpiCore, path: Optional[Path] = None):
    """Save the current session to a JSON file."""
    path = Path(path) if path else DEFAULT_SESSION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    host.engine.sync_state()
    data = _snapshot(host)
    path.write_text(json.dumps(data, indent=2) + "\n")
    logger.info("[Session] Saved to %s", path)


def restore(host: VcpiCore, path: Optional[Path] = None, *,
            start_audio: bool = True, skip_settings=()):
    """Restore a session from a JSON file.

    Loads instruments, effects, native plugin state, routing, gains, tempo,
    and previously connected audio/MIDI device targets.
    Set start_audio=False to restore setup without opening an audio device.
    skip_settings contains startup option destinations explicitly overridden by
    the caller, so saved connections and Link are not opened transiently.
    """
    path = Path(path) if path else DEFAULT_SESSION_PATH
    if not path.exists():
        logger.info("[Session] No session file at %s", path)
        return

    data = json.loads(path.read_text())
    version = data.get("version", 0)
    if version not in (1, 2, 3):
        logger.warning("[Session] Unknown session version %s, skipping", version)
        return

    # Validate before changing live state, including pausing a running output.
    bpm = None
    if "bpm" in data and "bpm" not in skip_settings:
        bpm = validate_bpm(data["bpm"])

    # Native loading and every restored control happen while audio is paused.
    was_running = host.engine.running
    previous_output = host.audio_output_name
    if was_running:
        host.stop_audio()

    errors = []

    # -- BPM / Link ----------------------------------------------------------
    if bpm is not None:
        host.link.bpm = bpm

    if "link" not in skip_settings:
        try:
            if data.get("link_enabled", False):
                host.start_link()
            else:
                host.stop_link()
        except Exception as exc:
            errors.append(f"link state: {exc}")

    # -- Master gain ---------------------------------------------------------
    mg = data.get("master_gain")
    if mg is not None:
        host.engine.set_master_gain(mg)

    _restore_phases._restore_tracks(host, data, version, errors)
    _restore_phases._restore_master_effects(host, data, version, errors)
    _restore_phases._restore_routing(host, data, errors)
    _restore_phases._restore_sequences(host, data, version)
    _restore_phases._restore_connections(
        host, data, errors,
        start_audio=start_audio,
        skip_settings=skip_settings,
        was_running=was_running,
        previous_output=previous_output,
    )

    # -- Report --------------------------------------------------------------
    if errors:
        logger.warning("[Session] Restored with %d error(s):", len(errors))
        for err in errors:
            logger.warning("  - %s", err)
    else:
        logger.info("[Session] Restored from %s", path)
