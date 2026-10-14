"""Consumer-visible native audio rendering, without opening an output device."""

import struct
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

import mido
import numpy as np

from core.audio_deps import require_native
from core.engine import AudioEngine
from core.models import InstrumentSlot
from core.plugin_host import NativePlugin
from core.audio_state import capture_plugin


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

    def close_runners(self):
        for index, runner, transport in self.runners:
            self.engine.clear_slot(index)
            runner.shutdown()
            if hasattr(transport, "close"):
                transport.close()

    def slot(self, index, velocity=127):
        plugin = NativePlugin(str(self.sample), 44100, 64, "wav")
        transport = self.backend.Transport(str(self.root / f"slot-{index}.mmap"), 64, True)
        runner = self.backend.SlotRunner(transport, plugin.native, 44100, 64)
        self.runners.append((index, runner, transport))
        slot = InstrumentSlot("sample", str(self.sample), plugin, gain=1.0,
                              render_owner=SimpleNamespace(transport=transport))
        self.engine.publish_slot(index, slot)
        self.engine.enqueue_midi(index, mido.Message("note_on", note=60, velocity=velocity))
        return slot

    def block(self):
        result = self.engine.render_block()
        deadline = time.monotonic() + 5
        for _, _, transport in self.runners:
            sequence = transport.stats()["sequence"]
            while transport.stats()["submitted"] < sequence:
                if time.monotonic() >= deadline:
                    self.fail("native slot failed to finish offline block")
                time.sleep(0.001)
        return result

    def test_first_block_silent_then_stereo_sampler_audio(self):
        self.slot(0)
        np.testing.assert_array_equal(self.block(), np.zeros((64, 2), np.float32))
        np.testing.assert_allclose(self.block(), 0.25, atol=1e-6)
        self.assertEqual(self.engine.callback_count, 2)

    def test_gain_mute_solo_master_and_sparse_slots(self):
        self.slot(0)
        self.slot(7)
        self.block()
        np.testing.assert_allclose(self.block(), 0.5, atol=1e-6)
        self.engine.set_slot_state(0, gain=0.5)
        np.testing.assert_allclose(self.block(), 0.375, atol=1e-6)
        self.engine.set_slot_state(7, solo=True)
        np.testing.assert_allclose(self.block(), 0.25, atol=1e-6)
        self.engine.set_slot_state(7, muted=True)
        np.testing.assert_array_equal(self.block(), 0)
        self.engine.set_slot_state(7, muted=False, solo=False)
        self.engine.set_master_gain(8)
        np.testing.assert_allclose(self.block(), 1, atol=1e-6)
        self.assertAlmostEqual(self.engine.stats()["output_peak"], 1.0)

    def test_muted_slot_still_consumes_midi_and_advances(self):
        self.slot(0, velocity=64)
        self.engine.set_slot_state(0, muted=True)
        self.block()
        np.testing.assert_array_equal(self.block(), 0)
        self.engine.set_slot_state(0, muted=False)
        np.testing.assert_allclose(self.block(), 0.25 * 64 / 127, atol=1e-6)

    def test_brief_negative_signal_peaks_survive_later_silent_blocks(self):
        with wave.open(str(self.sample), "wb") as sample:
            sample.setnchannels(1)
            sample.setsampwidth(2)
            sample.setframerate(44100)
            sample.writeframes(struct.pack("<h", -16384) + bytes(2 * 65535))
        slot = self.slot(0)
        self.engine.set_master_gain(0.5)
        np.testing.assert_array_equal(self.block(), 0)
        self.assertAlmostEqual(slot.render_owner.transport.stats()["audio_peak"], 0.5)
        self.assertEqual(self.engine.stats()["output_peak"], 0)
        audible = self.block()
        self.assertAlmostEqual(float(audible[0, 0]), -0.25)
        self.assertAlmostEqual(float(audible[0, 1]), -0.25)
        np.testing.assert_array_equal(audible[1:], 0)
        self.assertAlmostEqual(self.engine.stats()["output_peak"], 0.25)
        for _ in range(3):
            np.testing.assert_array_equal(self.block(), 0)
        self.assertAlmostEqual(slot.render_owner.transport.stats()["audio_peak"], 0.5)
        self.assertAlmostEqual(self.engine.stats()["output_peak"], 0.25)

    def test_muted_slot_has_signal_peak_without_output_peak(self):
        slot = self.slot(0)
        self.engine.set_slot_state(0, muted=True)
        np.testing.assert_array_equal(self.block(), 0)
        np.testing.assert_array_equal(self.block(), 0)
        self.assertAlmostEqual(slot.render_owner.transport.stats()["audio_peak"], 0.25)
        self.assertEqual(self.engine.stats()["output_peak"], 0)

    def test_midi_counters_track_publication_and_render_consumption(self):
        plugin = NativePlugin(str(self.sample), 44100, 64, "wav")
        transport = self.backend.Transport(str(self.root / "midi.mmap"), 64, True)
        peer = self.backend.Transport(str(self.root / "midi.mmap"), 64, False)
        runner = self.backend.SlotRunner(transport, plugin.native, 44100, 64)
        self.runners.append((0, runner, transport))
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (0, 0, 0))
        transport.send_midi(bytes((0x90, 60, 127)))
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (1, 0, 1))
        runner.barrier()
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (1, 0, 1))
        np.testing.assert_allclose(runner.render_once(), 0.25, atol=1e-6)
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (1, 1, 0))
        self.assertAlmostEqual(stats["audio_peak"], 0.25)
        transport.send_midi(bytes((0x80, 60, 0)))
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (2, 1, 1))
        runner.render_once()
        stats = peer.stats()
        self.assertEqual((stats["midi_sent"], stats["midi_consumed"], stats["midi_pending"]), (2, 2, 0))

    def test_generation_filter_and_retirement_discard_stopped_midi(self):
        slot = self.slot(0)
        slot.generation = 4
        self.assertFalse(self.engine.enqueue_midi(0, mido.Message("note_on"), generation=3))
        self.assertEqual(len(self.engine._midi_queue), 1)
        retired = self.engine.clear_slot(0)
        self.assertIs(retired, slot)
        self.assertFalse(self.engine._midi_queue)
        np.testing.assert_array_equal(self.block(), 0)

    def test_control_fifo_and_plain_sampler_metadata(self):
        slot = self.slot(0)
        self.engine.enqueue_slot_change(0, gain=0.5)
        self.engine.enqueue_slot_change(0, gain=0.25, muted=True)
        self.engine.flush_controls()
        self.assertEqual(slot.gain, 0.25)
        self.assertTrue(slot.muted)
        snapshot = capture_plugin(slot.plugin, {}, {})
        self.assertEqual(snapshot.info_type, "Sample")
        self.assertEqual(snapshot.path_to_plugin_file, str(self.sample))
        self.assertEqual(snapshot.parameters, {})
        self.assertEqual(snapshot.values, {})

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

    def test_slot_midi_overflow_does_not_silence_other_slots(self):
        failed = self.slot(0)
        self.slot(1)
        for _ in range(4096):
            failed.render_owner.transport.send_midi(bytes((0x90, 60, 127)))
        # Flushing the queued first note exceeds only slot zero's FIFO.
        np.testing.assert_array_equal(self.block(), 0)
        self.assertIsNotNone(failed.render_owner.failure)
        np.testing.assert_allclose(self.block(), 0.25, atol=1e-6)
        self.assertEqual(self.engine.render_errors, 0)


if __name__ == "__main__":
    unittest.main()