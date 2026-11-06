"""Session playlists, legacy bank migration and bounded catalog transfers."""

from __future__ import annotations

import json
import math
import pickle
from pathlib import Path
from uuid import uuid4

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


def load_bank(bank_path, *, track_id=None, bank_id=None, cue=0, pitch=0, sync=True,
              phase_offset=0):
    """Resolve ordered assets without opening them; select saved identity exactly."""
    path = Path(bank_path).expanduser().resolve()
    with path.open("rb") as source:
        raw = source.read(CATALOG_LIMIT + 1)
    if len(raw) > CATALOG_LIMIT:
        raise ValueError(f"deck manifest exceeds {CATALOG_LIMIT} bytes")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("deck manifest version must be 1")
    identity = _text(manifest.get("id"), "bank ID")
    if bank_id is not None and identity != bank_id:
        raise ValueError(f"deck bank ID mismatch: expected {bank_id!r}, got {identity!r}")
    entries = manifest.get("tracks")
    if not isinstance(entries, list) or not entries:
        raise ValueError("deck bank must contain at least one track")
    tracks, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("deck track must be an object")
        identity_track = _text(entry.get("id"), "track ID")
        if identity_track in seen:
            raise ValueError(f"duplicate deck track ID: {identity_track}")
        seen.add(identity_track)
        asset = Path(_text(entry.get("path"), "track path")).expanduser()
        if not asset.is_absolute():
            asset = path.parent / asset
        title = _text(entry.get("title", identity_track), "track title")
        tracks.append({"id": identity_track, "path": str(asset.resolve()), "title": title})
    return make_catalog(identity, tracks, bank_path=str(path), track_id=track_id,
                        cue=cue, pitch=pitch, sync=sync, phase_offset=phase_offset)


def audio_entry(audio_path):
    """Create a stable playlist entry from a supported, existing audio file."""
    path = Path(audio_path).expanduser().resolve()
    if path.suffix.lower() not in AUDIO_SUFFIXES:
        raise ValueError("deck audio must be WAV, AIFF, MP3 or FLAC")
    if not path.is_file():
        raise FileNotFoundError(f"deck audio file not found: {path}")
    return {"id": uuid4().hex, "path": str(path), "title": path.stem}


def make_catalog(identity, tracks, *, bank_path=None, track_id=None,
                 cue=0, pitch=0, sync=True, phase_offset=0):
    """Encode a playlist with selected-song phase correction in bounded beats."""
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
    initial = 1
    if track_id is not None:
        if track_id not in seen:
            raise ValueError(f"deck track ID not found: {track_id}")
        initial = next(i for i, entry in enumerate(entries, 1) if entry["id"] == track_id)
    cue, pitch = float(cue), float(pitch)
    if not math.isfinite(cue) or cue < 0:
        raise ValueError("deck cue must be finite and nonnegative")
    if not math.isfinite(pitch) or not -16 <= pitch <= 16:
        raise ValueError("deck pitch must be between -16 and 16")
    phase_offset = float(phase_offset)
    if not math.isfinite(phase_offset) or not -0.5 <= phase_offset <= 0.5:
        raise ValueError("deck phase offset must be finite and between -0.5 and 0.5")
    if type(sync) is not bool:
        raise ValueError("deck sync must be a boolean")
    catalog = {"version": 1, "id": identity,
               "bank_path": _text(bank_path or f"session:{identity}", "playlist locator"),
               "tracks": entries, "initial_track": initial, "cue": cue, "pitch": pitch,
               "sync": sync, "phase_offset": phase_offset}
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
