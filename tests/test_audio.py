"""Offline audio regressions: scheduling transitions, queue boundaries, and FX rollback."""

import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from pedalboard import Gain, Pedalboard

from core.engine import AudioEngine, _RenderWorker
from core.plugin_host import PluginHost
from core.models import InstrumentSlot, NUM_SLOTS
from sampler.plugin import WavSamplerPlugin


def note(kind="note_on", note=60, velocity=127):
    return SimpleNamespace(type=kind, note=note, velocity=velocity, time=0)


class HeldNoteInstrument:
    def __init__(self):
        self.held = set()
        self.amplitude = 0.2

    def process(self, messages, *, duration, sample_rate, num_channels, buffer_size, reset):
        for message in messages:
            if message.type == "note_on" and message.velocity:
                self.held.add(message.note)
            elif message.type in ("note_off", "note_on"):
                self.held.discard(message.note)
        return np.full((num_channels, buffer_size),
                       self.amplitude if self.held else 0, dtype=np.float32)


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.engine = AudioEngine(buffer_size=8)
        self.engine.render_workers = 2
        self.engine._prepare_buffers(8)
        self.engine._workers = [
            _RenderWorker(self.engine, idx) for idx in range(self.engine.render_workers)]
        self.addCleanup(self.engine.shutdown)

    def block(self, frames=8):
        out = np.empty((frames, 2), dtype=np.float32)
        self.engine._callback(out, frames, None, None)
        return out

    def slot(self, index, amplitude=0.2):
        instrument = HeldNoteInstrument()
        instrument.amplitude = amplitude
        slot = InstrumentSlot("held", "held", instrument, gain=1)
        self.engine.slots[index] = slot
        self.engine.enqueue_midi(index, note())
        return slot

    def test_inline_parallel_and_solo_transitions_preserve_sound(self):
        first = self.slot(0)
        np.testing.assert_allclose(self.block(), 0.2)
        second = self.slot(1, 0.3)
        np.testing.assert_allclose(self.block(), 0.5)
        first.solo = True
        np.testing.assert_allclose(self.block(), 0.2)
        first.solo = False
        second.muted = True
        np.testing.assert_allclose(self.block(), 0.2)
        second.muted = False
        first.gain = 0.5
        self.engine.master_gain = 0.5
        np.testing.assert_allclose(self.block(5), 0.2)

    def test_more_slots_than_workers_and_sparse_high_slots(self):
        for idx in range(NUM_SLOTS):
            self.slot(idx, amplitude=(idx + 1) / 100)
        np.testing.assert_allclose(self.block(), 0.36, atol=1e-6)
        for slot in self.engine.slots[:6]:
            slot.muted = True
        np.testing.assert_allclose(self.block(), 0.15, atol=1e-6)

    def test_failed_slot_does_not_interrupt_other_slots_or_next_block(self):
        class BrokenInstrument(HeldNoteInstrument):
            def process(self, *args, **kwargs):
                raise RuntimeError("instrument failure")

        self.slot(0)
        self.engine.slots[7] = InstrumentSlot("broken", "broken", BrokenInstrument())
        np.testing.assert_allclose(self.block(), 0.2)
        np.testing.assert_allclose(self.block(), 0.2)
        self.assertEqual(self.engine.render_errors, 2)

    def test_queue_budget_defers_but_does_not_lose_note_off(self):
        self.slot(0)
        for _ in range(self.engine._midi_budget - 1):
            self.engine.enqueue_midi(0, note())
        self.engine.enqueue_midi(0, note("note_off"))
        np.testing.assert_allclose(self.block(), 0.2)
        np.testing.assert_array_equal(self.block(), 0)
        np.testing.assert_array_equal(self.block(), 0)

    def test_parameter_budget_preserves_last_value_in_each_block(self):
        self.slot(0)
        for _ in range(self.engine._control_budget):
            self.engine.enqueue_param_change(0, "amplitude", 0.3)
        self.engine.enqueue_param_change(0, "amplitude", 0.4)
        np.testing.assert_allclose(self.block(), 0.3)
        np.testing.assert_allclose(self.block(), 0.4)

    def test_timeout_cannot_apply_a_control_in_a_later_block(self):
        self.slot(0)
        self.engine._running = True
        self.engine._control_timeout = 0.01
        with self.assertRaises(TimeoutError):
            self.engine.set_parameter(0, "amplitude", 0.9)
        np.testing.assert_allclose(self.block(), 0.2)

    def test_bool_surrogate_and_enum_controls_change_audio_with_acknowledgments(self):
        class BooleanValue:
            type = bool

            def __init__(self, value):
                self.value = value

            def __bool__(self):
                return self.value

        class Instrument(HeldNoteInstrument):
            def __init__(self):
                super().__init__()
                self._bypass = False
                self.mode = "Normal"

            @property
            def bypass(self):
                return BooleanValue(self._bypass)

            @bypass.setter
            def bypass(self, value):
                self._bypass = bool(value)

            def process(self, *args, **kwargs):
                audio = super().process(*args, **kwargs)
                audio *= 0 if self._bypass else 0.5 if self.mode == "Quiet Mode" else 1
                return audio

        self.engine.slots[0] = InstrumentSlot("controls", "controls", Instrument(), gain=1)
        self.engine.enqueue_midi(0, note())
        np.testing.assert_allclose(self.block(), 0.2)
        self.engine.set_parameter(0, "mode", "Quiet Mode")
        np.testing.assert_allclose(self.block(), 0.1)
        self.assertIs(self.engine.set_parameter(0, "bypass", True), True)
        np.testing.assert_array_equal(self.block(), 0)
        self.engine.set_parameter(0, "bypass", False)
        np.testing.assert_allclose(self.block(), 0.1)

    def test_old_generation_note_off_cannot_silence_a_replacement(self):
        self.slot(0).generation = 1
        np.testing.assert_allclose(self.block(), 0.2)
        replacement = self.slot(0)
        replacement.generation = 2
        self.engine.enqueue_midi(0, note("note_off"), generation=1)
        np.testing.assert_allclose(self.block(), 0.2)
        self.engine.enqueue_midi(0, note("note_off"), generation=2)
        np.testing.assert_array_equal(self.block(), 0)

    def test_gain_update_waits_for_the_next_render_boundary(self):
        slot = self.slot(0)
        np.testing.assert_allclose(self.block(), 0.2)
        entered, release = threading.Event(), threading.Event()
        original_process = slot.plugin.process
        outputs, errors = [], []

        def render(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test render gate timed out")
            return original_process(*args, **kwargs)

        def update():
            try:
                self.engine.set_slot_state(0, gain=0.5)
            except Exception as error:
                errors.append(error)

        slot.plugin.process = render
        self.engine._running = True
        callback = threading.Thread(target=lambda: outputs.append(self.block()))
        setter = threading.Thread(target=update)
        try:
            callback.start()
            self.assertTrue(entered.wait(2))
            setter.start()
            deadline = time.monotonic() + 2
            while not self.engine.pending_control_count and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(slot.gain, 1)
            release.set()
            callback.join(timeout=2)
            self.assertFalse(callback.is_alive())
            np.testing.assert_allclose(outputs[0], 0.2)
            np.testing.assert_allclose(self.block(), 0.1)
            setter.join(timeout=2)
            self.assertFalse(setter.is_alive())
            self.assertEqual(errors, [])
        finally:
            release.set()
            callback.join(timeout=2)
            if setter.ident is not None:
                setter.join(timeout=2)

    def test_mono_insert_master_and_clipping(self):
        sample = np.ones((1, 24), dtype=np.float32)
        sampler = WavSamplerPlugin("mono", sample, output_channels=1)
        slot = InstrumentSlot("mono", "mono", sampler, gain=0.8)
        slot._effects_board = Pedalboard([Gain(gain_db=-6.020599913279624)])
        self.engine.slots[0] = slot
        self.engine._master_board = Pedalboard([Gain(gain_db=6.020599913279624)])
        self.engine.enqueue_midi(0, note())
        np.testing.assert_allclose(self.block(), 0.8, atol=1e-6)
        self.engine.master_gain = 2
        np.testing.assert_array_equal(self.block(), 1)
        np.testing.assert_array_equal(self.block(), 1)
        np.testing.assert_array_equal(self.block(), 0)

    def test_failed_fx_warmup_preserves_old_audible_chain(self):
        class FailingGain(Gain):
            is_instrument = False

            def process(self, *args, **kwargs):
                raise RuntimeError("warmup failed")

        host = PluginHost(buffer_size=8)
        host.engine.shutdown()
        host.engine = self.engine
        slot = self.slot(0)
        slot.effects = [Gain(gain_db=-6.020599913279624)]
        slot._effects_board = Pedalboard(slot.effects)
        with patch("core.plugin_host.deps.load_plugin", return_value=FailingGain()):
            with self.assertRaisesRegex(RuntimeError, "warmup failed"):
                host.load_effect("failure", slot_index=0)
        np.testing.assert_allclose(self.block(), 0.1, atol=1e-6)
        host.remove_effect(0, 0)
        np.testing.assert_allclose(self.block(), 0.2)


class SamplerTests(unittest.TestCase):
    def test_interpolation_pitch_tail_and_empty_blocks(self):
        for channels in (1, 2):
            for midi_note, rate in ((48, 0.5), (60, 1), (72, 2)):
                with self.subTest(channels=channels, note=midi_note):
                    sample = np.array([[0.0, 0.2, 0.4, 0.6, 0.8]], dtype=np.float32)
                    sampler = WavSamplerPlugin("ramp", sample, channels)
                    sampler.send_midi(note(note=midi_note, velocity=64))
                    position = 0.0
                    for frames in (0, 3, 2, 7, 3):
                        expected = np.zeros(frames, dtype=np.float32)
                        for idx in range(frames):
                            pos = position + idx * rate
                            if pos < 5:
                                left = int(pos)
                                right = min(left + 1, 4)
                                expected[idx] = ((1 - (pos - left)) * sample[0, left]
                                                 + (pos - left) * sample[0, right]) * (64 / 127)
                        output = sampler.process(np.zeros((channels, frames), dtype=np.float32), 44100)
                        np.testing.assert_allclose(output, np.broadcast_to(expected, output.shape), atol=1e-7)
                        position += frames * rate

    def test_voice_limit_retires_oldest_voice(self):
        sampler = WavSamplerPlugin("ones", np.ones((1, 4), dtype=np.float32), 1, max_voices=1)
        sampler.send_midi(note(velocity=127))
        sampler.send_midi(note(velocity=32))
        np.testing.assert_allclose(sampler.process(np.zeros((1, 4), dtype=np.float32), 44100), 32 / 127)
        np.testing.assert_array_equal(sampler.process(np.zeros((1, 4), dtype=np.float32), 44100), 0)


if __name__ == "__main__":
    unittest.main()
