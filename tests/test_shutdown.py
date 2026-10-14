"""Shutdown ownership and pending-note regressions without audio hardware."""

import json
import socket
import tempfile
import threading
import unittest
import weakref
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from core import deps
from core.audio_process import AudioProcessError
from core.engine import AudioEngine
from core.models import InstrumentSlot
from core.host import VcpiCore
from core.main import _boot_host
from core.sequencer import Sequencer


class _ControlEngine(AudioEngine):
    """In-process audio state with real control handles, but no audio device."""

    def __init__(self, sample_rate=44100, buffer_size=512):
        super().__init__(sample_rate, buffer_size)
        self.state_lock = self._lock
        self.audio_output_name = None
        self.listeners = []
        self.control, self.owner_control = socket.socketpair()
        self.restore_interrupt = None
        self.snapshot_error = None
        self.failure = None
        self.cleanup_error = None

    def add_state_listener(self, listener):
        self.listeners.append(listener)

    def remove_state_listener(self, listener):
        self.listeners.remove(listener)

    def set_master_gain(self, value):
        result = super().set_master_gain(value)
        if self.restore_interrupt is not None:
            raise self.restore_interrupt
        return result

    def sync_state(self):
        if self.snapshot_error is not None:
            raise self.snapshot_error

    def shutdown(self):
        super().shutdown()
        self.control.close()
        self.owner_control.close()
        if self.cleanup_error is not None:
            raise self.cleanup_error


class _MidiHandle:
    """A hardware-free backend handle closed by the real MIDI wrappers."""

    def __init__(self):
        self.handle = socket.socket()
        self.deleted = False

    def close_port(self):
        self.handle.close()

    def delete(self):
        self.deleted = True


class HostShutdownTests(unittest.TestCase):
    def _activate_resources(self, host):
        self.addCleanup(host.shutdown, save=False)
        host.engine.slots[0] = InstrumentSlot(
            "incomplete", "/tmp/incomplete.wav", None, source_type="wav",
        )
        host.send_note(0, 60, duration=60.0)
        host.sequencer.start()
        host.sequencer._schedule_note_offs([(0, 64, 60.0, 0)], deps.mido)
        self.host_timers = list(host._note_timers)
        self.sequence_timers = list(host.sequencer._note_off_timers)
        self.sequence_thread = host.sequencer._thread
        host.link._start_loop_thread()
        self.link_thread = host.link._loop_thread

        from controllers.midi_input import MidiInputController

        self.midi_input = MidiInputController(host.engine)
        host.midi_inputs.append(self.midi_input)
        wrappers = (
            self.midi_input._port, host.midimix._in_port, host.midimix._out_port,
        )
        self.midi_handles = []
        for wrapper in wrappers:
            handle = _MidiHandle()
            wrapper._port = handle
            wrapper._name = "test MIDI"
            self.midi_handles.append(handle)
            self.addCleanup(handle.handle.close)

    def _assert_closed(self, host):
        self.assertTrue(host._shutdown)
        self.assertEqual(host._note_timers, {})
        self.assertEqual(host.sequencer._note_off_timers, [])
        self.assertFalse(host.sequencer._running)
        self.assertTrue(all(
            not timer.is_alive()
            for timer in self.host_timers + self.sequence_timers
        ))
        self.assertFalse(self.sequence_thread.is_alive())
        self.assertFalse(self.link_thread.is_alive())
        self.assertEqual(host.midi_inputs, [])
        self.assertIsNone(self.midi_input.port_name)
        self.assertIsNone(host.midimix.input_port_name)
        self.assertIsNone(host.midimix.output_port_name)
        self.assertTrue(all(handle.deleted for handle in self.midi_handles))
        self.assertTrue(all(
            handle.handle.fileno() == -1 for handle in self.midi_handles
        ))
        self.assertEqual(host.engine.listeners, [])
        self.assertTrue(host.engine._closed)
        self.assertTrue(all(slot is None for slot in host.engine.slots))
        self.assertEqual(host.engine.control.fileno(), -1)
        self.assertEqual(host.engine.owner_control.fileno(), -1)

    def _host(self, session_path):
        with patch("core.host.RemoteAudioEngine", _ControlEngine):
            host = VcpiCore(session_path=str(session_path))
        self._activate_resources(host)
        return host

    def test_shutdown_without_save_preserves_file_and_closes_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            original = b'{"version": 1, "master_gain": 0.75}\n'
            path.write_bytes(original)
            host = self._host(path)
            host.engine.snapshot_error = RuntimeError("native owner unavailable")
            host.shutdown(save=False)
            self.assertEqual(path.read_bytes(), original)
            self._assert_closed(host)
            host.shutdown()
            self.assertEqual(path.read_bytes(), original)

    def test_normal_shutdown_still_saves_before_releasing_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            host = self._host(path)
            host.engine.set_master_gain(0.25)
            host.shutdown()
            saved = json.loads(path.read_text())
            self.assertEqual(saved["master_gain"], 0.25)
            self.assertEqual(saved["slots"][0]["name"], "incomplete")
            self.assertEqual(saved["connections"]["midi_inputs"], ["test MIDI"])
            self._assert_closed(host)
            original = path.read_bytes()
            host.shutdown()
            self.assertEqual(path.read_bytes(), original)

    def test_save_failure_still_closes_resources_and_preserves_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            original = b'{"version": 1}\n'
            path.write_bytes(original)
            host = self._host(path)
            failure = RuntimeError("native snapshot failed")
            host.engine.snapshot_error = failure
            with self.assertRaises(RuntimeError) as raised:
                host.shutdown()
            self.assertIs(raised.exception, failure)
            self.assertEqual(path.read_bytes(), original)
            self._assert_closed(host)

    def test_audio_save_error_is_not_ignored_without_terminal_owner_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            original = b'{"version": 1}\n'
            path.write_bytes(original)
            host = self._host(path)
            failure = AudioProcessError("audio control channel is busy")
            host.engine.snapshot_error = failure
            with self.assertRaises(AudioProcessError) as raised:
                host.shutdown()
            self.assertIs(raised.exception, failure)
            self.assertEqual(path.read_bytes(), original)
            self._assert_closed(host)

    def test_failed_restore_preserves_original_cancellation_and_session(self):
        for cancellation in (KeyboardInterrupt("restore interrupted"), SystemExit(7)):
            for cleanup_error in (
                None, RuntimeError("control close failed"), SystemExit(11),
            ):
                with self.subTest(
                    cancellation=type(cancellation).__name__,
                    cleanup_error=type(cleanup_error).__name__,
                ), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "session.json"
                    original = (
                        b'{\n  "version": 1, "bpm": 91.0, "master_gain": 0.75,\n'
                        b'  "slots": [null], "routing": {"1": 1}\n}\n'
                    )
                    path.write_bytes(original)
                    hosts = []
                    test = self

                    class StartupHost(VcpiCore):
                        def __init__(self, **kwargs):
                            super().__init__(**kwargs)
                            hosts.append(self)
                            test._activate_resources(self)
                            self.engine.restore_interrupt = cancellation
                            self.engine.snapshot_error = RuntimeError(
                                "native owner unavailable during save",
                            )
                            self.engine.cleanup_error = cleanup_error

                    args = SimpleNamespace(
                        sr=44100, buf=512, session=str(path), bpm=120.0,
                        no_restore=False, link=False, midi_in=None,
                        mix_midi=None, mix_midi_out=None,
                    )
                    with patch("core.host.RemoteAudioEngine", _ControlEngine), \
                            patch("core.host.VcpiCore", StartupHost):
                        if cleanup_error is None:
                            with self.assertNoLogs("core.main", level="ERROR"), \
                                    self.assertRaises(type(cancellation)) as raised:
                                _boot_host(args)
                        else:
                            with self.assertLogs("core.main", level="ERROR") as logged, \
                                    self.assertRaises(type(cancellation)) as raised:
                                _boot_host(args)
                            self.assertIn("startup cleanup failed", logged.output[0])
                            self.assertIn(str(cleanup_error), logged.output[0])
                    self.assertIs(raised.exception, cancellation)
                    host = hosts[0]
                    self.assertEqual(host.link.bpm, 91.0)
                    self.assertEqual(host.engine.master_gain, 0.75)
                    self.assertEqual(host.channel_map, {})
                    self.assertEqual(path.read_bytes(), original)
                    self._assert_closed(host)

    def test_shutdown_without_save_propagates_cleanup_error_after_closing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            host = self._host(path)
            failure = RuntimeError("control close failed")
            host.engine.cleanup_error = failure
            with self.assertLogs("core.host", level="ERROR"), \
                    self.assertRaises(RuntimeError) as raised:
                host.shutdown(save=False)
            self.assertIs(raised.exception, failure)
            self.assertFalse(path.exists())
            self._assert_closed(host)


class ShutdownTests(unittest.TestCase):
    def test_engine_releases_control_plugin_references_before_returning(self):
        released_on: list[int] = []

        class PluginProbe:
            def __init__(self):
                self.parameter = SimpleNamespace(raw_value=0.0)

            def __del__(self):
                released_on.append(threading.get_ident())

        engine = AudioEngine()
        instrument = PluginProbe()
        effect = PluginProbe()
        master = PluginProbe()
        refs = [weakref.ref(plugin) for plugin in (instrument, effect, master)]
        engine.slots[0] = InstrumentSlot(
            "probe", "/tmp/probe.vst3", instrument, effects=[effect],
        )
        engine.master_effects.append(master)
        del instrument, effect, master
        try:
            engine.shutdown()
            self.assertTrue(all(ref() is None for ref in refs))
            self.assertEqual(released_on, [threading.get_ident()] * 3)
            engine.shutdown()
            self.assertEqual(len(released_on), 3)
        finally:
            engine.shutdown()

    def test_stopping_sequence_delivers_pending_note_off_once(self):
        host = SimpleNamespace(engine=AudioEngine())
        seq = Sequencer(host)
        seq._running = True
        seq._schedule_note_offs([(0, 60, 60.0, 0)], deps.mido)
        timers = list(seq._note_off_timers)
        try:
            seq.stop()
            messages = list(host.engine._midi_queue)
            self.assertEqual(
                [(slot, message.type, message.note) for slot, message in messages],
                [(0, "note_off", 60)],
            )
            self.assertTrue(all(not timer.is_alive() for timer in timers))
            seq.stop()
            self.assertEqual(list(host.engine._midi_queue), messages)
        finally:
            seq.stop()
            for timer in timers:
                timer.cancel()
                timer.join()
            host.engine.shutdown()

    def test_stopping_failed_sequence_still_releases_pending_notes(self):
        host = SimpleNamespace(engine=AudioEngine())
        seq = Sequencer(host)
        seq._schedule_note_offs([(0, 60, 60.0, 0)], deps.mido)
        timers = list(seq._note_off_timers)
        try:
            seq.stop()  # The playback thread has already marked itself stopped.
            self.assertEqual(
                [(slot, message.type, message.note) for slot, message in host.engine._midi_queue],
                [(0, "note_off", 60)],
            )
            self.assertTrue(all(not timer.is_alive() for timer in timers))
        finally:
            for timer in timers:
                timer.cancel()
                timer.join()
            host.engine.shutdown()

    def test_stopping_sequence_does_not_repeat_completed_note_off(self):
        host = SimpleNamespace(engine=AudioEngine())
        seq = Sequencer(host)
        seq._running = True
        seq._schedule_note_offs([(0, 60, 0.0, 0)], deps.mido)
        timers = list(seq._note_off_timers)
        try:
            for timer in timers:
                timer.join(timeout=2.0)
            self.assertTrue(all(not timer.is_alive() for timer in timers))
            seq.stop()
            self.assertEqual(
                [(slot, message.type, message.note)
                 for slot, message in host.engine._midi_queue],
                [(0, "note_off", 60)],
            )
        finally:
            seq.stop()
            for timer in timers:
                timer.cancel()
                timer.join()
            host.engine.shutdown()


if __name__ == "__main__":
    unittest.main()
