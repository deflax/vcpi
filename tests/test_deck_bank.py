"""Inline playlist validation and multipart catalog coverage."""

import json
import pickle
import tempfile
import unittest
from pathlib import Path

from vcpi.audio.protocol import CONTROL_LIMIT, STATE_LIMIT, UPDATE_LIMIT
from vcpi.deck_bank import (
    CATALOG_LIMIT, audio_entry, deck_status_frames, encode_deck_message,
    encode_deck_status, encode_deck_update, make_catalog, receive_deck_status,
    selectable_indexes,
)
from vcpi.state_transport import receive_frame, send_frame


class DeckBankTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tracks = [{"id": "first", "path": str(self.root / "missing.wav"),
                        "title": "First"}]

    def test_catalog_preserves_absolute_assets_without_opening(self):
        catalog = json.loads(make_catalog("set", self.tracks))
        self.assertEqual(catalog["version"], 1)
        self.assertEqual(catalog["tracks"][0]["path"], str(self.root / "missing.wav"))
        self.assertEqual(catalog["initial_track"], 1)
        self.assertIs(catalog["sync_filter"], True)
        self.assertTrue(catalog["sync"])
        self.assertEqual(catalog["phase_offset"], 0)
        self.assertFalse(Path(catalog["tracks"][0]["path"]).exists())

    def test_stable_identity_survives_reordering_without_fallback(self):
        tracks = [{"id": "second", "path": "/music/second.wav"},
                  {"id": "first", "path": str(self.root / "first.wav")}]
        catalog = json.loads(make_catalog("set", tracks, track_id="first",
                                         cue=12.5, pitch=-1.25, sync=False, phase_offset=-0.25))
        self.assertEqual(catalog["initial_track"], 2)
        self.assertEqual(catalog["cue"], 12.5)
        self.assertEqual(catalog["pitch"], -1.25)
        self.assertFalse(catalog["sync"])
        self.assertEqual(catalog["phase_offset"], -0.25)
        with self.assertRaisesRegex(ValueError, "not found"):
            make_catalog("set", tracks, track_id="removed")

    def test_sync_filter_accepts_only_booleans(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                catalog = json.loads(make_catalog("set", self.tracks, sync_filter=enabled))
                self.assertIs(catalog["sync_filter"], enabled)
        for invalid in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "sync_filter"):
                make_catalog("set", self.tracks, sync_filter=invalid)

    def test_selectable_indexes_keep_original_positions_and_ignore_hints(self):
        tracks = [{"id": identity, "bpm_hint": 120}
                  for identity in ("pending", "ready", "missing", "unavailable", "ready-too")]
        deck = {"tracks": tracks,
                "song_bpms": {"pending": None, "ready": 120, "unavailable": 0,
                              "ready-too": 83}}
        self.assertEqual(selectable_indexes(deck), [2, 5])
        self.assertEqual(selectable_indexes({**deck, "sync_filter": True}), [2, 5])
        self.assertEqual(selectable_indexes({**deck, "sync_filter": False}), [1, 2, 3, 4, 5])
        self.assertEqual(selectable_indexes({"tracks": tracks}), [])
        self.assertEqual(selectable_indexes({}), [])
        self.assertEqual([entry["id"] for entry in tracks],
                         ["pending", "ready", "missing", "unavailable", "ready-too"])

    def test_invalid_playlist_identity_and_entries(self):
        with self.assertRaises(ValueError):
            make_catalog(" ", self.tracks)
        invalid = [[], [{"id": "", "path": "/music/a.wav"}],
                   [{"id": "same", "path": "/music/a.wav"},
                    {"id": "same", "path": "/music/b.wav"}],
                   [{"id": "a", "path": ""}], [None]]
        for tracks in invalid:
            with self.subTest(tracks=tracks), self.assertRaises(ValueError):
                make_catalog("set", tracks)

    def test_optional_bpm_hints_are_numeric_bounded_and_per_entry(self):
        for hint in (60, 87.25, 174, 200):
            with self.subTest(hint=hint):
                tracks = [{**self.tracks[0], "bpm_hint": hint},
                          {"id": "unhinted", "path": str(self.root / "other.wav")}]
                catalog = json.loads(make_catalog("set", tracks))
                self.assertEqual(catalog["tracks"][0]["bpm_hint"], hint)
                self.assertNotIn("bpm_hint", catalog["tracks"][1])
                self.assertNotIn("bpm_hint", catalog)
                self.assertNotIn("bpm_hint", self.tracks[0])

    def test_catalog_keeps_hints_but_discards_derived_entry_analysis(self):
        entry = {**self.tracks[0], "bpm_hint": 174, "bpm": 166.0,
                 "song_bpms": {"first": 166.0}, "analyzing_track_id": "first"}
        catalog = json.loads(make_catalog("set", [entry]))
        self.assertEqual(catalog["tracks"], [{**self.tracks[0], "bpm_hint": 174}])
        self.assertNotIn("song_bpms", catalog)
        self.assertNotIn("analyzing_track_id", catalog)

    def test_malformed_bpm_hints_are_rejected(self):
        for hint in (None, True, False, "174", [], {}, 59.999, 200.001,
                     float("nan"), float("inf"), float("-inf"), 10 ** 1000):
            with self.subTest(hint=hint), self.assertRaisesRegex(ValueError, "BPM hint"):
                make_catalog("set", [{**self.tracks[0], "bpm_hint": hint}])

    def test_restore_scalars_are_finite_and_bounded(self):
        for arguments in ({"pitch": 17}, {"pitch": float("nan")},
                          {"cue": -1}, {"cue": float("inf")},
                          {"sync": "false"}, {"sync": 0}, {"sync": None}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                make_catalog("set", self.tracks, **arguments)

    def test_phase_offset_catalog_bounds_and_invalid_values(self):
        for phase_offset in (-0.5, -0.25, 0, 0.25, 0.5):
            with self.subTest(phase_offset=phase_offset):
                catalog = json.loads(make_catalog("set", self.tracks, phase_offset=phase_offset))
                self.assertEqual(catalog["phase_offset"], phase_offset)
        for phase_offset in (-0.500001, 0.500001, float("-inf"), float("inf"), float("nan")):
            with self.subTest(phase_offset=phase_offset):
                with self.assertRaisesRegex(ValueError, "phase offset"):
                    make_catalog("set", self.tracks, phase_offset=phase_offset)

    def test_large_catalog_uses_existing_bounded_frames(self):
        tracks = [{"id": str(i), "path": str(self.root / f"music/{i}.wav"),
                   "title": "Title " * 20} for i in range(1500)]
        catalog = make_catalog("set", tracks)
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
        catalog = make_catalog("set", self.tracks)
        with self.assertRaisesRegex(ValueError, "metadata"):
            encode_deck_message({"operation": "load_deck", "arguments": {
                "catalog": catalog, "name": "x" * 65536}}, 65536)
        tracks = [{"id": str(i), "path": str(self.root / f"{i}.wav"),
                   "title": "x" * 1024} for i in range(600)]
        with self.assertRaisesRegex(ValueError, "exceeds"):
            make_catalog("set", tracks)

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
        unsupported = self.root / "unsupported.json"
        unsupported.write_text("{}")
        with self.assertRaises(ValueError):
            audio_entry(unsupported)
        with self.assertRaises(FileNotFoundError):
            audio_entry(self.root / "absent.wav")

    def test_updates_retain_catalog_and_metadata_bounds(self):
        catalog = make_catalog("set", self.tracks)
        with self.assertRaisesRegex(ValueError, "metadata"):
            encode_deck_message({"operation": "update_deck_catalog", "arguments": {
                "catalog": catalog, "name": "x" * 65536}}, 65536)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            encode_deck_message({"operation": "update_deck_catalog", "arguments": {
                "catalog": "x" * (CATALOG_LIMIT + 1)}}, 65536)

    def test_song_state_precedence_and_transport_defaults(self):
        default = json.loads(make_catalog("set", self.tracks, cue=0.25))
        self.assertEqual(default["position"], 0.25)
        self.assertFalse(default["play"])
        state = {"position": 1.25, "cue": 0.5, "pitch": -2, "sync": False,
                 "phase_offset": -0.25, "play": True}
        catalog = json.loads(make_catalog("set", self.tracks, cue=0.75, position=0.8,
                                         song_states={"first": state}))
        self.assertEqual(catalog["song_states"], {"first": state})
        self.assertEqual({key: catalog[key] for key in state}, state)
        explicit = json.loads(make_catalog("set", self.tracks, cue=0.25, position=1, play=True))
        self.assertEqual(explicit["position"], 1)
        self.assertTrue(explicit["play"])

    def test_invalid_song_states_and_transport_values(self):
        state = {"position": 1, "cue": 0.5, "pitch": -2, "sync": False,
                 "phase_offset": -0.25, "play": True}
        for fields in ({"position": -1}, {"position": float("nan")},
                       {"position": float("inf")}, {"play": 1}, {"play": "true"},
                       {"song_states": []}, {"song_states": {"removed": state}},
                       {"song_states": {"first": {}}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                make_catalog("set", self.tracks, **fields)
        for field, value in (("position", -1), ("cue", -1), ("pitch", 17),
                             ("phase_offset", 0.51), ("sync", 1), ("play", "false")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                make_catalog("set", self.tracks, song_states={"first": {**state, field: value}})

    def test_full_status_maps_use_bounded_multipart_without_enlarging_metadata(self):
        state = {"position": 1.25, "cue": 0.5, "pitch": -2.0, "sync": False,
                 "phase_offset": -0.25, "play": True}
        identities = [f"song-{index:04d}-" + "x" * 64 for index in range(2000)]
        status = {
            "track_id": identities[0], "analyzing_track_id": identities[1],
            "song_states": {identity: dict(state) for identity in identities},
            "song_bpms": {identity: (None, 0.0, 123.5)[index % 3]
                          for index, identity in enumerate(identities)},
        }
        for field in ("song_states", "song_bpms"):
            self.assertGreater(len(pickle.dumps(status[field])), CONTROL_LIMIT)
        frames = deck_status_frames(status, CONTROL_LIMIT, STATE_LIMIT)
        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(frame) <= CONTROL_LIMIT for frame in frames))
        self.assertEqual(receive_deck_status(frames, CONTROL_LIMIT), status)
        message = ("updates", 1, 2, [("deck", 0, 1, status)])
        data = encode_deck_update(message, UPDATE_LIMIT, STATE_LIMIT)
        self.assertGreater(len(data), UPDATE_LIMIT)
        self.assertLessEqual(len(data), STATE_LIMIT)
        self.assertEqual(pickle.loads(data), message)
        for field in ("title", "analyzing_track_id", "other_map"):
            oversized = {**status, field: "x" * CONTROL_LIMIT}
            with self.subTest(field=field):
                with self.assertRaisesRegex(BufferError, "metadata"):
                    deck_status_frames(oversized, CONTROL_LIMIT, STATE_LIMIT)
                with self.assertRaisesRegex(BufferError, "metadata"):
                    encode_deck_update(
                        ("updates", 1, 2, [("deck", 0, 1, oversized)]),
                        UPDATE_LIMIT, STATE_LIMIT)
        with self.assertRaisesRegex(BufferError, "ordinary"):
            encode_deck_update(("updates", 1, 2, [("other", "x" * UPDATE_LIMIT)]),
                               UPDATE_LIMIT, STATE_LIMIT)
        with self.assertRaisesRegex(BufferError, "ordinary"):
            encode_deck_update(("updates", 1, 2, [
                ("deck", 0, 1, status), ("deck", 1, 1, status)]),
                UPDATE_LIMIT, STATE_LIMIT)

    def test_status_maps_share_one_exact_total_budget_including_update_envelope(self):
        status = {
            "track_id": "0", "analyzing_track_id": "",
            "song_states": {str(index): {"position": index} for index in range(100)},
            "song_bpms": {str(index): 120.0 for index in range(100)},
        }
        status_data = encode_deck_status(status, CONTROL_LIMIT, STATE_LIMIT)
        shared_limit = max(
            len(pickle.dumps({**status, field: {}}, protocol=pickle.HIGHEST_PROTOCOL))
            for field in ("song_states", "song_bpms"))
        with self.assertRaisesRegex(BufferError, "deck status exceeds"):
            encode_deck_status(status, CONTROL_LIMIT, shared_limit)
        self.assertEqual(
            encode_deck_status(status, CONTROL_LIMIT, len(status_data)), status_data)
        with self.assertRaisesRegex(BufferError, "deck status exceeds"):
            encode_deck_status(status, CONTROL_LIMIT, len(status_data) - 1)
        message = ("updates", 1, 2, [("deck", 0, 1, status)])
        update_data = encode_deck_update(message, UPDATE_LIMIT, STATE_LIMIT)
        self.assertGreater(len(update_data), len(status_data))
        self.assertEqual(
            encode_deck_update(message, UPDATE_LIMIT, len(update_data)), update_data)
        with self.assertRaisesRegex(BufferError, "deck update exceeds"):
            encode_deck_update(message, UPDATE_LIMIT, len(update_data) - 1)
