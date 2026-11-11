"""Consumer-visible native audio rendering, without opening an output device."""

import os
import struct
import tempfile
import time
import sys
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mido
import numpy as np

from vcpi.audio_deps import require_native
from vcpi.audio.control import AudioEngine
from vcpi.deck_bank import make_catalog
from vcpi.models import InstrumentTrack
from vcpi.plugin_host import NativePlugin
from vcpi.audio_state import capture_plugin


class NativeAudioTests(unittest.TestCase):
    def setUp(self):
        self.backend = require_native()
        self.directory = tempfile.TemporaryDirectory(prefix="vcpi-native-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sample = self.root / "sample.wav"
        with wave.open(str(self.sample), "wb") as sample:
            sample.setnchannels(1)
            sample.setsampwidth(2)
            sample.setframerate(44100)
            sample.writeframes(struct.pack("<h", 8192) * 65536)
        self.engine = AudioEngine(buffer_size=64)
        self.addCleanup(self.engine.shutdown)
        self.runners = []
        self.addCleanup(self.close_runners)
        # Mixer assertions are relative to decoded source amplitude, not the
        # deck resampler's filter gain.
        transport = self.backend.Transport(str(self.root / "reference.mmap"), 64, True)
        runner = self.backend.TrackRunner(transport, self.plugin().native, 44100, 64)
        try:
            for _ in range(16):
                reference = runner.render_once()
            self.level = float(reference[-1, 0])
        finally:
            runner.shutdown()

    def close_runners(self):
        for index, runner, transport in self.runners:
            self.engine.clear_track(index)
            runner.shutdown()
            if hasattr(transport, "close"):
                transport.close()

    def plugin(self):
        catalog = make_catalog("test-deck", [
            {"id": "audio", "path": str(self.sample), "title": "Audio"},
        ], play=True, sync=False, sync_filter=False)
        plugin = NativePlugin(catalog, 44100, 64, "deck")
        object.__setattr__(plugin, "_vcpi_path", "session:test-deck")
        return plugin

    def track(self, index):
        plugin = self.plugin()
        transport = self.backend.Transport(str(self.root / f"track-{index}.mmap"), 64, True)
        runner = self.backend.TrackRunner(transport, plugin.native, 44100, 64)
        self.runners.append((index, runner, transport))
        # Settle the deck's click-prevention ramp before testing mixer values.
        for _ in range(16):
            runner.render_once()
        track = InstrumentTrack("deck", "session:test-deck", plugin, gain=1.0,
                                source_type="deck",
                                render_owner=SimpleNamespace(transport=transport))
        self.engine.publish_track(index, track)
        self.engine.enqueue_midi(index, mido.Message("note_on", note=60, velocity=127))
        return track

    def block(self):
        result = self.engine.render_block()
        deadline = time.monotonic() + 5
        for _, _, transport in self.runners:
            sequence = transport.stats()["sequence"]
            while transport.stats()["submitted"] < sequence:
                if time.monotonic() >= deadline:
                    self.fail("native track failed to finish offline block")
                time.sleep(0.001)
        return result

    def test_first_block_silent_then_stereo_deck_audio(self):
        self.track(0)
        np.testing.assert_array_equal(self.block(), np.zeros((64, 2), np.float32))
        np.testing.assert_allclose(self.block(), self.level, atol=1e-6)
        self.assertEqual(self.engine.callback_count, 2)

    def test_gain_mute_solo_master_and_sparse_tracks(self):
        self.track(0)
        self.track(15)
        self.block()
        np.testing.assert_allclose(self.block(), self.level * 2, atol=1e-6)
        self.engine.set_track_state(0, gain=0.5)
        np.testing.assert_allclose(self.block(), self.level * 1.5, atol=1e-6)
        self.engine.set_track_state(15, solo=True)
        np.testing.assert_allclose(self.block(), self.level, atol=1e-6)
        self.engine.set_track_state(15, muted=True)
        np.testing.assert_array_equal(self.block(), 0)
        self.engine.set_track_state(15, muted=False, solo=False)
        self.engine.set_master_gain(8)
        np.testing.assert_allclose(self.block(), 1, atol=1e-6)
        self.assertAlmostEqual(self.engine.stats()["output_peak"], 1.0)

    def test_native_track_bounds_and_final_track_state(self):
        self.track(15)
        self.block()
        self.engine._native.set_track(15, 0.5, False, False)
        np.testing.assert_allclose(self.block(), self.level * 0.5, atol=1e-6)
        for index in (-1, 16):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    self.engine._native.attach(index, None)
                with self.assertRaises(ValueError):
                    self.engine._native.set_track(index, 1.0, False, False)
                np.testing.assert_allclose(self.block(), self.level * 0.5, atol=1e-6)

    def test_muted_track_still_consumes_midi_and_advances(self):
        track = self.track(0)
        position = track.plugin.native.deck_status()["position"]
        self.engine.set_track_state(0, muted=True)
        self.block()
        np.testing.assert_array_equal(self.block(), 0)
        self.engine.set_track_state(0, muted=False)
        np.testing.assert_allclose(self.block(), self.level, atol=1e-6)
        self.assertGreater(track.plugin.native.deck_status()["position"], position)
        self.assertEqual(track.render_owner.transport.stats()["midi_consumed"], 1)

    def test_brief_negative_signal_peaks_survive_later_silent_blocks(self):
        with wave.open(str(self.sample), "wb") as sample:
            sample.setnchannels(1)
            sample.setsampwidth(2)
            sample.setframerate(44100)
            sample.writeframes(bytes(2 * 2048) + struct.pack("<h", -16384) * 512
                               + bytes(2 * 8192))
        track = self.track(0)
        self.engine.set_master_gain(0.5)
        blocks = [self.block() for _ in range(48)]
        audio = np.concatenate(blocks)
        peak = float(np.abs(audio).max())
        np.testing.assert_allclose(float(audio.min()), -0.25, rtol=0.02)
        np.testing.assert_array_equal(audio[:, 0], audio[:, 1])
        self.assertAlmostEqual(track.render_owner.transport.stats()["audio_peak"], peak * 2, places=5)
        self.assertAlmostEqual(self.engine.stats()["output_peak"], peak, places=5)
        for _ in range(3):
            np.testing.assert_array_equal(self.block(), 0)
        self.assertAlmostEqual(track.render_owner.transport.stats()["audio_peak"], peak * 2, places=5)
        self.assertAlmostEqual(self.engine.stats()["output_peak"], peak, places=5)

    def test_muted_track_has_signal_peak_without_output_peak(self):
        track = self.track(0)
        self.engine.set_track_state(0, muted=True)
        np.testing.assert_array_equal(self.block(), 0)
        np.testing.assert_array_equal(self.block(), 0)
        self.assertAlmostEqual(track.render_owner.transport.stats()["audio_peak"], self.level)
        self.assertEqual(self.engine.stats()["output_peak"], 0)

    def test_midi_counters_track_publication_and_render_consumption(self):
        plugin = self.plugin()
        transport = self.backend.Transport(str(self.root / "midi.mmap"), 64, True)
        peer = self.backend.Transport(str(self.root / "midi.mmap"), 64, False)
        runner = self.backend.TrackRunner(transport, plugin.native, 44100, 64)
        self.runners.append((0, runner, transport))
        for _ in range(16):
            runner.render_once()
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (0, 0, 0))
        transport.send_midi(bytes((0x90, 60, 127)))
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (1, 0, 1))
        runner.barrier()
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (1, 0, 1))
        np.testing.assert_allclose(runner.render_once(), self.level, atol=1e-6)
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (1, 1, 0))
        self.assertAlmostEqual(stats["audio_peak"], self.level)
        transport.send_midi(bytes((0x80, 60, 0)))
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (2, 1, 1))
        runner.render_once()
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (2, 2, 0))

    def test_generation_filter_and_retirement_discard_stopped_midi(self):
        track = self.track(0)
        track.generation = 4
        self.assertFalse(self.engine.enqueue_midi(0, mido.Message("note_on"), generation=3))
        self.assertEqual(len(self.engine._midi_queue), 1)
        retired = self.engine.clear_track(0)
        self.assertIs(retired, track)
        self.assertFalse(self.engine._midi_queue)
        np.testing.assert_array_equal(self.block(), 0)

    def test_control_fifo_and_plain_deck_metadata(self):
        track = self.track(0)
        self.engine.enqueue_track_change(0, gain=0.5)
        self.engine.enqueue_track_change(0, gain=0.25, muted=True)
        self.engine.flush_controls()
        self.assertEqual(track.gain, 0.25)
        self.assertTrue(track.muted)
        snapshot = capture_plugin(track.plugin, {}, {})
        self.assertEqual(snapshot.info_type, "Deck")
        self.assertEqual(snapshot.path_to_plugin_file, "session:test-deck")
        self.assertIn("play", snapshot.parameters)
        self.assertTrue(snapshot.values["play"])

    def test_mixer_snapshot_applies_sparse_solo_and_rejects_partial_updates(self):
        self.track(0)
        self.track(15)
        self.block()
        states = [(1.0, False, False)] * 16
        states[0] = (0.5, False, False)
        states[15] = (0.25, False, True)
        self.engine._native.set_mixer(states, 0.5)
        np.testing.assert_allclose(self.block(), self.level * 0.125, atol=1e-6)
        for invalid in (-1.0, float("nan"), float("inf"), 1e39):
            with self.subTest(gain=invalid):
                rejected = list(states)
                rejected[0] = (0.0, True, False)
                rejected[15] = (invalid, False, False)
                with self.assertRaises(ValueError):
                    self.engine._native.set_mixer(rejected, 0.1)
                np.testing.assert_allclose(self.block(), self.level * 0.125, atol=1e-6)
        with self.assertRaises(ValueError):
            self.engine._native.set_mixer([(0.0, True, False)] * 16, float("nan"))
        np.testing.assert_allclose(self.block(), self.level * 0.125, atol=1e-6)
        for count in (8, 15, 17):
            with self.subTest(count=count):
                with self.assertRaises(TypeError):
                    self.engine._native.set_mixer([(0.0, True, False)] * count, 0.1)
                np.testing.assert_allclose(self.block(), self.level * 0.125, atol=1e-6)

    def test_gain_burst_preserves_fields_and_track_replacement_order(self):
        retired = self.track(0)
        for value in range(64):
            self.engine.enqueue_track_change(0, gain=value / 127.0)
            self.engine.enqueue_master_gain(value / 127.0)
        self.engine.enqueue_track_change(0, muted=True)
        replacement = self.track(0)
        self.assertIsNot(retired, replacement)
        self.engine.enqueue_track_change(0, gain=0.5)
        self.engine.enqueue_track_change(0, solo=True)
        self.engine.enqueue_master_gain(0.25)
        self.engine.flush_controls()
        self.assertEqual(retired.gain, 63 / 127.0)
        self.assertTrue(retired.muted)
        self.assertFalse(replacement.muted)
        self.assertTrue(replacement.solo)
        self.assertEqual(replacement.gain, 0.5)
        self.block()
        np.testing.assert_allclose(self.block(), self.level * 0.125, atol=1e-6)

    def test_gain_summaries_wait_for_quiet_without_delaying_applied_values(self):
        track = self.track(0)
        quiet = self.engine._gain_log_quiet_seconds
        with self.assertLogs("vcpi.audio.control", level="INFO") as captured:
            with patch("vcpi.audio.control.time.monotonic") as clock:
                for step in range(20):
                    clock.return_value = 10 + step * quiet / 2
                    gain = step / 20
                    self.engine.enqueue_track_change(0, gain=gain)
                    self.engine.enqueue_master_gain(gain)
                    self.engine.apply_pending_controls()
                    self.assertEqual(track.gain, gain)
                    self.assertEqual(self.engine.master_gain, gain)
                    self.assertEqual(captured.records, [])
                last_update = clock.return_value
                clock.return_value = last_update + quiet * 0.9
                self.engine.apply_pending_controls()
                self.assertEqual(captured.records, [])
                clock.return_value = last_update + quiet * 1.1
                self.engine.apply_pending_controls()
                self.assertEqual([record.args[-1] for record in captured.records],
                                 [19 / 20, 19 / 20])
                clock.return_value += quiet * 2
                self.engine.apply_pending_controls()
                self.assertEqual(len(captured.records), 2)

    def test_gain_summaries_are_independent_and_discard_retired_track_values(self):
        self.track(0)
        quiet = self.engine._gain_log_quiet_seconds
        with self.assertLogs("vcpi.audio.control", level="INFO") as captured:
            with patch("vcpi.audio.control.time.monotonic") as clock:
                clock.return_value = 10
                self.engine.set_track_state(0, gain=0.25)
                clock.return_value = 10 + quiet / 2
                self.engine.set_master_gain(0.5)
                clock.return_value = 10 + quiet * 1.1
                self.engine.apply_pending_controls()
                self.assertEqual([record.args[-1] for record in captured.records], [0.25])
                self.engine.set_track_state(0, gain=0.75)
                self.track(0)
                clock.return_value = 10 + quiet * 3
                self.engine.apply_pending_controls()
                self.assertEqual([record.args[-1] for record in captured.records], [0.25, 0.5])

    def test_midi_overflow_is_explicit(self):
        transport = self.backend.Transport(str(self.root / "overflow.mmap"), 64, True)
        try:
            for _ in range(4096):
                transport.send_midi(bytes((0x90, 60, 127)))
            with self.assertRaises((BufferError, RuntimeError)):
                transport.send_midi(bytes((0x80, 60, 0)))
        finally:
            if hasattr(transport, "close"):
                transport.close()

    def test_track_midi_overflow_does_not_silence_other_tracks(self):
        failed = self.track(0)
        self.track(1)
        for _ in range(4096):
            failed.render_owner.transport.send_midi(bytes((0x90, 60, 127)))
        # Flushing the queued first note exceeds only track zero's FIFO.
        np.testing.assert_array_equal(self.block(), 0)
        self.assertIsNotNone(failed.render_owner.failure)
        np.testing.assert_allclose(self.block(), self.level, atol=1e-6)
        self.assertEqual(self.engine.render_errors, 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux thread scheduling")
    def test_realtime_targets_track_thread_and_leaves_offline_control_unchanged(self):
        control_policy = os.sched_getscheduler(0)
        control_priority = os.sched_getparam(0).sched_priority
        # The macOS experiment must not disable Linux FIFO requests.
        with patch.dict(os.environ, {"VCPI_MACOS_TRACK_REALTIME": "0"}):
            track = self.track(0)
        self.block()
        np.testing.assert_allclose(self.block(), self.level, atol=1e-6)
        self.assertEqual(os.sched_getscheduler(0), control_policy)
        self.assertEqual(os.sched_getparam(0).sched_priority, control_priority)
        output = self.engine.stats()["realtime"]
        self.assertEqual(output["state"], "inactive")
        self.assertEqual(output["thread_id"], 0)
        transport = track.render_owner.transport
        realtime = transport.stats()["realtime"]
        self.assertNotEqual(realtime["thread_id"], os.getpid())
        actual_policy = os.sched_getscheduler(realtime["thread_id"])
        actual_priority = os.sched_getparam(realtime["thread_id"]).sched_priority
        self.assertEqual(realtime["priority"], actual_priority)
        policy_names = {
            os.SCHED_OTHER: "SCHED_OTHER", os.SCHED_FIFO: "SCHED_FIFO",
            os.SCHED_RR: "SCHED_RR", os.SCHED_BATCH: "SCHED_BATCH",
            os.SCHED_IDLE: "SCHED_IDLE",
        }
        self.assertEqual(realtime["policy"], policy_names.get(actual_policy, "unknown"))
        if realtime["error"]:
            self.assertEqual(realtime["state"], "denied")
            self.assertEqual(actual_policy, control_policy)
            self.assertEqual(actual_priority, control_priority)
        else:
            self.assertEqual(realtime["state"], "active")
            self.assertEqual(actual_policy, os.SCHED_FIFO)
            self.assertEqual(actual_priority, realtime["requested_priority"])
            os.sched_setscheduler(realtime["thread_id"], os.SCHED_OTHER, os.sched_param(0))
            demoted = transport.stats()["realtime"]
            self.assertEqual(demoted["state"], "degraded")
            self.assertEqual(demoted["policy"], "SCHED_OTHER")
            self.assertEqual(demoted["priority"], 0)

    @unittest.skipUnless(sys.platform == "darwin", "macOS thread scheduling")
    def test_macos_track_uses_audio_deadline_without_promoting_control(self):
        import ctypes as ct

        # Query the actual Mach thread, not just vcpi's startup report.
        system = ct.CDLL("/usr/lib/libSystem.B.dylib")
        uint = ct.c_uint32
        system.pthread_self.restype = ct.c_void_p
        system.pthread_mach_thread_np.argtypes = [ct.c_void_p]
        system.pthread_mach_thread_np.restype = uint
        system.thread_policy_get.argtypes = [
            uint, uint, ct.c_void_p, ct.POINTER(uint), ct.POINTER(ct.c_int)]
        system.task_threads.argtypes = [
            uint, ct.POINTER(ct.POINTER(uint)), ct.POINTER(uint)]
        system.thread_info.argtypes = [uint, uint, ct.c_void_p, ct.POINTER(uint)]
        system.mach_port_deallocate.argtypes = [uint, uint]
        system.vm_deallocate.argtypes = [uint, ct.c_size_t, ct.c_size_t]
        task = uint.in_dll(system, "mach_task_self_").value
        caller = system.pthread_mach_thread_np(system.pthread_self())

        def policy(port):
            values = (uint * 4)()
            count, default = uint(4), ct.c_int(0)
            self.assertEqual(system.thread_policy_get(
                port, 2, values, ct.byref(count), ct.byref(default)), 0)
            return tuple(values), default.value

        before = policy(caller)
        for index, enabled in enumerate((False, True)):
            with self.subTest(promotion_enabled=enabled):
                with patch.dict(os.environ, {"VCPI_MACOS_TRACK_REALTIME": str(int(enabled))}):
                    track = self.track(index)
                self.block()
                np.testing.assert_allclose(self.block(), self.level * (index + 1), atol=1e-6)
                realtime = track.render_owner.transport.stats()["realtime"]
                self.assertEqual(realtime["state"], "configured" if enabled else "disabled", realtime)
                self.assertEqual(policy(caller), before)

                ports, count = ct.POINTER(uint)(), uint()
                self.assertEqual(system.task_threads(task, ct.byref(ports), ct.byref(count)), 0)
                try:
                    worker_policy = None
                    for port in ports[:count.value]:
                        identity, size = (ct.c_uint64 * 3)(), uint(6)
                        if system.thread_info(port, 4, identity, ct.byref(size)) == 0:
                            if identity[0] == realtime["thread_id"]:
                                worker_policy = policy(port)
                                break
                    self.assertIsNotNone(worker_policy, "Reported track thread must exist")
                    values, default = worker_policy
                    self.assertEqual(default, 0 if enabled else 1)
                    if enabled:
                        timebase = (uint * 2)()
                        system.mach_timebase_info.argtypes = [ct.c_void_p]
                        self.assertEqual(system.mach_timebase_info(timebase), 0)
                        expected = int(64 / 44100 * 1e9 * timebase[1] / timebase[0])
                        self.assertAlmostEqual(values[0], expected, delta=1)
                        self.assertAlmostEqual(values[1], expected / 2, delta=1)
                        self.assertEqual(values[2], values[0])
                    else:
                        self.assertEqual(realtime["requested_policy"], "none")
                        self.assertEqual(realtime["policy"], "unchanged")
                finally:
                    for port in ports[:count.value]:
                        system.mach_port_deallocate(task, port)
                    system.vm_deallocate(task, ct.cast(ports, ct.c_void_p).value,
                                         count.value * ct.sizeof(uint))


if __name__ == "__main__":
    unittest.main()