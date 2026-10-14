"""Applied native controls and incremental slot/session cache regressions."""

import unittest
from types import SimpleNamespace


from core.audio_process import RemoteAudioEngine
from core.engine import AudioEngine
from core.models import InstrumentSlot
from core.session import _apply_plugin_params
from core.slot_process import (
    PARAMETER_BATCH_LIMIT, SlotProcessError, SlotWorker, _execute, _slot_state,
)


class ParameterReader:
    def __init__(self, plugin, name):
        self.plugin = plugin
        self.name = name
        self.kind = "bool" if name == "bypass" else "enum" if name == "mode" else "float"
        self.range = (False, True) if name == "bypass" else (0.0, 0.75)
        self.valid_values = ("Normal", "Quiet Mode") if name == "mode" else ()

    @property
    def raw_value(self):
        value = getattr(self.plugin, self.name)
        return float(value) if self.name != "mode" else float(value == "Quiet Mode")

    @property
    def string_value(self):
        return f"applied:{getattr(self.plugin, self.name)}"


class ControlledInstrument:
    __slots__ = ("_amplitude", "_bypass", "_mode")

    def __init__(self):
        self._amplitude = 0.25
        self._bypass = True
        self._mode = "Normal"

    @property
    def parameters(self):
        return {name: ParameterReader(self, name)
                for name in ("amplitude", "bypass", "mode")}

    @property
    def amplitude(self):
        return self._amplitude

    @amplitude.setter
    def amplitude(self, value):
        self._amplitude = max(0.0, min(0.75, float(value)))

    @property
    def bypass(self):
        return self._bypass

    @bypass.setter
    def bypass(self, value):
        if type(value) is not bool:
            raise TypeError("bypass requires a bool")
        self._bypass = value

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        terminal = {"timeout": TimeoutError, "overflow": BufferError,
                    "terminal": SlotProcessError}
        if value in terminal:
            raise terminal[value](f"native {value}")
        if value not in ("Normal", "Quiet Mode"):
            raise ValueError("unknown mode")
        if self.bypass:
            raise ValueError("disable bypass before selecting a mode")
        self._mode = value



class LocalRemoteEngine(RemoteAudioEngine):
    """Exercise production control batching using a protocol-only plugin mock."""

    def __init__(self, owner, worker):
        self.owner = owner
        self.worker = worker

    def request(self, operation, **arguments):
        reply = _execute(self.owner, self.worker.slot_index, operation, arguments)
        self.worker._accept(reply)
        return reply["result"]


class ParameterRestoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = AudioEngine(buffer_size=8, slot_mode=True)
        self.addCleanup(self.engine.shutdown)
        self.instrument = ControlledInstrument()
        self.native_slot = InstrumentSlot("controls", "controls", self.instrument)
        self.engine.slots[2] = self.native_slot
        self.owner = SimpleNamespace(engine=self.engine)
        self.worker = SlotWorker.__new__(SlotWorker)
        self.worker.slot_index = 2
        self.worker.slot = None
        self.worker._accept({"ok": True, "state": _slot_state(self.owner, 2)})
        self.remote = LocalRemoteEngine(self.owner, self.worker)

    def execute(self, operation, **arguments):
        reply = _execute(self.owner, 2, operation, arguments)
        self.assertNotIn("state", reply)
        self.worker._accept(reply)
        return reply


    def test_single_setter_returns_actual_value_and_live_reader_delta(self):
        self.instrument.bypass = False
        snapshot = self.worker.slot.plugin
        reply = self.execute("set_parameter", name="amplitude", value=0.95)
        self.assertEqual(reply["result"], 0.75)
        self.assertIs(self.worker.slot.plugin, snapshot)
        self.assertEqual(snapshot.values["amplitude"], 0.75)
        self.assertEqual(snapshot.parameters["amplitude"].raw_value, 0.75)
        self.assertEqual(snapshot.parameters["amplitude"].string_value, "applied:0.75")

    def test_batch_preserves_types_order_partial_errors_and_mixer_cache(self):
        slot = self.worker.slot
        snapshot = slot.plugin
        slot.gain, slot.generation, slot.midi_channels = 0.375, 42, {3}
        slot.muted, slot.solo = True, True
        reply = self.execute("set_parameters", params={
            "bypass": False, "removed_parameter": 0.9,
            "mode": "Quiet Mode", "amplitude": 0.4,
        })
        result = reply["result"]
        self.assertEqual(list(result["values"]), ["bypass", "mode", "amplitude"])
        self.assertIs(result["values"]["bypass"], False)
        self.assertEqual(result["values"]["mode"], "Quiet Mode")
        self.assertEqual(result["values"]["amplitude"], 0.4)
        self.assertEqual(list(result["errors"]), ["removed_parameter"])
        self.assertIn("removed_parameter", result["errors"]["removed_parameter"])
        self.assertEqual([update[3] for update in reply["updates"]],
                         ["bypass", "mode", "amplitude"])
        self.assertIs(slot.plugin, snapshot)
        self.assertEqual(snapshot.values, result["values"])
        self.assertEqual(snapshot.parameters["mode"].raw_value, 1.0)
        self.assertEqual(snapshot.parameters["mode"].string_value, "applied:Quiet Mode")
        self.assertEqual((slot.gain, slot.generation, slot.midi_channels), (0.375, 42, {3}))
        self.assertTrue(slot.muted)
        self.assertTrue(slot.solo)


    def test_insert_metadata_deltas_preserve_instrument_and_other_insert(self):
        first, second = ControlledInstrument(), ControlledInstrument()
        self.native_slot.effects = [first, second]
        self.worker._accept({"ok": True, "state": _slot_state(self.owner, 2)})
        slot = self.worker.slot
        instrument_values = dict(slot.plugin.values)
        first_values = dict(slot.effects[0].values)
        second_cache = slot.effects[1]
        self.execute("set_parameters", fx_index=1, params={
            "bypass": False, "mode": "Quiet Mode", "amplitude": 0.6,
        })
        reply = self.execute(
            "set_parameter", fx_index=1, name="amplitude", value=0.4)
        self.assertEqual(reply["result"], 0.4)
        self.assertEqual(slot.plugin.values, instrument_values)
        self.assertEqual(slot.effects[0].values, first_values)
        self.assertIs(slot.effects[1], second_cache)
        self.assertEqual(second_cache.values["amplitude"], 0.4)
        self.assertEqual(second_cache.parameters["amplitude"].raw_value, 0.4)
        self.assertEqual(second_cache.parameters["amplitude"].string_value, "applied:0.4")

    def test_oversized_batch_is_rejected_before_any_native_change(self):
        params = {"amplitude": 0.6}
        params.update({f"removed_{index}": index for index in range(PARAMETER_BATCH_LIMIT)})
        with self.assertRaisesRegex(ValueError, "exceeds 64"):
            self.execute("set_parameters", params=params)
        self.assertEqual(self.instrument.amplitude, 0.25)
        self.assertEqual(self.worker.slot.plugin.values["amplitude"], 0.25)

    def test_terminal_parameter_failures_are_not_partial_compatibility_errors(self):
        for mode, error in (("timeout", TimeoutError), ("overflow", BufferError),
                            ("terminal", SlotProcessError)):
            with self.subTest(mode=mode):
                self.instrument.bypass = False
                with self.assertRaisesRegex(error, f"native {mode}"):
                    self.execute("set_parameters", params={
                        "amplitude": 0.5, "mode": mode, "bypass": True,
                    })
                self.assertEqual(self.instrument.amplitude, 0.5)
                self.assertIs(self.instrument.bypass, False)
                self.engine._applied_updates.clear()

    def test_session_batch_applies_compatible_values_and_logs_individual_error(self):
        host = SimpleNamespace(engine=self.remote)
        with self.assertLogs("core.session", level="WARNING") as messages:
            _apply_plugin_params(host, 2, {
                "bypass": False, "removed_parameter": True,
                "mode": "Quiet Mode", "amplitude": 0.6,
            })
        self.assertEqual(len(messages.output), 1)
        self.assertIn("removed_parameter", messages.output[0])
        self.assertIs(self.worker.slot.plugin.values["bypass"], False)
        self.assertEqual(self.worker.slot.plugin.values["mode"], "Quiet Mode")
        self.assertEqual(self.worker.slot.plugin.values["amplitude"], 0.6)

    def test_session_preserves_dependent_control_order_across_batch_boundary(self):
        params = {f"removed_{index}": index for index in range(PARAMETER_BATCH_LIMIT - 1)}
        # Bypass is the final value in the first batch; mode depends on its
        # application and starts the second batch.
        params.update({"bypass": False, "mode": "Quiet Mode", "amplitude": 0.6})
        with self.assertLogs("core.session", level="WARNING") as messages:
            _apply_plugin_params(SimpleNamespace(engine=self.remote), 2, params)
        for index, message in enumerate(messages.output):
            self.assertIn(f"'removed_{index}'", message)
        self.assertEqual(len(messages.output), PARAMETER_BATCH_LIMIT - 1)
        self.assertIs(self.worker.slot.plugin.values["bypass"], False)
        self.assertEqual(self.worker.slot.plugin.values["mode"], "Quiet Mode")
        self.assertEqual(self.worker.slot.plugin.values["amplitude"], 0.6)


if __name__ == "__main__":
    unittest.main()
