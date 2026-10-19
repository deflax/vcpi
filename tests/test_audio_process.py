"""Real spawned-owner regressions without opening audio or MIDI hardware."""

import base64
import json
import math
import multiprocessing
import os
import signal
import platform
import struct
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from core.audio_deps import require_native
from core.audio_process import AudioProcessError
from core.host import VcpiCore
from core.plugin_host import NativePlugin


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
        self.assertEqual(saved["version"], 2)
        self.assertNotIn("params", saved["slots"][0])
        self.assertNotIn("native_state", saved["slots"][0])
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

    def test_native_session_chunks_round_trip_all_rpc_targets_without_parameter_replay(self):
        fixture_root = Path(__file__).resolve().parents[1] / "vst3"
        instrument_path = fixture_root / "Surge XT.vst3"
        effect_path = fixture_root / "DragonflyRoomReverb.vst3"
        if sys.platform == "darwin":
            binary_suffix = Path("Contents/MacOS")
        elif sys.platform.startswith("linux"):
            architecture = {"arm64": "aarch64", "amd64": "x86_64"}.get(
                platform.machine(), platform.machine())
            binary_suffix = Path("Contents") / f"{architecture}-linux"
        else:
            self.skipTest("native audio supports Linux and macOS")
        if not all((bundle / binary_suffix).is_dir()
                   for bundle in (instrument_path, effect_path)):
            self.skipTest("fetch native Surge XT and DragonflyRoomReverb bundles")
        host = self.host
        host.load_instrument(0, str(instrument_path))
        host.load_effect(str(effect_path), 0)
        host.load_effect(str(effect_path), None)
        targets = ((0, None), (0, 0), (None, 0))
        expected = {}
        for slot_index, fx_index in targets:
            plugin = host.engine._plugin(slot_index, fx_index)
            name = next(
                name for name, parameter in plugin.parameters.items()
                if parameter.kind == "float" and parameter.is_automatable
                and len(parameter.range) == 2
                and all(math.isfinite(value) for value in parameter.range)
                and parameter.range[0] < parameter.range[1])
            low, high = plugin.parameters[name].range
            actual = host.engine.set_parameter(
                slot_index, name, low + (high - low) * 0.3, fx_index)
            expected[(slot_index, fx_index)] = (name, actual)
        host.save_session()
        saved = json.loads(host.session_path.read_text())
        entries = (saved["slots"][0], saved["slots"][0]["effects"][0],
                   saved["master_effects"][0])
        for entry, target in zip(entries, targets):
            # A conflicting, valid legacy value must never override the chunk.
            name, actual = expected[target]
            parameter = host.engine._plugin(*target).parameters[name]
            entry["params"] = {name: parameter.range[1]}
        host.session_path.write_text(json.dumps(saved))
        host.load_wav(1, str(self.sample))
        restored = VcpiCore(session_path=str(host.session_path))
        self.addCleanup(restored.engine.shutdown)
        restored.restore_session()
        for (slot_index, fx_index), (name, actual) in expected.items():
            plugin = restored.engine._plugin(slot_index, fx_index)
            self.assertAlmostEqual(plugin.values[name], actual)

    def test_plugdata_patch_worker_failure_and_native_session_round_trip(self):
        configured = os.environ.get("PLUGDATA_VST3_PATH")
        bundle = (Path(configured).expanduser() if configured else
                  Path(__file__).resolve().parents[1] / "vst3" / "plugdata.vst3")
        if sys.platform == "darwin":
            binary_suffix = Path("Contents/MacOS")
        elif sys.platform.startswith("linux"):
            architecture = {"arm64": "aarch64", "amd64": "x86_64"}.get(
                platform.machine(), platform.machine())
            binary_suffix = Path("Contents") / f"{architecture}-linux"
        else:
            self.skipTest("native audio supports Linux and macOS")
        if not (bundle / binary_suffix).is_dir():
            self.skipTest("set PLUGDATA_VST3_PATH or install vst3/plugdata.vst3")
        bundle = bundle.resolve()
        patches = self.root / "patches"
        patches.mkdir()
        main = patches / "relative.pd"
        main.write_text(
            "#N canvas 0 0 400 300 10;\n"
            "#X obj 20 20 notein;\n"
            "#X obj 20 60 vcpi-helper;\n"
            "#X obj 20 100 dac~;\n"
            "#X connect 0 0 1 0;\n"
            "#X connect 1 0 2 0;\n"
            "#X connect 1 0 2 1;\n",
            encoding="utf-8",
        )
        (patches / "vcpi-helper.pd").write_text(
            "#N canvas 0 0 300 200 10;\n"
            "#X obj 20 20 inlet;\n"
            "#X obj 20 50 mtof;\n"
            "#X obj 20 80 osc~;\n"
            "#X obj 20 110 outlet~;\n"
            "#X connect 0 0 1 0;\n"
            "#X connect 1 0 2 0;\n"
            "#X connect 2 0 3 0;\n",
            encoding="utf-8",
        )
        (patches / "broken.pd").write_text("not a Pure Data patch", encoding="utf-8")
        with patch.dict(os.environ, {
            "VCPI_PATCHES_DIR": str(patches),
            "PLUGDATA_VST3_PATH": str(bundle),
        }):
            host = VcpiCore(session_path=str(self.root / "pd-session.json"))
            self.addCleanup(host.engine.shutdown)
            slot, _, used_plugin, used_patch = host.load_pd_patch(
                0, "relative", name="relative voice")
            self.assertEqual(used_plugin, str(bundle))
            self.assertEqual(used_patch, main.resolve())
            self.assertEqual(slot.source_type, "pd")
            self.assertEqual(slot.pd_patch_path, str(main.resolve()))
            self.assertEqual(slot.display_label, "pd::relative")
            generation, worker_pid = slot.generation, slot.worker_pid
            self.assertNotEqual(worker_pid, host.engine.owner_pid)
            with self.assertRaises(ValueError):
                host.load_pd_patch(0, "broken.pd")
            retained = host.engine.slots[0]
            self.assertEqual(retained.generation, generation)
            self.assertEqual(retained.worker_pid, worker_pid)
            self.assertEqual(retained.source_type, "pd")
            self.assertEqual(retained.pd_patch_path, str(main.resolve()))
            host.engine.set_slot_state(0, gain=0.375)
            host.save_session()
            saved = json.loads(host.session_path.read_text())
            entry = saved["slots"][0]
            self.assertEqual(saved["version"], 2)
            self.assertNotIn("params", entry)
            main.unlink()
            host.engine.shutdown()
            restored = VcpiCore(session_path=str(host.session_path))
            self.addCleanup(restored.engine.shutdown)
            restored.restore_session()
            slot = restored.engine.slots[0]
            self.assertIsNotNone(slot)
            self.assertEqual(slot.source_type, "pd")
            self.assertEqual(slot.pd_patch_path, str(main.resolve()))
            self.assertEqual(slot.display_label, "pd::relative")
            self.assertEqual(slot.name, "relative voice")
            self.assertEqual(slot.path, str(bundle))
            self.assertEqual(slot.gain, 0.375)
            restored.save_session()
            restored_entry = json.loads(restored.session_path.read_text())["slots"][0]
            restored.engine.shutdown()

            # Render the chunk captured after the fresh-process restore. The
            # deleted main file cannot provide this patch, while its relative
            # abstraction must still resolve for MIDI to produce the right tone.
            plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
            plugin.load_saved_state(base64.b64decode(
                restored_entry["native_state"], validate=True))
            backend = require_native()
            transport = backend.Transport(str(self.root / "pd-restored.mmap"), 512, True)
            runner = backend.SlotRunner(transport, plugin.native, 44100, 512)
            try:
                transport.send_midi(bytes((0x90, 69, 100)))
                for _ in range(8):
                    runner.render_once()
                audio = np.concatenate([runner.render_once() for _ in range(64)])
                self.assertTrue(np.isfinite(audio).all())
                self.assertGreater(float(np.abs(audio).max()), 1e-4)
                signal = audio[:, 0]
                spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
                frequencies = np.fft.rfftfreq(len(signal), 1 / 44100)
                self.assertAlmostEqual(
                    float(frequencies[np.argmax(spectrum)]), 440,
                    delta=44100 / len(signal))
            finally:
                runner.shutdown()

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
            host.save_session()
        with self.assertLogs("core.host", level="WARNING"):
            host.shutdown()
        self.assertEqual(host.session_path.read_bytes(), previous)
        self.assertFalse(process.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in host.engine._threads))
        host.shutdown()

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "requires POSIX process termination")
    def test_slot_crash_is_local_and_can_be_replaced(self):
        host = self.host
        first = host.load_wav(0, str(self.sample), "healthy")
        second = host.load_wav(1, str(self.sample), "crashing")
        self.assertNotEqual(first.worker_pid, second.worker_pid)
        self.assertNotEqual(first.worker_pid, host.engine.owner_pid)
        os.kill(second.worker_pid, signal.SIGKILL)
        with host.engine.state_lock:
            reported = host.engine._state_condition.wait_for(
                lambda: host.engine.slots[1].worker_failure is not None,
                timeout=5,
            )
        self.assertTrue(reported)
        self.assertIsNone(host.engine.failure)
        host.engine.set_slot_state(0, gain=0.375)
        self.assertEqual(host.engine.slots[0].worker_pid, first.worker_pid)
        self.assertEqual(host.engine.slots[0].gain, 0.375)
        replacement = host.load_wav(1, str(self.sample), "recovered")
        self.assertNotEqual(replacement.worker_pid, second.worker_pid)
        self.assertIsNone(replacement.worker_failure)
        host.save_session()
        saved = json.loads(host.session_path.read_text())
        self.assertEqual(saved["slots"][0]["name"], "healthy")
        self.assertEqual(saved["slots"][1]["name"], "recovered")

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "requires POSIX process suspension")
    def test_hung_slot_snapshot_preserves_session_and_output_owner(self):
        host = self.host
        slot = host.load_wav(0, str(self.sample), "retained")
        host.save_session()
        previous = host.session_path.read_bytes()
        os.kill(slot.worker_pid, signal.SIGSTOP)
        with self.assertRaises(RuntimeError):
            host.save_session()
        self.assertEqual(host.session_path.read_bytes(), previous)
        self.assertIsNone(host.engine.failure)
        host.engine.set_master_gain(0.375)
        self.assertEqual(host.engine.master_gain, 0.375)
        with host.engine.state_lock:
            reported = host.engine._state_condition.wait_for(
                lambda: host.engine.slots[0].worker_failure is not None,
                timeout=5,
            )
        self.assertTrue(reported)

    @unittest.skipUnless(
        hasattr(signal, "SIGUSR1") and hasattr(signal, "SIGSTOP"),
        "requires POSIX signals",
    )
    def test_interrupted_slot_rpc_retires_owners_and_preserves_good_session(self):
        host = self.host
        slot = host.load_wav(0, str(self.sample), "retained")
        host.save_session()
        previous = host.session_path.read_bytes()
        os.kill(slot.worker_pid, signal.SIGSTOP)

        def interrupt(signum, frame):
            raise KeyboardInterrupt

        previous_handler = signal.signal(signal.SIGUSR1, interrupt)
        timer = threading.Timer(0.05, os.kill, args=(os.getpid(), signal.SIGUSR1))
        try:
            timer.start()
            with self.assertRaises(KeyboardInterrupt):
                host.engine.sync_state()
        finally:
            timer.join()
            signal.signal(signal.SIGUSR1, previous_handler)
            host.shutdown(save=False)
        self.assertEqual(host.session_path.read_bytes(), previous)
        self.assertEqual(host.engine._process.exitcode, 0)
        self.assertTrue(all(not thread.is_alive() for thread in host.engine._threads))
        with self.assertRaises(ProcessLookupError):
            os.kill(slot.worker_pid, 0)

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "requires POSIX suspension")
    def test_large_state_send_timeout_is_terminal_and_releases_transfer_thread(self):
        engine = self.host.engine
        os.kill(engine.owner_pid, signal.SIGSTOP)
        with self.assertRaises(AudioProcessError):
            engine._rpc("load_plugin_state", {
                "slot_index": 0, "fx_index": None,
                "state": b"x" * (1024 * 1024),
            }, timeout=0.05)
        with self.assertRaises(AudioProcessError):
            engine.set_master_gain(0.4)
        engine.shutdown()
        self.assertFalse(engine._process.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in engine._threads))

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
