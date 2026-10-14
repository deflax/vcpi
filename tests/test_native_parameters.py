"""Real controller conversions must preserve saved typed parameter values."""

import math
from pathlib import Path
import platform
import sys
import unittest

from core.audio_state import capture_plugin
from core.plugin_host import NativePlugin


class NativeParameterRestorationTests(unittest.TestCase):
    def test_typed_values_survive_changes_and_complete_restoration(self):
        bundle = Path(__file__).resolve().parents[1] / "vst3" / "Surge XT.vst3"
        if sys.platform == "darwin":
            binary_directory = bundle / "Contents" / "MacOS"
        elif sys.platform.startswith("linux"):
            architecture = {"arm64": "aarch64", "amd64": "x86_64"}.get(
                platform.machine(), platform.machine())
            binary_directory = bundle / "Contents" / f"{architecture}-linux"
        else:
            self.skipTest("native audio supports Linux and macOS")
        if not binary_directory.is_dir():
            self.skipTest("fetch a native Surge XT bundle for this platform")

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


if __name__ == "__main__":
    unittest.main()
