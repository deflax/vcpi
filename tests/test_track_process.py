"""Fixed handoff invariants and real spawned deck ownership, without devices."""

import multiprocessing as mp
import os
import signal
import struct
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from functools import partial, wraps
from multiprocessing.connection import wait as wait_connections
from multiprocessing.reduction import DupFd
from unittest.mock import patch

import mido
import numpy as np

from vcpi.audio_state import PluginSnapshot
from vcpi.audio_deps import require_native
from vcpi.audio.control import AudioEngine
from vcpi.audio.track_client import TrackWorker
from vcpi.audio.track_protocol import MIDI_CAPACITY, TrackProcessError
from vcpi.audio.track_owner import _track_owner, _watch_parent
from vcpi.deck_bank import make_catalog


def _both_wakeup_modes(test):
    @wraps(test)
    def run(self):
        for mode in ("notify", "poll"):
            with self.subTest(wakeup_mode=mode), patch.dict(
                    os.environ, {"VCPI_TRACK_WAKEUP": mode}):
                test(self)
    return run


def _track_owner_with_wakeup_env(mode, *args):
    # A real spawned owner must ignore even an invalid joining environment.
    os.environ["VCPI_TRACK_WAKEUP"] = mode
    _track_owner(*args)


def _own_track_for_orphan_test(connection, catalog):
    worker = TrackWorker(44100, 64, 2, "load_deck", {"catalog": catalog})
    try:
        connection.send((worker.pid, DupFd(worker._process.sentinel)))
        connection.recv()
    finally:
        worker.shutdown()
        connection.close()


def _watch_lifeline_for_cleanup_test(connection, lifeline, shared):
    stopping = threading.Event()
    watcher = threading.Thread(target=_watch_parent,
                               args=(lifeline, shared, stopping), daemon=True)
    watcher.start()
    try:
        connection.send("watching")
        connection.recv()
        stopping.set()
        # The parent has already closed the writer; cleanup must still finish.
        time.sleep(0.1)
        watcher.join(2)
        connection.send(("cleanup completed", watcher.is_alive()))
    finally:
        lifeline.close()
        connection.close()


class NativeTransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vcpi-transport-test-")
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "audio.mmap")
        backend = require_native()
        self.transport = backend.Transport(self.path, 64, True)
        self.peer = backend.Transport(self.path, 64, False)
        self.addCleanup(self.close)

    def close(self):
        for transport in (self.peer, self.transport):
            if hasattr(transport, "close"):
                transport.close()

    def test_midi_is_shared_and_full_fifo_reports_overflow(self):
        self.transport.send_midi(bytes((0x90, 60, 127)))
        self.assertEqual(self.peer.stats()["midi_pending"], 1)
        for _ in range(MIDI_CAPACITY - 1):
            self.transport.send_midi(bytes((0x80, 60, 0)))
        with self.assertRaises((BufferError, RuntimeError)):
            self.transport.send_midi(bytes((0x80, 60, 0)))
        self.assertEqual(self.peer.stats()["midi_pending"], MIDI_CAPACITY)


    def test_unconsumed_native_requests_are_silent_not_stale(self):
        engine = AudioEngine(buffer_size=64)
        self.addCleanup(engine.shutdown)
        engine._native.attach(0, self.transport, 1.0, False, False)
        for _ in range(8):
            np.testing.assert_array_equal(engine.render_block(), 0)
        stats = self.transport.stats()
        self.assertEqual(stats["sequence"], 8)
        self.assertGreaterEqual(stats["misses"], 7)

    def test_oversized_midi_is_rejected_before_publication(self):
        with self.assertRaises((ValueError, BufferError, RuntimeError)):
            self.transport.send_midi(bytes((0xf0,)) + bytes(1023) + bytes((0xf7,)))
        self.assertEqual(self.peer.stats()["midi_pending"], 0)

    def test_invalid_creation_mode_does_not_create_or_modify_transport(self):
        backend = require_native()
        existing = Path(self.path).read_bytes()
        missing = Path(self.path + ".invalid")
        for value in ("", "Notify", "POLL", "poll ", " notify", "1"):
            with self.subTest(value=value), patch.dict(
                    os.environ, {"VCPI_TRACK_WAKEUP": value}):
                with self.assertRaisesRegex(ValueError, "VCPI_TRACK_WAKEUP"):
                    backend.Transport(str(missing), 64, True)
                self.assertFalse(missing.exists())
                with self.assertRaisesRegex(ValueError, "VCPI_TRACK_WAKEUP"):
                    backend.Transport(self.path, 128, True)
                self.assertEqual(Path(self.path).read_bytes(), existing)
                # Invalid environment is a creator error, never a joiner error.
                peer = backend.Transport(self.path, 64, False)
                self.assertEqual(peer.stats()["wakeup_mode"],
                                 self.transport.stats()["wakeup_mode"])
        self.transport.send_midi(bytes((0x90, 60, 127)))
        self.assertEqual(self.peer.stats()["midi_pending"], 1)

    @_both_wakeup_modes
    def test_requests_survive_prepublication_and_coalescing(self):
        backend = require_native()
        path = self.path + "." + os.environ["VCPI_TRACK_WAKEUP"]
        transport = backend.Transport(path, 64, True)
        peer = backend.Transport(path, 64, False)
        sample_path = Path(self.path).with_suffix(".wav")
        with wave.open(str(sample_path), "wb") as sample:
            sample.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            sample.writeframes(struct.pack("<h", 4096) * 44100)
        catalog = make_catalog("wakeup-test", [
            {"id": "tone", "path": str(sample_path), "title": "Tone"},
        ], play=True, sync=False, sync_filter=False)
        plugin = backend.Plugin(catalog, 44100, 64, "deck")
        engine = backend.Engine(44100, 64)
        self.addCleanup(engine.shutdown)
        engine.attach(0, transport, 1.0, False, False)
        # Publish before a renderer even exists. Waiting must consume this
        # durable notification without requiring a second callback or barrier.
        engine.render_block()
        runner = backend.TrackRunner(peer, plugin, 44100, 64)
        self.addCleanup(runner.shutdown)

        def await_sequence(sequence):
            deadline = time.monotonic() + 5
            while transport.stats()["submitted"] < sequence:
                self.assertLess(time.monotonic(), deadline,
                                "a published request lost its wakeup")
                time.sleep(0.001)

        await_sequence(1)
        position = plugin.deck_status()["position"]
        runner.barrier()
        self.assertEqual(plugin.deck_status()["position"], position)
        for _ in range(4):
            # Several notifications may collapse into one; the latest request
            # must still complete after publication stops.
            for _ in range(64):
                engine.render_block()
            sequence = transport.stats()["sequence"]
            await_sequence(sequence)
            self.assertEqual(transport.stats()["submitted"], sequence)
            runner.barrier()
        self.assertEqual(transport.stats()["failure"], "")
        progress = transport.stats()["progress"]
        transport.send_midi(bytes((0x90, 60, 127)))
        runner.barrier()
        self.assertEqual(transport.stats()["midi_pending"], 1)
        self.assertEqual(transport.stats()["progress"], progress)
        runner.set_effects([])
        offline = runner.render_once()
        self.assertEqual(offline.shape, (64, 2))
        self.assertTrue(np.isfinite(offline).all())
        self.assertGreater(np.max(np.abs(offline)), 0)
        runner.barrier()
        self.assertEqual(transport.stats()["midi_pending"], 0)
        self.assertEqual(transport.stats()["midi_consumed"], 1)
        self.assertEqual(transport.stats()["progress"], progress + 1)
        runner.shutdown()
        self.assertEqual(transport.stats()["renderer_pid"], 0)



class SpawnedTrackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vcpi-track-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sample = self.root / "sample.wav"
        with wave.open(str(self.sample), "wb") as sample:
            sample.setnchannels(1)
            sample.setsampwidth(2)
            sample.setframerate(44100)
            sample.writeframes(struct.pack("<h", 4096) * 4096)
        self.catalog = make_catalog("process-test", [
            {"id": "audio", "path": str(self.sample), "title": "Audio"},
        ], play=True, sync=False, sync_filter=False)

    def worker(self, **kwargs):
        worker = TrackWorker(44100, 64, 2, "load_deck",
                             {"catalog": self.catalog, "name": "remote"}, **kwargs)
        engine = AudioEngine(buffer_size=64)
        engine.publish_track(2, worker.track)
        engine.set_track_state(2, gain=1.0)
        worker.test_engine = engine

        def close():
            engine.clear_track(2)
            worker.shutdown()
            engine.shutdown()

        self.addCleanup(close)
        return worker

    def render(self, worker, messages=()):
        for message in messages:
            worker.test_engine.enqueue_midi(2, message)
        return worker.test_engine.render_block()

    def await_render(self, worker, sequence):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if worker.transport.stats()["submitted"] >= sequence:
                return
            if worker.poll_failure():
                self.fail(worker.failure)
            time.sleep(0.001)
        self.fail("spawned track did not finish the offered block")

    def settled_audio(self, worker):
        # Real decks fade in; don't mistake their startup envelope for mixer gain.
        for _ in range(16):
            self.await_render(worker, worker.transport.stats()["sequence"])
            audio = self.render(worker)
        return audio

    @_both_wakeup_modes
    def test_persistent_native_owner_and_one_block_deck_audio(self):
        worker = self.worker()
        self.assertNotEqual(worker.pid, os.getpid())
        self.assertFalse(worker._process.daemon)
        self.assertIsInstance(worker.track.plugin, PluginSnapshot)
        self.assertIs(worker.track.render_owner, worker)
        np.testing.assert_array_equal(
            self.render(worker, [mido.Message("note_on", note=60, velocity=127)]), 0)
        self.await_render(worker, 1)
        audio = self.render(worker)
        self.assertIsNotNone(audio)
        self.assertEqual(audio.shape, (64, 2))
        self.assertTrue(np.isfinite(audio).all())
        # Allow the decoder's resampling filter gain; this checks IPC audio,
        # not the exact filter coefficients.
        np.testing.assert_allclose(self.settled_audio(worker), 0.125, rtol=0.02)
        pid = worker.pid
        self.await_render(worker, worker.transport.stats()["sequence"])
        reply = worker.request("sync_state")
        self.assertEqual(worker.pid, pid)
        self.assertIsInstance(reply["state"]["plugin"], PluginSnapshot)
        worker.test_engine.clear_track(2)
        worker.shutdown()
        worker.shutdown()
        self.assertFalse(worker._process.is_alive())
        self.assertFalse(worker._io_thread.is_alive())
        self.assertEqual(worker._process.exitcode, 0)

    @_both_wakeup_modes
    def test_native_timing_is_opt_in_and_cumulative_across_processes(self):
        worker = self.worker()
        np.testing.assert_allclose(self.settled_audio(worker), 0.125, rtol=0.02)
        self.await_render(worker, worker.transport.stats()["sequence"])
        initial = worker.transport.stats()
        self.assertEqual(initial["wakeup_mode"], os.environ["VCPI_TRACK_WAKEUP"])
        self.assertEqual(initial["renderer_pid"], worker.pid)
        self.assertFalse(initial["timing_enabled"])
        self.assertTrue(initial["timing"]["coherent"])
        self.assertEqual(initial["timing"]["count"], 0)
        self.assertEqual(initial["timing"]["wakeup_ns_sum"], 0)
        self.assertEqual(initial["timing"]["render_ns_sum"], 0)
        self.assertEqual(initial["timing"]["render_cpu_ns_sum"], 0)
        self.assertEqual(initial["timing"]["cpu_clock_errors"], 0)
        self.assertEqual(initial["timing"]["wakeup_histogram"], [0] * 64)
        self.assertEqual(initial["timing"]["render_histogram"], [0] * 64)
        self.assertEqual(initial["timing"]["render_cpu_histogram"], [0] * 64)

        expected_count = 0
        previous = initial["timing"]
        for enabled, blocks in ((True, 4), (False, 3), (True, 3), (True, 2), (False, 2)):
            with self.subTest(enabled=enabled, blocks=blocks):
                worker.transport.set_timing_enabled(enabled)
                # Enabling (even repeatedly) must not reset an accumulated sample.
                self.assertEqual(worker.transport.stats()["timing"], previous)
                for _ in range(blocks):
                    np.testing.assert_allclose(self.render(worker), 0.125, rtol=0.02)
                    self.await_render(worker, worker.transport.stats()["sequence"])
                if enabled:
                    expected_count += blocks
                stats = worker.transport.stats()
                timing = stats["timing"]
                self.assertEqual(stats["timing_enabled"], enabled)
                self.assertTrue(timing["coherent"])
                self.assertEqual(timing["count"], expected_count)
                self.assertEqual(timing["cpu_clock_errors"], 0)
                if expected_count:
                    self.assertGreater(timing["render_cpu_ns_sum"], 0)
                if not enabled:
                    self.assertEqual(timing, previous)
                for name in ("wakeup", "render", "render_cpu"):
                    histogram = timing[f"{name}_histogram"]
                    total = timing[f"{name}_ns_sum"]
                    self.assertEqual(len(histogram), 64)
                    self.assertEqual(sum(histogram), expected_count)
                    self.assertTrue(all(count >= 0 for count in histogram))
                    lower = sum(count * (0 if i == 0 else 1 << i)
                                for i, count in enumerate(histogram))
                    upper = sum(count * ((1 << (i + 1)) - 1)
                                for i, count in enumerate(histogram))
                    self.assertGreaterEqual(total, lower)
                    self.assertLessEqual(total, upper)
                    self.assertGreaterEqual(total, previous[f"{name}_ns_sum"])
                    self.assertTrue(all(new >= old for new, old in
                                        zip(histogram, previous[f"{name}_histogram"])))
                previous = timing

    @_both_wakeup_modes
    def test_creator_mode_controls_spawned_rendering_despite_joiner_environment(self):
        creator_mode = os.environ["VCPI_TRACK_WAKEUP"]
        other_mode = "poll" if creator_mode == "notify" else "notify"
        for joiner_mode in (other_mode, "invalid-joiner-mode"):
            with self.subTest(joiner_mode=joiner_mode), patch(
                    "vcpi.audio.track_client._track_owner",
                    partial(_track_owner_with_wakeup_env, joiner_mode)):
                worker = self.worker()
                self.assertNotEqual(worker.pid, os.getpid())
                self.assertEqual(worker.transport.stats()["wakeup_mode"], creator_mode)
                worker.transport.set_timing_enabled(True)
                worker.transport.send_midi(bytes((0x90, 60, 127)))
                # An idle barrier must acknowledge buffered MIDI without DSP.
                initial_position = worker.request("sync_state")["state"]["deck"]["position"]
                self.assertEqual(worker.transport.stats()["progress"], 0)
                self.assertEqual(worker.transport.stats()["midi_pending"], 1)
                with patch.dict(os.environ, {"VCPI_TRACK_WAKEUP": other_mode}):
                    for sequence in range(1, 17):
                        # Exercise idle waits, not just a continuously busy renderer.
                        time.sleep(0.002)
                        audio = self.render(worker)
                        self.await_render(worker, sequence)
                        stats = worker.transport.stats()
                        self.assertEqual(stats["wakeup_mode"], creator_mode)
                        self.assertEqual(stats["submitted"], sequence)
                        self.assertEqual(stats["progress"], sequence)
                        self.assertEqual(stats["timing"]["count"], sequence)
                        self.assertEqual(stats["midi_pending"], 0)
                        self.assertEqual(stats["midi_consumed"], 1)
                        # Control ownership and a barrier must not create another
                        # render, nor lose the next request after releasing ownership.
                        worker.request("deck_command", action="cue_set")
                        state = worker.request("sync_state")["state"]["deck"]
                        self.assertAlmostEqual(
                            state["position"] - initial_position, sequence * 64 / 44100)
                    np.testing.assert_allclose(audio, 0.125, rtol=0.02)
                    time.sleep(0.01)
                    final = worker.transport.stats()
                    self.assertEqual(final["progress"], 16)
                    self.assertEqual(final["timing"]["count"], 16)
                    self.assertGreater(final["timing"]["wakeup_ns_sum"], 0)
                    self.assertGreater(final["timing"]["render_ns_sum"], 0)
                    self.assertGreater(final["timing"]["render_cpu_ns_sum"], 0)
                    self.assertEqual(final["failure"], "")
                worker.test_engine.clear_track(2)
                transport = worker.transport
                worker.shutdown()
                self.assertEqual(worker._process.exitcode, 0)
                self.assertFalse(worker._io_thread.is_alive())
                self.assertEqual(transport.stats()["renderer_pid"], 0)

    def test_track_ignores_foreground_interrupt_and_still_cleans_up(self):
        worker = self.worker()
        os.kill(worker.pid, signal.SIGINT)
        reply = worker.request("sync_state")
        self.assertEqual(reply["state"]["name"], "remote")
        self.render(worker, [mido.Message("note_on", note=60, velocity=127)])
        self.await_render(worker, 1)
        np.testing.assert_allclose(self.settled_audio(worker), 0.125, rtol=0.02)
        worker.test_engine.clear_track(2)
        worker.shutdown()
        self.assertEqual(worker._process.exitcode, 0)
        self.assertFalse(worker._io_thread.is_alive())

    def test_lifeline_eof_before_parent_exit_allows_cleanup_to_complete(self):
        context = mp.get_context("spawn")
        shared = str(self.root / "lifeline.mmap")
        Path(shared).touch()
        connection, child_connection = context.Pipe()
        lifeline, writer = context.Pipe(duplex=False)
        child = context.Process(target=_watch_lifeline_for_cleanup_test,
                                args=(child_connection, lifeline, shared))
        child.start()
        child_connection.close()
        lifeline.close()
        try:
            self.assertTrue(connection.poll(10))
            self.assertEqual(connection.recv(), "watching")
            writer.close()
            # EOF must not kill a child while its creator is still alive.
            time.sleep(0.1)
            self.assertTrue(child.is_alive())
            connection.send("shutdown")
            self.assertTrue(connection.poll(5))
            self.assertEqual(connection.recv(), ("cleanup completed", False))
            child.join(2)
            self.assertEqual(child.exitcode, 0)
        finally:
            writer.close()
            connection.close()
            if child.is_alive():
                child.kill()
            child.join(2)

    @_both_wakeup_modes
    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "requires POSIX process suspension")
    def test_snapshot_barrier_does_not_render_an_extra_discarded_block(self):
        ramp = np.arange(4096, dtype="<i2")
        with wave.open(str(self.sample), "wb") as sample:
            sample.setnchannels(1)
            sample.setsampwidth(2)
            sample.setframerate(44100)
            sample.writeframes(ramp.tobytes())
        worker = self.worker()
        os.kill(worker.pid, signal.SIGSTOP)
        resume = threading.Timer(0.05, os.kill, args=(worker.pid, signal.SIGCONT))
        resume.start()
        self.addCleanup(resume.join)
        self.render(worker, [mido.Message("note_on", note=60, velocity=127)])
        first_state = worker.request("sync_state")["state"]["deck"]
        self.assertAlmostEqual(first_state["position"], 64 / 44100)
        first = self.render(worker)
        self.assertEqual(first.shape, (64, 2))
        self.assertTrue(np.isfinite(first).all())
        self.await_render(worker, 2)
        second_state = worker.request("sync_state")["state"]["deck"]
        self.assertAlmostEqual(second_state["position"], 128 / 44100)
        # Another snapshot without a render request must not advance the source.
        repeated = worker.request("sync_state")["state"]["deck"]
        self.assertEqual(repeated["position"], second_state["position"])

    def test_snapshot_refresh_preserves_parent_mixer_state(self):
        worker = self.worker()
        worker.track.gain = 0.375
        worker.track.muted = True
        worker.track.solo = True
        worker.track.generation = 42
        worker.track.midi_channels = {3}
        worker.request("sync_state")
        self.assertEqual(worker.track.gain, 0.375)
        self.assertTrue(worker.track.muted)
        self.assertTrue(worker.track.solo)
        self.assertEqual(worker.track.generation, 42)
        self.assertEqual(worker.track.midi_channels, {3})

    def test_invalid_load_preserves_native_validation_error(self):
        catalog = make_catalog("missing", [
            {"id": "missing", "path": str(self.root / "missing.wav"), "title": "Missing"},
        ], sync_filter=False)
        with self.assertRaisesRegex(ValueError, "regular file"):
            TrackWorker(44100, 64, 2, "load_deck", {"catalog": catalog})

    def test_dead_child_does_not_escape_into_audio_callback(self):
        worker = self.worker()
        worker._process.terminate()
        worker._process.join(2)
        self.assertIsNotNone(worker.poll_failure())
        with self.assertRaises(TrackProcessError):
            worker.request("sync_state")

    @unittest.skipUnless(os.name == "posix", "requires POSIX descriptor transfer")
    def test_child_exits_when_output_owner_is_killed(self):
        context = mp.get_context("spawn")
        connection, child_connection = context.Pipe()
        owner = context.Process(target=_own_track_for_orphan_test,
                                args=(child_connection, self.catalog),
                                daemon=False)
        owner.start()
        child_connection.close()
        pid = None
        sentinel = None
        try:
            self.assertTrue(connection.poll(15), "intermediate owner did not start")
            pid, handle = connection.recv()
            sentinel = handle.detach()
            owner.terminate()
            owner.join(2)
            self.assertFalse(owner.is_alive())
            self.assertTrue(wait_connections([sentinel], timeout=5),
                            "track survived the output owner's death")
        finally:
            if sentinel is not None:
                if not wait_connections([sentinel], timeout=0):
                    os.kill(pid, signal.SIGKILL)
                os.close(sentinel)
            if owner.is_alive():
                owner.kill()
                owner.join(2)
            connection.close()

    def test_rpc_is_main_thread_only(self):
        worker = self.worker()
        errors = []

        def request():
            try:
                worker.request("sync_state")
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=request)
        thread.start()
        thread.join(2)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("main thread", str(errors[0]))

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "requires POSIX suspension")
    def test_rpc_timeout_remains_track_local_and_cleanup_is_bounded(self):
        worker = self.worker()
        os.kill(worker.pid, signal.SIGSTOP)
        with patch("vcpi.audio.track_client.PARAMETER_TIMEOUT", 0.02):
            with self.assertRaises(TrackProcessError):
                worker.request("sync_state")
        self.assertIsNotNone(worker.failure)
        self.assertFalse(worker._process.is_alive())


if __name__ == "__main__":
    unittest.main()
