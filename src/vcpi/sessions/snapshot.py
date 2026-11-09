"""Coherent session snapshots from cached host state."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .plugin_state import _saved_plugin_state

if TYPE_CHECKING:
    from vcpi.host import VcpiCore


def _fx_path(plugin) -> str:
    """Return the effect path captured by the audio process."""
    return plugin.path_to_plugin_file


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
            active_state = {
                "position": status.get("position", status["cue"]),
                "cue": status["cue"],
                "pitch": status["pitch"],
                "sync": status.get("sync", True),
                "phase_offset": status.get("phase_offset", 0),
                "play": status.get("play", False),
            }
            song_states = {identity: dict(state)
                           for identity, state in status.get("song_states", {}).items()}
            song_states[status["track_id"]] = dict(active_state)
            track_entry["deck"] = {
                "bank_path": status["bank_path"],
                "bank_id": status["bank_id"],
                "tracks": [dict(entry) for entry in status["tracks"]],
                "track_id": status["track_id"],
                **active_state,
                "song_states": song_states,
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
        "beatstep_in": host.preferred_beatstep_input,
    }

    return {
        "version": 3,
        "focus": host.track_focus.snapshot()[0] + 1,
        "sample_rate": host.sample_rate,
        "buffer_size": host.buffer_size,
        "bpm": host.timeline.bpm,
        "link_enabled": host.link.enabled,
        "clock_source": host.timeline.source,
        "master_gain": host.engine.master_gain,
        "routing": routing,
        "tracks": tracks_data,
        "master_effects": master_fx_data,
        "connections": connections,
        "sequences": host.sequencer.snapshot(),
    }
