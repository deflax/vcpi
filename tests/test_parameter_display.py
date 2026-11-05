"""Cached parameter groups, explicit access, target selection, and presentation."""

import io
from types import SimpleNamespace
from typing import cast
import unittest
from unittest.mock import Mock

from rich.console import Console

from core.audio_state import ParameterSnapshot, PluginSnapshot
from core.cli import HostCLI
from core.client_display import _get_table, display_response
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
        track = SimpleNamespace(plugin=self.instrument, name="Synth", effects=[self.effect],
                                source_type="plugin")
        host = SimpleNamespace(engine=SimpleNamespace(
            tracks=[track, None], master_effects=[self.master, self.effect]))
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

    def test_distinct_instrument_track_effect_and_master_targets(self):
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
                        'get 1 "unterminated'):
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

    def group_instrument(self):
        self.instrument.name = "plugdata"
        self.instrument.manufacturer_name = "plugdata"
        self.instrument.parameters.update({
            "volume": ParameterSnapshot(label="Volume", is_automatable=False),
            "bypass": ParameterSnapshot(label="Bypass", kind="bool"),
            "program": ParameterSnapshot(label="Program", kind="enum",
                                         valid_values=("Default", "Alternate")),
            "disabled_param": ParameterSnapshot(label="(DISABLED) Param 1"),
        })
        self.instrument.values.update({
            "volume": 0.0, "bypass": False, "program": "Default", "disabled_param": 0.0,
        })

    @staticmethod
    def parameter_names(text):
        return [line[2:].split(" = ", 1)[0] for line in text.splitlines()
                if line.startswith("  ") and " = " in line]

    def test_group_listings_and_exact_reads_preserve_raw_state(self):
        self.group_instrument()
        self.instrument.native_state = b"native plugin chunk"
        parameters = dict(self.instrument.parameters)
        values = dict(self.instrument.values)
        expected = {
            "controls": ["cutoff", "manual", "mode", "midi_depth", "custom", "volume"],
            "settings": ["bypass", "program"],
            "midi": ["midi_cc_0_1", "midi_cc_15_129"],
            "unused": ["disabled_param"],
        }
        self.assertEqual(self.parameter_names(self.execute("get 1")), expected["controls"])
        for group, names in expected.items():
            with self.subTest(group=group):
                text = self.execute(f"get 1 --{group}")
                self.assertEqual(self.parameter_names(text), names)
                self.assertFalse(any(line.endswith(":") for line in text.splitlines()))
                for name in names:
                    self.assertEqual(self.parameter_names(self.execute(f"get 1 {name}")), [name])
        text = self.execute("get 1 --all")
        sections = {}
        for line in text.splitlines():
            if not line.startswith("  "):
                section = sections.setdefault(line, [])
            elif " = " in line:
                section.append(line[2:].split(" = ", 1)[0])
        self.assertEqual(sections, {
            "Controls:": expected["controls"], "Plugin settings:": expected["settings"],
            "MIDI parameters:": expected["midi"], "Unused parameters:": expected["unused"],
        })
        self.assertEqual(self.instrument.parameters, parameters)
        self.assertEqual(self.instrument.values, values)
        self.assertEqual(self.instrument.native_state, b"native plugin chunk")

    def test_groups_work_for_track_and_master_effect_targets(self):
        self.effect.parameters["midi"] = ParameterSnapshot(label="MIDI CC 0|1")
        self.effect.values["midi"] = 0.5
        for target in ("1 fx 1", "master 2"):
            with self.subTest(target=target):
                self.assertEqual(self.parameter_names(self.execute(f"get {target}")), ["mix"])
                self.assertEqual(self.parameter_names(
                    self.execute(f"get {target} --midi")), ["midi"])
                self.assertEqual(self.parameter_names(self.execute(f"get {target} midi")), ["midi"])
        self.assertEqual(self.parameter_names(self.execute("get master --controls")), ["gain"])

    def test_cardinal_listing_separates_midi_and_window_settings_from_cv_controls(self):
        self.effect.name = "Cardinal"
        self.effect.manufacturer_name = "DISTRHO"
        self.effect.parameters = {
            "midi_ch_1_cc_0": ParameterSnapshot(label="MIDI Ch 1 CC 0"),
            "parameter_1": ParameterSnapshot(label="Parameter 1", is_automatable=False),
            "parameter_24": ParameterSnapshot(label="Parameter 24"),
            "midi_ch_16_cc_129": ParameterSnapshot(label="MIDI Ch 16 CC 129"),
            "show_tooltips": ParameterSnapshot(label="Show tooltips", kind="bool"),
            "midi_ch_17_cc_0": ParameterSnapshot(label="MIDI Ch 17 CC 0"),
        }
        self.effect.values = {name: 0.0 for name in self.effect.parameters}
        self.assertEqual(self.parameter_names(self.execute("get 1 fx 1")),
                         ["parameter_1", "parameter_24", "midi_ch_17_cc_0"])
        self.assertEqual(self.parameter_names(self.execute("get 1 fx 1 --settings")),
                         ["show_tooltips"])
        self.assertEqual(self.parameter_names(self.execute("get 1 fx 1 --midi")),
                         ["midi_ch_1_cc_0", "midi_ch_16_cc_129"])
        self.assertEqual(self.parameter_names(self.execute("get 1 fx 1 midi_ch_1_cc_0")),
                         ["midi_ch_1_cc_0"])

    def test_listing_flags_reject_combinations_names_and_unknown_options(self):
        for target in ("1", "1 fx 1", "master", "master 1"):
            for arguments in ("--group", "--group settings", "--unknown",
                              "--settings extra", "--controls cutoff", "cutoff --controls",
                              "--all --midi", "--settings --settings"):
                with self.subTest(target=target, arguments=arguments):
                    text = self.execute(f"get {target} {arguments}")
                    self.assertTrue(text.startswith("Error:"), text)
                    self.assertEqual(self.parameter_names(text), [])

    def test_unused_slots_are_rejected_before_set_but_other_groups_remain_writable(self):
        self.group_instrument()
        setter = Mock(return_value=0.0)
        self.cli.host.engine.set_parameter = setter
        self.cli.host.engine.master_effects[0] = self.instrument
        self.cli.host.engine.tracks[0].effects[0] = self.instrument
        for target in ("1", "1 fx 1", "master 1"):
            with self.subTest(target=target):
                text = self.execute(f"set {target} disabled_param 0.5")
                self.assertTrue(text.startswith("Error:"), text)
                self.assertIn("disabled_param", text)
                setter.assert_not_called()
        for name, value, applied in (("volume", "0", 0.0), ("bypass", "true", True),
                                     ("program", "Alternate", "Alternate"),
                                     ("midi_cc_0_1", "0.5", 0.5)):
            with self.subTest(name=name):
                text = self.execute(f"set 1 {name} {value}")
                self.assertFalse(text.startswith("Error:"), text)
                setter.assert_called_once_with(0, name, applied, fx_index=None)
                setter.reset_mock()

    def test_grouped_response_remains_a_rich_table_including_empty_groups(self):
        self.group_instrument()
        lines = self.execute("get 1 --all").splitlines()
        table = _get_table(lines)
        self.assertIsNotNone(table)
        self.assertEqual(table.row_count, len(self.instrument.parameters) + 4)
        rendered = io.StringIO()
        display_response(Console(file=rendered, width=140, color_system=None),
                         "get 1 --all", lines)
        for heading in ("Controls:", "Plugin settings:", "MIDI parameters:", "Unused parameters:"):
            self.assertIn(heading, rendered.getvalue())
        self.assertIn("Range / choices", rendered.getvalue())
        lines = self.execute("get master --all").splitlines()
        table = _get_table(lines)
        self.assertIsNotNone(table)
        self.assertEqual(table.row_count, 8)
        self.assertIsNone(_get_table(["Controls:", "Error: failed"]))

    def test_empty_parameter_value_preserves_following_parameters(self):
        self.instrument.values["mode"] = ""
        rendered = io.StringIO()
        display_response(Console(file=rendered, width=140, color_system=None),
                         "get 1", self.execute("get 1").splitlines())
        text = rendered.getvalue()
        self.assertIn("mode", text)
        self.assertIn("cutoff", text)
        self.assertIn("1200.0 Hz", text)
        self.assertIn("midi_depth", text)

    def test_deck_status_with_empty_errors_preserves_playlist_and_parameters(self):
        track = self.cli.host.engine.tracks[0]
        track.source_type = "deck"
        track.deck = {
            "bank_id": "playlist", "track_id": "song", "track_title": "Opening",
            "error": "", "sync_error": "",
            "tracks": [{"id": "song", "title": "Opening", "path": "/music/opening.wav"}],
        }
        rendered = io.StringIO()
        display_response(Console(file=rendered, width=140, color_system=None),
                         "get 1", self.execute("get 1").splitlines())
        text = rendered.getvalue()
        self.assertIn("track_title = Opening", text)
        self.assertIn("1: Opening", text)
        self.assertIn("/music/opening.wav", text)
        self.assertIn("cutoff = 1200.0 Hz", text)


if __name__ == "__main__":
    unittest.main()
