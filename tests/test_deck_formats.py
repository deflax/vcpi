"""Decode and seek real encoded deck audio (FFmpeg is a fixture generator only)."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import wave

import numpy as np

from vcpi.audio_deps import require_native


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required to generate codec fixtures")
class DeckFormatTests(unittest.TestCase):
    def test_all_formats_stream_stereo_and_seek_in_source_seconds(self):
        backend = require_native()
        rate = 48000
        with tempfile.TemporaryDirectory(prefix="vcpi-formats-") as directory:
            root = Path(directory)
            original = root / "source.wav"
            phase = np.arange(rate * 6) * (2 * np.pi * 1000 / rate)
            left = np.sin(phase) * 0.25
            samples = np.column_stack((left, -left * 0.5))
            with wave.open(str(original), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes((samples * 32767).astype("<i2").tobytes())
            for extension in ("wav", "aiff", "mp3", "flac"):
                with self.subTest(format=extension):
                    asset = root / ("encoded." + extension)
                    subprocess.run(["ffmpeg", "-v", "error", "-i", str(original),
                                    "-y", str(asset)], check=True, capture_output=True)
                    catalog = {"id": "formats", "sync_filter": False,
                               "tracks": [{"id": extension, "path": str(asset)}]}
                    plugin = backend.Plugin(json.dumps(catalog), rate, 512, "deck")
                    transport = backend.Transport(str(root / (extension + ".mmap")), 512, True)
                    runner = backend.TrackRunner(transport, plugin, rate, 512)
                    try:
                        plugin.set_parameter("sync", False)
                        plugin.deck_command("seek", 2.125)
                        deadline = time.monotonic() + 5
                        while plugin.deck_status()["loading"]:
                            self.assertLess(time.monotonic(), deadline, plugin.deck_status())
                            runner.render_once()
                            time.sleep(0.001)
                        self.assertAlmostEqual(plugin.deck_status()["position"], 2.125, places=6)
                        plugin.set_parameter("play", True)
                        runner.render_once()
                        audio = np.concatenate([runner.render_once() for _ in range(12)])
                        signal = audio[:, 0].astype(np.float64)
                        spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
                        peak = np.argmax(spectrum) * rate / len(signal)
                        self.assertAlmostEqual(peak, 1000, delta=8)
                        self.assertAlmostEqual(float(np.sqrt(np.mean(signal ** 2))),
                                               0.25 / np.sqrt(2), delta=0.015)
                        np.testing.assert_allclose(audio[:, 1], -audio[:, 0] * 0.5, atol=0.01)
                        self.assertAlmostEqual(plugin.deck_status()["position"],
                                               2.125 + 13 * 512 / rate, places=6)
                        self.assertEqual(transport.stats()["failure"], "")
                    finally:
                        runner.shutdown()


if __name__ == "__main__":
    unittest.main()
