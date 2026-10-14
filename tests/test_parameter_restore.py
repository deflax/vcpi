"""Applied native controls and incremental slot/session cache regressions."""

import base64
import json
import multiprocessing
import pickle
import threading
import unittest
from types import SimpleNamespace


from core.audio_process import RemoteAudioEngine, _OwnerRuntime
from core.engine import AudioEngine
from core.models import InstrumentSlot
from core.session import _apply_plugin_params, _restore_plugin_state, _saved_plugin_state
from core.slot_process import (
    PARAMETER_BATCH_LIMIT, SlotProcessError, SlotWorker, _execute, _slot_state,
)
from core.audio_state import PluginSnapshot, capture_plugin
from core.state_transport import (
    NATIVE_MESSAGE_LIMIT, encode_native_message, receive_frame,
    validate_native_state,
)


class ParameterReader:
    def __init__(self, plugin, name):
        self.plugin = plugin
        self.name = name
        self.kind = "bool" if name == "bypass" else "enum" if name == "mode" else "float"
        self.range = (False, True) if name == "bypass" else (0.0, 0.75)
        self.valid_values = ("Normal", "Quiet Mode") if name == "mode" else ()
        self.is_automatable = True

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

    def get_state(self):
        return json.dumps([self._amplitude, self._bypass, self._mode]).encode()

    def load_saved_state(self, state):
        self._amplitude, self._bypass, self._mode = json.loads(state)



class LocalRemoteEngine(RemoteAudioEngine):
    """Exercise production control batching using a protocol-only plugin mock."""

    def __init__(self, owner, worker):
        self.owner = owner
        self.worker = worker

    @property
    def slots(self):
        slots = [None] * 8
        slots[self.worker.slot_index] = self.worker.slot
        return slots

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

    def test_session_batch_applies_compatible_values_and_logs_skipped_unknown(self):
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

    def test_session_filters_unknown_controls_before_ordered_legacy_batch(self):
        params = {f"removed_{index}": index for index in range(PARAMETER_BATCH_LIMIT - 1)}
        params.update({"bypass": False, "mode": "Quiet Mode", "amplitude": 0.6})
        with self.assertLogs("core.session", level="WARNING") as messages:
            _apply_plugin_params(SimpleNamespace(engine=self.remote), 2, params)
        for index, message in enumerate(messages.output):
            self.assertIn(f"'removed_{index}'", message)
        self.assertEqual(len(messages.output), PARAMETER_BATCH_LIMIT - 1)
        self.assertIs(self.worker.slot.plugin.values["bypass"], False)
        self.assertEqual(self.worker.slot.plugin.values["mode"], "Quiet Mode")
        self.assertEqual(self.worker.slot.plugin.values["amplitude"], 0.6)

    def test_legacy_nonautomatable_control_cannot_trigger_sound_side_effect(self):
        self.instrument.bypass = False
        self.worker._accept({"ok": True, "state": _slot_state(self.owner, 2)})
        self.worker.slot.plugin.parameters["bypass"].is_automatable = False
        with self.assertLogs("core.session", level="WARNING") as messages:
            _apply_plugin_params(SimpleNamespace(engine=self.remote), 2, {
                "bypass": True, "mode": "Quiet Mode", "amplitude": 0.6,
            })
        self.assertFalse(self.instrument.bypass)
        self.assertEqual(self.instrument.mode, "Quiet Mode")
        self.assertEqual(self.instrument.amplitude, 0.6)
        self.assertIn("not automatable", messages.output[0])

    def test_native_chunk_precedence_refreshes_instrument_and_insert_without_replay(self):
        self.native_slot.effects = [ControlledInstrument()]
        saved = json.dumps([0.6, False, "Quiet Mode"]).encode()
        for fx_index in (None, 0):
            with self.subTest(fx_index=fx_index):
                entry = {"native_state": base64.b64encode(saved).decode(),
                         "params": {"mode": "timeout", "amplitude": 0.0}}
                _restore_plugin_state(
                    SimpleNamespace(engine=self.remote), 2, entry, 2, fx_index)
                cached = (self.worker.slot.plugin if fx_index is None
                          else self.worker.slot.effects[fx_index])
                self.assertIsInstance(cached, PluginSnapshot)
                self.assertEqual(cached.values["amplitude"], 0.6)
                self.assertEqual(cached.values["mode"], "Quiet Mode")
                self.assertIsNone(cached.native_state)

    def test_native_state_capture_is_explicit_and_missing_capture_cannot_save(self):
        ordinary = capture_plugin(self.instrument, {}, {})
        self.assertIsNone(ordinary.native_state)
        with self.assertRaisesRegex(RuntimeError, "not captured"):
            _saved_plugin_state(ordinary)
        reply = _execute(self.owner, 2, "sync_state", {})
        saved = reply["state"]["plugin"]
        self.assertEqual(base64.b64decode(_saved_plugin_state(saved)),
                         self.instrument.get_state())
        self.assertIsNone(capture_plugin(saved, {}, {}).native_state)

    def test_master_chunk_restore_and_capture_refresh_metadata_without_normal_chunk_messages(self):
        effect = ControlledInstrument()
        self.engine.master_effects = [effect]
        self.owner.audio_output_name = None
        runtime = _OwnerRuntime.__new__(_OwnerRuntime)
        runtime.owner = self.owner
        runtime.engine = self.engine
        runtime.generations = [0] * 8
        runtime.epoch = 0
        runtime.devices = []
        runtime.master_snapshots = []
        runtime.update_serial = 0
        saved = json.dumps([0.6, False, "Quiet Mode"]).encode()
        reply = pickle.loads(runtime.execute(
            "load_plugin_state", {"slot_index": None, "fx_index": 0, "state": saved}))
        self.assertIsInstance(reply["result"], PluginSnapshot)
        self.assertEqual(reply["result"].values["amplitude"], 0.6)
        self.assertIsNone(reply["result"].native_state)
        self.assertIsNone(reply["state"]["master_effects"][0].native_state)
        captured = runtime.snapshot(inspect_master=True, capture_native_state=True)
        self.assertEqual(captured["master_effects"][0].native_state, saved)
        ordinary = runtime.snapshot()
        self.assertIsNone(ordinary["master_effects"][0].native_state)

    def test_malformed_or_missing_chunk_never_falls_back_to_parameters(self):
        for entry in (
            {"native_state": "@not-base64", "params": {"amplitude": 0.0}},
            {"native_state": "", "params": {"amplitude": 0.0}},
            {"native_state": 123, "params": {"amplitude": 0.0}},
            {"native_state": base64.b64encode(b"not-native-state").decode(),
             "params": {"amplitude": 0.0}},
            {"params": {"amplitude": 0.0}},
        ):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                _restore_plugin_state(SimpleNamespace(engine=self.remote), 2, entry, 2)
        self.assertEqual(self.instrument.amplitude, 0.25)


class NativeStateTransportTests(unittest.TestCase):
    def test_multipart_requires_explicit_allowance_and_bounded_aggregate(self):
        for size, enabled in ((65 * 1024, False), (NATIVE_MESSAGE_LIMIT + 1, True)):
            sender, receiver = multiprocessing.Pipe()
            try:
                sender.send_bytes(pickle.dumps(("vcpi-native-state-v1", size)))
                with self.assertRaises(ValueError):
                    receive_frame(receiver, 64 * 1024, native_state=enabled)
            finally:
                sender.close()
                receiver.close()

    def test_state_transfer_keeps_metadata_and_operation_limits(self):
        message = {"operation": "load_plugin_state",
                   "arguments": {"state": b"opaque", "name": "x" * 65536}}
        with self.assertRaisesRegex(ValueError, "metadata exceeds"):
            encode_native_message(message, 64 * 1024)
        with self.assertRaisesRegex(ValueError, "explicit capture/load"):
            encode_native_message({"operation": "set_parameter", "arguments": {}}, 65536)

    def test_native_chunk_requires_nonempty_bytes(self):
        for invalid in (None, "", bytearray(b"state"), b""):
            with self.subTest(invalid=invalid), self.assertRaises((TypeError, ValueError)):
                validate_native_state(invalid)


if __name__ == "__main__":
    unittest.main()
