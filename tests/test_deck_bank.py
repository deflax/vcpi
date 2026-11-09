"""Inline playlist validation, legacy migration and multipart catalog coverage."""

import json
import tempfile
import unittest
from pathlib import Path

from vcpi.deck_bank import (
    CATALOG_LIMIT, audio_entry, deck_status_frames, encode_deck_message,
    encode_deck_update, load_bank, make_catalog, receive_deck_status,
)
from vcpi.state_transport import receive_frame, send_frame


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
        self.assertTrue(catalog["sync"])
        self.assertEqual(catalog["phase_offset"], 0)
        self.assertFalse(Path(catalog["tracks"][0]["path"]).exists())

    def test_stable_identity_survives_reordering_without_fallback(self):
        self.bank([{"id": "second", "path": "/music/second.wav"},
                   {"id": "first", "path": "first.wav"}])
        catalog = json.loads(load_bank(self.path, track_id="first", bank_id="set",
                                      cue=12.5, pitch=-1.25, sync=False, phase_offset=-0.25))
        self.assertEqual(catalog["initial_track"], 2)
        self.assertEqual(catalog["cue"], 12.5)
        self.assertEqual(catalog["pitch"], -1.25)
        self.assertFalse(catalog["sync"])
        self.assertEqual(catalog["phase_offset"], -0.25)
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
                          {"cue": -1}, {"cue": float("inf")},
                          {"sync": "false"}, {"sync": 0}, {"sync": None}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                load_bank(self.path, **arguments)

    def test_phase_offset_catalog_bounds_and_invalid_values(self):
        self.bank()
        tracks = [{"id": "first", "path": str(self.root / "missing.wav")}]
        for phase_offset in (-0.5, -0.25, 0, 0.25, 0.5):
            with self.subTest(phase_offset=phase_offset):
                inline = json.loads(make_catalog("set", tracks, phase_offset=phase_offset))
                legacy = json.loads(load_bank(self.path, phase_offset=phase_offset))
                self.assertEqual(inline["phase_offset"], phase_offset)
                self.assertEqual(legacy["phase_offset"], phase_offset)
        for phase_offset in (-0.500001, 0.500001, float("-inf"), float("inf"), float("nan")):
            with self.subTest(phase_offset=phase_offset):
                with self.assertRaisesRegex(ValueError, "phase offset"):
                    make_catalog("set", tracks, phase_offset=phase_offset)
                with self.assertRaisesRegex(ValueError, "phase offset"):
                    load_bank(self.path, phase_offset=phase_offset)

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

    def test_audio_entries_have_unique_stable_ids_and_absolute_paths(self):
        audio = self.root / "song.WAV"
        audio.write_bytes(b"audio validation belongs to the native loader")
        first, second = audio_entry(audio), audio_entry(audio)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["path"], str(audio.resolve()))
        self.assertEqual(first["title"], "song")
        encoded = make_catalog("playlist", [first, second], track_id=second["id"])
        catalog = json.loads(encoded)
        self.assertEqual(catalog["tracks"], [first, second])
        self.assertEqual(catalog["initial_track"], 2)
        self.assertEqual(catalog["bank_path"], "session:playlist")
        self.assertEqual(catalog["phase_offset"], 0)

    def test_inline_playlist_rejects_empty_duplicate_and_relative_entries(self):
        valid = {"id": "entry", "path": str(self.root / "song.wav")}
        for entries in ([], [valid, valid], [{"id": "entry", "path": "song.wav"}]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                make_catalog("playlist", entries)
        self.bank()
        with self.assertRaises(ValueError):
            audio_entry(self.path)
        with self.assertRaises(FileNotFoundError):
            audio_entry(self.root / "absent.wav")

    def test_updates_retain_catalog_and_metadata_bounds(self):
        catalog = load_bank(self.bank())
        with self.assertRaisesRegex(ValueError, "metadata"):
            encode_deck_message({"operation": "update_deck_catalog", "arguments": {
                "catalog": catalog, "name": "x" * 65536}}, 65536)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            encode_deck_message({"operation": "update_deck_catalog", "arguments": {
                "catalog": "x" * (CATALOG_LIMIT + 1)}}, 65536)

    def test_song_state_precedence_and_legacy_transport_defaults(self):
        self.bank()
        legacy = json.loads(load_bank(self.path, cue=0.25))
        self.assertEqual(legacy["position"], 0.25)
        self.assertFalse(legacy["play"])
        state = {"position": 1.25, "cue": 0.5, "pitch": -2, "sync": False,
                 "phase_offset": -0.25, "play": True}
        catalog = json.loads(load_bank(self.path, cue=0.75, position=0.8,
                                      song_states={"first": state}))
        self.assertEqual(catalog["song_states"], {"first": state})
        self.assertEqual({key: catalog[key] for key in state}, state)
        explicit = json.loads(load_bank(self.path, cue=0.25, position=1, play=True))
        self.assertEqual(explicit["position"], 1)
        self.assertTrue(explicit["play"])

    def test_invalid_song_states_and_transport_values(self):
        self.bank()
        state = {"position": 1, "cue": 0.5, "pitch": -2, "sync": False,
                 "phase_offset": -0.25, "play": True}
        for fields in ({"position": -1}, {"position": float("nan")},
                       {"position": float("inf")}, {"play": 1}, {"play": "true"},
                       {"song_states": []}, {"song_states": {"removed": state}},
                       {"song_states": {"first": {}}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                load_bank(self.path, **fields)
        for field, value in (("position", -1), ("cue", -1), ("pitch", 17),
                             ("phase_offset", 0.51), ("sync", 1), ("play", "false")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_bank(self.path, song_states={"first": {**state, field: value}})

    def test_full_status_map_uses_bounded_multipart_without_enlarging_metadata(self):
        state = {"position": 1.25, "cue": 0.5, "pitch": -2.0, "sync": False,
                 "phase_offset": -0.25, "play": True}
        status = {"track_id": "0", "song_states": {
            str(index): dict(state) for index in range(2000)}}
        frames = deck_status_frames(status, 65536, 16 * 1024 * 1024)
        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(frame) <= 65536 for frame in frames))
        self.assertEqual(receive_deck_status(frames, 65536), status)
        message = ("updates", 1, 2, [("deck", 0, 1, status)])
        self.assertGreater(len(encode_deck_update(message, 65536, 16 * 1024 * 1024)), 65536)
        with self.assertRaisesRegex(BufferError, "metadata"):
            deck_status_frames({**status, "title": "x" * 65536}, 65536, 16 * 1024 * 1024)
        with self.assertRaisesRegex(BufferError, "ordinary"):
            encode_deck_update(("updates", 1, 2, [("other", "x" * 65536)]),
                               65536, 16 * 1024 * 1024)
