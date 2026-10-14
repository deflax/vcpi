"""Fixed handoff invariants and real spawned sampler ownership, without devices."""

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
from multiprocessing.connection import wait as wait_connections
from multiprocessing.reduction import DupFd
from unittest.mock import patch

import mido
import numpy as np

from core.audio_state import PluginSnapshot
from core.audio_deps import require_native
from core.engine import AudioEngine
from core.slot_process import (
    MIDI_CAPACITY, SlotProcessError, SlotWorker, _watch_parent,
)


def _own_slot_for_orphan_test(connection, sample):
    worker = SlotWorker(44100, 64, 2, "load_wav", {"wav_path": sample})
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



class SpawnedSlotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vcpi-slot-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sample = self.root / "sample.wav"
        with wave.open(str(self.sample), "wb") as sample:
            sample.setnchannels(1)
            sample.setsampwidth(2)
            sample.setframerate(44100)
            sample.writeframes(struct.pack("<h", 4096) * 4096)

    def worker(self, **kwargs):
        worker = SlotWorker(44100, 64, 2, "load_wav",
                            {"wav_path": str(self.sample), "name": "remote"}, **kwargs)
        engine = AudioEngine(buffer_size=64)
        engine.publish_slot(2, worker.slot)
        engine.set_slot_state(2, gain=1.0)
        worker.test_engine = engine

        def close():
            engine.clear_slot(2)
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
        self.fail("spawned slot did not finish the offered block")

    def test_persistent_native_owner_and_one_block_sampler_audio(self):
        worker = self.worker()
        self.assertNotEqual(worker.pid, os.getpid())
        self.assertFalse(worker._process.daemon)
        self.assertIsInstance(worker.slot.plugin, PluginSnapshot)
        self.assertIs(worker.slot.render_owner, worker)
        np.testing.assert_array_equal(
            self.render(worker, [mido.Message("note_on", note=60, velocity=127)]), 0)
        self.await_render(worker, 1)
        audio = self.render(worker)
        self.assertIsNotNone(audio)
        self.assertEqual(audio.shape, (64, 2))
        np.testing.assert_array_equal(audio, np.full((64, 2), 0.125, dtype=np.float32))
        pid = worker.pid
        self.await_render(worker, 2)
        reply = worker.request("sync_state")
        self.assertEqual(worker.pid, pid)
        self.assertIsInstance(reply["state"]["plugin"], PluginSnapshot)
        worker.test_engine.clear_slot(2)
        worker.shutdown()
        worker.shutdown()
        self.assertFalse(worker._process.is_alive())
        self.assertFalse(worker._io_thread.is_alive())
        self.assertEqual(worker._process.exitcode, 0)

    def test_slot_ignores_foreground_interrupt_and_still_cleans_up(self):
        worker = self.worker()
        os.kill(worker.pid, signal.SIGINT)
        reply = worker.request("sync_state")
        self.assertEqual(reply["state"]["name"], "remote")
        self.render(worker, [mido.Message("note_on", note=60, velocity=127)])
        self.await_render(worker, 1)
        np.testing.assert_array_equal(
            self.render(worker), np.full((64, 2), 0.125, dtype=np.float32))
        worker.test_engine.clear_slot(2)
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
        worker.request("sync_state")
        first = self.render(worker)
        expected = ramp[:64].astype(np.float32) / 32768.0
        np.testing.assert_array_equal(first[:, 0], expected)
        self.await_render(worker, 2)
        worker.request("sync_state")
        second = self.render(worker)
        expected = ramp[64:128].astype(np.float32) / 32768.0
        np.testing.assert_array_equal(second[:, 0], expected)

    def test_snapshot_refresh_preserves_parent_mixer_state(self):
        worker = self.worker()
        worker.slot.gain = 0.375
        worker.slot.muted = True
        worker.slot.solo = True
        worker.slot.generation = 42
        worker.slot.midi_channels = {3}
        worker.request("sync_state")
        self.assertEqual(worker.slot.gain, 0.375)
        self.assertTrue(worker.slot.muted)
        self.assertTrue(worker.slot.solo)
        self.assertEqual(worker.slot.generation, 42)
        self.assertEqual(worker.slot.midi_channels, {3})

    def test_invalid_load_preserves_native_validation_error(self):
        with self.assertRaises(FileNotFoundError):
            SlotWorker(44100, 64, 2, "load_wav",
                       {"wav_path": str(self.root / "missing.wav")})

    def test_dead_child_does_not_escape_into_audio_callback(self):
        worker = self.worker()
        worker._process.terminate()
        worker._process.join(2)
        self.assertIsNotNone(worker.poll_failure())
        with self.assertRaises(SlotProcessError):
            worker.request("sync_state")

    @unittest.skipUnless(os.name == "posix", "requires POSIX descriptor transfer")
    def test_child_exits_when_output_owner_is_killed(self):
        context = mp.get_context("spawn")
        connection, child_connection = context.Pipe()
        owner = context.Process(target=_own_slot_for_orphan_test,
                                args=(child_connection, str(self.sample)),
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
                            "slot survived the output owner's death")
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
    def test_rpc_timeout_remains_slot_local_and_cleanup_is_bounded(self):
        worker = self.worker()
        os.kill(worker.pid, signal.SIGSTOP)
        with patch("core.slot_process.PARAMETER_TIMEOUT", 0.02):
            with self.assertRaises(SlotProcessError):
                worker.request("sync_state")
        self.assertIsNotNone(worker.failure)
        self.assertFalse(worker._process.is_alive())


if __name__ == "__main__":
    unittest.main()
