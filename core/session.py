"""Session persistence -- save and restore host state to a JSON file.

Saved state includes:
  - Per-slot: source kind (plugin/wav), instrument path, name, gain, muted,
    solo, insert effect paths/names, and all plugin parameter values
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
import time
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
    names = getattr(plugin, "parameters", {})
    for name in names:
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
        slot_entry = {
            "kind": slot.source_type,
            "path": slot.path,
            "name": slot.name,
            "gain": slot.gain,
            "muted": slot.muted,
            "solo": slot.solo,
            "params": _plugin_params(slot.plugin),
            "effects": effects_data,
        }
        if slot.source_type == "vcv" and slot.vcv_patch_path:
            slot_entry["vcv_patch_path"] = slot.vcv_patch_path
        slots_data.append(slot_entry)

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
        slot_kind = slot_data.get("kind", "plugin")
        try:
            match slot_kind:
                case "wav":
                    slot = host.load_wav(idx, plugin_path, slot_data.get("name"))
                case "vcv":
                    vcv_patch = slot_data.get("vcv_patch_path", "")
                    if vcv_patch:
                        slot, _, _ = host.load_vcv(
                            idx, vcv_patch,
                            cardinal_path=plugin_path,
                            name=slot_data.get("name"),
                        )
                    else:
                        slot = host.load_instrument(idx, plugin_path, slot_data.get("name"))
                        slot.source_type = "vcv"
                case _:
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
            errors.append(f"slot {idx + 1} ({slot_kind}) '{plugin_path}': {e}")

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

    def _restore_audio(name: str, retries: int = max_retries):
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

    audio_output = connections.get("audio_output")
    audio_selected = audio_output is not None
    if isinstance(audio_output, str) and not audio_output.strip():
        audio_selected = False

    if audio_selected and isinstance(audio_output, str):
        _restore_audio(audio_output)

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
