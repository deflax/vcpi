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

import base64
import binascii
import json
import logging
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from core.link import validate_bpm
from core.models import NUM_TRACKS
from core.state_transport import NATIVE_STATE_LIMIT, validate_native_state

if TYPE_CHECKING:
    from core.host import VcpiCore

DEFAULT_SESSION_PATH = Path("~/.config/vcpi/session.json").expanduser()


logger = logging.getLogger(__name__)


# ===========================================================================
# Snapshot / restore helpers
# ===========================================================================

def _fx_path(plugin) -> str:
    """Return the effect path captured by the audio process."""
    return plugin.path_to_plugin_file


def _saved_plugin_state(plugin) -> str:
    """Encode only a chunk captured by the explicit native save barrier."""
    if plugin is None or plugin.native_state is None:
        raise RuntimeError("native plugin state was not captured; refusing parameter-only save")
    return base64.b64encode(validate_native_state(plugin.native_state)).decode("ascii")


def _apply_plugin_params(
    host: VcpiCore,
    track_index: int | None,
    params: dict[str, bool | str | int | float],
    fx_index: int | None = None,
):
    """Conservatively migrate v1 values using freshly loaded live descriptors."""
    if track_index is None:
        plugin = host.engine.master_effects[fx_index]
    else:
        track = host.engine.tracks[track_index]
        plugin = track.plugin if fx_index is None else track.effects[fx_index]
    descriptors = plugin.parameters if plugin is not None else {}
    safe_params = {}
    for name, value in params.items():
        descriptor = descriptors.get(name)
        if descriptor is None or not descriptor.is_automatable:
            logger.warning(
                "[session] skipped legacy param '%s': %s", name,
                "unavailable in live metadata" if descriptor is None else "not automatable",
            )
        else:
            safe_params[name] = value
    if not safe_params:
        return
    result = host.engine.set_parameters(track_index, safe_params, fx_index=fx_index)
    for name, error in result["errors"].items():
        logger.warning(
            "[session] could not restore param '%s' = %s: %s",
            name, params[name], error,
        )


def _restore_plugin_state(host, track_index, entry, version, fx_index=None, *, wav=False):
    """Native chunks take precedence; parameter replay is legacy-v1-only."""
    if wav:
        return
    if "native_state" in entry:
        encoded = entry["native_state"]
        if not isinstance(encoded, str):
            raise ValueError("native_state must be a base64 string")
        if len(encoded) > 4 * ((NATIVE_STATE_LIMIT + 2) // 3):
            raise ValueError("native_state exceeds the bounded state allowance")
        try:
            state = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("native_state is not valid base64") from error
        host.engine.load_plugin_state(
            track_index, validate_native_state(state), fx_index=fx_index)
    elif version == 1:
        _apply_plugin_params(host, track_index, entry.get("params", {}), fx_index)
    else:
        raise ValueError(f"version {version} VST3 entry is missing native_state")


def snapshot(host: VcpiCore) -> dict:
    """Copy one coherent cached state; never inspect a live native plugin."""
    with host.engine.state_lock:
        return _snapshot_cached(host)


def _snapshot_cached(host: VcpiCore) -> dict:
    """Capture the full restorable state of the host as a plain dict."""
    tracks_data = []
    for track in host.engine.tracks:
        if track is None:
            tracks_data.append(None)
            continue
        effects_data = []
        for fx in track.effects:
            fxp = _fx_path(fx)
            effects_data.append({
                "path": fxp,
                "name": Path(fxp).stem if fxp else "unknown",
                "native_state": _saved_plugin_state(fx),
            })
        track_entry = {
            "kind": track.source_type,
            "path": track.path,
            "name": track.name,
            "gain": track.gain,
            "enabled": track.enabled,
            "muted": track.muted,
            "solo": track.solo,
            "effects": effects_data,
        }
        if track.source_type == "deck":
            status = track.deck
            track_entry["deck"] = {
                "bank_path": status["bank_path"],
                "bank_id": status["bank_id"],
                "tracks": [dict(entry) for entry in status["tracks"]],
                "track_id": status["track_id"],
                "cue": status["cue"],
                "pitch": status["pitch"],
                "sync": status.get("sync", True),
            }
        elif track.source_type != "wav":
            track_entry["native_state"] = _saved_plugin_state(track.plugin)
        if track.source_type == "vcv" and track.vcv_patch_path:
            track_entry["vcv_patch_path"] = track.vcv_patch_path
        if track.source_type == "pd" and track.pd_patch_path:
            track_entry["pd_patch_path"] = track.pd_patch_path
        tracks_data.append(track_entry)

    master_fx_data = []
    for fx in host.engine.master_effects:
        fxp = _fx_path(fx)
        master_fx_data.append({
            "path": fxp,
            "name": Path(fxp).stem if fxp else "unknown",
            "native_state": _saved_plugin_state(fx),
        })

    # Routing: store as 1-based for readability in the JSON file
    routing = {str(ch + 1): idx + 1 for ch, idx in host.channel_map.items()}

    connections = {
        "audio_output": (
            host.preferred_output_device
            if host.preferred_output_device is not None else host.audio_output_name
        ),
        "midi_inputs": host.midi_input_names,
        "midi_mix_in": host.mixer_midi_name,
        "midi_mix_out": host.mixer_midi_out_name,
    }

    return {
        "version": 3,
        "sample_rate": host.sample_rate,
        "buffer_size": host.buffer_size,
        "bpm": host.link.bpm,
        "link_enabled": host.link.enabled,
        "master_gain": host.engine.master_gain,
        "routing": routing,
        "tracks": tracks_data,
        "master_effects": master_fx_data,
        "connections": connections,
        "sequences": host.sequencer.snapshot(),
    }


def save(host: VcpiCore, path: Optional[Path] = None):
    """Save the current session to a JSON file."""
    path = Path(path) if path else DEFAULT_SESSION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    host.engine.sync_state()
    data = snapshot(host)
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

    # -- Tracks ---------------------------------------------------------------
    for idx, track_data in enumerate(data.get("tracks" if version == 3 else "slots", [])):
        if idx >= NUM_TRACKS:
            break
        if track_data is None:
            continue
        track_kind = track_data.get("kind", "plugin")
        plugin_path = track_data.get("path")
        if track_kind != "deck" and not plugin_path:
            continue
        try:
            match track_kind:
                case "deck":
                    deck = track_data.get("deck")
                    if not isinstance(deck, dict):
                        raise ValueError("deck payload is required")
                    track = host.restore_deck(idx, deck, track_data.get("name"))
                case "wav":
                    track = host.load_wav(idx, plugin_path, track_data.get("name"))
                case "vcv":
                    vcv_patch = track_data.get("vcv_patch_path", "")
                    if version == 1 and vcv_patch:
                        track, _, _ = host.load_vcv(
                            idx, vcv_patch,
                            cardinal_path=plugin_path,
                            name=track_data.get("name"),
                        )
                    else:
                        track = host.load_instrument(idx, plugin_path, track_data.get("name"))
                case _:
                    track = host.load_instrument(idx, plugin_path, track_data.get("name"))
            track = host.engine.set_track_state(
                idx,
                gain=track_data.get("gain", 0.8),
                enabled=track_data.get("enabled", True),
                muted=track_data.get("muted", False),
                solo=track_data.get("solo", False),
                source_type=track_kind,
                vcv_patch_path=track_data.get("vcv_patch_path", ""),
                pd_patch_path=track_data.get("pd_patch_path", ""),
            )
            if track_kind != "deck":
                _restore_plugin_state(
                    host, idx, track_data, version, wav=track_kind == "wav")
            logger.info("[session] track %d: %s", idx + 1, track.name)

            for fx_data in track_data.get("effects", []):
                try:
                    host.load_effect(fx_data["path"], idx, fx_data.get("name"))
                    track = host.engine.tracks[idx]
                    _restore_plugin_state(
                        host, idx, fx_data, version, len(track.effects) - 1)
                except Exception as e:
                    errors.append(f"track {idx + 1} fx '{fx_data.get('path')}': {e}")

        except Exception as e:
            errors.append(f"track {idx + 1} ({track_kind}) '{plugin_path}': {e}")

    # -- Master effects ------------------------------------------------------
    # A loaded session replaces the bus; retaining it doubles effects on reload.
    for effect_index in reversed(range(len(host.engine.master_effects))):
        host.remove_effect(None, effect_index)
    for fx_data in data.get("master_effects", []):
        try:
            host.load_effect(fx_data["path"], None, fx_data.get("name"))
            _restore_plugin_state(
                host, None, fx_data, version, len(host.engine.master_effects) - 1)
        except Exception as e:
            errors.append(f"master fx '{fx_data.get('path')}': {e}")

    # -- Routing (stored as 1-based strings in JSON) -------------------------
    for ch_str, track_num in data.get("routing", {}).items():
        try:
            ch_internal = int(ch_str) - 1
            track_internal = int(track_num) - 1
            host.route(ch_internal, track_internal)
        except Exception as e:
            errors.append(f"route ch {ch_str} -> track {track_num}: {e}")

    # -- Sequences -----------------------------------------------------------
    seq_data = data.get("sequences")
    if isinstance(seq_data, list):
        if version < 3:
            seq_data = [
                {**entry, "linked_track": entry.get("linked_slot")}
                if isinstance(entry, dict) else entry
                for entry in seq_data
            ]
        host.sequencer.restore(seq_data)

    # -- Device connections --------------------------------------------------
    connections = data.get("connections", {})
    if not isinstance(connections, dict):
        connections = {}

    # Maximum retries and delay for USB devices that may not be ready at boot.
    max_retries = 3
    retry_delay = 2.0  # seconds

    def _restore_port(label: str, value, opener, retries: int = max_retries):
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        last_exc = None
        for attempt in range(retries):
            try:
                opener(value)
                if attempt > 0:
                    logger.info("[Session] %s connected on attempt %d", label, attempt + 1)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    logger.debug(
                        "[Session] %s attempt %d failed (%s), retrying in %.1fs",
                        label, attempt + 1, exc, retry_delay,
                    )
                    time.sleep(retry_delay)
        errors.append(f"{label} '{value}': {last_exc}")

    def _restore_audio(name: int | str | None, retries: int = max_retries):
        last_exc = None
        for attempt in range(retries):
            try:
                if host.engine.running:
                    if host.audio_output_name != name:
                        host.stop_audio()
                        host.start_audio(name)
                else:
                    host.start_audio(name)
                if attempt > 0:
                    logger.info("[Session] audio output connected on attempt %d", attempt + 1)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    logger.debug(
                        "[Session] audio output attempt %d failed (%s), retrying in %.1fs",
                        attempt + 1, exc, retry_delay,
                    )
                    time.sleep(retry_delay)
        errors.append(f"audio output '{name}': {last_exc}")

    audio_output = connections.get("audio_output") if "output" not in skip_settings else None
    audio_selected = audio_output is not None
    if isinstance(audio_output, str) and not audio_output.strip():
        audio_selected = False
    if audio_selected:
        host.preferred_output_device = audio_output

    midi_inputs = connections.get("midi_inputs") if "midi_input" not in skip_settings else None
    if isinstance(midi_inputs, list):
        already_open = set(host.midi_input_names)
        for port_name in midi_inputs:
            if port_name in already_open:
                logger.debug("[Session] MIDI in '%s' already open, skipping", port_name)
                continue
            _restore_port(f"MIDI in '{port_name}'", port_name, host.open_midi_input)

    if "midimix_input" not in skip_settings:
        _restore_port("MIDI mix in", connections.get("midi_mix_in"), host.open_mixer_midi)
    if "midimix_output" not in skip_settings:
        _restore_port("MIDI mix out", connections.get("midi_mix_out"), host.open_mixer_midi_out)
    if start_audio:
        if audio_selected:
            _restore_audio(audio_output)
        elif was_running:
            _restore_audio(previous_output)

    # -- Report --------------------------------------------------------------
    if errors:
        logger.warning("[Session] Restored with %d error(s):", len(errors))
        for err in errors:
            logger.warning("  - %s", err)
    else:
        logger.info("[Session] Restored from %s", path)
