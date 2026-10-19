"""Real plugdata Pure Data import, MIDI audio, and self-contained state restore."""

import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest

import numpy as np

from core.audio_deps import require_native
from core.plugin_host import NativePlugin


MIDI_VOICE = """#N canvas 0 0 450 300 12;
#X obj 20 20 notein;
#X obj 20 60 mtof;
#X obj 20 100 osc~;
#X obj 180 60 / 127;
#X obj 180 100 sig~;
#X obj 20 140 *~;
#X obj 20 180 *~ 0.2;
#X obj 20 220 dac~;
#X connect 0 0 1 0;
#X connect 0 1 3 0;
#X connect 1 0 2 0;
#X connect 2 0 5 0;
#X connect 3 0 4 0;
#X connect 4 0 5 1;
#X connect 5 0 6 0;
#X connect 6 0 7 0;
#X connect 6 0 7 1;
"""
SILENT_PATCH = """#N canvas 0 0 450 300 12;
#X obj 20 20 sig~ 0;
#X obj 20 60 dac~;
#X connect 0 0 1 0;
#X connect 0 0 1 1;
"""


class PlugdataPatchTests(unittest.TestCase):
    def native_bundle(self, path, missing_message):
        bundle = Path(path).expanduser()
        if not bundle.exists():
            self.skipTest(missing_message)
        if sys.platform == "darwin":
            binary_directory = bundle / "Contents" / "MacOS"
        elif sys.platform.startswith("linux"):
            architecture = {"arm64": "aarch64", "amd64": "x86_64"}.get(
                platform.machine(), platform.machine())
            binary_directory = bundle / "Contents" / f"{architecture}-linux"
        else:
            self.skipTest("native audio supports Linux and macOS")
        if not binary_directory.is_dir():
            self.skipTest(f"fetch a native {bundle.name} bundle for this platform")
        return bundle.resolve()

    def plugdata_bundle(self):
        default = Path(__file__).resolve().parents[1] / "vst3" / "plugdata.vst3"
        return self.native_bundle(
            os.environ.get("PLUGDATA_VST3_PATH", default),
            "install plugdata 0.9.3 or set PLUGDATA_VST3_PATH")

    def temporary_directory(self):
        directory = tempfile.TemporaryDirectory(prefix="vcpi-plugdata-test-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def runner(self, plugin, directory, name):
        backend = require_native()
        transport = backend.Transport(str(directory / f"{name}.mmap"), 512, True)
        runner = backend.SlotRunner(transport, plugin.native, 44100, 512)
        self.addCleanup(runner.shutdown)
        return transport, runner

    def peak(self, runner, blocks=32):
        peak = 0.0
        for _ in range(blocks):
            audio = runner.render_once()
            self.assertEqual(audio.shape, (512, 2))
            self.assertTrue(np.isfinite(audio).all())
            peak = max(peak, float(np.abs(audio).max()))
        return peak

    def assert_note_and_release(self, transport, runner):
        transport.send_midi(bytes((0x90, 60, 100)))
        self.assertGreater(self.peak(runner), 1e-5)
        transport.send_midi(bytes((0x80, 60, 0)))
        self.peak(runner, blocks=8)
        self.assertLess(self.peak(runner), 1e-7)

    def test_midi_patch_replacement_and_restore_without_source_file(self):
        bundle = self.plugdata_bundle()
        directory = self.temporary_directory()
        patch = directory / "voice.pd"
        patch.write_text(MIDI_VOICE, encoding="utf-8")
        plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
        volume = next(name for name, descriptor in plugin.parameters.items()
                      if descriptor.label.lower() == "volume")
        setattr(plugin, volume, 0.37)
        expected_volume = getattr(plugin, volume)
        plugin.load_preset(str(patch))
        self.assertEqual(getattr(plugin, volume), expected_volume)
        transport, runner = self.runner(plugin, directory, "voice")
        self.assert_note_and_release(transport, runner)
        saved = plugin.get_state()

        # Replacement must update both legacy records and the preferred XML
        # patch list, not append a second patch beside the audible source.
        patch.write_text(SILENT_PATCH, encoding="utf-8")
        plugin.load_preset(str(patch))
        self.assertEqual(getattr(plugin, volume), expected_volume)
        self.peak(runner, blocks=8)
        transport.send_midi(bytes((0x90, 60, 100)))
        self.assertLess(self.peak(runner), 1e-7)
        transport.send_midi(bytes((0x80, 60, 0)))
        patch.unlink()

        plugin.load_saved_state(saved)
        self.assertEqual(getattr(plugin, volume), expected_volume)
        self.assert_note_and_release(transport, runner)
        # Also restore into a fresh processor: no in-memory patch or source file
        # is available to conceal a missing Content attribute in saved state.
        restored = NativePlugin(str(bundle), 44100, 512, "instrument")
        restored.load_saved_state(saved)
        self.assertEqual(getattr(restored, volume), expected_volume)
        restored_transport, restored_runner = self.runner(restored, directory, "restored")
        self.assert_note_and_release(restored_transport, restored_runner)

    def test_invalid_patch_is_rejected_without_replacing_live_voice(self):
        bundle = self.plugdata_bundle()
        directory = self.temporary_directory()
        patch = directory / "voice.pd"
        patch.write_text(MIDI_VOICE, encoding="utf-8")
        plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
        plugin.load_preset(str(patch))
        transport, runner = self.runner(plugin, directory, "invalid")
        invalid = directory / "invalid.pd"
        for content in (
            b"",
            b"not a Pure Data patch",
            b"#N canvas 0 0 450 300 12",
            b"#N canvas ;\n",
            b"#N canvas 0 0 450 300 12;\n\x00",
            b"#N canvas 0 0 450 300 12;\n#X text 0 0 \xff;\n",
            b"#N canvas 0 0 450 300 12;\n#X restore 0 0 pd extra;\n",
        ):
            with self.subTest(content=content):
                invalid.write_bytes(content)
                with self.assertRaises(ValueError):
                    plugin.load_preset(str(invalid))
                self.assert_note_and_release(transport, runner)

    def test_wrong_plugin_rejects_pd_import(self):
        bundle = self.native_bundle(
            Path(__file__).resolve().parents[1] / "vst3" / "Surge XT.vst3",
            "fetch a native Surge XT bundle for wrong-plugin rejection coverage")
        directory = self.temporary_directory()
        patch = directory / "voice.pd"
        patch.write_text(MIDI_VOICE, encoding="utf-8")
        plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
        before = {name: getattr(plugin, name) for name in plugin.parameters}
        with self.assertRaisesRegex(ValueError, "require.*plugdata"):
            plugin.load_preset(str(patch))
        self.assertEqual({name: getattr(plugin, name) for name in plugin.parameters}, before)


if __name__ == "__main__":
    unittest.main()
