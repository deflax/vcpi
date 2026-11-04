"""Real spawned deck ownership and cache updates without audio devices."""

import json
import os
import struct
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from core.audio_process import RemoteAudioEngine
from core.audio_state import PluginSnapshot
from core.deck_bank import load_bank
from core.engine import AudioEngine
from core.models import InstrumentTrack, NUM_TRACKS
from core.track_process import TrackWorker


class DeckOwnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vcpi-deck-owner-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.wav = self.root / "tone.wav"
        with wave.open(str(self.wav), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(struct.pack("<h", 4096) * 48000)
        self.bank = self.root / "bank.json"
        self.write_bank()

    def write_bank(self, count=3):
        self.bank.write_text(json.dumps({"version": 1, "id": "set", "tracks": [
            {"id": str(i), "path": str(self.wav), "title": "Track " + str(i)}
            for i in range(1, count + 1)] + [
            {"id": "missing", "path": "absent.wav", "title": "Missing"}]}))

    def remote(self):
        engine = RemoteAudioEngine(buffer_size=64)
        self.addCleanup(engine.shutdown)
        return engine

    def worker(self, **restore):
        worker = TrackWorker(44100, 64, 0, "load_deck", {
            "catalog": load_bank(self.bank, **restore)})
        engine = AudioEngine(buffer_size=64)
        engine.publish_track(0, worker.track)

        def close():
            engine.clear_track(0)
            worker.shutdown()
            engine.shutdown()

        self.addCleanup(close)
        return worker, engine

    def await_status(self, worker, predicate, engine=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if engine is not None:
                engine.render_block()
                sequence = worker.transport.stats()["sequence"]
                while worker.transport.stats()["submitted"] < sequence:
                    if time.monotonic() >= deadline:
                        self.fail("deck render deadline expired")
                    time.sleep(0.001)
            worker.poll_deck_status()
            if predicate(worker.track.deck):
                return dict(worker.track.deck)
            time.sleep(0.005)
        self.fail(f"deck status deadline expired: {worker.track.deck}")

    def test_restore_prepares_saved_id_paused_without_rendering(self):
        worker, _ = self.worker(track_id="2", cue=0.25, pitch=-1.25)
        status = worker.track.deck
        self.assertEqual(status["track_id"], "2")
        self.assertAlmostEqual(status["position"], 0.25)
        self.assertAlmostEqual(status["cue"], 0.25)
        self.assertEqual(status["pitch"], -1.25)
        self.assertFalse(status["play"])
        self.assertEqual(status["nudge"], 0)
        reply = worker.request("sync_state")
        self.assertIsNone(reply["state"]["plugin"].native_state)

    def test_newest_selection_and_missing_asset_keep_worker_and_mixer(self):
        worker, engine = self.worker()
        pid = worker.pid
        engine.set_track_state(0, gain=0.375, muted=True)
        worker.request("set_parameter", name="pitch", value=2)
        worker.request("set_parameter", name="track_select", value=2)
        worker.request("set_parameter", name="track_select", value=3)
        status = self.await_status(worker, lambda s: s["track_id"] == "3", engine)
        self.assertFalse(status["play"])
        self.assertEqual(status["pitch"], 2)
        worker.request("set_parameter", name="track_select", value=4)
        status = self.await_status(worker, lambda s: bool(s["error"]), engine)
        self.assertEqual(status["track_id"], "3")
        self.assertEqual(status["requested_track_id"], "missing")
        self.assertEqual(worker.pid, pid)
        self.assertEqual(worker.track.gain, 0.375)
        self.assertTrue(worker.track.muted)

    def test_active_selection_cancels_pending_and_pause_freezes_cursor(self):
        worker, engine = self.worker()
        worker.request("set_parameter", name="track_select", value=2)
        worker.request("set_parameter", name="track_select", value=1)
        status = self.await_status(worker, lambda s: not s["loading"], engine)
        self.assertEqual(status["track_id"], "1")
        worker.request("set_parameter", name="play", value=True)
        self.await_status(worker, lambda s: s["position"] > 0, engine)
        worker.request("set_parameter", name="play", value=False)
        before = worker.request("sync_state")["state"]["deck"]["position"]
        for _ in range(8):
            engine.render_block()
            worker.request("sync_state")
        after = worker.request("sync_state")["state"]["deck"]["position"]
        self.assertAlmostEqual(after, before)

    def test_large_catalog_stages_across_both_framed_hops(self):
        self.write_bank(1800)
        engine = self.remote()
        catalog = load_bank(self.bank, track_id="1800")
        self.assertGreater(len(catalog.encode()), 65536)
        engine.request("load_deck", track_index=0, catalog=catalog)
        track = engine.tracks[0]
        self.assertEqual(track.deck["track_id"], "1800")
        self.assertEqual(len(track.deck["tracks"]), 1801)
        self.assertFalse(track.deck["play"])

    def test_failed_initial_load_retains_previous_source_and_generation(self):
        engine = self.remote()
        engine.request("load_wav", track_index=0, wav_path=str(self.wav))
        previous = engine.tracks[0]
        with self.assertRaises((RuntimeError, ValueError, FileNotFoundError)):
            engine.request("load_deck", track_index=0,
                           catalog=load_bank(self.bank, track_id="missing"))
        self.assertEqual(engine.tracks[0].worker_pid, previous.worker_pid)
        self.assertEqual(engine.tracks[0].generation, previous.generation)
        engine.set_track_state(0, gain=0.5)
        self.assertEqual(engine.tracks[0].gain, 0.5)

    def test_cached_status_updates_while_stopped_and_route_removal_clears_nudge(self):
        engine = self.remote()
        engine.request("load_deck", track_index=0, catalog=load_bank(self.bank))
        original = engine.tracks[0]
        engine.route(2, 0)
        engine.set_parameter(0, "play", True)
        engine.set_parameter(0, "nudge", 1)
        engine.unroute(2)
        with engine.state_lock:
            ready = engine._state_condition.wait_for(
                lambda: engine.tracks[0].deck["nudge"] == 0, timeout=5)
        self.assertTrue(ready)
        engine.set_parameter(0, "track_select", 4)
        with engine.state_lock:
            ready = engine._state_condition.wait_for(
                lambda: bool(engine.tracks[0].deck["error"]), timeout=5)
        self.assertTrue(ready)
        self.assertEqual(engine.tracks[0].deck["track_id"], "1")
        self.assertEqual(engine.tracks[0].worker_pid, original.worker_pid)
        self.assertEqual(engine.tracks[0].generation, original.generation)
        self.assertEqual(engine.tracks[0].deck["position"], 0)
        with patch.object(engine, "request", side_effect=AssertionError("unexpected RPC")):
            self.assertEqual(engine.tracks[0].deck["tracks"][0]["title"], "Track 1")

    @unittest.skipUnless(os.environ.get("DECK_TEST_EFFECT"), "set DECK_TEST_EFFECT to a native VST3 effect")
    def test_insert_native_state_survives_bank_selection(self):
        worker, engine = self.worker()
        worker.request("load_effect", path=os.environ["DECK_TEST_EFFECT"])
        before = worker.request("sync_state")["state"]["effects"][0]
        worker.request("set_parameter", name="track_select", value=2)
        self.await_status(worker, lambda s: s["track_id"] == "2", engine)
        after = worker.request("sync_state")["state"]["effects"][0]
        self.assertEqual(after.token, before.token)
        self.assertEqual(after.native_state, before.native_state)


class DeckCacheTests(unittest.TestCase):
    def test_old_epoch_and_generation_status_cannot_mutate_new_source(self):
        engine = RemoteAudioEngine.__new__(RemoteAudioEngine)
        engine._state_condition = threading.Condition()
        engine._state_listeners = []
        engine._epoch = 4
        engine._received_update = 0
        engine.tracks = [None] * NUM_TRACKS
        track = InstrumentTrack("deck", "bank", PluginSnapshot(1),
                                source_type="deck", generation=7,
                                deck={"track_id": "new", "position": 0})
        engine.tracks[0] = track
        engine._apply_updates(3, 1, [("deck", 0, 7, {"track_id": "old"})])
        engine._apply_updates(4, 2, [("deck", 0, 6, {"track_id": "old"})])
        self.assertEqual(track.deck["track_id"], "new")
        engine._apply_updates(4, 3, [("deck", 0, 7, {"position": 2, "pitch": -2})])
        self.assertEqual(track.deck["position"], 2)
        self.assertEqual(track.plugin.values["pitch"], -2)
