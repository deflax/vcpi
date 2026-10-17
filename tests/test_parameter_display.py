"""Cached parameter reads, target selection, and MIDI helper filtering."""

import io
from types import SimpleNamespace
from typing import cast
import unittest

from core.audio_state import ParameterSnapshot, PluginSnapshot
from core.cli import HostCLI
from core.host import VcpiCore


class ParameterDisplayTests(unittest.TestCase):
    instrument: PluginSnapshot
    effect: PluginSnapshot
    master: PluginSnapshot
    output: io.StringIO
    cli: HostCLI

    def setUp(self):
        self.instrument = PluginSnapshot(1, parameters={
            "cutoff": ParameterSnapshot(label="Cutoff", units="Hz", range=(20.0, 20000.0)),
            "manual": ParameterSnapshot(label="Manual control"),
            "mode": ParameterSnapshot(kind="enum", valid_values=("Quiet Mode", "Loud Mode")),
            "midi_depth": ParameterSnapshot(label="MIDI CC depth"),
            "custom": ParameterSnapshot(label="MIDI CC 0|1 depth"),
            "midi_cc_0_1": ParameterSnapshot(label="MIDI CC 0|1"),
            "midi_cc_15_129": ParameterSnapshot(label="MIDI CC 15|129"),
        }, values={"cutoff": 1200.0, "manual": 0.5, "mode": "Quiet Mode",
                   "midi_depth": 0.2, "custom": 0.3,
                   "midi_cc_0_1": 0.0, "midi_cc_15_129": 0.0})
        self.effect = PluginSnapshot(2, parameters={"mix": ParameterSnapshot()},
                                     values={"mix": 0.25})
        self.master = PluginSnapshot(3, parameters={"gain": ParameterSnapshot()},
                                     values={"gain": 0.75})
        slot = SimpleNamespace(plugin=self.instrument, name="Synth", effects=[self.effect])
        host = SimpleNamespace(engine=SimpleNamespace(
            slots=[slot, None], master_effects=[self.master, self.effect]))
        self.output = io.StringIO()
        self.cli = HostCLI(cast(VcpiCore, cast(object, host)), stdout=self.output, owns_host=False)

    def execute(self, command: str) -> str:
        self.output.seek(0)
        self.output.truncate()
        self.cli.onecmd(command)
        return self.output.getvalue()

    def test_listing_preserves_controls_units_and_choices_but_hides_helpers(self):
        text = self.execute("get 1")
        for name in ("cutoff", "manual", "mode", "midi_depth", "custom"):
            self.assertIn(name, text)
        self.assertIn("1200.0 Hz", text)
        self.assertIn("range 20 .. 2e+04", text)
        self.assertIn("choices Quiet Mode, Loud Mode", text)
        self.assertNotIn("midi_cc_0_1", text)
        self.assertNotIn("midi_cc_15_129", text)
        self.assertIn("midi_cc_0_1", self.instrument.parameters)
        self.assertEqual(self.instrument.values["midi_cc_0_1"], 0.0)

    def test_distinct_instrument_slot_effect_and_master_targets(self):
        for command, expected, absent in (
            ("get 1 cutoff", "cutoff = 1200.0 Hz", "mode ="),
            ("get 1 fx 1", "mix = 0.25", "cutoff ="),
            ("get 1 fx 1 mix", "mix = 0.25", "gain ="),
            ("get master", "gain = 0.75", "mix ="),
            ("get master gain", "gain = 0.75", "mix ="),
            ("get master 2", "mix = 0.25", "gain ="),
            ("get master 2 mix", "mix = 0.25", "gain ="),
        ):
            with self.subTest(command=command):
                text = self.execute(command)
                self.assertIn(expected, text)
                self.assertNotIn(absent, text)

    def test_invalid_reads_never_fall_back_to_a_different_target(self):
        for command in ("get", "get 0", "get 2", "get 1 fx", "get 1 fx 0",
                        "get 1 fx 2", "get master 0", "get master -1",
                        "get master 3", "get 1 missing", "get 1 cutoff extra",
                        "get 1 midi_cc_0_1", 'get 1 "unterminated'):
            with self.subTest(command=command):
                text = self.execute(command)
                self.assertTrue(text.startswith("Error:"), text)
                self.assertNotIn("cutoff =", text)

    def test_helper_only_and_empty_plugins_have_no_visible_parameters(self):
        self.effect.parameters = {
            "midi_cc_0_1": ParameterSnapshot(label="MIDI CC 0|1")}
        self.effect.values = {"midi_cc_0_1": 0.5}
        self.assertIn("No parameters", self.execute("get 1 fx 1"))
        self.effect.parameters.clear()
        self.effect.values.clear()
        self.assertIn("No parameters", self.execute("get 1 fx 1"))


if __name__ == "__main__":
    unittest.main()
