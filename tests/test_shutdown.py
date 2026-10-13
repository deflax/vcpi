"""Shutdown ownership and pending-note regressions without audio hardware."""

import threading
import unittest
import weakref
from types import SimpleNamespace

from core import deps
from core.engine import AudioEngine
from core.models import InstrumentSlot
from core.host import VcpiCore


class ShutdownTests(unittest.TestCase):
    def test_engine_releases_plugin_cycles_before_returning(self):
        released_on: list[int] = []

        class PluginProbe:
            def __init__(self):
                # Model the plugin -> parameter cache -> plugin cycle.
                self.parameter = SimpleNamespace(plugin=self)

            def __del__(self):
                released_on.append(threading.get_ident())

        engine = AudioEngine()
        instrument = PluginProbe()
        effect = PluginProbe()
        master = PluginProbe()
        refs = [weakref.ref(plugin) for plugin in (instrument, effect, master)]
        engine.slots[0] = InstrumentSlot(
            "probe", "/tmp/probe.vst3", instrument, effects=[effect],
            _effects_board=SimpleNamespace(plugin=effect),
        )
        engine.master_effects.append(master)
        engine._master_board = SimpleNamespace(plugin=master)
        del instrument, effect, master
        try:
            engine.shutdown()
            self.assertTrue(all(ref() is None for ref in refs))
            self.assertEqual(released_on, [threading.get_ident()] * 3)
            engine.shutdown()
            self.assertEqual(len(released_on), 3)
        finally:
            engine.shutdown()

    def test_stopping_sequence_delivers_pending_note_off_once(self):
        host = VcpiCore()
        seq = host.sequencer
        seq._running = True
        seq._schedule_note_offs([(0, 60, 60.0)], deps.mido)
        timers = list(seq._note_off_timers)
        try:
            seq.stop()
            messages = list(host.engine._midi_queue)
            self.assertEqual(
                [(slot, message.type, message.note) for slot, message in messages],
                [(0, "note_off", 60)],
            )
            self.assertTrue(all(not timer.is_alive() for timer in timers))
            seq.stop()
            self.assertEqual(list(host.engine._midi_queue), messages)
        finally:
            seq.stop()
            for timer in timers:
                timer.cancel()
                timer.join()
            host.engine.shutdown()

    def test_stopping_sequence_does_not_repeat_completed_note_off(self):
        host = VcpiCore()
        seq = host.sequencer
        seq._running = True
        seq._schedule_note_offs([(0, 60, 0.0)], deps.mido)
        timers = list(seq._note_off_timers)
        try:
            for timer in timers:
                timer.join(timeout=2.0)
            self.assertTrue(all(not timer.is_alive() for timer in timers))
            seq.stop()
            self.assertEqual(
                [(slot, message.type, message.note)
                 for slot, message in host.engine._midi_queue],
                [(0, "note_off", 60)],
            )
        finally:
            seq.stop()
            for timer in timers:
                timer.cancel()
                timer.join()
            host.engine.shutdown()


if __name__ == "__main__":
    unittest.main()
