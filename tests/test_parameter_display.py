"""User-facing parameter displays omit generated MIDI controller helpers."""

import io
from types import SimpleNamespace
from typing import cast
import unittest

from core.audio_state import ParameterSnapshot, PluginSnapshot
from core.cli import HostCLI
from core.host import VcpiCore


class ParameterDisplayTests(unittest.TestCase):
    def cli_for(self, plugin: PluginSnapshot):
        slot = SimpleNamespace(plugin=plugin, name="Synth", effects=[plugin])
        host = SimpleNamespace(engine=SimpleNamespace(
            slots=[slot], master_effects=[plugin]))
        output = io.StringIO()
        return HostCLI(cast(VcpiCore, host), stdout=output, owns_host=False), output

    def test_helpers_hidden_without_hiding_normal_nonautomatable_controls(self):
        parameters = {
            "cutoff": ParameterSnapshot(label="Cutoff", is_automatable=True),
            "manual_control": ParameterSnapshot(label="Manual control"),
            "midi_depth": ParameterSnapshot(label="MIDI CC depth"),
            "custom_control": ParameterSnapshot(label="MIDI CC 0|1 depth"),
            "midi_cc_0_1": ParameterSnapshot(label="MIDI CC 0|1"),
            "midi_cc_15_129": ParameterSnapshot(label="MIDI CC 15|129"),
        }
        plugin = PluginSnapshot(1, parameters=parameters,
                                values={name: 0.5 for name in parameters})
        cli, output = self.cli_for(plugin)
        for command in ("params 1", "params master 1", "knobs 1",
                        "knobs 1 fx 1", "knobs master 1"):
            with self.subTest(command=command):
                output.seek(0)
                output.truncate()
                cli.onecmd(command)
                text = output.getvalue()
                for name in ("cutoff", "manual_control", "midi_depth", "custom_control"):
                    self.assertIn(name, text)
                self.assertNotIn("midi_cc_0_1", text)
                self.assertNotIn("midi_cc_15_129", text)
        self.assertIn("midi_cc_0_1", plugin.parameters)
        self.assertEqual(plugin.values["midi_cc_0_1"], 0.5)

    def test_helper_only_plugin_uses_empty_knobs_display(self):
        plugin = PluginSnapshot(1, parameters={
            "midi_cc_0_1": ParameterSnapshot(label="MIDI CC 0|1")},
            values={"midi_cc_0_1": 0.5})
        cli, output = self.cli_for(plugin)
        cli.onecmd("knobs 1")
        self.assertIn("(no parameters)", output.getvalue())
        self.assertNotIn("midi_cc_0_1", output.getvalue())
        output.seek(0)
        output.truncate()
        cli.onecmd("params 1")
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
