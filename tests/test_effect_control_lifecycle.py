"""Queued controller edits across insert-chain changes, without native audio."""

from dataclasses import replace
import math
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vcpi.audio.client import RemoteAudioEngine
from vcpi.audio.control import AudioEngine
from vcpi.audio.owner import _OwnerRuntime
from vcpi.audio_state import ParameterSnapshot, PluginSnapshot, capture_plugin
from vcpi.controllers.beatstep_pro import BeatStepProController
from vcpi.deps import HAS_MIDO
from vcpi.models import InstrumentTrack
from vcpi.link import LinkSync
from vcpi.timeline import MusicalTimeline


class QueueConnection:
    def __init__(self, messages=None):
        self.messages = messages if messages is not None else queue.Queue()

    def poll(self, timeout=0):
        return not self.messages.empty()

    def recv_bytes(self, limit):
        return self.messages.get_nowait()

    def send_bytes(self, data):
        self.messages.put_nowait(data)


class InsertWorker:
    """In-process worker boundary using the real parameter control dispatcher."""

    def __init__(self, engine, effects):
        self.engine = engine
        self.engine.tracks[0] = InstrumentTrack(
            "Deck", "", PluginSnapshot(1), effects=effects, source_type="deck", generation=7)
        self.failure = None
        self.accept_during_edit = True
        self.refresh()

    def refresh(self):
        self.track = replace(self.engine.tracks[0], effects=[
            capture_plugin(effect, {"amount": effect.amount}, {})
            for effect in self.engine.tracks[0].effects])

    def request(self, operation, *, wait_hook, **arguments):
        if operation in {"remove_effect", "load_effect"}:
            if self.accept_during_edit:
                wait_hook()
            if operation == "remove_effect":
                del self.engine.tracks[0].effects[arguments["fx_index"]]
            else:
                self.engine.tracks[0].effects.append(arguments["effect"])
            result = None
        elif operation == "set_parameter":
            result = self.engine.set_parameter(0, **arguments)
        else:
            raise AssertionError(operation)
        self.refresh()
        updates = list(self.engine._applied_updates)
        self.engine._applied_updates.clear()
        return {"result": result, "updates": updates}


@unittest.skipUnless(HAS_MIDO, "mido required for controller MIDI parsing")
class EffectControlLifecycleTests(unittest.TestCase):
    def setUp(self):
        # Only native construction/device discovery and wall time are isolated;
        # controller mapping, wire encoding, owner dispatch and parameter writes
        # all execute their production implementations.
        with patch("vcpi.audio.control.require_native", return_value=SimpleNamespace()):
            self.output = AudioEngine(track_mode=True)
            worker_engine = AudioEngine(track_mode=True)
        self.first = self.effect(0.25)
        self.second = self.effect(0.5)
        self.worker = InsertWorker(worker_engine, [self.first, self.second])
        self.output.tracks[0] = replace(self.worker.track, render_owner=self.worker)
        self.client = RemoteAudioEngine.__new__(RemoteAudioEngine)
        self.client._closed = self.client._closing = False
        self.client._failure = None
        self.client._process = SimpleNamespace(exitcode=None)
        self.client._input_lock = threading.Lock()
        self.client._events = queue.Queue(maxsize=64)
        self.client._last_input = 0
        self.client.tracks = [replace(self.output.tracks[0])] + [None] * 7
        self.controller = BeatStepProController(self.client, MusicalTimeline(LinkSync()))
        self.addCleanup(self.controller.close)
        clock = patch("vcpi.audio.owner.time.monotonic", return_value=10.0)
        clock.start()
        self.addCleanup(clock.stop)
        with patch("vcpi.audio.owner._discover_devices", return_value=[]):
            self.runtime = _OwnerRuntime(
                SimpleNamespace(engine=self.output), None,
                QueueConnection(self.client._events), QueueConnection(),
                SimpleNamespace(value=False))
        self.runtime.generations[0] = 7
        self.runtime._last_track_health = 10.0

    @staticmethod
    def effect(amount):
        return SimpleNamespace(amount=amount, parameters={
            "amount": ParameterSnapshot(range=(0.0, 1.0))})

    def rotary(self, ordinal, value):
        self.controller.on_midi(([
            0xBF, self.controller.profile.rotary_ccs[ordinal], value], 0.0))

    def apply_inputs(self):
        while self.runtime.inputs.poll():
            self.runtime.receive_input()
        self.runtime.apply_track_parameters()

    def refresh_client(self):
        self.client.tracks[0] = replace(self.output.tracks[0])

    def test_removal_drops_shifted_and_out_of_range_inputs_received_while_waiting(self):
        self.rotary(8, 127)
        self.rotary(9, 0)
        self.runtime.track_request(0, "remove_effect", fx_index=0)
        self.assertEqual(len(self.runtime.pending_parameters), 2)
        self.runtime.apply_track_parameters()
        self.assertEqual(self.second.amount, 0.5)
        self.assertIsNone(self.worker.failure)
        self.assertEqual(self.output.tracks[0].effects[0].token, id(self.second))
        self.assertEqual(self.runtime.generations[0], 7)

        # The surviving insert remains controllable at its new index.
        self.refresh_client()
        self.rotary(8, 127)
        self.apply_inputs()
        self.assertEqual(self.second.amount, 1.0)
        self.assertIsNone(self.worker.failure)

    def test_replacement_rejects_old_snapshot_even_when_input_arrives_after_edit(self):
        self.worker.accept_during_edit = False
        self.rotary(9, 127)
        self.runtime.track_request(0, "remove_effect", fx_index=1)
        replacement = self.effect(0.125)
        self.runtime.track_request(0, "load_effect", effect=replacement)
        self.apply_inputs()
        self.assertEqual(replacement.amount, 0.125)
        self.assertIsNone(self.worker.failure)

        self.refresh_client()
        self.rotary(9, 127)
        self.apply_inputs()
        self.assertEqual(replacement.amount, 1.0)
        self.assertIsNone(self.worker.failure)

    def test_already_received_input_is_checked_again_after_removal(self):
        self.rotary(8, 127)
        self.runtime.receive_input()
        self.runtime.track_request(0, "remove_effect", fx_index=0)
        self.runtime.apply_track_parameters()
        self.assertEqual(self.second.amount, 0.5)
        self.assertIsNone(self.worker.failure)

    def test_unsupported_numeric_ranges_stay_inactive_without_worker_edits(self):
        self.controller._port = SimpleNamespace(name="Connected", close=lambda: None)
        parameter = self.client.tracks[0].effects[0].parameters["amount"]
        for bounds in ((-math.inf, 6), (0, math.inf), (math.nan, 1),
                       (None, 1), (), (2, 1), (-1e308, 1e308)):
            for mode in ("absolute", "twos_complement"):
                with self.subTest(bounds=bounds, mode=mode):
                    parameter.range = bounds
                    modes = list(self.controller.profile.rotary_modes)
                    modes[8] = mode
                    self.controller.profile = replace(
                        self.controller.profile, rotary_modes=tuple(modes))
                    for value in (0, 64, 127):
                        self.rotary(8, value)
                    self.apply_inputs()
                    self.assertEqual(self.first.amount, 0.25)
                    self.assertEqual(self.client._last_input, 0)
                    self.assertIsNone(self.worker.failure)
                    binding = next(row for row in self.controller.control_bindings()
                                   if row.control == "Rotary 9")
                    self.assertEqual(binding.availability, "unsupported")
                    self.assertEqual(binding.reason, "unsupported effect parameter range")

        # Unsupported controls retain their ordinal; the next insert still works.
        self.rotary(9, 127)
        self.apply_inputs()
        self.assertEqual(self.second.amount, 1.0)

    def test_nonfinite_observed_value_does_not_generate_relative_nan(self):
        snapshot = self.client.tracks[0].effects[0]
        modes = list(self.controller.profile.rotary_modes)
        modes[8] = "twos_complement"
        self.controller.profile = replace(self.controller.profile, rotary_modes=tuple(modes))
        for observed in (math.nan, math.inf, -math.inf):
            with self.subTest(observed=observed):
                snapshot.values["amount"] = observed
                self.rotary(8, 1)
                self.apply_inputs()
                self.assertEqual(self.first.amount, 0.25)
                self.assertEqual(self.client._last_input, 0)
                self.assertIsNone(self.worker.failure)
