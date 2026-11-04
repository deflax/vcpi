"""Plugin-specific parameter roles preserve real controls and raw inventories."""

import unittest

from core.audio_state import PARAMETER_GROUPS, ParameterSnapshot, PluginSnapshot


class ParameterGroupTests(unittest.TestCase):
    def test_plugdata_unused_slots_are_not_controls_even_when_automatable(self):
        parameters = {"volume": ParameterSnapshot(label="volume", range=(0.0, 1.0))}
        parameters.update({f"disabled_param{index}": ParameterSnapshot(
            label=f"(DISABLED) param{index}", is_automatable=True, range=(0.0, 1.0))
            for index in range(1, 513)})
        parameters.update({
            "cutoff": ParameterSnapshot(label="Cutoff", range=(0.0, 1.0)),
            "bypass": ParameterSnapshot(label="Bypass", kind="bool"),
            "program": ParameterSnapshot(label="Program", kind="enum"),
            "midi_cc_0_1": ParameterSnapshot(label="MIDI CC 0|1"),
        })
        plugin = PluginSnapshot(1, name="plugdata", manufacturer_name="plugdata",
                                parameters=parameters, values={name: 0.0 for name in parameters})
        self.assertEqual(list(dict(plugin.parameter_items())), ["volume", "cutoff"])
        self.assertEqual(list(dict(plugin.parameter_items("settings"))), ["bypass", "program"])
        self.assertEqual(list(dict(plugin.parameter_items("unused"))),
                         [f"disabled_param{index}" for index in range(1, 513)])
        self.assertEqual(list(dict(plugin.parameter_items("midi"))), ["midi_cc_0_1"])
        self.assertEqual(dict(plugin.parameter_items("all")), parameters)
        self.assertEqual(plugin.values["cutoff"], 0.0)
        self.assertFalse(plugin.parameters["cutoff"].is_automatable)

    def test_cardinal_controller_inventory_and_window_settings_are_separate(self):
        parameters = {
            f"midi_ch_{channel}_cc_{controller}": ParameterSnapshot(
                label=f"MIDI Ch {channel} CC {controller}", range=(0.0, 127.0))
            for channel in range(1, 17) for controller in range(130)
        }
        controls = [f"parameter_{index}_v" for index in range(1, 25)]
        parameters.update({name: ParameterSnapshot(label=f"Parameter {index}",
                                                  units="v", range=(0.0, 10.0))
                           for index, name in enumerate(controls, 1)})
        settings = ("bypass", "show_tooltips", "cable_opacity", "cable_tension",
                    "room_brightness", "light_bloom", "knob_mode",
                    "scroll_wheel_knob_control", "scroll_wheel_knob_sensitivity",
                    "lock_module_positions", "update_rate_limit", "browser_sort",
                    "browser_zoom", "invert_zoom", "auto_squeeze_module_positions")
        parameters.update({name: ParameterSnapshot(label=name) for name in settings})
        plugin = PluginSnapshot(2, name="Cardinal", manufacturer_name="DISTRHO",
                                parameters=parameters, values={name: 0.0 for name in parameters})
        self.assertEqual(list(dict(plugin.parameter_items())), controls)
        self.assertEqual(len(dict(plugin.parameter_items("midi"))), 2080)
        self.assertEqual(list(dict(plugin.parameter_items("settings"))), list(settings))
        self.assertEqual(list(plugin.parameter_items("unused")), [])
        grouped_names = [name for group in PARAMETER_GROUPS for name, _ in plugin.parameter_items(group)]
        self.assertEqual(set(grouped_names), set(parameters))
        self.assertEqual(len(grouped_names), len(parameters))
        self.assertTrue(all(plugin.values[name] == 0.0 for name in controls))

    def test_plugin_rules_do_not_hide_similarly_named_controls_in_other_plugins(self):
        parameters = {
            "disabled_param1": ParameterSnapshot(label="(DISABLED) param1"),
            "midi_ch_1_cc_1": ParameterSnapshot(label="MIDI Ch 1 CC 1"),
            "show_tooltips": ParameterSnapshot(label="Show tooltips"),
            "bypass": ParameterSnapshot(label="Bypass"),
            "program": ParameterSnapshot(label="Program"),
        }
        for name, vendor in (("Other", "Other"), ("Cardinal", "Other"),
                             ("Other", "DISTRHO"), ("Other", "plugdata")):
            with self.subTest(name=name, vendor=vendor):
                plugin = PluginSnapshot(3, name=name, manufacturer_name=vendor, parameters=parameters)
                self.assertEqual(dict(plugin.parameter_items()), parameters)

    def test_enabled_plugdata_parameters_can_have_names_resembling_disabled_slots(self):
        plugin = PluginSnapshot(4, name="plugdata", manufacturer_name="plugdata", parameters={
            "disabled_param1": ParameterSnapshot(label="disabled param1"),
            "cutoff": ParameterSnapshot(label="(DISABLED) Cutoff"),
        })
        self.assertEqual(list(dict(plugin.parameter_items())), ["disabled_param1"])
        plugin.parameters["cutoff"].label = "Cutoff"
        self.assertEqual(list(dict(plugin.parameter_items())), ["disabled_param1", "cutoff"])

    def test_juce_controller_detection_is_exact_and_bounded(self):
        parameters = {str(index): ParameterSnapshot(label=label) for index, label in enumerate((
            "MIDI CC 0|0", "MIDI CC 15|129", "MIDI CC depth", "MIDI CC 0|1 depth",
            "MIDI CC 16|1", "MIDI CC 0|130",
        ))}
        plugin = PluginSnapshot(5, parameters=parameters)
        self.assertEqual(list(dict(plugin.parameter_items("midi"))), ["0", "1"])
        self.assertEqual(list(dict(plugin.parameter_items())), ["2", "3", "4", "5"])

    def test_invalid_group_is_not_silently_replaced_with_controls(self):
        plugin = PluginSnapshot(6)
        with self.assertRaises(ValueError):
            dict(plugin.parameter_items("missing"))
