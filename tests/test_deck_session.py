"""Format-3 deck identity, paused restore, routed input and daemon commands."""

import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
import wave

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
        self.bank = self.root / "live set.json"
        self.entries = [{"id": name, "path": f"{name}.wav", "title": name.title()}
                        for name in ("first", "second")]
        self.write_bank(self.entries)
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

    def write_bank(self, entries):
        self.bank.write_text(json.dumps({"version": 1, "id": "live-set", "tracks": entries}))

    def await_status(self, host, index, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = host.engine.tracks[index].deck
            if predicate(status):
                return status
            time.sleep(0.01)
        self.fail(f"deck status did not converge: {host.engine.tracks[index].deck}")

    def test_inline_playlist_restores_live_position_and_play_without_manifest(self):
        host = self.host()
        host.load_deck(0, str(self.root), "A")
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
        self.assertEqual(entry["deck"]["phase_offset"], -0.375)
        self.assertTrue(entry["deck"]["play"])
        self.assertAlmostEqual(entry["deck"]["position"], 0.75)
        self.assertEqual(entry["deck"]["song_states"][selected]["position"], 0.75)
        self.assertNotIn("nudge", entry["deck"]["song_states"][selected])
        self.bank.unlink()
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
        self.assertFalse(track.plugin.values["sync"])
        self.assertEqual(status["nudge"], 0)
        self.assertAlmostEqual(status["pitch"], -1.25)
        self.assertEqual(status["phase_offset"], -0.375)
        self.assertEqual(track.plugin.values["phase_offset"], -0.375)
        self.assertAlmostEqual(status["cue"], 0.25)
        self.assertAlmostEqual(status["position"], 0.75, places=3)
        self.assertEqual(track.plugin.values["track_select"], 2)
        self.assertEqual(status["tracks"], entries)

    def test_old_session_defaults_to_sync_and_zero_phase_and_restores_paused(self):
        self.session.write_text(json.dumps({
            "version": 3, "tracks": [
                {"kind": "deck", "name": "Old", "path": str(self.bank),
                 "deck": {"bank_path": str(self.bank), "bank_id": "live-set",
                          "track_id": "first", "cue": 0, "pitch": 0}},
            ]}))
        host = self.host()
        host.restore_session(start_audio=False)
        status = self.await_status(host, 0, lambda state: state.get("state") == "ready")
        self.assertTrue(status["sync"])
        self.assertFalse(status["play"])
        self.assertEqual(status["phase_offset"], 0)
        self.assertEqual(host.engine.tracks[0].plugin.values["phase_offset"], 0)
        host.save_session()
        saved = json.loads(self.session.read_text())
        self.assertEqual(saved["tracks"][0]["deck"]["phase_offset"], 0)
        self.assertEqual(saved["tracks"][0]["deck"]["tracks"],
                         [{"id": entry["id"], "title": entry["title"],
                           "path": str((self.root / entry["path"]).resolve())}
                          for entry in self.entries])
        del saved["tracks"][0]["deck"]["phase_offset"]
        self.session.write_text(json.dumps(saved))
        self.bank.unlink()
        restored = self.host()
        restored.restore_session(start_audio=False)
        self.assertEqual(restored.engine.tracks[0].deck["track_id"], "first")
        self.assertEqual(restored.engine.tracks[0].deck["phase_offset"], 0)

    def test_synced_session_restores_selected_song_phase_offset(self):
        host = self.host()
        host.load_deck(0, str(self.root))
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
        host.load_deck(0, str(self.root))
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

    def test_large_song_map_propagates_through_live_status_channels(self):
        host = self.host()
        count = 1200
        host.restore_deck(0, {
            "bank_id": "large-set", "bank_path": "session:large-set", "track_id": "0",
            "tracks": [{"id": str(index), "path": str(self.root / "first.wav"),
                        "title": str(index)} for index in range(count)],
            "cue": 0, "pitch": 0,
        })
        host.engine.set_parameter(0, "phase_offset", 0.25)
        status = self.await_status(
            host, 0, lambda state: state.get("phase_offset") == 0.25
            and len(state.get("song_states", {})) == count)
        self.assertEqual(status["song_states"]["0"]["phase_offset"], 0.25)
        self.assertEqual(status["song_states"][str(count - 1)]["position"], 0)
        host.save_session()
        saved = json.loads(self.session.read_text())["tracks"][0]["deck"]
        self.assertEqual(len(saved["song_states"]), count)

    def test_invalid_phase_offset_restore_preserves_existing_deck(self):
        host = self.host()
        host.load_deck(0, str(self.root / "first.wav"))
        host.engine.set_parameter(0, "phase_offset", 0.25)
        host.engine.set_parameter(0, "nudge", 0.75)
        self.await_status(host, 0, lambda state: state.get("state") == "ready")
        previous = host.engine.tracks[0]
        fields = ("track_id", "cue", "pitch", "play", "sync", "nudge", "phase_offset")
        before = {key: previous.deck[key] for key in fields}
        inline = dict(snapshot(host)["tracks"][0]["deck"])
        legacy = {"bank_path": str(self.bank), "bank_id": "live-set",
                  "track_id": "second", "cue": 0.5, "pitch": -1}
        for deck in (inline, legacy):
            for phase_offset in (-0.500001, 0.500001, float("-inf"), float("inf"), float("nan")):
                with self.subTest(inline="tracks" in deck, phase_offset=phase_offset):
                    with self.assertRaisesRegex(ValueError, "phase offset"):
                        host.restore_deck(0, {**deck, "phase_offset": phase_offset}, "Replacement")
                    current = host.engine.tracks[0]
                    self.assertEqual(current.worker_pid, previous.worker_pid)
                    self.assertEqual(current.generation, previous.generation)
                    self.assertEqual(current.name, previous.name)
                    self.assertEqual({key: current.deck[key] for key in fields}, before)

    def test_missing_id_and_bank_identity_report_errors_but_restore_other_tracks(self):
        for bank_id, track_id, bank_path in (
                ("live-set", "removed", self.bank),
                ("wrong-bank", "first", self.bank),
                ("live-set", "first", self.root / "missing.json")):
            with self.subTest(bank_id=bank_id, track_id=track_id, bank_path=bank_path):
                self.session.write_text(json.dumps({
                    "version": 3, "tracks": [
                        {"kind": "deck", "name": "Missing", "path": str(self.bank),
                         "deck": {"bank_path": str(bank_path), "bank_id": bank_id,
                                  "track_id": track_id, "cue": 0, "pitch": 0}},
                        {"kind": "wav", "name": "Unaffected", "path": str(self.root / "first.wav")},
                    ]}))
                restored = self.host()
                with self.assertLogs("vcpi.sessions", level="WARNING") as messages:
                    restored.restore_session()
                self.assertIsNone(restored.engine.tracks[0])
                self.assertEqual(restored.engine.tracks[1].name, "Unaffected")
                self.assertTrue(any("track 1 (deck)" in line for line in messages.output))

    def test_snapshot_persists_committed_selection_not_requested(self):
        host = self.host()
        host.load_deck(0, str(self.root / "first.wav"))
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
        host.load_deck(0, str(self.root / "first.wav"))
        host.load_deck(1, str(self.root / "first.wav"))
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
        host.load_deck(0, str(self.root / "first.wav"))
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
        host.load_deck(0, str(self.root / "first.wav"))
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

            self.assertIn("track 1 = A", command(f'track 1 deck "{self.root / "first.wav"}" --name A'))
            first = host.engine.tracks[0]
            generation, pid = first.generation, first.worker_pid
            first_id = first.deck["track_id"]
            self.assertFalse(first.deck["play"])
            self.assertNotIn("Error:", command("set 1 play true"))
            self.assertNotIn("Error:", command(f'track 1 deck "{self.root / "second.wav"}"'))
            track = host.engine.tracks[0]
            self.assertEqual((track.generation, track.worker_pid), (generation, pid))
            self.assertEqual(track.name, "A")
            self.assertFalse(track.deck["play"])
            self.assertNotEqual(track.deck["track_id"], first_id)
            self.assertEqual(len(track.deck["tracks"]), 1)
            self.assertEqual(track.plugin.parameters["track_select"].range, (1, 1))
            self.assertEqual(list(track.plugin.parameters)[:3], ["track_select", "seek", "nudge"])
            self.assertNotIn("Error:", command(f'track 1 deck "{self.root}" --name Renamed'))
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
            self.assertIn("track_select", command("get 1"))
            self.assertIn("request accepted", command("set 1 track_select 2"))
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
