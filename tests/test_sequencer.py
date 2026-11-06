"""Shared-timeline scheduling behavior without audio hardware or sleeps."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vcpi import deps
from vcpi.link import LinkSync
from vcpi.sequencer import Sequencer
from vcpi.timeline import MusicalTimeline


class SequencerTimingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.events = []
        self.timeline = MusicalTimeline(LinkSync(), clock=lambda: self.now)
        engine = SimpleNamespace(
            tracks=[SimpleNamespace(generation=index + 10) for index in range(8)],
            enqueue_midi=lambda track, message, generation=None:
                self.events.append((self.now, track, message, generation)),
        )
        self.seq = Sequencer(SimpleNamespace(engine=engine, timeline=self.timeline))
        self.addCleanup(self.seq.stop)

    def bank(self, index, notes):
        self.seq.set_bank(index, notes)
        with patch.object(self.seq, "start"):
            self.seq.link(index, index)

    def tick(self, when=None):
        if when is not None:
            self.now = when
        return self.seq._tick(self.timeline.snapshot(), deps.mido)

    def ons(self, track=0):
        return [(time, message.note) for time, ti, message, _ in self.events
                if ti == track and message.type == "note_on"]

    def offs(self, track=0):
        return [(time, message.note) for time, ti, message, _ in self.events
                if ti == track and message.type == "note_off"]

    def test_linked_pattern_waits_for_next_bar_without_auto_starting_transport(self):
        self.now = 0.3
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.tick()
        self.tick(1.99)
        self.assertEqual(self.ons(), [])
        self.tick(2)
        self.assertEqual(self.ons(), [(2, 60)])
        self.timeline.stop()
        self.bank(1, ["C5"])
        self.assertFalse(self.timeline.running)

    def test_link_after_start_still_waits_for_next_bar(self):
        self.timeline.start()
        self.bank(0, ["C4"])
        self.tick()
        self.assertEqual(self.ons(), [])
        self.tick(2)
        self.assertEqual(self.ons(), [(2, 60)])

    def test_stale_sample_cannot_replay_reset_before_a_bank_was_linked(self):
        stale = self.timeline.snapshot()
        self.timeline.start()
        self.bank(0, ["C4", "E4", "G4", "C5"])
        # The worker sampled before Start, then link() saw the newer revision.
        self.seq._tick(stale, deps.mido)
        self.tick()
        self.assertEqual(self.ons(), [])
        self.tick(2)
        self.assertEqual(self.ons(), [(2, 60)])
        # Delayed old samples must not release or retrigger active notes either.
        self.seq._tick(stale, deps.mido)
        self.tick()
        self.assertEqual(self.offs(), [])
        self.assertEqual(self.ons(), [(2, 60)])
        self.tick(2.5)
        self.assertEqual(self.ons(), [(2, 60), (2.5, 64)])

    def test_relink_after_worker_shutdown_does_not_replay_old_transport_reset(self):
        self.bank(0, ["C4"])
        self.seq.stop()
        self.timeline.start()
        with patch.object(self.seq, "start"):
            self.seq.link(0, 0)
        self.tick()
        self.assertEqual(self.ons(), [])
        self.tick(2)
        self.assertEqual(self.ons(), [(2, 60)])

    def follow_peer(self, origin=12):
        self.timeline.link = SimpleNamespace(
            enabled=True, num_peers=1, bpm=120, playing=True,
            timeline=lambda: (120, origin + self.now * 2, self.now),
            publish=lambda *args, **kwargs: None,
        )
        self.timeline.select_source("link")

    def test_source_acquisition_does_not_launch_a_pending_bank_immediately(self):
        self.bank(0, ["C4"])
        self.follow_peer()
        self.tick(2)
        self.assertEqual(self.ons(), [])
        self.tick(4)
        self.assertEqual(self.ons(), [(4, 60)])

    def test_source_phase_jump_skips_current_step_without_duplicate_or_burst(self):
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.timeline.start()
        self.tick()
        self.follow_peer()
        self.tick(2)
        self.tick()
        self.assertEqual(self.ons(), [(0, 60)])
        self.tick(2.5)
        self.assertEqual(self.ons(), [(0, 60), (2.5, 64)])

    def test_backward_source_jump_keeps_active_bank_playing_and_pending_bank_gated(self):
        self.now = 49.5
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.tick(50)  # Launch at local beat 100.
        self.now = 50.5
        self.bank(1, ["C5", "D5", "E5", "F5"])
        self.follow_peer(origin=-96)
        self.tick(52)  # Acquire external beat eight, behind both launch gates.
        self.tick()
        self.assertEqual(self.ons(), [(50, 60)])
        self.assertEqual(self.offs(), [(52, 60)])
        self.assertEqual(self.ons(1), [])
        self.tick(52.5)
        self.assertEqual(self.ons(), [(50, 60), (52.5, 64)])
        self.assertEqual(self.ons(1), [])
        self.tick(54)
        self.assertEqual(self.ons(1), [(54, 72)])

    def test_midi_start_fires_first_step_at_zero_once(self):
        self.bank(0, ["C4", "E4"])
        self.now = 0.5
        self.timeline.midi_message([0xFA])
        self.tick()
        self.tick()
        self.assertEqual(self.ons(), [(0.5, 60)])
        self.tick(0.51)
        self.assertEqual(len(self.ons()), 1)

    def test_mixed_pattern_lengths_share_each_bar_with_wake_jitter(self):
        self.bank(0, ["C4", "E4", "G4"])
        self.bank(1, ["C5", "D5", "E5", "F5"])
        self.timeline.start()
        for when in (0, 0.504, 2 / 3 + 0.004, 1.004, 4 / 3 + 0.004, 1.504, 2.004):
            self.tick(when)
        self.assertEqual([note for _, note in self.ons(0)], [60, 64, 67, 60])
        self.assertEqual([note for _, note in self.ons(1)], [72, 74, 76, 77, 72])
        self.assertEqual(self.ons(0)[-1][0], self.ons(1)[-1][0])

    def test_late_wake_skips_missed_steps_without_catchup_burst(self):
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.timeline.start()
        for when in (0, 1.3, 1.5, 2):
            self.tick(when)
        self.assertEqual(self.ons(), [(0, 60), (1.3, 67), (1.5, 72), (2, 60)])

    def test_expired_gate_is_not_emitted_after_long_stall(self):
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.timeline.start()
        self.tick()
        self.tick(1.49)
        self.assertEqual(self.ons(), [(0, 60)])
        self.tick(1.5)
        self.assertEqual(self.ons()[-1], (1.5, 72))

    def test_tempo_change_preserves_cursor_and_note_gate_in_beats(self):
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.timeline.start()
        self.tick()
        self.now = 0.25
        self.timeline.bpm = 60
        self.tick()
        self.tick(0.65)
        self.assertEqual(self.offs(), [(0.65, 60)])
        self.tick(0.75)
        self.assertEqual(self.ons(), [(0, 60), (0.75, 64)])
        self.tick(0.76)
        self.assertEqual(len(self.ons()), 2)

    def test_stop_silences_held_note_continue_resumes_without_retrigger(self):
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.timeline.start()
        self.tick()
        self.now = 0.1
        self.timeline.stop()
        self.tick()
        self.assertEqual(self.offs(), [(0.1, 60)])
        self.tick(10)
        self.timeline.resume()
        self.tick()
        self.assertEqual(self.ons(), [(0, 60)])
        self.tick(10.4)
        self.assertEqual(self.ons()[-1], (10.4, 64))

    def test_continue_on_an_unplayed_boundary_does_not_drop_the_step(self):
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.timeline.start()
        self.tick()
        self.now = 0.5
        self.timeline.stop()
        self.tick()
        self.now = 5
        self.timeline.resume()
        self.tick()
        self.assertEqual(self.ons(), [(0, 60), (5, 64)])

    def test_song_position_seeks_current_step_without_burst(self):
        self.bank(0, ["C4", "E4", "G4", "C5"])
        self.timeline.start()
        self.tick()
        self.timeline.midi_message([0xF2, 24, 0])  # beat six, third note
        self.tick()
        self.tick()
        self.assertEqual([note for _, note in self.ons()], [60, 67])
        self.assertEqual(len(self.offs()), 1)

    def test_detaching_one_bank_releases_only_its_note(self):
        self.bank(0, ["C4"])
        self.bank(1, ["E4"])
        self.timeline.start()
        self.tick()
        self.seq.detach_bank(0)
        self.assertEqual(self.offs(0), [(0, 60)])
        self.assertEqual(self.offs(1), [])
        self.tick(2)
        self.assertEqual(len(self.ons(0)), 1)
        self.assertEqual(len(self.ons(1)), 2)

    def test_generation_safe_shutdown_releases_once_even_if_worker_already_failed(self):
        self.bank(0, ["C4"])
        self.timeline.start()
        self.tick()
        self.seq._host.engine.tracks[0].generation = 99
        self.seq.stop()
        self.seq.stop()
        self.assertEqual(self.offs(), [(0, 60)])
        self.assertEqual([generation for _, _, _, generation in self.events], [10, 10])
        self.assertTrue(self.timeline.running)

    def test_shutdown_does_not_repeat_completed_note_off(self):
        self.bank(0, ["C4"])
        self.timeline.start()
        self.tick()
        self.tick(1.8)
        self.seq.stop()
        self.assertEqual(self.offs(), [(1.8, 60)])

    def test_restore_preserves_patterns_and_defers_launch_to_next_bar(self):
        self.bank(0, ["C4", "D4", "E4"])
        saved = self.seq.snapshot()
        self.timeline.start()
        self.now = 0.75
        with patch.object(self.seq, "start"):
            self.seq.restore(saved)
        self.assertEqual(self.seq.snapshot(), saved)
        self.tick()
        self.assertEqual(self.ons(), [])
        self.tick(2)
        self.assertEqual(self.ons(), [(2, 60)])


if __name__ == "__main__":
    unittest.main()
