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
from collections import deque
from pathlib import Path
from multiprocessing.connection import wait as wait_connections
from multiprocessing.reduction import DupFd
from unittest.mock import patch

import mido
import numpy as np

from core.audio_state import PluginSnapshot
from core.slot_process import (
    MIDI_CAPACITY, RING_SIZE, SlotProcessError, SlotWorker,
    _SharedBlocks, _next_job, _take_midi,
)


def _own_slot_for_orphan_test(connection, sample):
    worker = SlotWorker(44100, 64, 2, "load_wav", {"wav_path": sample})
    try:
        connection.send((worker.pid, DupFd(worker._process.sentinel)))
        connection.recv()
    finally:
        worker.shutdown()
        connection.close()


class SlotHandoffTests(unittest.TestCase):
    def setUp(self):
        # Exercise the real callback and shared storage independently of native
        # scheduling. No production fallback or injected process mode is used.
        worker = SlotWorker.__new__(SlotWorker)
        worker.buffer_size = 8
        worker._closed = False
        worker.failure = None
        worker.misses = 0
        worker._sequence = 0
        worker._submitted = 0
        worker._published_midi = 0
        worker._pending_midi = deque()
        worker._progress_time = time.monotonic()
        worker._callback_time = worker._progress_time
        worker._shared = _SharedBlocks(mp.get_context("spawn"), 8)
        worker._audio = np.frombuffer(worker._shared.audio, dtype=np.float32).reshape(
            RING_SIZE, 8, 2)
        worker._output = np.empty((8, 2), dtype=np.float32)
        self.worker = worker

    def publish(self, sequence, value):
        cell = sequence % RING_SIZE
        with self.worker._shared.output_locks[cell]:
            self.worker._audio[cell].fill(value)
            self.worker._shared.outputs[cell] = sequence

    def test_exact_previous_block_and_reusable_parent_owned_copy(self):
        worker = self.worker
        self.assertIsNone(worker.render([], 8))
        self.publish(1, 0.25)
        first = worker.render([], 8)
        self.assertIs(first, worker._output)
        np.testing.assert_array_equal(first, np.full((8, 2), 0.25))
        # A child's subsequent cell reuse cannot mutate the parent copy.
        self.publish(1 + RING_SIZE, 0.75)
        np.testing.assert_array_equal(first, np.full((8, 2), 0.25))
        self.publish(2, 0.5)
        self.assertIs(worker.render([], 8), first)
        np.testing.assert_array_equal(first, np.full((8, 2), 0.5))

    def test_late_blocks_never_reappear_on_ring_wrap(self):
        worker = self.worker
        worker.render([], 8)
        self.assertIsNone(worker.render([], 8))  # block 1 missed its deadline
        self.publish(1, 0.75)
        for _ in range(RING_SIZE + 1):
            self.assertIsNone(worker.render([], 8))
        self.assertEqual(worker.misses, RING_SIZE + 2)

    def test_busy_output_and_request_cells_are_nonblocking(self):
        worker = self.worker
        worker.render([], 8)
        output_lock = worker._shared.output_locks[1]
        request_locks = worker._shared.request_locks
        output_lock.acquire()
        for lock in request_locks:
            lock.acquire()
        try:
            self.assertIsNone(worker.render([], 8))
            self.assertEqual(worker._submitted, 1)
            self.assertEqual(worker.misses, 1)
        finally:
            output_lock.release()
            for lock in request_locks:
                lock.release()

    def test_busy_request_cell_does_not_lose_a_timely_audio_job(self):
        worker = self.worker
        worker.render([], 8)
        lock = worker._shared.request_locks[2]
        lock.acquire()
        try:
            worker.render([mido.Message("note_off", note=60)], 8)
        finally:
            lock.release()
        sequence, endpoint = _next_job(worker._shared, 0)
        self.assertEqual(sequence, 2)
        messages = _take_midi(worker._shared, endpoint, mido)
        self.assertEqual([(message.type, message.note) for message in messages],
                         [("note_off", 60)])

    def test_skipped_jobs_keep_note_on_note_off_order(self):
        worker = self.worker
        worker.render([mido.Message("note_on", note=60, velocity=90)], 8)
        worker.render([mido.Message("note_off", note=60)], 8)
        worker.render([mido.Message("note_on", note=62, velocity=80)], 8)
        # The native owner takes only the newest audio job, but all MIDI.
        sequence, endpoint = _next_job(worker._shared, 0)
        self.assertEqual(sequence, 3)
        messages = _take_midi(worker._shared, endpoint, mido)
        self.assertEqual([(message.type, message.note) for message in messages],
                         [("note_on", 60), ("note_off", 60), ("note_on", 62)])
        self.assertIsNone(_next_job(worker._shared, sequence))
        self.assertEqual(_take_midi(worker._shared, 1, mido), [])

    def test_busy_midi_lock_defers_without_losing_note_off(self):
        worker = self.worker
        lock = worker._shared.midi_lock
        lock.acquire()
        try:
            worker.render([mido.Message("note_off", note=60)], 8)
            self.assertEqual(worker._published_midi, 0)
            self.assertEqual(len(worker._pending_midi), 1)
        finally:
            lock.release()
        worker.render([], 8)
        self.assertEqual(worker._published_midi, 1)
        messages = _take_midi(worker._shared, 1, mido)
        self.assertEqual(messages[0].type, "note_off")

    def test_midi_wrap_preserves_fifo_order(self):
        worker = self.worker
        with worker._shared.midi_lock:
            worker._shared.midi_cursors[0] = MIDI_CAPACITY - 1
            worker._shared.midi_cursors[1] = MIDI_CAPACITY - 1
        worker.render([mido.Message("note_on", note=60),
                       mido.Message("note_off", note=60)], 8)
        messages = _take_midi(worker._shared, MIDI_CAPACITY + 1, mido)
        self.assertEqual([message.type for message in messages], ["note_on", "note_off"])

    def test_shared_midi_overflow_is_explicit_local_failure(self):
        worker = self.worker
        worker.render([mido.Message("note_on")] * MIDI_CAPACITY, 8)
        self.assertIsNone(worker.render([mido.Message("note_off")], 8))
        self.assertIn("FIFO is full", worker.failure)
        self.assertIsNone(worker.render([], 8))

    def test_oversized_sysex_and_changed_block_size_fail_locally(self):
        worker = self.worker
        self.assertIsNone(worker.render([mido.Message("sysex", data=[1] * 1024)], 8))
        self.assertIn("bounded storage", worker.failure)
        worker.failure = None
        self.assertIsNone(worker.render([], 16))
        self.assertIn("block size changed", worker.failure)


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
        self.addCleanup(worker.shutdown)
        return worker

    def await_render(self, worker, sequence):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with worker._shared.progress_lock:
                if worker._shared.progress[0] >= sequence:
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
        self.assertIsNone(worker.render([mido.Message("note_on", note=60, velocity=127)], 64))
        self.await_render(worker, 1)
        audio = worker.render([], 64)
        self.assertIsNotNone(audio)
        self.assertEqual(audio.shape, (64, 2))
        np.testing.assert_array_equal(audio, np.full((64, 2), 0.125, dtype=np.float32))
        pid = worker.pid
        self.await_render(worker, 2)
        reply = worker.request("sync_state")
        self.assertEqual(worker.pid, pid)
        self.assertIsInstance(reply["state"]["plugin"], PluginSnapshot)
        worker.shutdown()
        worker.shutdown()
        self.assertFalse(worker._process.is_alive())
        self.assertFalse(worker._io_thread.is_alive())

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
        worker.render([mido.Message("note_on", note=60, velocity=127)], 64)
        worker.request("sync_state")
        first = worker.render([], 64)
        expected = ramp[:64].astype(np.float32) / 32768.0
        np.testing.assert_array_equal(first[:, 0], expected)
        self.await_render(worker, 2)
        worker.request("sync_state")
        second = worker.render([], 64)
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
        self.assertIsNone(worker.render([mido.Message("note_off")], 64))
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
        with patch("core.slot_process.REQUEST_TIMEOUT", 0.02):
            with self.assertRaises(SlotProcessError):
                worker.request("sync_state")
        self.assertIsNotNone(worker.failure)
        self.assertFalse(worker._process.is_alive())
        self.assertIsNone(worker.render([], 64))


if __name__ == "__main__":
    unittest.main()
