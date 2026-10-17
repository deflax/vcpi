"""Daemon completion framing and prompt insertion without native audio or a TTY."""

import io
import json
import shlex
import socket
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import cast

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from core.audio_state import ParameterSnapshot, PluginSnapshot
from core.client import CommandCompleter, _read_response_lines
from core.host import VcpiCore
from core.server import VcpiServer


class _CachedEngine:
    def __init__(self):
        plugin = PluginSnapshot(1, parameters={
            "mode": ParameterSnapshot(kind="enum", valid_values=("Quiet Mode", "Loud Mode")),
        }, values={"mode": "Quiet Mode"})
        self.slots = [SimpleNamespace(plugin=plugin, effects=[]), *[None] * 7]
        self.master_effects = []
        self.devices = [{"index": 3, "name": "Output", "max_output_channels": 2}]
        self.device_read_threads = []

    @property
    def audio_devices(self):
        self.device_read_threads.append(threading.current_thread())
        return self.devices

    def __getattr__(self, name):
        raise AssertionError(f"completion must not access native engine operation {name}")


class CompletionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.engine = _CachedEngine()
        self.host = SimpleNamespace(engine=self.engine, midi_inputs=[])
        self.server = VcpiServer(cast(VcpiCore, cast(object, self.host)))
        self.sock, peer = socket.socketpair()
        self.sock.settimeout(2)
        self.rfile = self.sock.makefile("r", encoding="utf-8")
        self.wfile = self.sock.makefile("w", encoding="utf-8")
        self.handler = threading.Thread(target=self.server._handle_client, args=(peer,), daemon=True)
        self.handler.start()
        self.addCleanup(self._close_connection)
        banner = _read_response_lines(self.rfile)
        self.assertIsNotNone(banner)
        self.completer = CommandCompleter(self.rfile, self.wfile)

    def _close_connection(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.wfile.close()
        self.rfile.close()
        self.sock.close()
        self.handler.join(timeout=2)

    def _dispatch_pending(self):
        # The socket reader queues work; the test's main thread executes it.
        request = self.server._cmd_queue.get(timeout=2)
        self.server._cmd_queue.put(request)
        self.server._drain_commands()

    def _request(self, line):
        self.wfile.write(line + "\n")
        self.wfile.flush()
        self._dispatch_pending()
        return _read_response_lines(self.rfile)

    def _complete(self, document):
        result = []
        errors = []

        def request():
            try:
                result.extend(self.completer.get_completions(document, CompleteEvent(completion_requested=True)))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        self._dispatch_pending()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "completion response did not finish")
        if errors:
            raise errors[0]
        return result

    def test_root_and_arguments_share_framed_stream_with_normal_commands(self):
        root = self._complete(Document(""))
        self.assertIn("seq ", [item.text for item in root])
        self.assertIn("status ", [item.text for item in self._complete(Document("sta"))])
        options = self._complete(Document("debug o"))
        self.assertEqual([item.text for item in options], ["off ", "on "])
        self.assertIn("mode = Quiet Mode", "\n".join(self._request("get 1 mode")))
        self.assertEqual(self._complete(Document("status ")), [])
        self.assertIn("mode = Quiet Mode", "\n".join(self._request("get 1 mode")))

    def test_current_cached_state_is_read_only_on_dispatch_thread(self):
        self.assertEqual([item.text for item in self._complete(Document("audio start "))], ["3 "])
        self.engine.devices = [{"index": 7, "name": "New output", "max_output_channels": 2}]
        self.assertEqual([item.text for item in self._complete(Document("audio start "))], ["7 "])
        self.assertTrue(self.engine.device_read_threads)
        self.assertTrue(all(thread is threading.current_thread()
                            for thread in self.engine.device_read_threads))
        self.assertEqual(self.engine.slots[0].plugin.values, {"mode": "Quiet Mode"})

    def test_completion_inserts_at_cursor_and_preserves_text_after_it(self):
        before = "debug o"
        document = Document(before + "TAIL", cursor_position=len(before))
        selected = next(item for item in self._complete(document) if item.text == "off ")
        buffer = Buffer(document=document)
        buffer.apply_completion(selected)
        self.assertEqual(buffer.text, "debug off TAIL")
        self.assertEqual(buffer.cursor_position, len("debug off "))

    def test_incomplete_quote_is_replaced_by_parseable_enum_choice(self):
        before = 'set 1 mode "Qu'
        document = Document(before + "TAIL", cursor_position=len(before))
        choices = self._complete(document)
        self.assertEqual(len(choices), 1)
        buffer = Buffer(document=document)
        buffer.apply_completion(choices[0])
        self.assertEqual(shlex.split(buffer.text[:buffer.cursor_position]), ["set", "1", "mode", "Quiet Mode"])
        self.assertEqual(buffer.text[buffer.cursor_position:], "TAIL")
        self.assertIn("mode = Quiet Mode", "\n".join(self._request("get 1 mode")))

    def test_internal_requests_cannot_execute_commands_and_errors_keep_alignment(self):
        for payload in ('{"before":"shutdown"}', '"unterminated', "null", "42"):
            with self.subTest(payload=payload):
                lines = self._request("\x01" + payload)
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("Error:"), lines)
                self.assertIn("mode = Quiet Mode", "\n".join(self._request("get 1 mode")))
        for before in ("quit", "shutdown", "shutdown\nquit", "set 1 mode Loud Mode"):
            with self.subTest(before=before):
                lines = self._request("\x01" + json.dumps(before))
                self.assertEqual(len(lines), 1)
                reply = json.loads(lines[0])
                self.assertEqual(set(reply), {"start_position", "matches"})
                self.assertIn("mode = Quiet Mode", "\n".join(self._request("get 1 mode")))
        self.assertEqual(self._request("quit"), ["[Host] Disconnected."])
        self.assertIsNone(_read_response_lines(self.rfile))


class CompletionReplyBoundaryTests(unittest.TestCase):
    def test_invalid_replies_are_silent_fully_consumed_and_never_commands(self):
        invalid_frames = (
            "Error: unsupported request\n",
            '{"matches": [\n',
            '{}\n',
            '[]\n',
            '{"start_position": true, "matches": []}\n',
            '{"start_position": -99, "matches": ["wrong"]}\n',
            '{"start_position": 1, "matches": []}\n',
            '{"start_position": 0, "matches": [7]}\n',
            '{"start_position": 0, "matches": [], "extra": 1}\n',
            '{"start_position": 0, "matches": []}\nunexpected output\n',
        )
        for frame in invalid_frames:
            with self.subTest(frame=frame):
                reader = io.StringIO(frame + "\x00\nnormal command result\n\x00\n")
                writer = io.StringIO()
                completer = CommandCompleter(reader, writer)
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    suggestions = list(completer.get_completions(Document("sta"), CompleteEvent()))
                self.assertEqual(suggestions, [])
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(writer.getvalue(), '\x01"sta"\n')
                self.assertEqual(_read_response_lines(reader), ["normal command result"])

    def test_closed_or_unframed_stream_yields_no_suggestions(self):
        for text in ("", '{"start_position": 0, "matches": []}\n'):
            with self.subTest(text=text):
                completer = CommandCompleter(io.StringIO(text), io.StringIO())
                self.assertEqual(list(completer.get_completions(Document(""), CompleteEvent())), [])
        reader, writer = io.StringIO(), io.StringIO()
        writer.close()
        self.assertEqual(list(CommandCompleter(reader, writer).get_completions(Document(""), CompleteEvent())), [])
        reader, writer = io.StringIO(), io.StringIO()
        reader.close()
        self.assertEqual(list(CommandCompleter(reader, writer).get_completions(Document(""), CompleteEvent())), [])


if __name__ == "__main__":
    unittest.main()
