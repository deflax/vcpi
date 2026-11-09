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
                            "kind": "wav", "path": str(sample), "name": "Legacy bass",
                            "gain": 0.35, "muted": True, "solo": True,
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
