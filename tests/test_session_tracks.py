"""Track session migration preserves instruments, routing and sequence links."""

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vcpi.host import VcpiCore
from vcpi.models import NUM_TRACKS, TrackFocus
from vcpi.sessions.persistence import restore
from vcpi.sessions.restore import _restore_tracks


class TrackSessionTests(unittest.TestCase):
    def test_legacy_sessions_restore_and_resave_with_track_keys(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-track-session-") as directory:
            root = Path(directory)
            sample = root / "sample.wav"
            with wave.open(str(sample), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(44100)
                audio.writeframes(struct.pack("<h", 4096) * 64)
            for version in (1, 2):
                with self.subTest(version=version):
                    path = root / f"legacy-{version}.json"
                    path.write_text(json.dumps({
                        "version": version,
                        "slots": [None, {
                            "kind": "deck", "name": "Legacy bass",
                            "gain": 0.35, "muted": True, "solo": True,
                            "deck": {
                                "bank_id": "legacy", "track_id": "bass",
                                "tracks": [{"id": "bass", "path": str(sample), "title": "Bass"}],
                                "cue": 0, "pitch": 0,
                            },
                        }],
                        "routing": {"4": 2},
                        "sequences": [{"notes": ["C4", "E4"], "velocity": 73,
                                       "linked_slot": 2}],
                    }))
                    host = VcpiCore(session_path=str(path))
                    try:
                        host.track_focus.select(8)
                        host.restore_session()
                        self.assertEqual(host.track_focus.snapshot()[0], 0)
                        host.sequencer.stop()
                        track = host.engine.tracks[1]
                        self.assertEqual(track.name, "Legacy bass")
                        self.assertEqual(track.source_type, "deck")
                        self.assertAlmostEqual(track.gain, 0.35)
                        self.assertTrue(track.muted)
                        self.assertTrue(track.solo)
                        self.assertEqual(host.channel_map, {3: 1})
                        self.assertEqual(host.sequencer.banks[0].linked_track, 1)
                        self.assertEqual(host.sequencer.banks[0].velocity, 73)
                        host.save_session()
                        saved = json.loads(path.read_text())
                        self.assertEqual(saved["version"], 3)
                        self.assertEqual(saved["focus"], 1)
                        self.assertNotIn("slots", saved)
                        self.assertEqual(saved["tracks"][1]["name"], "Legacy bass")
                        self.assertEqual(saved["tracks"][1]["kind"], "deck")
                        self.assertEqual(saved["sequences"][0]["linked_track"], 2)
                        self.assertNotIn("linked_slot", saved["sequences"][0])
                    finally:
                        host.shutdown(save=False)
                    restored = VcpiCore(session_path=str(path))
                    try:
                        restored.restore_session()
                        restored.sequencer.stop()
                        self.assertEqual(restored.engine.tracks[1].name, "Legacy bass")
                        self.assertEqual(restored.channel_map, {3: 1})
                        self.assertEqual(restored.sequencer.banks[0].linked_track, 1)
                    finally:
                        restored.shutdown(save=False)

    def test_unsupported_sources_never_load_as_plugins(self):
        for version in (1, 2, 3):
            for kind in ("wav", "unknown-source"):
                with self.subTest(version=version, kind=kind):
                    host = Mock()
                    errors = []
                    key = "tracks" if version == 3 else "slots"
                    _restore_tracks(host, {
                        key: [{"kind": kind, "path": "/unsupported/source", "name": "Old source"}],
                    }, version, errors)
                    self.assertEqual(len(errors), 1)
                    self.assertIn("unsupported track source kind", errors[0])
                    host.load_instrument.assert_not_called()
                    host.restore_deck.assert_not_called()
                    host.engine.set_track_state.assert_not_called()

    def test_shared_focus_roundtrip_precedes_device_connections(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-focus-session-") as directory:
            path = Path(directory) / "focus.json"
            host = VcpiCore(session_path=str(path))
            try:
                host.track_focus.select(NUM_TRACKS - 1)
                host.save_session()
                self.assertEqual(json.loads(path.read_text())["focus"], NUM_TRACKS)
                host.track_focus.select(2)
                observed = []
                with patch("vcpi.sessions.restore._restore_connections",
                           side_effect=lambda host, *args, **kwargs:
                           observed.append(host.track_focus.snapshot()[0])):
                    host.restore_session(start_audio=False)
                self.assertEqual(observed, [NUM_TRACKS - 1])
                self.assertEqual(host.beatstep._focused_index(), NUM_TRACKS - 1)
            finally:
                host.shutdown(save=False)

    def test_invalid_focus_rejects_before_mutating_running_host(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-focus-validation-") as directory:
            path = Path(directory) / "invalid.json"
            focus = TrackFocus()
            focus.select(3)
            host = SimpleNamespace(
                track_focus=focus, engine=SimpleNamespace(running=True),
                stop_audio=Mock(), timeline=SimpleNamespace(bpm=123))
            for value in (0, NUM_TRACKS + 1, -1, True, False, 1.0, "1", None):
                with self.subTest(value=value):
                    path.write_text(json.dumps({"version": 3, "focus": value, "bpm": 99}))
                    with self.assertRaisesRegex(ValueError, "focus"):
                        restore(host, path)
                    host.stop_audio.assert_not_called()
                    self.assertTrue(host.engine.running)
                    self.assertEqual(host.timeline.bpm, 123)
                    self.assertEqual(focus.snapshot()[0], 3)
