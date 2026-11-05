"""Control-side music-bank validation and bounded resolved catalog transfers."""

from __future__ import annotations

import json
import math
import pickle
from pathlib import Path

# Catalogs use the existing multipart pipe framing, never a larger frame limit.
CATALOG_LIMIT = 512 * 1024
TEXT_LIMIT = 1024


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    if len(value.encode("utf-8")) > TEXT_LIMIT or "\x00" in value:
        raise ValueError(f"{label} is too long or contains NUL")
    return value


def load_bank(bank_path, *, track_id=None, bank_id=None, cue=0, pitch=0, sync=True):
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
    initial = 1
    if track_id is not None:
        if track_id not in seen:
            raise ValueError(f"deck track ID not found: {track_id}")
        initial = next(i for i, track in enumerate(tracks, 1) if track["id"] == track_id)
    cue, pitch = float(cue), float(pitch)
    if not math.isfinite(cue) or cue < 0:
        raise ValueError("deck cue must be finite and nonnegative")
    if not math.isfinite(pitch) or not -16 <= pitch <= 16:
        raise ValueError("deck pitch must be between -16 and 16")
    if type(sync) is not bool:
        raise ValueError("deck sync must be a boolean")
    catalog = {"version": 1, "id": identity, "bank_path": str(path),
               "tracks": tracks, "initial_track": initial, "cue": cue, "pitch": pitch,
               "sync": sync}
    encoded = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    validate_catalog(encoded)
    return encoded


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
