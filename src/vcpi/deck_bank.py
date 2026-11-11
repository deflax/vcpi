"""Session playlists and bounded catalog transfers."""

from __future__ import annotations

import json
import math
import pickle
from collections import deque
from pathlib import Path
from uuid import uuid4

from vcpi.state_transport import receive_frame, send_frame

# Catalogs use the existing multipart pipe framing, never a larger frame limit.
CATALOG_LIMIT = 512 * 1024
TEXT_LIMIT = 1024
AUDIO_SUFFIXES = frozenset({".wav", ".wave", ".aif", ".aiff", ".aifc", ".mp3", ".flac"})


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    if len(value.encode("utf-8")) > TEXT_LIMIT or "\x00" in value:
        raise ValueError(f"{label} is too long or contains NUL")
    return value


def audio_entry(audio_path):
    """Create a stable playlist entry from a supported, existing audio file."""
    path = Path(audio_path).expanduser().resolve()
    if path.suffix.lower() not in AUDIO_SUFFIXES:
        raise ValueError("deck audio must be WAV, AIFF, MP3 or FLAC")
    if not path.is_file():
        raise FileNotFoundError(f"deck audio file not found: {path}")
    return {"id": uuid4().hex, "path": str(path), "title": path.stem}


def _song_state(*, cue, pitch, sync, phase_offset, position=None, play=False):
    cue, pitch = float(cue), float(pitch)
    if not math.isfinite(cue) or cue < 0:
        raise ValueError("deck cue must be finite and nonnegative")
    if not math.isfinite(pitch) or not -16 <= pitch <= 16:
        raise ValueError("deck pitch must be between -16 and 16")
    phase_offset = float(phase_offset)
    if not math.isfinite(phase_offset) or not -0.5 <= phase_offset <= 0.5:
        raise ValueError("deck phase offset must be finite and between -0.5 and 0.5")
    position = cue if position is None else float(position)
    if not math.isfinite(position) or position < 0:
        raise ValueError("deck position must be finite and nonnegative")
    if type(sync) is not bool or type(play) is not bool:
        raise ValueError("deck sync and play must be booleans")
    return {"position": position, "cue": cue, "pitch": pitch, "sync": sync,
            "phase_offset": phase_offset, "play": play}


def make_catalog(identity, tracks, *, bank_path=None, track_id=None,
                 cue=0, pitch=0, sync=True, phase_offset=0,
                 position=None, play=False, song_states=None):
    """Encode a bounded playlist and durable song state."""
    identity = _text(identity, "playlist ID")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("deck playlist must contain at least one track")
    entries, seen = [], set()
    for entry in tracks:
        if not isinstance(entry, dict):
            raise ValueError("deck track must be an object")
        entry_id = _text(entry.get("id"), "track ID")
        if entry_id in seen:
            raise ValueError(f"duplicate deck track ID: {entry_id}")
        seen.add(entry_id)
        path = Path(_text(entry.get("path"), "track path")).expanduser()
        if not path.is_absolute():
            raise ValueError("inline deck track paths must be absolute")
        entries.append({"id": entry_id, "path": str(path.resolve()),
                        "title": _text(entry.get("title", entry_id), "track title")})
        if "bpm_hint" in entry:
            hint = entry["bpm_hint"]
            if isinstance(hint, bool) or not isinstance(hint, (int, float)) or not 60 <= hint <= 200:
                raise ValueError("deck BPM hint must be a finite number between 60 and 200")
            entries[-1]["bpm_hint"] = hint
    initial = 1
    if track_id is not None:
        if track_id not in seen:
            raise ValueError(f"deck track ID not found: {track_id}")
        initial = next(i for i, entry in enumerate(entries, 1) if entry["id"] == track_id)
    initial_state = _song_state(cue=cue, pitch=pitch, sync=sync,
                                phase_offset=phase_offset, position=position, play=play)
    states = {}
    if song_states is not None:
        if not isinstance(song_states, dict):
            raise ValueError("deck song_states must be an object")
        for song_id, state in song_states.items():
            if song_id not in seen:
                raise ValueError(f"deck song state track ID not found: {song_id}")
            if not isinstance(state, dict) or set(state) != set(initial_state):
                raise ValueError("deck song state must contain position, cue, pitch, sync, phase_offset and play")
            if state["position"] is None:
                raise ValueError("deck position must be finite and nonnegative")
            states[song_id] = _song_state(**state)
    selected_id = entries[initial - 1]["id"]
    initial_state = states.get(selected_id, initial_state)
    catalog = {"version": 1, "id": identity,
               "bank_path": _text(bank_path or f"session:{identity}", "playlist locator"),
               "tracks": entries, "initial_track": initial, **initial_state,
               "song_states": states}
    return validate_catalog(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")))


def validate_catalog(catalog):
    if not isinstance(catalog, str) or not catalog:
        raise ValueError("deck catalog must be a nonempty JSON string")
    if len(catalog.encode("utf-8")) > CATALOG_LIMIT:
        raise ValueError(f"resolved deck catalog exceeds {CATALOG_LIMIT} bytes")
    return catalog


def encode_deck_message(message, ordinary_limit):
    """Budget only the catalog separately; all other metadata keeps its limit."""
    metadata = dict(message)
    arguments = dict(message["arguments"])
    validate_catalog(arguments["catalog"])
    arguments["catalog"] = None
    metadata["arguments"] = arguments
    if len(pickle.dumps(metadata, protocol=pickle.HIGHEST_PROTOCOL)) > ordinary_limit:
        raise ValueError(f"deck metadata exceeds {ordinary_limit} bytes")
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > CATALOG_LIMIT + ordinary_limit:
        raise ValueError("deck catalog transfer exceeds bounded message allowance")
    return data


def encode_deck_status(status, ordinary_limit, state_limit):
    """Bound ordinary metadata separately from both full per-song status maps."""
    metadata = {**status, "song_states": None, "song_bpms": None}
    if len(pickle.dumps(metadata, protocol=pickle.HIGHEST_PROTOCOL)) > ordinary_limit:
        raise BufferError(f"deck status metadata exceeds {ordinary_limit} bytes")
    data = pickle.dumps(status, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > state_limit:
        raise BufferError(f"deck status exceeds {state_limit} bytes")
    return data


def encode_deck_update(message, ordinary_limit, state_limit):
    """Only a single deck status may use the existing state-transfer allowance."""
    kind, epoch, serial, updates = message
    if kind != "updates" or len(updates) != 1 or updates[0][0] != "deck":
        raise BufferError("oversized ordinary update")
    _, index, generation, status = updates[0]
    encode_deck_status(status, ordinary_limit, state_limit)
    metadata = (kind, epoch, serial, [("deck", index, generation,
                                     {**status, "song_states": None, "song_bpms": None})])
    if len(pickle.dumps(metadata, protocol=pickle.HIGHEST_PROTOCOL)) > ordinary_limit:
        raise BufferError(f"deck update metadata exceeds {ordinary_limit} bytes")
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > state_limit:
        raise BufferError(f"deck update exceeds {state_limit} bytes")
    return data


class _StatusFrames:
    """Adapt one bounded queue item to the existing multipart pipe framing."""

    def __init__(self, frames=()):
        self.frames = deque(frames)

    def send_bytes(self, frame):
        self.frames.append(bytes(frame))

    def recv_bytes(self, limit):
        frame = self.frames.popleft()
        if len(frame) > limit:
            raise ValueError("deck status frame exceeds existing limit")
        return frame


def deck_status_frames(status, ordinary_limit, state_limit):
    frames = _StatusFrames()
    send_frame(frames, encode_deck_status(status, ordinary_limit, state_limit), ordinary_limit)
    return tuple(frames.frames)


def receive_deck_status(frames, ordinary_limit):
    return receive_frame(_StatusFrames(frames), ordinary_limit, native_state=True)
