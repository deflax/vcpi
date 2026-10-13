"""Real spawned-owner regressions without opening audio or MIDI hardware."""

import json
import multiprocessing
import os
import signal
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from core.audio_process import AudioProcessError
from core.host import VcpiCore


class AudioProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="vcpi-process-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sample = self.root / "sample.wav"
        with wave.open(str(self.sample), "wb") as sample:
            sample.setnchannels(1)
            sample.setsampwidth(2)
            sample.setframerate(44100)
            sample.writeframes(struct.pack("<h", 4096) * 64)
        self.host = VcpiCore(session_path=str(self.root / "session.json"))
        self.addCleanup(self.host.engine.shutdown)

    def test_applied_async_controls_and_routing_survive_session_round_trip(self):
        host = self.host
        host.load_wav(0, str(self.sample), "original")
        host.route(3, 0)
        host.engine.enqueue_slot_change(0, gain=0.8, muted=True)
        host.engine.enqueue_slot_change(0, gain=0.25, muted=False, solo=True)
        host.engine.enqueue_master_gain(0.7)
        host.save_session()
        saved = json.loads(host.session_path.read_text())
        self.assertEqual(saved["slots"][0]["gain"], 0.25)
        self.assertFalse(saved["slots"][0]["muted"])
        self.assertTrue(saved["slots"][0]["solo"])
        self.assertEqual(saved["routing"], {"4": 1})
        self.assertEqual(saved["master_gain"], 0.7)
        restored = VcpiCore(session_path=str(host.session_path))
        self.addCleanup(restored.engine.shutdown)
        restored.restore_session()
        self.assertEqual(restored.engine.slots[0].name, "original")
        self.assertEqual(restored.engine.slots[0].gain, 0.25)
        self.assertFalse(restored.engine.slots[0].muted)
        self.assertTrue(restored.engine.slots[0].solo)
        self.assertEqual(restored.channel_map, {3: 0})
        self.assertEqual(restored.engine.master_gain, 0.7)
        self.assertIsNone(restored.audio_output_name)

    def test_retired_instance_inputs_cannot_change_replacement_state(self):
        host = self.host
        retired = host.load_wav(0, str(self.sample), "retired")
        host.load_wav(0, str(self.sample), "replacement")
        host.engine.set_slot_state(0, gain=0.375)
        host.engine.enqueue_slot_change(0, generation=retired.generation, gain=0.9, muted=True)
        host.save_session()
        saved = json.loads(host.session_path.read_text())
        self.assertEqual(saved["slots"][0]["name"], "replacement")
        self.assertEqual(saved["slots"][0]["gain"], 0.375)
        self.assertFalse(saved["slots"][0]["muted"])

    def test_failed_load_keeps_previous_source_and_owner_usable(self):
        host = self.host
        host.load_wav(0, str(self.sample), "retained")
        with self.assertRaises(FileNotFoundError):
            host.load_wav(0, str(self.root / "missing.wav"), "missing")
        host.engine.set_slot_state(0, gain=0.375)
        host.save_session()
        saved = json.loads(host.session_path.read_text())
        self.assertEqual(saved["slots"][0]["name"], "retained")
        self.assertEqual(saved["slots"][0]["path"], str(self.sample))
        self.assertEqual(saved["slots"][0]["gain"], 0.375)

    def test_dead_owner_rejects_mutations_and_shutdown_preserves_last_good_file(self):
        host = self.host
        host.engine.set_master_gain(0.5)
        host.save_session()
        previous = host.session_path.read_bytes()
        process = host.engine._process
        process.terminate()
        process.join(timeout=2)
        self.assertFalse(host.engine.running)
        self.assertIsNotNone(host.engine.failure)
        with self.assertRaises(AudioProcessError):
            host.engine.set_master_gain(0.9)
        with self.assertRaises(AudioProcessError):
            host.shutdown()
        self.assertEqual(host.session_path.read_bytes(), previous)
        self.assertFalse(process.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in host.engine._threads))
        host.shutdown()

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "requires POSIX process suspension")
    def test_timed_out_rpc_is_terminal_not_a_late_accepted_change(self):
        engine = self.host.engine
        engine.set_master_gain(0.7)
        os.kill(engine.owner_pid, signal.SIGSTOP)
        with self.assertRaises(AudioProcessError):
            engine._rpc("set_master_gain", {"value": 0.2}, timeout=0.05)
        self.assertEqual(engine.master_gain, 0.7)
        with self.assertRaises(AudioProcessError):
            engine.set_master_gain(0.4)
        engine.shutdown()
        self.assertFalse(engine._process.is_alive())
        self.assertNotIn(engine.owner_pid, [child.pid for child in multiprocessing.active_children()])


if __name__ == "__main__":
    unittest.main()
