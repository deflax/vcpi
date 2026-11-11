"""Session persistence -- save and restore host state to a JSON file.

Saved state includes:
  - Per-track: source kind (deck/vcv/pd), backend path, name, gain,
    enabled, muted, solo, insert effect paths/names, and opaque native VST3 state
  - Master effects: paths, names, and opaque native VST3 state
  - Decks: inline playlist with optional per-entry BPM hints, stable active ID,
    position/play, sync-ready filtering and durable per-song cue, pitch, sync
    and phase correction; no derived analysis, transient nudge or VST chunk
  - Shared track focus (one-based)
  - Master gain
  - MIDI channel -> track routing
  - Shared timeline BPM and source selection, Link enabled state
  - Audio/MIDI device connection targets

The session file is human-readable JSON so it can be hand-edited if needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from vcpi.link import validate_bpm
from vcpi.models import NUM_TRACKS

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
    version = data.get("version")
    if type(version) is not int or version != 3:
        raise ValueError(f"Unsupported session version {version!r}; only format 3 is supported")

    # Validate before changing live state, including pausing a running output.
    focus = data.get("focus", 1)
    if type(focus) is not int or not 1 <= focus <= NUM_TRACKS:
        raise ValueError(f"focus must be an integer between 1 and {NUM_TRACKS}")
    bpm = None
    if "bpm" in data and "bpm" not in skip_settings:
        bpm = validate_bpm(data["bpm"])
    clock_source = data.get("clock_source", "auto")
    if "clock_source" not in skip_settings and clock_source not in ("auto", "internal", "link", "beatstep"):
        raise ValueError("clock_source must be auto, internal, link, or beatstep")
    for track in data.get("tracks", []):
        if track is not None and track.get("kind") == "deck":
            deck = track.get("deck", {})
            if isinstance(deck, dict) and type(deck.get("sync_filter", True)) is not bool:
                raise ValueError("deck sync_filter must be a boolean")

    # Native loading and every restored control happen while audio is paused.
    was_running = host.engine.running
    previous_output = host.audio_output_name
    if was_running:
        host.stop_audio()

    errors = []

    # -- BPM / Link ----------------------------------------------------------
    if bpm is not None:
        host.timeline.bpm = bpm

    if "link" not in skip_settings:
        try:
            if data.get("link_enabled", False):
                host.start_link()
            else:
                host.stop_link()
        except Exception as exc:
            errors.append(f"link state: {exc}")
    if "clock_source" not in skip_settings:
        host.timeline.select_source(clock_source)

    # -- Master gain ---------------------------------------------------------
    mg = data.get("master_gain")
    if mg is not None:
        host.engine.set_master_gain(mg)

    _restore_phases._restore_tracks(host, data, errors)
    _restore_phases._restore_master_effects(host, data, errors)
    _restore_phases._restore_routing(host, data, errors)
    _restore_phases._restore_sequences(host, data)
    host.track_focus.select(focus - 1)
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
