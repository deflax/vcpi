"""Freewheel note-on timing regressions (no audio hardware required)."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import deps

from core.engine import AudioEngine
from core.sequencer import Sequencer


class FreewheelTimingTests(unittest.TestCase):
    def run_pattern(
        self, patterns: list[list[str]], duration: float,
        overshoots: tuple[float, ...] = (),
    ) -> list[tuple[float, int, int]]:
        clock = SimpleNamespace(now=0.0)
        events: list[tuple[float, int, int]] = []

        def enqueue(slot, message, generation=None):
            events.append((clock.now, slot, message.note))

        host = SimpleNamespace(engine=AudioEngine(), link=SimpleNamespace(bpm=120.0, enabled=False))
        seq = Sequencer(host)
        for index, notes in enumerate(patterns):
            bank = seq.set_bank(index, notes)
            bank.linked_slot = index

        delays = iter(overshoots)

        def wait(timeout: float) -> None:
            clock.now += timeout + next(delays, 0.0)
            if clock.now >= duration:
                seq._running = False

        seq._running = True
        try:
            with patch("core.sequencer.time", SimpleNamespace(monotonic=lambda: clock.now)), \
                    patch.object(host.engine, "enqueue_midi", side_effect=enqueue), \
                    patch.object(seq, "_stop_event", SimpleNamespace(wait=wait)), \
                    patch.object(seq, "_schedule_note_offs"):
                seq._run_freewheel(deps.mido)
        finally:
            seq._running = False
            host.engine.shutdown()
        return events

    def test_one_trigger_per_step_despite_wake_jitter(self):
        events = self.run_pattern(
            [["C4", "E4", "G4", "C5"]], 2.1,
            overshoots=(0.004, 0.007, 0.003, 0.005),
        )
        self.assertEqual([note for _, _, note in events], [60, 64, 67, 72, 60])
        for (timestamp, _, _), expected in zip(events, (0, 0.5, 1, 1.5, 2)):
            self.assertAlmostEqual(timestamp, expected, delta=0.01)

    def test_mixed_pattern_lengths_share_bar_boundaries(self):
        events = self.run_pattern(
            [["C4", "E4", "G4"], ["C5", "D5", "E5", "F5"]], 2.1,
            overshoots=(0.004,) * 13,
        )
        triples = [(timestamp, note) for timestamp, slot, note in events if slot == 0]
        quarters = [(timestamp, note) for timestamp, slot, note in events if slot == 1]
        self.assertEqual([note for _, note in triples], [60, 64, 67, 60])
        self.assertEqual([note for _, note in quarters], [72, 74, 76, 77, 72])
        for (timestamp, _), expected in zip(triples, (0, 2 / 3, 4 / 3, 2)):
            self.assertAlmostEqual(timestamp, expected, delta=0.01)
        for (timestamp, _), expected in zip(quarters, (0, 0.5, 1, 1.5, 2)):
            self.assertAlmostEqual(timestamp, expected, delta=0.01)

    def test_late_wake_skips_missed_steps_without_catchup_burst(self):
        events = self.run_pattern(
            [["C4", "E4", "G4", "C5"]], 2.1, overshoots=(0.8,),
        )
        self.assertEqual([note for _, _, note in events], [60, 67, 72, 60])
        self.assertEqual([timestamp for timestamp, _, _ in events], [0, 1.3, 1.5, 2])


if __name__ == "__main__":
    unittest.main()
