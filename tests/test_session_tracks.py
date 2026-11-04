"""Track session migration preserves instruments, routing and sequence links."""

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from core.host import VcpiCore


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
                        host.restore_session()
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
