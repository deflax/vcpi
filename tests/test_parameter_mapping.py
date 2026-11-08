"""Real MIDI Mix dispatch and displayed bindings from classified snapshots."""

from dataclasses import replace
import unittest

from vcpi.controllers.akai_midimix import KNOB_CCS, MidiMixController, midimix_bindings
from vcpi.audio_state import ParameterSnapshot, PluginSnapshot
from vcpi.models import InstrumentTrack
from vcpi.presentation.plugin_info import plugin_metadata_rows


class RecordingEngine:
    """Record commands at the controller's asynchronous engine boundary."""

    def __init__(self, plugin):
        self.tracks = [InstrumentTrack("Synth", "", plugin, generation=7)] + [None] * 7
        self.commands = []

    def enqueue_param_change(self, index, name, value, *, generation):
        self.commands.append((index, name, value, generation))


class ParameterMappingTests(unittest.TestCase):
    def controller(self, plugin):
        engine = RecordingEngine(plugin)
        controller = MidiMixController(engine)
        self.addCleanup(controller.close)
        return engine, controller

    def turn(self, controller, knob, value):
        controller.on_midi(([0xB0, KNOB_CCS[0][knob], value], 0.0))

    def knob_rows(self, engine):
        return midimix_bindings(engine, "MIDI Mix", None)[1:4]

    def test_zero_and_nonautomatable_controls_keep_order_and_cc_ranges(self):
        plugin = PluginSnapshot(1, parameters={
            "midi_first": ParameterSnapshot(label="MIDI CC 0|1"),
            "zero": ParameterSnapshot(range=(-2, 2), raw_value=0, is_automatable=False),
            "midi_between": ParameterSnapshot(label="MIDI CC 1|1"),
            "enabled": ParameterSnapshot(kind="bool", is_boolean=True),
            "mode": ParameterSnapshot(kind="enum", valid_values=("Soft", "Hard", "Bright")),
            "fourth": ParameterSnapshot(range=(0, 1)),
        }, values={"zero": 0.0, "enabled": False, "mode": "Soft"})
        engine, controller = self.controller(plugin)
        for knob, values in enumerate(((0, 127), (63, 64), (0, 64, 127))):
            for value in values:
                self.turn(controller, knob, value)
        self.assertEqual(engine.commands, [
            (0, "zero", -2.0, 7), (0, "zero", 2.0, 7),
            (0, "enabled", False, 7), (0, "enabled", True, 7),
            (0, "mode", "Soft", 7), (0, "mode", "Hard", 7),
            (0, "mode", "Bright", 7),
        ])
        rows = self.knob_rows(engine)
        self.assertEqual([row.target for row in rows], [
            "Track 1 instrument: zero", "Track 1 instrument: enabled",
            "Track 1 instrument: mode",
        ])
        self.assertEqual(rows[0].value, "0")
        self.assertEqual(rows[0].range, "-2 .. 2")
        self.assertEqual(rows[1].range, "false / true")
        self.assertIn("CC 0-63 / 64-127", rows[1].mode)
        self.assertEqual(rows[2].range, "indices 0-2: Soft, Hard, Bright")
        self.assertTrue(all(row.availability == "mapped" for row in rows))
        # Dispatch queues changes; it does not pretend they have been applied.
        self.assertEqual(plugin.values["zero"], 0.0)

    def test_volume_only_plugdata_leaves_knobs_unbound_without_hiding_controls(self):
        plugin = PluginSnapshot(1, name="plugdata", manufacturer_name="plugdata", parameters={
            "disabled_param": ParameterSnapshot(label="(DISABLED) Param 1", is_automatable=True),
            "bypass": ParameterSnapshot(label="Bypass", kind="bool", is_boolean=True,
                                          is_discrete=True),
            "midi_cc": ParameterSnapshot(label="MIDI CC 3|1", is_automatable=True),
            "program": ParameterSnapshot(kind="enum", valid_values=("Default",), is_discrete=True),
            "volume": ParameterSnapshot(label="Volume", range=(0, 2), is_automatable=False),
            "disabled_param_2": ParameterSnapshot(label="(DISABLED) Param 2", is_automatable=True),
        }, values={"volume": 0.0})
        engine, controller = self.controller(plugin)
        for knob in range(3):
            self.turn(controller, knob, 127)
        self.assertEqual(engine.commands, [])
        rows = self.knob_rows(engine)
        self.assertTrue(all(row.target == "Track 1: (unbound)" for row in rows))
        self.assertTrue(all(row.availability == "unbound" for row in rows))
        self.assertEqual(list(plugin.parameter_items("controls")),
                         [("volume", plugin.parameters["volume"])])
        self.assertEqual(plugin.values["volume"], 0.0)
        metadata = dict(plugin_metadata_rows(plugin))
        for label, count in {
            "Reported parameters": 6, "Sound controls": 1, "Plugin settings": 2,
            "MIDI parameters": 1, "Unused parameters": 2,
            "Automatable parameters (reported)": 3,
            "Boolean parameters (reported)": 1, "Discrete parameters (reported)": 2,
        }.items():
            self.assertEqual(metadata[label], str(count), label)

    def test_plugdata_knobs_bind_patch_controls_with_displayed_ranges(self):
        plugin = PluginSnapshot(1, name="plugdata", manufacturer_name="plugdata", parameters={
            "volume": ParameterSnapshot(label="Volume", range=(0, 2), raw_value=0.8),
            "disabled": ParameterSnapshot(label="(DISABLED) Param 1"),
            "bypass": ParameterSnapshot(kind="bool"),
            "cutoff": ParameterSnapshot(label="Cutoff", range=(20, 20000), units="Hz"),
            "midi_cc": ParameterSnapshot(label="MIDI CC 3|1"),
            "resonance": ParameterSnapshot(label="Resonance", range=(0.1, 10)),
            "envelope_amount": ParameterSnapshot(label="Envelope amount", range=(0, 10000),
                                                 units="Hz"),
            "fourth": ParameterSnapshot(range=(0, 1)),
        }, values={"volume": 0.8, "cutoff": 300.0, "resonance": 2.5,
                   "envelope_amount": 2000.0})
        engine, controller = self.controller(plugin)
        for knob in range(3):
            self.turn(controller, knob, 0)
            self.turn(controller, knob, 127)
        self.assertEqual(engine.commands, [
            (0, "cutoff", 20.0, 7), (0, "cutoff", 20000.0, 7),
            (0, "resonance", 0.1, 7), (0, "resonance", 10.0, 7),
            (0, "envelope_amount", 0.0, 7), (0, "envelope_amount", 10000.0, 7),
        ])
        rows = self.knob_rows(engine)
        self.assertEqual([row.target for row in rows], [
            "Track 1 instrument: cutoff", "Track 1 instrument: resonance",
            "Track 1 instrument: envelope_amount",
        ])
        self.assertEqual([row.value for row in rows], ["300 Hz", "2.5", "2000 Hz"])
        self.assertEqual([row.range for row in rows], [
            "20 .. 20000", "0.1 .. 10", "0 .. 10000",
        ])
        self.assertTrue(all(row.availability == "mapped" for row in rows))
        self.assertEqual(plugin.values["volume"], 0.8)
        self.assertEqual(plugin.parameters["volume"].raw_value, 0.8)

    def test_only_verified_plugdata_identity_excludes_volume_from_knobs(self):
        for name, manufacturer, excluded in (
            ("plugdata", "plugdata", True),
            ("PlugData-FX", "PlugData", True),
            ("Plug Data", "plugdata", True),
            ("Other synth", "Other vendor", False),
            ("plugdata", "Other vendor", False),
            ("Other synth", "plugdata", False),
        ):
            with self.subTest(name=name, manufacturer=manufacturer):
                plugin = PluginSnapshot(1, name=name, manufacturer_name=manufacturer,
                                        parameters={"volume": ParameterSnapshot(range=(0, 2))})
                engine, controller = self.controller(plugin)
                for knob in range(3):
                    self.turn(controller, knob, 127)
                self.assertEqual(engine.commands, [] if excluded else [(0, "volume", 2.0, 7)])
                rows = self.knob_rows(engine)
                self.assertEqual(rows[0].target, "Track 1: (unbound)" if excluded
                                 else "Track 1 instrument: volume")
                self.assertTrue(all(row.target == "Track 1: (unbound)" for row in rows[1:]))

    def test_cardinal_skips_all_midi_parameters_and_window_settings(self):
        parameters = {
            f"midi_ch_{channel}_cc_{cc}": ParameterSnapshot(label=f"MIDI Ch {channel} CC {cc}")
            for channel in range(1, 17) for cc in range(130)
        }
        parameters["show_tooltips"] = ParameterSnapshot(label="Show tooltips", kind="bool")
        parameters["cable_opacity"] = ParameterSnapshot(label="Cable opacity", range=(0, 1))
        parameters.update({
            f"parameter_{number}_v": ParameterSnapshot(label=f"Parameter {number} v", range=(-10, 10))
            for number in range(1, 25)
        })
        plugin = PluginSnapshot(1, name="Cardinal", manufacturer_name="DISTRHO",
                                parameters=parameters, values={"parameter_1_v": 0.0})
        engine, controller = self.controller(plugin)
        for knob, value in enumerate((0, 127, 127)):
            self.turn(controller, knob, value)
        self.assertEqual(engine.commands, [
            (0, "parameter_1_v", -10.0, 7), (0, "parameter_2_v", 10.0, 7),
            (0, "parameter_3_v", 10.0, 7),
        ])
        self.assertEqual([row.target for row in self.knob_rows(engine)], [
            "Track 1 instrument: parameter_1_v", "Track 1 instrument: parameter_2_v",
            "Track 1 instrument: parameter_3_v",
        ])
        metadata = dict(plugin_metadata_rows(plugin))
        self.assertEqual(metadata["Reported parameters"], "2106")
        self.assertEqual(metadata["Sound controls"], "24")
        self.assertEqual(metadata["MIDI parameters"], "2080")
        self.assertEqual(metadata["Plugin settings"], "2")
        self.assertEqual(metadata["Unused parameters"], "0")

    def test_metadata_refresh_rebinds_controls_and_ranges_with_same_generation(self):
        plugin = PluginSnapshot(1, name="plugdata", manufacturer_name="plugdata", parameters={
            "slot": ParameterSnapshot(label="(DISABLED) Param 1", range=(0, 1)),
            "volume": ParameterSnapshot(label="Volume", range=(0, 1)),
            "resonance": ParameterSnapshot(label="Resonance", range=(0, 1)),
        })
        engine, controller = self.controller(plugin)
        self.turn(controller, 0, 127)
        self.turn(controller, 1, 127)
        self.assertEqual(engine.commands, [(0, "resonance", 1.0, 7)])
        # Full owner publications replace descriptor mappings without necessarily
        # replacing the instrument. Value-only updates leave the mapping intact.
        refreshed = replace(plugin, parameters={
            "slot": ParameterSnapshot(label="Cutoff", range=(-5, 5)),
            "volume": ParameterSnapshot(label="Volume", range=(10, 20)),
            "resonance": ParameterSnapshot(label="Resonance", range=(10, 20)),
        })
        engine.tracks[0] = replace(engine.tracks[0], plugin=refreshed)
        self.turn(controller, 0, 0)
        self.turn(controller, 0, 127)
        self.turn(controller, 1, 127)
        self.assertEqual(engine.commands[1:], [
            (0, "slot", -5.0, 7), (0, "slot", 5.0, 7), (0, "resonance", 20.0, 7),
        ])
        rows = self.knob_rows(engine)
        self.assertEqual(rows[0].target, "Track 1 instrument: slot")
        self.assertEqual(rows[0].range, "-5 .. 5")
        self.assertEqual(rows[1].target, "Track 1 instrument: resonance")
        self.assertEqual(rows[1].range, "10 .. 20")
        self.assertEqual(rows[2].target, "Track 1: (unbound)")

    def test_unusable_knob_keeps_ordinal_and_binding_availability(self):
        plugin = PluginSnapshot(1, parameters={
            "unsupported": ParameterSnapshot(kind="enum", valid_values=()),
            "amount": ParameterSnapshot(range=(0, 10)),
        }, values={"amount": 3.0})
        engine, controller = self.controller(plugin)
        engine.tracks[0].muted = True
        engine.tracks[0].enabled = False
        self.turn(controller, 0, 127)
        self.turn(controller, 1, 127)
        self.assertEqual(engine.commands, [(0, "amount", 10.0, 7)])
        bindings = midimix_bindings(engine, "Connected", None)
        self.assertEqual([row.availability for row in bindings[:4]],
                         ["mapped", "unsupported", "mapped", "unbound"])
        self.assertEqual(bindings[2].value, "3")
        self.assertEqual(bindings[2].track, 1)
        self.assertIn("output disconnected", bindings[4].feedback)
        disconnected = controller.control_bindings()
        self.assertEqual([row.availability for row in disconnected[:4]],
                         ["disconnected", "unsupported", "disconnected", "unbound"])
        self.assertEqual(disconnected[6].availability, "unavailable")
        self.assertEqual(disconnected[-1].target, "Master gain")

    def test_refresh_removes_cached_destination_and_clears_all_controls(self):
        plugin = PluginSnapshot(1, parameters={"old": ParameterSnapshot(range=(0, 1))})
        engine, controller = self.controller(plugin)
        self.turn(controller, 0, 127)
        refreshed = replace(plugin, parameters={"new": ParameterSnapshot(range=(2, 4))})
        engine.tracks[0] = replace(engine.tracks[0], plugin=refreshed)
        self.turn(controller, 0, 127)
        engine.tracks[0] = replace(engine.tracks[0], plugin=replace(refreshed, parameters={}))
        for knob in range(3):
            self.turn(controller, knob, 127)
        self.assertEqual(engine.commands, [(0, "old", 1.0, 7), (0, "new", 4.0, 7)])
        self.assertTrue(all(row.target == "Track 1: (unbound)" for row in self.knob_rows(engine)))


if __name__ == "__main__":
    unittest.main()
