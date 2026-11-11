"""Format-3 deck identity, paused restore, routed input and daemon commands."""

import json
import pickle
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

import numpy as np

from vcpi.audio.protocol import CONTROL_LIMIT, STATE_LIMIT
from vcpi.controllers.midi_input import MidiInputController
from vcpi.client import _read_response_lines
from vcpi.deps import HAS_MIDO, mido
from vcpi.host import VcpiCore
from vcpi.server import VcpiServer
from vcpi.sessions.snapshot import snapshot


class DeckSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vcpi-deck-session-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, rate in (("first", 44100), ("second", 32000)):
            with wave.open(str(self.root / f"{name}.wav"), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(rate)
                audio.writeframes(struct.pack("<h", 4096) * (rate * 2))
        self.entries = [{"id": name, "path": str(self.root / f"{name}.wav"),
                         "title": name.title()} for name in ("first", "second")]
        self.session = self.root / "session.json"
        self.hosts = []
        self.addCleanup(self.close_hosts)

    def close_hosts(self):
        for host in reversed(self.hosts):
            host.shutdown(save=False)

    def host(self):
        host = VcpiCore(session_path=str(self.session))
        self.hosts.append(host)
        return host

    def pulse_song(self, bpm=120):
        rate = 16000
        audio = np.zeros(rate * 40)
        t = np.arange(int(0.025 * rate)) / rate
        pulse = 0.7 * np.exp(-t * 180) * np.cos(2 * np.pi * 800 * t)
        for beat in np.arange(0.137, 40 - 0.025, 60 / bpm):
            begin = round(beat * rate)
            audio[begin:begin + len(pulse)] += pulse
        song = self.root / "pulse.wav"
        with wave.open(str(song), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes((audio * 32767).astype("<i2").tobytes())
        return song

    def await_status(self, host, index, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = host.engine.tracks[index].deck
            if predicate(status):
                return status
            time.sleep(0.01)
        self.fail(f"deck status did not converge: {host.engine.tracks[index].deck}")

    def test_sync_filter_default_and_optout_survive_both_hops_and_session_restore(self):
        self.pulse_song()
        host = self.host()
        restored = self.host()
        for enabled in (True, False, True):
            with self.subTest(sync_filter=enabled):
                options = {} if enabled else {"sync_filter": False}
                track = host.load_deck(0, str(self.root), **options)
                self.assertIs(track.deck["sync_filter"], enabled)
                self.assertEqual([Path(entry["path"]).name for entry in track.deck["tracks"]],
                                 ["first.wav", "pulse.wav", "second.wav"])
                self.assertEqual(track.deck["index"], 2 if enabled else 1)
                self.assertFalse(track.deck["play"])
                if enabled:
                    self.assertGreater(track.deck["song_bpms"][track.deck["track_id"]], 0)
                host.save_session()
                data = json.loads(self.session.read_text())
                saved = data["tracks"][0]["deck"]
                self.assertEqual(data["version"], 3)
                self.assertIs(saved["sync_filter"], enabled)
                for field in ("song_bpms", "analyzing_track_id", "bpm", "beat_confidence"):
                    self.assertNotIn(field, saved)
                restored.restore_session(start_audio=False)
                self.assertIs(restored.engine.tracks[0].deck["sync_filter"], enabled)
                self.assertEqual(restored.engine.tracks[0].deck["track_id"], saved["track_id"])
                if enabled:
                    del saved["sync_filter"]
                    self.session.write_text(json.dumps(data))
                    restored.restore_session(start_audio=False)
                    self.assertIs(restored.engine.tracks[0].deck["sync_filter"], True)
                    self.assertIs(snapshot(restored)["tracks"][0]["deck"]["sync_filter"], True)

    def test_invalid_sync_filter_rejected_before_host_or_session_mutation(self):
        song = self.pulse_song()
        host = self.host()
        host.load_deck(0, str(song), "Original")
        host.engine.set_parameter(0, "play", True)
        host.engine.set_track_state(0, gain=0.375, muted=True)
        host.save_session()
        before = snapshot(host)
        deck = before["tracks"][0]["deck"]
        previous = host.engine.tracks[0]
        for invalid in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(sync_filter=invalid):
                with patch.object(host.engine, "request") as request, \
                        patch.object(host, "_clear_deck_holds") as clear_holds:
                    with self.assertRaisesRegex(ValueError, "sync_filter"):
                        host.load_deck(0, str(song), "Replacement", sync_filter=invalid)
                    with self.assertRaisesRegex(ValueError, "sync_filter"):
                        host.restore_deck(0, {**deck, "sync_filter": invalid}, "Replacement")
                    request.assert_not_called()
                    clear_holds.assert_not_called()
                malformed = {**before, "bpm": 135, "focus": 2, "master_gain": 0.1,
                             "tracks": [{**before["tracks"][0],
                                         "deck": {**deck, "sync_filter": invalid}}]}
                self.session.write_text(json.dumps(malformed))
                with patch.object(host, "stop_audio") as stop:
                    with self.assertRaisesRegex(ValueError, "sync_filter"):
                        host.restore_session(start_audio=False)
                    stop.assert_not_called()
                self.assertEqual(snapshot(host), before)
                current = host.engine.tracks[0]
                self.assertEqual((current.worker_pid, current.generation),
                                 (previous.worker_pid, previous.generation))

    def test_no_sync_ready_replacement_preserves_loaded_source_and_playback(self):
        song = self.pulse_song()
        host = self.host()
        host.load_deck(0, str(song), "Original")
        host.engine.set_parameter(0, "sync", False)
        host.engine.set_parameter(0, "play", True)
        host.engine.set_track_state(0, gain=0.375, muted=True)
        host.engine.sync_state()
        before = snapshot(host)
        previous = host.engine.tracks[0]
        deck = before["tracks"][0]["deck"]
        with self.assertRaisesRegex((ValueError, RuntimeError), "No sync-ready playlist entries"):
            host.load_deck(0, str(self.root / "first.wav"), "Replacement", bpm_hint=120)
        self.assertEqual(snapshot(host), before)
        with self.assertRaisesRegex((ValueError, RuntimeError), "No sync-ready playlist entries"):
            host.restore_deck(0, {**deck, "tracks": self.entries, "track_id": "first",
                                  "song_states": {}}, "Replacement")
        self.assertEqual(snapshot(host), before)
        current = host.engine.tracks[0]
        self.assertEqual((current.worker_pid, current.generation),
                         (previous.worker_pid, previous.generation))

    def test_inline_playlist_restores_live_position_and_play_without_manifest(self):
        host = self.host()
        host.load_deck(0, str(self.root), "A", sync_filter=False)
        entries = [dict(entry) for entry in host.engine.tracks[0].deck["tracks"]]
        selected = entries[1]["id"]
        host.restore_deck(0, {
            **host.engine.tracks[0].deck, "track_id": selected,
            "song_states": {}, "cue": 0.25, "position": 0.75, "pitch": -1.25,
        }, "A")
        host.route(3, 0)
        host.engine.set_track_state(0, gain=0.4, muted=True)
        host.engine.set_parameter(0, "play", True)
        host.engine.set_parameter(0, "nudge", 1)
        host.engine.set_parameter(0, "sync", False)
        host.engine.set_parameter(0, "phase_offset", -0.375)
        host.track_focus.select(5)
        host.save_session()
        saved = json.loads(self.session.read_text())
        entry = saved["tracks"][0]
        self.assertEqual(saved["version"], 3)
        self.assertEqual(saved["focus"], 6)
        self.assertNotIn("native_state", entry)
        self.assertEqual(entry["deck"]["tracks"], entries)
        self.assertEqual(entry["deck"]["track_id"], selected)
        self.assertEqual(entry["deck"]["cue"], 0.25)
        self.assertEqual(entry["deck"]["pitch"], -1.25)
        self.assertFalse(entry["deck"]["sync"])
        self.assertIs(entry["deck"]["sync_filter"], False)
        self.assertEqual(entry["deck"]["phase_offset"], -0.375)
        self.assertTrue(entry["deck"]["play"])
        self.assertAlmostEqual(entry["deck"]["position"], 0.75)
        self.assertEqual(entry["deck"]["song_states"][selected]["position"], 0.75)
        self.assertNotIn("nudge", entry["deck"]["song_states"][selected])
        restored = self.host()
        restored.restore_session()
        track = restored.engine.tracks[0]
        self.assertEqual(track.name, "A")
        self.assertAlmostEqual(track.gain, 0.4)
        self.assertTrue(track.muted)
        self.assertEqual(restored.channel_map, {3: 0})
        self.assertEqual(restored.track_focus.snapshot()[0], 5)
        status = self.await_status(restored, 0, lambda state: state.get("track_id") == selected)
        self.assertTrue(status["play"])
        self.assertFalse(status["sync"])
        self.assertIs(status["sync_filter"], False)
        self.assertFalse(track.plugin.values["sync"])
        self.assertEqual(status["nudge"], 0)
        self.assertAlmostEqual(status["pitch"], -1.25)
        self.assertEqual(status["phase_offset"], -0.375)
        self.assertEqual(track.plugin.values["phase_offset"], -0.375)
        self.assertAlmostEqual(status["cue"], 0.25)
        self.assertAlmostEqual(status["position"], 0.75, places=3)
        self.assertEqual(track.plugin.values["index"], 2)
        self.assertEqual(status["tracks"], entries)

    def test_mixed_bpm_hints_roundtrip_in_format_three(self):
        host = self.host()
        host.load_deck(0, str(self.root), bpm_hint=174, sync_filter=False)
        deck = snapshot(host)["tracks"][0]["deck"]
        entries = deck["tracks"]
        self.assertEqual([entry["bpm_hint"] for entry in entries], [174, 174])
        entries[1]["bpm_hint"] = 87.25
        entries.append({key: value for key, value in
                        {**entries[0], "id": "unhinted"}.items() if key != "bpm_hint"})
        host.restore_deck(0, deck)
        host.save_session()
        saved = json.loads(self.session.read_text())
        self.assertEqual(saved["version"], 3)
        self.assertEqual(saved["tracks"][0]["deck"]["tracks"], entries)
        self.assertNotIn("bpm_hint", saved["tracks"][0]["deck"])
        self.assertNotIn("bpm_hint", saved["tracks"][0]["deck"]["tracks"][2])

        restored = self.host()
        restored.restore_session(start_audio=False)
        self.assertEqual(restored.engine.tracks[0].deck["tracks"], entries)
        self.assertEqual(snapshot(restored)["tracks"][0]["deck"]["tracks"], entries)

    def test_unvisited_song_bpms_reach_host_cache_and_stay_out_of_sessions(self):
        song = self.pulse_song(83)
        entries = [
            dict(self.entries[0]),
            {"id": "slow", "path": str(song), "title": "Slow", "bpm_hint": 83},
            {"id": "fast", "path": str(song), "title": "Fast", "bpm_hint": 174},
            {"id": "missing", "path": str(self.root / "absent.wav"), "title": "Missing"},
        ]
        host = self.host()
        host.restore_deck(0, {
            "bank_id": "analysis", "bank_path": "session:analysis", "track_id": "first",
            "tracks": entries, "cue": 0.1, "pitch": 2, "position": 0.25,
            "play": False, "sync": False, "phase_offset": 0.125, "song_states": {},
            "sync_filter": False,
        })
        track = host.engine.tracks[0]
        identity = (track.worker_pid, track.generation)
        transport = {key: track.deck[key] for key in (
            "track_id", "requested_track_id", "position", "cue", "pitch", "play",
            "sync", "phase_offset", "bpm")}
        ids = {entry["id"] for entry in entries}
        # Only read the control cache while both native owners publish asynchronously.
        with patch.object(host.engine, "request", side_effect=AssertionError("unexpected RPC")):
            status = self.await_status(
                host, 0, lambda state: set(state.get("song_bpms", {})) == ids
                and all(value is not None for value in state["song_bpms"].values())
                and state.get("analyzing_track_id") == "", timeout=15)
        self.assertEqual({key: status[key] for key in transport}, transport)
        self.assertEqual((track.worker_pid, track.generation), identity)
        self.assertEqual(status["song_bpms"]["first"], 0)
        self.assertEqual(status["song_bpms"]["missing"], 0)
        self.assertAlmostEqual(status["song_bpms"]["slow"], 83, delta=0.2)
        self.assertAlmostEqual(status["song_bpms"]["fast"], 166, delta=0.2)
        self.assertEqual(status["tracks"], entries)
        self.assertFalse(status["error"])
        self.assertFalse(track.worker_failure)
        expected = dict(status["song_bpms"])
        host.save_session()
        self.assertEqual(host.engine.tracks[0].deck["song_bpms"], expected)
        self.assertEqual(host.engine.tracks[0].deck["analyzing_track_id"], "")
        saved = json.loads(self.session.read_text())["tracks"][0]["deck"]
        self.assertEqual(saved["tracks"], entries)
        for field in ("song_bpms", "analyzing_track_id", "bpm", "beat_confidence"):
            self.assertNotIn(field, saved)
            self.assertTrue(all(field not in entry for entry in saved["tracks"]))
        restored = self.host()
        restored.restore_session(start_audio=False)
        completed = self.await_status(
            restored, 0, lambda state: state.get("song_bpms") == expected
            and state.get("analyzing_track_id") == "", timeout=15)
        self.assertEqual(completed["tracks"], entries)
        self.assertEqual(completed["track_id"], "first")

    def test_malformed_bpm_hints_preserve_existing_deck_on_load_and_restore(self):
        host = self.host()
        host.load_deck(0, str(self.root / "first.wav"), "Original", bpm_hint=174,
                       sync_filter=False)
        self.await_status(host, 0, lambda state: state.get("state") == "ready")
        host.engine.set_track_state(0, gain=0.375, muted=True)
        previous = host.engine.tracks[0]
        deck = snapshot(host)["tracks"][0]["deck"]
        for hint in (True, False, "174", [], {}, 59.999, 200.001,
                     float("nan"), float("inf"), float("-inf"), None):
            with self.subTest(hint=hint):
                if hint is not None:
                    with self.assertRaisesRegex(ValueError, "BPM hint"):
                        host.load_deck(0, str(self.root / "second.wav"),
                                       "Replacement", bpm_hint=hint)
                malformed = {**deck, "tracks": [
                    {**entry, "bpm_hint": hint} for entry in deck["tracks"]]}
                with self.assertRaisesRegex(ValueError, "BPM hint"):
                    host.restore_deck(0, malformed, "Replacement")
                current = host.engine.tracks[0]
                self.assertEqual((current.worker_pid, current.generation),
                                 (previous.worker_pid, previous.generation))
                self.assertEqual(current.name, "Original")
                self.assertEqual(current.gain, 0.375)
                self.assertTrue(current.muted)
                self.assertEqual(snapshot(host)["tracks"][0]["deck"], deck)

    def test_synced_session_restores_selected_song_phase_offset(self):
        host = self.host()
        host.load_deck(0, str(self.root), sync_filter=False)
        selected = host.engine.tracks[0].deck["tracks"][1]["id"]
        # Restore the desired selection directly: stopped output cannot commit a queued switch.
        host.restore_deck(0, {**host.engine.tracks[0].deck, "track_id": selected})
        host.engine.set_parameter(0, "phase_offset", 0.5)
        host.save_session()
        saved = json.loads(self.session.read_text())
        self.assertEqual(saved["tracks"][0]["deck"]["track_id"], selected)
        self.assertEqual(saved["tracks"][0]["deck"]["phase_offset"], 0.5)
        restored = self.host()
        restored.restore_session(start_audio=False)
        status = self.await_status(restored, 0, lambda state: state.get("state") == "ready")
        self.assertEqual(status["track_id"], selected)
        self.assertEqual(status["phase_offset"], 0.5)
        self.assertTrue(status["sync"])
        self.assertFalse(status["play"])
        self.assertEqual(restored.engine.tracks[0].plugin.values["phase_offset"], 0.5)

    def test_all_song_states_roundtrip_with_selected_live_overlay(self):
        host = self.host()
        host.load_deck(0, str(self.root), sync_filter=False)
        deck = host.engine.tracks[0].deck
        first, second = (entry["id"] for entry in deck["tracks"])
        states = {
            first: {"position": 0.4, "cue": 0.1, "pitch": -3,
                    "sync": False, "phase_offset": -0.2, "play": True},
            second: {"position": 1.2, "cue": 0.6, "pitch": 4,
                     "sync": True, "phase_offset": 0.3, "play": False},
        }
        host.restore_deck(0, {**deck, "track_id": second, "song_states": states,
                              "position": 0, "cue": 0, "pitch": 0, "play": True})
        status = host.engine.tracks[0].deck
        self.assertEqual(status["song_states"], states)
        self.assertEqual({key: status[key] for key in states[second]}, states[second])
        host.engine.set_parameter(0, "pitch", 5)
        host.engine.set_parameter(0, "phase_offset", 0.4)
        host.engine.set_parameter(0, "nudge", -1)
        host.save_session()
        expected = {first: states[first], second: {**states[second], "pitch": 5,
                                                   "phase_offset": 0.4}}
        saved = json.loads(self.session.read_text())["tracks"][0]["deck"]
        self.assertEqual(saved["song_states"], expected)
        restored = self.host()
        restored.restore_session(start_audio=False)
        current = restored.engine.tracks[0].deck
        self.assertEqual(current["song_states"], expected)
        self.assertEqual(current["nudge"], 0)
        self.assertEqual(current["track_id"], second)
        # Reload the other identity to exercise constructor map precedence.
        restored.restore_deck(0, {**saved, "track_id": first})
        current = restored.engine.tracks[0].deck
        self.assertEqual({key: current[key] for key in states[first]}, states[first])
        self.assertEqual(current["song_states"], expected)

    def test_large_song_maps_propagate_through_live_status_channels(self):
        host = self.host()
        count = 1200
        identities = [f"song-{index:04d}-" + "x" * 64 for index in range(count)]
        host.restore_deck(0, {
            "bank_id": "large-set", "bank_path": "session:large-set", "track_id": identities[0],
            "tracks": [{"id": identity, "path": str(self.root / "first.wav"),
                        "title": str(index)} for index, identity in enumerate(identities)],
            "cue": 0, "pitch": 0, "position": 0, "play": False,
            "sync": True, "phase_offset": 0, "song_states": {},
            "sync_filter": False,
        })
        host.engine.set_parameter(0, "phase_offset", 0.25)
        status = self.await_status(
            host, 0, lambda state: state.get("phase_offset") == 0.25
            and len(state.get("song_states", {})) == count
            and len(state.get("song_bpms", {})) == count)
        self.assertEqual(status["song_states"][identities[0]]["phase_offset"], 0.25)
        self.assertEqual(status["song_states"][identities[-1]]["position"], 0)
        self.assertEqual(set(status["song_bpms"]), set(identities))
        self.assertIn("analyzing_track_id", status)
        self.assertFalse(host.engine.tracks[0].worker_failure)
        for field in ("song_states", "song_bpms"):
            self.assertGreater(len(pickle.dumps(status[field])), CONTROL_LIMIT)
        self.assertLess(len(pickle.dumps(status)), STATE_LIMIT)
        host.save_session()
        saved = json.loads(self.session.read_text())["tracks"][0]["deck"]
        self.assertEqual(len(saved["song_states"]), count)
        self.assertNotIn("song_bpms", saved)
        self.assertNotIn("analyzing_track_id", saved)
        captured = host.engine.tracks[0].deck
        self.assertEqual(set(captured["song_bpms"]), set(identities))
        self.assertIn("analyzing_track_id", captured)

    def test_invalid_phase_offset_restore_preserves_existing_deck(self):
        host = self.host()
        host.load_deck(0, str(self.root / "first.wav"), sync_filter=False)
        host.engine.set_parameter(0, "phase_offset", 0.25)
        host.engine.set_parameter(0, "nudge", 0.75)
        self.await_status(host, 0, lambda state: state.get("state") == "ready")
        previous = host.engine.tracks[0]
        fields = ("track_id", "cue", "pitch", "play", "sync", "nudge", "phase_offset")
        before = {key: previous.deck[key] for key in fields}
        deck = dict(snapshot(host)["tracks"][0]["deck"])
        for phase_offset in (-0.500001, 0.500001, float("-inf"), float("inf"), float("nan")):
            with self.subTest(phase_offset=phase_offset):
                with self.assertRaisesRegex(ValueError, "phase offset"):
                    host.restore_deck(0, {**deck, "phase_offset": phase_offset}, "Replacement")
                current = host.engine.tracks[0]
                self.assertEqual(current.worker_pid, previous.worker_pid)
                self.assertEqual(current.generation, previous.generation)
                self.assertEqual(current.name, previous.name)
                self.assertEqual({key: current.deck[key] for key in fields}, before)

    def test_invalid_inline_decks_report_errors_but_restore_other_tracks(self):
        deck = {"bank_path": "session:live-set", "bank_id": "live-set",
                "tracks": self.entries, "track_id": "first", "cue": 0, "pitch": 0,
                "position": 0, "play": False, "sync": True, "phase_offset": 0,
                "song_states": {}, "sync_filter": False}
        for fields in (
                {"track_id": "removed"},
                {"bank_id": ""},
                {"track_id": ""},
                {"tracks": [{"id": "first", "path": str(self.root / "missing.wav")}]},
                {"tracks": []}):
            with self.subTest(fields=fields):
                self.session.write_text(json.dumps({
                    "version": 3, "tracks": [
                        {"kind": "deck", "name": "Invalid", "deck": {**deck, **fields}},
                        {"kind": "deck", "name": "Unaffected", "deck": deck},
                    ]}))
                restored = self.host()
                with self.assertLogs("vcpi.sessions", level="WARNING") as messages:
                    restored.restore_session(start_audio=False)
                self.assertIsNone(restored.engine.tracks[0])
                self.assertEqual(restored.engine.tracks[1].name, "Unaffected")
                self.assertEqual(restored.engine.tracks[1].deck["track_id"], "first")
                self.assertTrue(any("track 1 (deck)" in line for line in messages.output))

    def test_manifest_only_deck_is_rejected_without_reading_bank(self):
        bank = self.root / "live set.json"
        bank.write_text(json.dumps({"version": 1, "id": "live-set", "tracks": self.entries}))
        deck = {"bank_path": str(bank), "bank_id": "live-set", "track_id": "first",
                "cue": 0, "pitch": 0, "position": 0, "play": False,
                "sync": True, "phase_offset": 0, "song_states": {}, "sync_filter": False}
        self.session.write_text(json.dumps({
            "version": 3, "tracks": [
                {"kind": "deck", "name": "Manifest", "path": str(bank), "deck": deck},
                {"kind": "deck", "name": "Unaffected",
                 "deck": {**deck, "tracks": self.entries}},
            ]}))
        restored = self.host()
        open_path = Path.open

        def open_without_bank(path, *args, **kwargs):
            self.assertNotEqual(path, bank, "session restore must not read bank manifests")
            return open_path(path, *args, **kwargs)

        with patch.object(Path, "open", open_without_bank):
            with self.assertLogs("vcpi.sessions", level="WARNING") as messages:
                restored.restore_session(start_audio=False)
        self.assertIsNone(restored.engine.tracks[0])
        self.assertEqual(restored.engine.tracks[1].name, "Unaffected")
        self.assertEqual(restored.engine.tracks[1].deck["track_id"], "first")
        self.assertTrue(any("deck tracks are required" in line for line in messages.output))

    def test_snapshot_persists_committed_selection_not_requested(self):
        host = self.host()
        host.load_deck(0, str(self.root / "first.wav"), sync_filter=False)
        selected = host.engine.tracks[0].deck["track_id"]
        with host.engine.state_lock:
            host.engine.tracks[0].deck.update(requested_track_id="second", state="loading")
            saved = snapshot(host)
        self.assertEqual(saved["tracks"][0]["deck"]["track_id"], selected)
        self.assertNotIn("requested_track_id", saved["tracks"][0]["deck"])
        self.assertNotIn("native_state", saved["tracks"][0])

    @unittest.skipUnless(HAS_MIDO, "mido required for routed MIDI")
    def test_real_routed_midi_disconnect_unroute_and_replacement_clear_nudge(self):
        host = self.host()
        host.load_deck(0, str(self.root / "first.wav"), sync_filter=False)
        host.load_deck(1, str(self.root / "first.wav"), sync_filter=False)
        host.route(0, 0)
        host.route(1, 1)
        controller = MidiInputController(host.engine)
        host.midi_inputs.append(controller)

        def send(raw):
            controller.on_midi((raw, 0.0))
            host.engine.sync_state()

        send(mido.Message("control_change", channel=0, control=20, value=127).bytes())
        self.await_status(host, 0, lambda state: state.get("pitch") == 16)
        self.assertEqual(host.engine.tracks[1].deck["pitch"], 0)
        send(mido.Message("note_on", channel=0, note=64, velocity=127).bytes())
        self.await_status(host, 0, lambda state: state.get("nudge") == 1)
        self.assertEqual(host.engine.tracks[1].deck["nudge"], 0)
        send(mido.Message("note_on", channel=0, note=64, velocity=0).bytes())
        self.await_status(host, 0, lambda state: state.get("nudge") == 0)
        send([0x90, 63, 127])
        host.unroute(0)
        host.engine.sync_state()
        self.await_status(host, 0, lambda state: state.get("nudge") == 0)
        host.route(0, 0)
        send([0x90, 64, 127])
        controller.close()
        host.engine.sync_state()
        self.await_status(host, 0, lambda state: state.get("nudge") == 0)
        send([0x90, 63, 127])
        old_generation = host.engine.tracks[0].generation
        host.remove_instrument(0)
        host.load_deck(0, str(self.root / "first.wav"), sync_filter=False)
        self.assertNotEqual(host.engine.tracks[0].generation, old_generation)
        self.assertEqual(host.engine.tracks[0].deck["nudge"], 0)
        send([0x90, 64, 127])
        self.await_status(host, 0, lambda state: state.get("nudge") == 1)
        send([0x80, 63, 0])  # late release from old generation cannot clear new hold
        self.assertEqual(host.engine.tracks[0].deck["nudge"], 1)
        send([0x80, 64, 0])
        self.await_status(host, 0, lambda state: state.get("nudge") == 0)

    @unittest.skipUnless(HAS_MIDO, "mido required for controller MIDI")
    def test_generic_reroute_preserves_dedicated_controller_nudge(self):
        host = self.host()
        host.load_deck(0, str(self.root / "first.wav"), sync_filter=False)
        profile = host.beatstep.profile
        status = (0x90 if profile.pad_type == "note" else 0xB0) | profile.control_channel
        host.beatstep.on_midi(([status, profile.pad_numbers[4], 127], 0))
        host.engine.sync_state()
        self.await_status(host, 0, lambda state: state.get("nudge") == -1)
        host.route(profile.control_channel, 1)
        host.unroute(profile.control_channel)
        host.engine.sync_state()
        self.assertEqual(host.engine.tracks[0].deck["nudge"], -1)
        host.beatstep.on_midi(([status, profile.pad_numbers[4], 0], 0))
        host.engine.sync_state()
        self.await_status(host, 0, lambda state: state.get("nudge") == 0)


    def test_commands_use_real_daemon_client_framing(self):
        host = self.host()
        server = VcpiServer(host)
        sock, peer = socket.socketpair()
        sock.settimeout(5)
        reader = sock.makefile("r", encoding="utf-8")
        writer = sock.makefile("w", encoding="utf-8")
        handler = threading.Thread(target=server._handle_client, args=(peer,), daemon=True)
        handler.start()
        try:
            self.assertIsNotNone(_read_response_lines(reader))

            def command(text):
                writer.write(text + "\n")
                writer.flush()
                pending = server._cmd_queue.get(timeout=5)
                server._cmd_queue.put(pending)
                server._drain_commands()
                return "\n".join(_read_response_lines(reader))

            self.assertIn("track 1 = A", command(
                f'track 1 deck "{self.root / "first.wav"}" --no-sync-filter --name A'))
            first = host.engine.tracks[0]
            generation, pid = first.generation, first.worker_pid
            first_id = first.deck["track_id"]
            self.assertFalse(first.deck["play"])
            self.assertIs(first.deck["sync_filter"], False)
            self.assertNotIn("Error:", command("set 1 play true"))
            self.assertNotIn("Error:", command(
                f'track 1 deck "{self.root / "second.wav"}" --no-sync-filter'))
            track = host.engine.tracks[0]
            self.assertEqual((track.generation, track.worker_pid), (generation, pid))
            self.assertEqual(track.name, "A")
            self.assertFalse(track.deck["play"])
            self.assertNotEqual(track.deck["track_id"], first_id)
            self.assertEqual(len(track.deck["tracks"]), 1)
            self.assertEqual(track.plugin.parameters["index"].range, (1, 1))
            self.assertEqual(list(track.plugin.parameters)[:3], ["index", "seek", "nudge"])
            self.assertNotIn("Error:", command(
                f'track 1 deck "{self.root}" --name Renamed --no-sync-filter'))
            first_id = host.engine.tracks[0].deck["track_id"]
            self.assertEqual(host.engine.tracks[0].name, "Renamed")
            self.assertIn("pitch =", command("set 1 pitch -1.25"))
            self.assertIn("seek accepted", command("track 1 deck seek 0.5"))
            self.assertIn("cue_set accepted", command("track 1 deck cue set"))
            self.assertIn("cue accepted", command("track 1 deck cue"))
            status_text = command("show 1 --details")
            # Plain show wraps long playlist paths into the third column.
            paths = "".join(line.rsplit("|", 1)[-1].strip()
                            for line in status_text.splitlines() if "|" in line)
            self.assertIn(str(self.root / "first.wav"), paths)
            self.assertIn(str(self.root / "second.wav"), paths)
            self.assertIn("index", command("get 1"))
            self.assertIn("request accepted", command("set 1 index 2"))
            selected = host.engine.tracks[0].deck["tracks"][1]["id"]
            status = self.await_status(host, 0, lambda state: state.get("requested_track_id") == selected)
            self.assertEqual(status["track_id"], first_id)
            self.assertNotIn("Error:", command("track 1 clear"))
            self.assertIsNone(host.engine.tracks[0])
        finally:
            sock.shutdown(socket.SHUT_RDWR)
            writer.close()
            reader.close()
            sock.close()
            handler.join(timeout=5)
