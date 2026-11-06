"""Shared deck timing without audio hardware or a running Link network."""

import pickle
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vcpi.audio.client import RemoteAudioEngine
from vcpi.host import VcpiCore
from vcpi.link import LinkSync
from vcpi.timeline import MusicalTimeline


class DeckClockTests(unittest.TestCase):
    def test_local_tempo_changes_preserve_continuous_phase(self):
        with patch("vcpi.link.time.monotonic", return_value=10) as clock:
            link = LinkSync(120)
            clock.return_value = 12
            self.assertEqual(link.timeline(), (120, 4, 12))
            link.bpm = 90
            self.assertEqual(link.beat, 4)
            clock.return_value = 14
            self.assertEqual(link.timeline(), (90, 7, 14))
            link.bpm = 180
            clock.return_value = 15
            self.assertEqual(link.beat, 10)

    def test_link_peer_tempo_and_phase_continue_locally_on_disable(self):
        with patch("vcpi.link.time.monotonic", return_value=10) as clock:
            link = LinkSync()
            peer = SimpleNamespace(tempo=128, beat=19.25, enabled=True)
            link._link, link._enabled = peer, True
            self.assertEqual(link.timeline(), (128, 19.25, 10))
            peer.tempo, peer.beat = 90, 23.5
            clock.return_value = 12
            self.assertEqual(link.timeline(), (90, 23.5, 12))
            link.disable()
            self.assertFalse(peer.enabled)
            clock.return_value = 14
            self.assertEqual(link.timeline(), (90, 26.5, 14))

    def host(self):
        host = VcpiCore.__new__(VcpiCore)
        host.link = LinkSync(120)
        host.timeline = MusicalTimeline(host.link)
        host.controller_discovery_enabled = False
        host._beatstep_discovery_at = 0.0
        host._deck_clock_stop = threading.Event()
        host._deck_clock_thread = None
        host.engine = SimpleNamespace(
            tracks=[SimpleNamespace(source_type="deck", generation=7), None,
                    SimpleNamespace(source_type="wav", generation=8),
                    SimpleNamespace(source_type="deck", generation=9)],
            enqueue_deck_command=Mock(return_value=True))
        return host

    def test_failed_feeder_is_stoppable_and_logs_once_per_failure_run(self):
        host = self.host()
        attempts = threading.Event()

        def fail():
            attempts.set()
            raise RuntimeError("owner unavailable")

        host._feed_deck_clock = fail
        host._deck_clock_thread = threading.Thread(target=host._run_deck_clock)
        with self.assertLogs("vcpi.host", level="WARNING") as logs:
            host._deck_clock_thread.start()
            self.addCleanup(host._stop_deck_clock)
            self.assertTrue(attempts.wait(1))
            host._stop_deck_clock()
        self.assertFalse(host._deck_clock_thread.is_alive())
        self.assertEqual(len(logs.output), 1)

    def test_clock_backpressure_preserves_input_sequence_and_midi_capacity(self):
        engine = RemoteAudioEngine.__new__(RemoteAudioEngine)
        engine._check_owner = Mock()
        engine._input_lock = threading.Lock()
        engine._events = queue.Queue(maxsize=4)
        engine._last_input = 0
        engine._fail = Mock(side_effect=AssertionError("must not kill audio"))
        self.assertTrue(engine.enqueue_deck_command(0, "sync_tempo", 120, generation=7))
        self.assertTrue(engine.enqueue_deck_command(0, "sync_beat", 4, generation=7))
        self.assertFalse(engine.enqueue_deck_command(0, "sync_tempo", 130, generation=7))
        self.assertEqual(engine._last_input, 2)
        engine.enqueue_param_change(0, "sync", False, generation=7)
        self.assertEqual(engine._last_input, 3)
        messages = [pickle.loads(engine._events.get_nowait()) for _ in range(3)]
        self.assertEqual([message[0] for message in messages], [1, 2, 3])
        engine._fail.assert_not_called()
