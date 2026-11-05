"""Ordered session restore phases and device reconnection policy."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from vcpi.models import NUM_TRACKS

from .plugin_state import _restore_plugin_state

if TYPE_CHECKING:
    from vcpi.host import VcpiCore

logger = logging.getLogger(__name__)


def _restore_tracks(host: VcpiCore, data: dict, version: int, errors: list[str]):
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


def _restore_master_effects(host: VcpiCore, data: dict, version: int, errors: list[str]):
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


def _restore_routing(host: VcpiCore, data: dict, errors: list[str]):
    """Restore routing stored as one-based channel and track numbers."""
    for ch_str, track_num in data.get("routing", {}).items():
        try:
            ch_internal = int(ch_str) - 1
            track_internal = int(track_num) - 1
            host.route(ch_internal, track_internal)
        except Exception as e:
            errors.append(f"route ch {ch_str} -> track {track_num}: {e}")


def _restore_sequences(host: VcpiCore, data: dict, version: int):
    seq_data = data.get("sequences")
    if isinstance(seq_data, list):
        if version < 3:
            seq_data = [
                {**entry, "linked_track": entry.get("linked_slot")}
                if isinstance(entry, dict) else entry
                for entry in seq_data
            ]
        host.sequencer.restore(seq_data)


def _restore_connections(
    host: VcpiCore,
    data: dict,
    errors: list[str],
    *,
    start_audio: bool,
    skip_settings,
    was_running: bool,
    previous_output: int | str | None,
):
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
