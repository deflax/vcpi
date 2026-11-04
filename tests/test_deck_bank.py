"""Music-bank validation and multipart catalog regression coverage."""

import json
import tempfile
import unittest
from pathlib import Path

from core.deck_bank import CATALOG_LIMIT, encode_deck_message, load_bank
from core.state_transport import receive_frame, send_frame


class DeckBankTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "bank.json"

    def bank(self, tracks=None, **fields):
        data = {"version": 1, "id": "set", "tracks": tracks if tracks is not None else [
            {"id": "first", "path": "missing.wav", "title": "First"}]}
        data.update(fields)
        self.path.write_text(json.dumps(data))
        return self.path

    def test_relative_assets_resolve_from_manifest_without_opening(self):
        catalog = json.loads(load_bank(self.bank()))
        self.assertEqual(catalog["tracks"][0]["path"], str(self.root / "missing.wav"))
        self.assertEqual(catalog["initial_track"], 1)
        self.assertFalse(Path(catalog["tracks"][0]["path"]).exists())

    def test_stable_identity_survives_reordering_without_fallback(self):
        self.bank([{"id": "second", "path": "/music/second.wav"},
                   {"id": "first", "path": "first.wav"}])
        catalog = json.loads(load_bank(self.path, track_id="first", bank_id="set",
                                      cue=12.5, pitch=-1.25))
        self.assertEqual(catalog["initial_track"], 2)
        self.assertEqual(catalog["cue"], 12.5)
        self.assertEqual(catalog["pitch"], -1.25)
        with self.assertRaisesRegex(ValueError, "not found"):
            load_bank(self.path, track_id="removed")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            load_bank(self.path, bank_id="other")

    def test_invalid_manifest_identity_version_and_empty_banks(self):
        invalid = [{"version": True}, {"version": 2}, {"id": " "}, {"tracks": []},
                   {"tracks": [{"id": "", "path": "a.wav"}]},
                   {"tracks": [{"id": "same", "path": "a.wav"},
                               {"id": "same", "path": "b.wav"}]},
                   {"tracks": [{"id": "a", "path": ""}]}]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                load_bank(self.bank(**fields))

    def test_restore_scalars_are_finite_and_bounded(self):
        self.bank()
        for arguments in ({"pitch": 17}, {"pitch": float("nan")},
                          {"cue": -1}, {"cue": float("inf")}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                load_bank(self.path, **arguments)

    def test_large_catalog_uses_existing_bounded_frames(self):
        tracks = [{"id": str(i), "path": f"music/{i}.wav", "title": "Title " * 20}
                  for i in range(1500)]
        catalog = load_bank(self.bank(tracks))
        data = encode_deck_message({"operation": "load_deck", "arguments": {
            "catalog": catalog, "track_index": 0, "name": "Large"}}, 65536)
        self.assertGreater(len(data), 65536)
        self.assertLess(len(data), CATALOG_LIMIT + 65536)

        class Frames:
            def __init__(self):
                self.parts = []

            def send_bytes(self, part):
                self.parts.append(bytes(part))

            def recv_bytes(self, limit):
                part = self.parts.pop(0)
                if len(part) > limit:
                    raise AssertionError("frame exceeds existing limit")
                return part

        frames = Frames()
        send_frame(frames, data, 65536)
        self.assertTrue(all(len(part) <= 65536 for part in frames.parts))
        restored = receive_frame(frames, 65536, native_state=True)
        self.assertEqual(restored["arguments"]["catalog"], catalog)
        self.assertEqual(len(json.loads(catalog)["tracks"]), 1500)

    def test_catalog_budget_does_not_expand_metadata_budget(self):
        catalog = load_bank(self.bank())
        with self.assertRaisesRegex(ValueError, "metadata"):
            encode_deck_message({"operation": "load_deck", "arguments": {
                "catalog": catalog, "name": "x" * 65536}}, 65536)
        self.path.write_bytes(b" " * (CATALOG_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            load_bank(self.path)
