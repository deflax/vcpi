"""Real Cardinal project import and opaque-state restoration."""

import json
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np

from core.audio_deps import require_native
from core.plugin_host import NativePlugin


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
            runner = backend.SlotRunner(transport, plugin.native, 44100, 512)
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


if __name__ == "__main__":
    unittest.main()
