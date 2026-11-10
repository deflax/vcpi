"""Real Cardinal project import and opaque-state restoration."""

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np

from vcpi.audio_deps import require_native
from vcpi.host import VcpiCore
from vcpi.plugin_host import NativePlugin


class CardinalPatchTests(unittest.TestCase):
    def test_import_replaces_patch_and_saved_state_restores_audio(self):
        bundle = Path(os.environ.get("CARDINAL_VST3_PATH", "/usr/lib/vst3/Cardinal.vst3")).expanduser()
        if not bundle.exists():
            self.skipTest("install Cardinal or set CARDINAL_VST3_PATH")
        backend = require_native()
        with tempfile.TemporaryDirectory(prefix="vcpi-cardinal-test-") as directory:
            patch_path = Path(directory) / "voice.vcv"
            # Continuous oscillator: no MIDI or envelope timing can mask a
            # failed import. Removing the cable must make the source silent.
            patch = {
                "version": "2.4.1",
                "modules": [
                    {"id": 1, "plugin": "Fundamental", "model": "VCO",
                     "version": "2.1.0", "params": [], "pos": [0, 0]},
                    {"id": 2, "plugin": "Cardinal", "model": "HostAudio2",
                     "version": "2.0.0", "params": [], "pos": [10, 0]},
                ],
                "cables": [{"id": 1, "outputModuleId": 1, "outputId": 0,
                            "inputModuleId": 2, "inputId": 0}],
            }
            patch_path.write_text(json.dumps(patch))
            plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
            plugin.load_preset(str(patch_path))
            saved = plugin.get_state()
            assert isinstance(saved, bytes)
            transport = backend.Transport(str(Path(directory) / "audio.mmap"), 512, True)
            runner = backend.TrackRunner(transport, plugin.native, 44100, 512)
            try:
                def peak():
                    value = 0.0
                    for _ in range(32):
                        audio = runner.render_once()
                        self.assertTrue(np.isfinite(audio).all())
                        value = max(value, float(np.abs(audio).max()))
                    return value

                self.assertGreater(peak(), 1e-5)
                patch["cables"] = []
                patch_path.write_text(json.dumps(patch))
                plugin.load_preset(str(patch_path))
                # Let the host audio module's DC filter settle.
                for _ in range(128):
                    runner.render_once()
                self.assertLess(peak(), 1e-7)
                plugin.load_saved_state(saved)
                self.assertGreater(peak(), 1e-5)
            finally:
                runner.shutdown()

    def test_session_restores_embedded_patch_after_original_file_is_removed(self):
        bundle = Path(os.environ.get("CARDINAL_VST3_PATH", "/usr/lib/vst3/Cardinal.vst3")).expanduser()
        if not bundle.exists():
            self.skipTest("install Cardinal or set CARDINAL_VST3_PATH")
        with tempfile.TemporaryDirectory(prefix="vcpi-cardinal-session-") as directory:
            root = Path(directory)
            patch_path = root / "voice.vcv"
            patch_path.write_text(json.dumps({
                "version": "2.4.1",
                "modules": [
                    {"id": 1, "plugin": "Fundamental", "model": "VCO",
                     "version": "2.1.0", "params": [], "pos": [0, 0]},
                    {"id": 2, "plugin": "Cardinal", "model": "HostAudio2",
                     "version": "2.0.0", "params": [], "pos": [10, 0]},
                ],
                "cables": [{"id": 1, "outputModuleId": 1, "outputId": 0,
                            "inputModuleId": 2, "inputId": 0}],
            }))
            session_path = root / "session.json"
            host = VcpiCore(session_path=str(session_path))
            try:
                track, _, _ = host.load_vcv(
                    0, str(patch_path), cardinal_path=str(bundle), name="Saved oscillator")
                self.assertEqual(track.source_type, "vcv")
                host.engine.set_track_state(0, gain=0.375)
                host.save_session()
            finally:
                host.shutdown(save=False)
            patch_path.unlink()
            restored = VcpiCore(session_path=str(session_path))
            try:
                restored.restore_session(start_audio=False)
                track = restored.engine.tracks[0]
                self.assertIsNotNone(track)
                self.assertEqual(track.source_type, "vcv")
                self.assertEqual(track.vcv_patch_path, str(patch_path))
                self.assertEqual(track.name, "Saved oscillator")
                self.assertEqual(track.gain, 0.375)
                restored.save_session()
            finally:
                restored.shutdown(save=False)
            entry = json.loads(session_path.read_text())["tracks"][0]
            plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
            plugin.load_saved_state(base64.b64decode(entry["native_state"], validate=True))
            backend = require_native()
            transport = backend.Transport(str(root / "restored.mmap"), 512, True)
            runner = backend.TrackRunner(transport, plugin.native, 44100, 512)
            try:
                peak = 0.0
                for _ in range(32):
                    audio = runner.render_once()
                    self.assertTrue(np.isfinite(audio).all())
                    peak = max(peak, float(np.abs(audio).max()))
                self.assertGreater(peak, 1e-5)
            finally:
                runner.shutdown()


if __name__ == "__main__":
    unittest.main()
