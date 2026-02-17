"""Session persistence -- save and restore host state to a JSON file.

Saved state includes:
  - Per-slot: instrument path, name, gain, muted, solo, insert effect paths/names,
    and all plugin parameter values
  - Master effects: paths, names, and parameter values
  - Master gain
  - MIDI channel -> slot routing
  - Link BPM and enabled state
  - Audio/MIDI device connection targets

The session file is human-readable JSON so it can be hand-edited if needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from core.models import NUM_SLOTS

if TYPE_CHECKING:
    from core.host import VcpiCore

DEFAULT_SESSION_PATH = Path("~/.config/vcpi/session.json").expanduser()


logger = logging.getLogger(__name__)


# ===========================================================================
# Snapshot / restore helpers
# ===========================================================================

def _plugin_params(plugin) -> dict[str, float]:
    """Extract all parameter values from a pedalboard plugin."""
    params = {}
    for name in plugin.parameters:
        try:
            params[name] = float(getattr(plugin, name))
        except Exception:
            pass
    return params


def _apply_plugin_params(plugin, params: dict[str, float]):
    """Apply saved parameter values to a pedalboard plugin."""
    for name, value in params.items():
        try:
            setattr(plugin, name, value)
        except Exception:
            logger.warning("[session] could not restore param '%s' = %s", name, value)


def snapshot(host: VcpiCore) -> dict:
    """Capture the full restorable state of the host as a plain dict."""
    slots_data = []
    for slot in host.engine.slots:
        if slot is None:
            slots_data.append(None)
            continue
        effects_data = []
        for fx in slot.effects:
            effects_data.append({
                "path": fx.path_to_plugin_file,
                "name": Path(fx.path_to_plugin_file).stem,
                "params": _plugin_params(fx),
            })
        slots_data.append({
            "path": slot.path,
            "name": slot.name,
            "gain": slot.gain,
            "muted": slot.muted,
            "solo": slot.solo,
            "params": _plugin_params(slot.plugin),
            "effects": effects_data,
        })

    master_fx_data = []
    for fx in host.engine.master_effects:
        master_fx_data.append({
            "path": fx.path_to_plugin_file,
            "name": Path(fx.path_to_plugin_file).stem,
            "params": _plugin_params(fx),
        })

    # Routing: store as 1-based for readability in the JSON file
    routing = {str(ch + 1): idx + 1 for ch, idx in host.channel_map.items()}

    connections = {
        "audio_output": host.audio_output_name,
        "midi_seq_in": host.sequencer_midi_name,
        "midi_keys_in": host.keyboard_midi_name,
        "midi_mix_in": host.mixer_midi_name,
        "midi_mix_out": host.mixer_midi_out_name,
    }

    return {
        "version": 1,
        "sample_rate": host.sample_rate,
        "buffer_size": host.buffer_size,
        "bpm": host.link.bpm,
        "link_enabled": host.link.enabled,
        "master_gain": host.engine.master_gain,
        "routing": routing,
        "slots": slots_data,
        "master_effects": master_fx_data,
        "connections": connections,
    }


def save(host: VcpiCore, path: Optional[Path] = None):
    """Save the current session to a JSON file."""
    path = Path(path) if path else DEFAULT_SESSION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = snapshot(host)
    path.write_text(json.dumps(data, indent=2) + "\n")
    logger.info("[Session] Saved to %s", path)


def restore(host: VcpiCore, path: Optional[Path] = None):
    """Restore a session from a JSON file.

    Loads instruments, effects, parameters, routing, gains, tempo,
    and previously connected audio/MIDI device targets.
    """
    path = Path(path) if path else DEFAULT_SESSION_PATH
    if not path.exists():
        logger.info("[Session] No session file at %s", path)
        return

    data = json.loads(path.read_text())
    version = data.get("version", 0)
    if version != 1:
        logger.warning("[Session] Unknown session version %s, skipping", version)
        return

    errors = []

    # -- BPM / Link ----------------------------------------------------------
    bpm = data.get("bpm")
    if bpm is not None:
        host.link._bpm = bpm

    link_enabled = data.get("link_enabled", False)
    if link_enabled:
        try:
            host.start_link(bpm)
        except Exception as exc:
            errors.append(f"link enable: {exc}")

    # -- Master gain ---------------------------------------------------------
    mg = data.get("master_gain")
    if mg is not None:
        host.engine.master_gain = mg

    # -- Slots ---------------------------------------------------------------
    for idx, slot_data in enumerate(data.get("slots", [])):
        if idx >= NUM_SLOTS:
            break
        if slot_data is None:
            continue
        plugin_path = slot_data.get("path")
        if not plugin_path:
            continue
        try:
            slot = host.load_instrument(idx, plugin_path, slot_data.get("name"))
            slot.gain = slot_data.get("gain", 0.8)
            slot.muted = slot_data.get("muted", False)
            slot.solo = slot_data.get("solo", False)
            _apply_plugin_params(slot.plugin, slot_data.get("params", {}))
            logger.info("[session] slot %d: %s", idx + 1, slot.name)

            for fx_data in slot_data.get("effects", []):
                try:
                    host.load_effect(fx_data["path"], idx, fx_data.get("name"))
                    fx_plugin = slot.effects[-1]
                    _apply_plugin_params(fx_plugin, fx_data.get("params", {}))
                except Exception as e:
                    errors.append(f"slot {idx + 1} fx '{fx_data.get('path')}': {e}")

        except Exception as e:
            errors.append(f"slot {idx + 1} '{plugin_path}': {e}")

    # -- Master effects ------------------------------------------------------
    for fx_data in data.get("master_effects", []):
        try:
            host.load_effect(fx_data["path"], None, fx_data.get("name"))
            fx_plugin = host.engine.master_effects[-1]
            _apply_plugin_params(fx_plugin, fx_data.get("params", {}))
        except Exception as e:
            errors.append(f"master fx '{fx_data.get('path')}': {e}")

    # -- Routing (stored as 1-based strings in JSON) -------------------------
    for ch_str, slot_num in data.get("routing", {}).items():
        try:
            ch_internal = int(ch_str) - 1
            slot_internal = int(slot_num) - 1
            host.route(ch_internal, slot_internal)
        except Exception as e:
            errors.append(f"route ch {ch_str} -> slot {slot_num}: {e}")

    # -- Device connections --------------------------------------------------
    connections = data.get("connections", {})
    if not isinstance(connections, dict):
        connections = {}

    def _restore_port(label: str, value, opener):
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        try:
            opener(value)
        except Exception as exc:
            errors.append(f"{label} '{value}': {exc}")

    audio_output = connections.get("audio_output")
    audio_selected = audio_output is not None
    if isinstance(audio_output, str) and not audio_output.strip():
        audio_selected = False

    if audio_selected:
        try:
            if host.engine.running:
                if host.audio_output_name != audio_output:
                    host.stop_audio()
                    host.start_audio(audio_output)
            else:
                host.start_audio(audio_output)
        except Exception as e:
            errors.append(f"audio output '{audio_output}': {e}")

    _restore_port("MIDI seq in", connections.get("midi_seq_in"), host.open_sequencer_midi)
    _restore_port("MIDI keys in", connections.get("midi_keys_in"), host.open_keyboard_midi)
    _restore_port("MIDI mix in", connections.get("midi_mix_in"), host.open_mixer_midi)
    _restore_port("MIDI mix out", connections.get("midi_mix_out"), host.open_mixer_midi_out)

    # -- Report --------------------------------------------------------------
    if errors:
        logger.warning("[Session] Restored with %d error(s):", len(errors))
        for err in errors:
            logger.warning("  - %s", err)
    else:
        logger.info("[Session] Restored from %s", path)
