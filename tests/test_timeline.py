"""Deterministic source arbitration, phase and transport regressions."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from vcpi.link import LinkSync
from vcpi.timeline import MusicalTimeline


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class PeerLink:
    def __init__(self, clock):
        self.clock = clock
        self.bpm = 120.0
        self.enabled = False
        self.num_peers = 0
        self.playing = True
        self.origin = 0.0
        self.publications = []

    def timeline(self):
        return self.bpm, self.origin + self.clock() * self.bpm / 60, self.clock()

    def publish(self, bpm, beat, running, *, phase=False, transport=False):
        if not self.enabled:
            return
        self.publications.append((bpm, beat, running, phase, transport))
        self.bpm = bpm
        if phase:
            self.origin = beat - self.clock() * bpm / 60
        if transport:
            self.playing = running


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.link = PeerLink(self.clock)
        self.timeline = MusicalTimeline(self.link, clock=self.clock)

    def pulse(self, count=1, bpm=120):
        for _ in range(count):
            self.clock.now += 60 / bpm / 24
            self.timeline.midi_message([0xF8], self.clock.now)

    def test_tempo_changes_preserve_beat_and_sample_time(self):
        self.clock.now = 2
        self.assertEqual(self.timeline.timeline(), (120, 4, 2))
        self.timeline.bpm = 90
        self.assertEqual(self.timeline.timeline(), (90, 4, 2))
        self.clock.now = 4
        self.assertEqual(self.timeline.timeline(), (90, 7, 4))

    def test_stop_freezes_continue_preserves_start_rewinds(self):
        self.clock.now = 1.25
        self.timeline.stop()
        revision = self.timeline.revision
        self.clock.now = 10
        self.assertEqual(self.timeline.timeline()[1], 2.5)
        self.timeline.resume()
        self.assertEqual(self.timeline.revision, revision)
        self.clock.now = 10.25
        self.assertEqual(self.timeline.timeline()[1], 3)
        self.timeline.start()
        self.assertEqual(self.timeline.timeline()[1], 0)
        self.assertTrue(self.timeline.running)
        self.assertGreater(self.timeline.revision, revision)

    def test_deck_clock_continues_through_stop_tempo_change_and_continue(self):
        self.clock.now = 1
        self.timeline.stop()
        self.clock.now = 2
        self.assertEqual(self.timeline.timeline(), (120, 2, 2))
        self.assertEqual(self.timeline.clock_timeline(), (120, 4, 2))
        self.timeline.bpm = 60
        self.clock.now = 3
        self.assertEqual(self.timeline.clock_timeline(), (60, 5, 3))
        self.timeline.resume()
        self.assertEqual(self.timeline.clock_timeline(), (60, 5, 3))
        self.clock.now = 4
        self.assertEqual(self.timeline.timeline(), (60, 3, 4))
        self.assertEqual(self.timeline.clock_timeline(), (60, 6, 4))
        self.timeline.align()
        self.assertEqual(self.timeline.clock_timeline(), (60, 0, 4))

    def test_deck_clock_survives_link_transport_stop_and_continue(self):
        self.link.enabled = True
        self.link.num_peers = 1
        self.timeline.snapshot()
        self.clock.now = 2
        self.timeline.snapshot()
        self.link.playing = False
        self.timeline.snapshot()
        self.clock.now = 5
        self.assertEqual(self.timeline.clock_timeline()[1], 10)
        self.link.playing = True
        self.assertEqual(self.timeline.clock_timeline()[1], 10)
        self.clock.now = 6
        self.assertEqual(self.timeline.clock_timeline()[1], 12)
        self.assertEqual(self.timeline.timeline()[1], 6)

    def test_link_acquisition_reanchors_deck_after_stop_with_or_without_continue(self):
        for running in (False, True):
            with self.subTest(running=running):
                clock = Clock()
                link = PeerLink(clock)
                timeline = MusicalTimeline(link, clock=clock)
                clock.now = 1
                timeline.stop()
                clock.now = 1.5
                if running:
                    timeline.resume()
                link.enabled = True
                link.num_peers = 1
                link.bpm = 90
                link.origin = 12
                timeline.select_source("link")
                clock.now = 2.5  # Running acquisition reaches local beat four.
                sample = timeline.snapshot()
                self.assertEqual(sample.active_source, "link")
                self.assertEqual(sample.running, running)
                self.assertEqual(sample.beat, 15.75 if running else 2)
                self.assertEqual(timeline.clock_timeline(), (90, 15.75, 2.5))

                # Change peer tempo without moving its phase.
                link.bpm = 60
                link.origin = 15.75 - clock.now
                self.assertEqual(timeline.clock_timeline(), (60, 15.75, 2.5))
                clock.now = 3.5
                self.assertEqual(timeline.clock_timeline(), (60, 16.75, 3.5))
                self.assertEqual(timeline.snapshot().beat, 16.75 if running else 2)
                if not running:
                    timeline.resume()
                    self.assertEqual(timeline.clock_timeline(), (60, 16.75, 3.5))
                clock.now = 4.5
                self.assertEqual(timeline.clock_timeline(), (60, 17.75, 4.5))
                self.assertEqual(timeline.snapshot().beat, 17.75 if running else 3)

    def test_midi_acquisition_reanchors_deck_after_stop_with_or_without_continue(self):
        for running in (False, True):
            with self.subTest(running=running):
                clock = Clock()
                link = PeerLink(clock)
                timeline = MusicalTimeline(link, clock=clock)
                timeline.select_source("internal")
                clock.now = 1
                timeline.stop()
                clock.now = 1.5
                if running:
                    timeline.resume()
                # Learn an independent external position and tempo without
                # applying its transport commands to the internal transport.
                timeline.midi_message([0xF2, 48, 0])  # Beat twelve.
                timeline.midi_message([0xFB])
                for _ in range(24):
                    clock.now += 60 / 90 / 24
                    timeline.midi_message([0xF8])
                timeline.select_source("beatstep")
                for _ in range(24):
                    clock.now += 60 / 90 / 24
                    timeline.midi_message([0xF8])
                expected = 12 + 47 / 24
                sample = timeline.snapshot()
                self.assertEqual(sample.active_source, "beatstep")
                self.assertEqual(sample.running, running)
                self.assertAlmostEqual(sample.bpm, 90)
                self.assertAlmostEqual(sample.beat, expected if running else 2)
                self.assertAlmostEqual(timeline.clock_timeline()[1], expected)

                # Publishing to Link uses the acquired deck phase even while
                # the sequencer remains stopped at its original position.
                link.enabled = True
                timeline.snapshot()
                self.assertAlmostEqual(link.publications[-1][1], expected)
                self.assertEqual(link.publications[-1][2], running)
                for _ in range(12):
                    clock.now += 60 / 90 / 24
                    timeline.midi_message([0xF8])
                self.assertAlmostEqual(timeline.clock_timeline()[1], expected + 0.5)
                self.assertAlmostEqual(timeline.snapshot().beat,
                                       expected + 0.5 if running else 2)

    def test_auto_acquires_stable_clock_at_bar_without_inventing_downbeat(self):
        self.pulse(16)
        self.assertEqual(self.timeline.active_source, "internal")
        self.assertIn("acquiring beatstep", self.timeline.status)
        self.pulse(82)
        self.assertEqual(self.timeline.active_source, "beatstep")
        self.assertFalse(self.timeline.position_confirmed)
        self.assertTrue(self.timeline.running)
        self.timeline.align()
        self.assertEqual(self.timeline.timeline()[1], 0)
        self.assertTrue(self.timeline.position_confirmed)

    def test_one_pulse_or_unstable_clock_cannot_steal_authority(self):
        self.pulse()
        self.clock.now += 2
        self.assertEqual(self.timeline.active_source, "internal")
        for index in range(32):
            self.clock.now += 0.01 if index % 2 else 0.04
            self.timeline.midi_message([0xF8])
        self.assertEqual(self.timeline.active_source, "internal")

    def test_clock_loss_and_disconnect_preserve_tempo_and_transport(self):
        self.pulse(100, bpm=100)
        # Acquisition waits for the original 120 BPM bar, then follows MIDI.
        before = self.timeline.snapshot()
        self.assertEqual(before.active_source, "beatstep")
        self.clock.now += 1
        lost = self.timeline.snapshot()
        self.assertEqual(lost.active_source, "internal")
        self.assertTrue(lost.running)
        self.assertAlmostEqual(lost.bpm, 100)
        self.assertAlmostEqual(lost.beat, before.beat + 100 / 60)
        self.assertIn("holdover", lost.status)
        self.timeline.disconnect_midi()
        self.assertEqual(self.timeline.snapshot().beat, lost.beat)

    def test_reconnected_clock_requires_reacquisition_without_inventing_position(self):
        self.timeline.midi_message([0xFA])
        self.pulse(100)
        self.clock.now += 1
        before = self.timeline.snapshot()
        self.pulse()
        self.assertAlmostEqual(self.timeline.snapshot().beat, before.beat + 1 / 24)
        self.pulse(100)
        self.assertEqual(self.timeline.active_source, "beatstep")
        self.assertFalse(self.timeline.position_confirmed)

    def test_manual_link_selection_does_not_follow_midi_transport(self):
        self.link.enabled = True
        self.timeline.select_source("link")
        self.clock.now = 2
        self.timeline.snapshot()
        self.timeline.midi_message([0xFC])
        self.pulse(100)
        self.assertTrue(self.timeline.running)
        self.assertEqual(self.timeline.active_source, "link")

    def test_midi_stop_freezes_even_when_clock_keeps_arriving(self):
        self.timeline.midi_message([0xFA])
        self.pulse(100)
        self.timeline.midi_message([0xFC])
        stopped = self.timeline.snapshot()
        self.pulse(80)
        self.assertFalse(self.timeline.running)
        self.assertEqual(self.timeline.snapshot().beat, stopped.beat)
        self.timeline.midi_message([0xFB])
        self.assertTrue(self.timeline.running)
        self.assertEqual(self.timeline.snapshot().beat, stopped.beat)
        self.timeline.midi_message([0xFA])
        self.assertEqual(self.timeline.snapshot().beat, 0)

    def test_song_position_uses_sixteenth_notes_and_does_not_start_transport(self):
        self.timeline.midi_message([0xFC])
        self.timeline.midi_message([0xF2, 8, 1])
        self.assertEqual(self.timeline.snapshot().beat, 34)
        self.assertTrue(self.timeline.position_confirmed)
        self.assertFalse(self.timeline.running)
        self.timeline.midi_message([0xFB])
        self.assertEqual(self.timeline.snapshot().beat, 34)
        self.clock.now += 0.25
        self.assertEqual(self.timeline.snapshot().beat, 34.5)

    def test_manual_internal_ignores_external_transport(self):
        self.timeline.select_source("internal")
        self.clock.now = 1
        self.timeline.midi_message([0xFC])
        self.timeline.midi_message([0xFA])
        self.timeline.midi_message([0xF2, 100, 0])
        self.assertEqual(self.timeline.snapshot().beat, 2)
        self.assertTrue(self.timeline.running)
        self.pulse(100)
        self.assertEqual(self.timeline.active_source, "internal")

    def test_link_peer_acquisition_and_manual_link_without_peers(self):
        self.link.enabled = True
        self.link.origin = 12
        self.clock.now = 0.5
        self.assertEqual(self.timeline.active_source, "internal")
        self.link.num_peers = 1
        self.timeline.snapshot()
        self.clock.now = 2
        self.assertEqual(self.timeline.snapshot().beat, 16)
        self.assertEqual(self.timeline.active_source, "link")
        self.link.num_peers = 0
        before = self.timeline.snapshot()
        self.assertEqual(before.active_source, "internal")
        self.timeline.select_source("link")
        self.clock.now = 4
        self.assertEqual(self.timeline.active_source, "link")

    def test_link_peer_transport_edges_stop_and_continue_without_rewind(self):
        self.link.enabled = True
        self.link.num_peers = 1
        self.timeline.snapshot()
        self.clock.now = 2
        self.timeline.snapshot()
        self.link.playing = False
        self.assertFalse(self.timeline.running)
        stopped = self.timeline.snapshot().beat
        self.clock.now = 8
        self.assertEqual(self.timeline.snapshot().beat, stopped)
        self.link.playing = True
        self.assertTrue(self.timeline.running)
        self.assertEqual(self.timeline.snapshot().beat, stopped)
        self.clock.now += 0.5
        self.assertEqual(self.timeline.snapshot().beat, stopped + 1)

    def test_midi_priority_publishes_tempo_phase_and_transport_to_link(self):
        self.link.enabled = True
        self.link.num_peers = 1
        self.timeline.midi_message([0xFA])
        self.pulse(100, bpm=100)
        self.assertEqual(self.timeline.active_source, "beatstep")
        self.assertTrue(any(abs(bpm - 100) < 1e-6 and phase
                            for bpm, _, _, phase, _ in self.link.publications))
        publications = len(self.link.publications)
        self.timeline.snapshot()
        self.timeline.snapshot()
        self.assertEqual(len(self.link.publications), publications)
        self.timeline.midi_message([0xFC])
        self.assertFalse(self.link.playing)
        self.timeline.midi_message([0xFB])
        self.assertTrue(self.link.playing)

    def test_cached_snapshot_is_read_only_and_never_queries_link(self):
        cached = self.timeline.cached_snapshot
        self.clock.now = 4
        self.link.timeline = Mock(side_effect=AssertionError("must not query Link"))
        self.assertIs(self.timeline.cached_snapshot, cached)
        self.assertEqual(cached.beat, 0)

    def test_invalid_source_tempo_and_malformed_position_leave_state_intact(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.timeline.bpm = value
        with self.assertRaises(ValueError):
            self.timeline.select_source("invalid")
        for raw in ([0xF2], [0xF2, 1], [0xF2, 128, 0]):
            self.timeline.midi_message(raw)
        self.assertEqual(self.timeline.timeline(), (120, 0, 0))


class LinkPublicationTests(unittest.TestCase):
    def test_external_phase_uses_verified_force_beat_not_cooperative_request(self):
        link = LinkSync()
        peer = SimpleNamespace(tempo=120, playing=True, force_beat=Mock())
        link._link = peer
        link.publish(100, 13.5, False, phase=True, transport=True)
        self.assertEqual(peer.tempo, 100)
        self.assertFalse(peer.playing)
        peer.force_beat.assert_called_once_with(13.5)
        link.publish(100, 14, False)
        peer.force_beat.assert_called_once_with(13.5)

    def test_seed_offers_phase_without_overwriting_existing_peer_tempo_or_transport(self):
        link = LinkSync()
        peer = SimpleNamespace(tempo=130, playing=False, num_peers=1, request_beat=Mock())
        link._link = peer
        link.seed(100, 13.5, True)
        self.assertEqual(peer.tempo, 130)
        self.assertFalse(peer.playing)
        peer.request_beat.assert_called_once_with(13.5)

    def test_seed_initializes_an_empty_session(self):
        link = LinkSync()
        peer = SimpleNamespace(tempo=120, playing=False, num_peers=0, request_beat=Mock())
        link._link = peer
        link.seed(100, 13.5, True)
        self.assertEqual(peer.tempo, 100)
        self.assertTrue(peer.playing)
        peer.request_beat.assert_called_once_with(13.5)


if __name__ == "__main__":
    unittest.main()
