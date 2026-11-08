"""Real plugdata Pure Data import, MIDI audio, and self-contained state restore."""

import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest

import numpy as np

from vcpi.audio_deps import require_native
from vcpi.audio_state import capture_plugin
from vcpi.plugin_host import NativePlugin


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
    def native_bundle(self, name, environment_variable):
        if sys.platform == "darwin":
            directories = ("~/Library/Audio/Plug-Ins/VST3", "/Library/Audio/Plug-Ins/VST3")
            binary_suffix = Path("Contents/MacOS")
        elif sys.platform.startswith("linux"):
            directories = ("~/.vst3", "/usr/lib/vst3", "/usr/local/lib/vst3")
            architecture = {"arm64": "aarch64", "amd64": "x86_64"}.get(
                platform.machine(), platform.machine())
            binary_suffix = Path("Contents") / f"{architecture}-linux"
        else:
            self.skipTest("native audio supports Linux and macOS")
        configured = os.environ.get(environment_variable)
        candidates = ([Path(configured).expanduser()] if configured else
                      [Path(directory).expanduser() / name for directory in directories])
        for bundle in candidates:
            if (bundle / binary_suffix).is_dir():
                return bundle.resolve()
        self.skipTest(
            f"install a native {name} in a user/system VST3 directory "
            f"or set {environment_variable}")

    def plugdata_bundle(self):
        return self.native_bundle("plugdata.vst3", "PLUGDATA_VST3_PATH")

    def temporary_directory(self):
        directory = tempfile.TemporaryDirectory(prefix="vcpi-plugdata-test-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def runner(self, plugin, directory, name):
        backend = require_native()
        transport = backend.Transport(str(directory / f"{name}.mmap"), 512, True)
        runner = backend.TrackRunner(transport, plugin.native, 44100, 512)
        self.addCleanup(runner.shutdown)
        # Match worker startup: finish patch activation before accepting controls.
        runner.barrier()
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

    def test_classic_patch_parameters_change_audio_and_restore_without_source(self):
        bundle = self.plugdata_bundle()
        directory = self.temporary_directory()
        patch = directory / "classic-subtractive-synth.pd"
        source = Path(__file__).resolve().parents[1] / "assets" / "patches" / patch.name
        patch.write_bytes(source.read_bytes())
        plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
        volume = next(name for name, descriptor in plugin.parameters.items()
                      if descriptor.label.lower() == "volume")
        setattr(plugin, volume, 0.37)
        expected_volume = getattr(plugin, volume)
        plugin.load_preset(str(patch))
        ranges = {
            "cutoff": (20.0, 12000.0),
            "resonance": (0.0, 4.0),
            "envelope_amount": (0.0, 8000.0),
        }

        def assert_controls(processor, expected):
            snapshot = capture_plugin(processor, {}, {})
            controls = [name for name, descriptor in snapshot.parameter_items()
                        if name != volume]
            self.assertEqual(controls, list(ranges))
            for name, bounds in ranges.items():
                descriptor = snapshot.parameters[name]
                self.assertEqual(descriptor.label, name)
                self.assertEqual(descriptor.range, bounds)
                self.assertTrue(descriptor.is_automatable)
                self.assertAlmostEqual(snapshot.values[name], expected[name], delta=0.01)
            self.assertEqual(snapshot.values[volume], expected_volume)

        assert_controls(plugin, {
            "cutoff": 300.0, "resonance": 2.5, "envelope_amount": 2000.0,
        })
        transport, runner = self.runner(plugin, directory, "classic")

        def note_spectrum(note_transport, note_runner):
            # The oscillator's second phasor is detuned by 0.5 Hz. Integrate a
            # complete two-second beat cycle: a short FFT changes with beat
            # phase even when every control and MIDI note is unchanged.
            note_transport.send_midi(bytes((0x80, 60, 0)))
            self.peak(note_runner, blocks=48)
            note_transport.send_midi(bytes((0x90, 60, 100)))
            self.peak(note_runner, blocks=24)
            blocks = []
            sample_count = 88200
            for _ in range((sample_count + 511) // 512):
                audio = note_runner.render_once()
                self.assertEqual(audio.shape, (512, 2))
                self.assertTrue(np.isfinite(audio).all())
                blocks.append(audio[:, 0].copy())
            note_transport.send_midi(bytes((0x80, 60, 0)))
            self.peak(note_runner, blocks=32)
            audio = np.concatenate(blocks)[:sample_count]
            self.assertGreater(float(np.abs(audio).max()), 1e-5)
            power = np.abs(np.fft.rfft(audio)) ** 2 / sample_count ** 2
            frequencies = np.fft.rfftfreq(sample_count, 1 / 44100)
            # Coarse band RMS retains filter/resonance differences without
            # requiring individual FFT bins or oscillator phases to match.
            band_power, _ = np.histogram(
                frequencies, bins=(0, 200, 400, 800, 1600, 3200, 6400, 22051),
                weights=power)
            return np.sqrt(band_power)

        for name, baseline, changed in (
            ("cutoff", {"cutoff": 100.0, "resonance": 0.0,
                        "envelope_amount": 0.0}, 4000.0),
            ("resonance", {"cutoff": 700.0, "resonance": 0.0,
                           "envelope_amount": 0.0}, 3.5),
            ("envelope_amount", {"cutoff": 100.0, "resonance": 0.0,
                                 "envelope_amount": 0.0}, 6000.0),
        ):
            with self.subTest(parameter=name):
                for control, value in baseline.items():
                    setattr(plugin, control, value)
                before = note_spectrum(transport, runner)
                repeated = note_spectrum(transport, runner)
                scale = np.linalg.norm(before)
                self.assertLess(np.linalg.norm(repeated - before) / scale, 0.05)
                setattr(plugin, name, changed)
                after = note_spectrum(transport, runner)
                self.assertGreater(np.linalg.norm(after - before) / scale, 0.15)

        values = {"cutoff": 1800.0, "resonance": 1.25, "envelope_amount": 4200.0}
        for name, value in values.items():
            setattr(plugin, name, value)
        expected_spectrum = note_spectrum(transport, runner)
        assert_controls(plugin, values)
        saved = plugin.get_state()
        patch.unlink()
        restored = NativePlugin(str(bundle), 44100, 512, "instrument")
        restored.load_saved_state(saved)
        assert_controls(restored, values)
        restored_transport, restored_runner = self.runner(restored, directory, "classic-restored")
        actual_spectrum = note_spectrum(restored_transport, restored_runner)
        self.assertLess(
            np.linalg.norm(actual_spectrum - expected_spectrum)
            / np.linalg.norm(expected_spectrum), 0.05)

    def test_poly_pad_controls_change_every_voice_and_survive_native_restore(self):
        bundle = self.plugdata_bundle()
        directory = self.temporary_directory()
        source = Path(__file__).resolve().parents[1] / "assets" / "patches" / "poly-pad.pd"
        patch = directory / source.name
        patch.write_bytes(source.read_bytes())
        plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
        plugin.load_preset(str(patch))
        transport, runner = self.runner(plugin, directory, "pad")
        snapshot = capture_plugin(plugin, {}, {})
        for name, default, bounds in (
            ("cutoff", 1800, (20, 12000)),
            ("attack", 800, (1, 5000)),
            ("release", 2200, (1, 10000)),
        ):
            self.assertEqual(snapshot.parameters[name].range, bounds)
            self.assertAlmostEqual(snapshot.values[name], default, delta=0.01)

        def rms(audio):
            self.assertTrue(np.isfinite(audio).all())
            return float(np.sqrt(np.mean(audio ** 2)))

        def render_note(note, *, cutoff=1800, attack=1, release=1):
            setattr(plugin, "cutoff", cutoff)
            setattr(plugin, "attack", attack)
            setattr(plugin, "release", release)
            transport.send_midi(bytes((0x90, note, 100)))
            held = np.concatenate([runner.render_once() for _ in range(24)])
            transport.send_midi(bytes((0x80, note, 0)))
            tail = np.concatenate([runner.render_once() for _ in range(64)])
            # Clear the tail before another voice/setting is measured.
            setattr(plugin, "release", 1)
            transport.send_midi(bytes((0x80, note, 0)))
            self.peak(runner, blocks=192)
            return rms(held[:4096]), rms(held[-4096:]), rms(tail[-4096:])

        # Complete a full eight-voice allocation cycle for each setting, so no
        # control can pass merely because it reaches a subset of the voices.
        baseline = {note: render_note(note) for note in range(60, 68)}
        for name, value in (("attack", 3000), ("cutoff", 100), ("release", 2000)):
            for note in range(60, 68):
                with self.subTest(parameter=name, note=note):
                    early, sustained, tail = render_note(note, **{name: value})
                    fast, bright, short_tail = baseline[note]
                    if name == "attack":
                        self.assertLess(early, fast * 0.1)
                    elif name == "cutoff":
                        self.assertGreater(bright, sustained * 1.5)
                    else:
                        self.assertLess(short_tail, tail * 0.01)

        values = {"cutoff": 950.0, "attack": 350.0, "release": 1700.0}
        for name, value in values.items():
            setattr(plugin, name, value)
        # Production track RPCs order state capture through this barrier.
        runner.barrier()
        saved = plugin.get_state()
        patch.unlink()
        restored = NativePlugin(str(bundle), 44100, 512, "instrument")
        restored.load_saved_state(saved)
        restored_snapshot = capture_plugin(restored, {}, {})
        for name, value in values.items():
            self.assertAlmostEqual(restored_snapshot.values[name], value, delta=0.01)

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
        bundle = self.native_bundle("Surge XT.vst3", "SURGE_VST3_PATH")
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
