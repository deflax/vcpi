"""Real VST3 controller conversions and opaque native-state restoration."""

import math
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


class NativeParameterRestorationTests(unittest.TestCase):
    def surge_bundle(self):
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
        configured = os.environ.get("SURGE_VST3_PATH")
        candidates = ([Path(configured).expanduser()] if configured else
                      [Path(directory).expanduser() / "Surge XT.vst3"
                       for directory in directories])
        for bundle in candidates:
            if (bundle / binary_suffix).is_dir():
                return bundle.resolve()
        self.skipTest(
            "install a native Surge XT bundle in a user/system VST3 directory "
            "or set SURGE_VST3_PATH")

    def test_typed_values_survive_changes_and_complete_restoration(self):
        bundle = self.surge_bundle()
        plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
        before = capture_plugin(plugin, {}, {})
        descriptors = before.parameters
        boolean = next(name for name, parameter in descriptors.items()
                       if parameter.kind == "bool")
        enumeration = next(name for name, parameter in descriptors.items()
                           if parameter.kind == "enum" and len(parameter.valid_values) > 1)
        numeric = next(name for name, parameter in descriptors.items()
                       if parameter.kind == "float" and parameter.is_automatable
                       and all(math.isfinite(value) for value in parameter.range)
                       and parameter.range[1] > parameter.range[0])
        negative_infinity = next(name for name, parameter in descriptors.items()
                                 if parameter.kind == "float"
                                 and parameter.range[0] == -math.inf
                                 and math.isfinite(parameter.range[1]))

        setattr(plugin, boolean, not before.values[boolean])
        self.assertEqual(getattr(plugin, boolean), not before.values[boolean])
        choice = next(value for value in descriptors[enumeration].valid_values
                      if value != before.values[enumeration])
        setattr(plugin, enumeration, choice)
        self.assertEqual(getattr(plugin, enumeration), choice)
        low, high = descriptors[numeric].range
        target = low + (high - low) * 0.3
        if abs(target - before.values[numeric]) < 1e-6:
            target = low + (high - low) * 0.7
        setattr(plugin, numeric, target)
        self.assertAlmostEqual(getattr(plugin, numeric), target,
                               delta=max(0.01, (high - low) * 0.001))
        setattr(plugin, negative_infinity, -math.inf)
        self.assertEqual(getattr(plugin, negative_infinity), -math.inf)

        # Includes continuous text displays such as semitones, milliseconds,
        # and note names: a sampled range midpoint must not change their value.
        for name, value in before.values.items():
            setattr(plugin, name, value)
        after = capture_plugin(plugin, {}, {})
        self.assertEqual(after.values, before.values)

    def test_native_state_restores_synth_parameters_and_audible_notes(self):
        bundle = self.surge_bundle()
        plugin = NativePlugin(str(bundle), 44100, 512, "instrument")
        descriptors = plugin.parameters
        volume = next(name for name, parameter in descriptors.items()
                      if "global volume" in parameter.label.lower()
                      and parameter.kind == "float" and parameter.is_automatable)
        cutoff = next(name for name, parameter in descriptors.items()
                      if "cutoff" in name and parameter.kind == "float"
                      and parameter.is_automatable
                      and all(math.isfinite(value) for value in parameter.range)
                      and parameter.range[1] > parameter.range[0])
        directory = tempfile.TemporaryDirectory(prefix="vcpi-surge-state-")
        self.addCleanup(directory.cleanup)
        backend = require_native()
        transport = backend.Transport(str(Path(directory.name) / "state.mmap"), 512, True)
        runner = backend.TrackRunner(transport, plugin.native, 44100, 512)
        self.addCleanup(runner.shutdown)

        def render_peak(blocks=32):
            peak = 0.0
            for _ in range(blocks):
                audio = runner.render_once()
                self.assertEqual(audio.shape, (512, 2))
                self.assertTrue(np.isfinite(audio).all())
                peak = max(peak, float(np.abs(audio).max()))
            return peak

        setattr(plugin, volume, -6.0)
        before = {name: getattr(plugin, name) for name in (volume, cutoff)}
        state = plugin.get_state()

        low, high = descriptors[cutoff].range
        target = low + (high - low) * 0.3
        if abs(target - before[cutoff]) < (high - low) * 0.01:
            target = low + (high - low) * 0.7
        setattr(plugin, cutoff, target)
        self.assertNotEqual(getattr(plugin, cutoff), before[cutoff])
        # A controller's advertised floor is not necessarily negative infinity.
        # Compare actual signal levels instead of pinning an incidental range.
        setattr(plugin, volume, descriptors[volume].range[0])
        render_peak()
        transport.send_midi(bytes((0x90, 60, 100)))
        quiet_peak = render_peak()
        transport.send_midi(bytes((0x80, 60, 0)))
        transport.send_midi(bytes((0xB0, 120, 0)))
        render_peak()

        # Restore the exact captured chunk through the existing raw counterpart;
        # no parameter setters or queued controls participate in restoration.
        plugin.load_state(state)
        for name, expected in before.items():
            self.assertEqual(getattr(plugin, name), expected)
        transport.send_midi(bytes((0x90, 60, 100)))
        restored_peak = render_peak()
        self.assertGreater(restored_peak, 1e-5)
        self.assertGreater(restored_peak, quiet_peak * 10)
        transport.send_midi(bytes((0x80, 60, 0)))
        transport.send_midi(bytes((0xB0, 120, 0)))
        render_peak()

        # Session restoration uses the validated counterpart on a fresh instance.
        restored = NativePlugin(str(bundle), 44100, 512, "instrument")
        restored.load_saved_state(state)
        for name, expected in before.items():
            self.assertEqual(getattr(restored, name), expected)
        restored_transport = backend.Transport(
            str(Path(directory.name) / "restored.mmap"), 512, True)
        restored_runner = backend.TrackRunner(
            restored_transport, restored.native, 44100, 512)
        self.addCleanup(restored_runner.shutdown)
        restored_transport.send_midi(bytes((0x90, 60, 100)))
        peak = 0.0
        for _ in range(32):
            audio = restored_runner.render_once()
            self.assertTrue(np.isfinite(audio).all())
            peak = max(peak, float(np.abs(audio).max()))
        self.assertGreater(peak, 1e-5)

        # JUCE silently ignores non-XML raw chunks; saved-state loading must not.
        with self.assertRaises(ValueError):
            restored.load_saved_state(b"not a JUCE VST3 state chunk")
        with self.assertRaises(ValueError):
            restored.load_saved_state(b"")
        for name, expected in before.items():
            self.assertEqual(getattr(restored, name), expected)

    def test_native_plugin_rejects_unsupported_source_kinds(self):
        backend = require_native()
        for kind in ("wav", "sampler", "unknown"):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    backend.Plugin("/not/a/plugin", 44100, 512, kind)


if __name__ == "__main__":
    unittest.main()
