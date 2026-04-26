"""Unix socket server for vcpi.

Runs VcpiCore headless and accepts CLI connections over a Unix domain socket.
Each connected client gets its own reader thread sharing the same core
instance.  The protocol is line-oriented text:

  client -> server:  one command per line (UTF-8)
  server -> client:  output lines, terminated by a sentinel line

The sentinel is a NUL byte on its own line (``\\x00\\n``).  The client reads
lines until it sees the sentinel, then prints everything before it and
prompts for the next command.

Command execution happens on the **main thread** so that native libraries
(e.g. pedalboard/JUCE VST3 hosting) which require main-thread access work
correctly.  Per-client reader threads enqueue commands and block until the
main thread has executed them and posted the result.
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, NamedTuple

from core.host import VcpiCore
from core.cli import HostCLI
from core.models import NUM_SLOTS, InstrumentSlot
from core.paths import DEFAULT_SOCK_PATH

# Sentinel that marks the end of a command's output.
END_OF_RESPONSE = "\x00"
JSON_REQUEST_PREFIX = "__vcpi_json__ "


logger = logging.getLogger(__name__)


class _CommandRequest(NamedTuple):
    """A command submitted by a reader thread for main-thread execution."""
    line: str
    client_id: str
    result: threading.Event
    # Mutable container so the main thread can store the result.
    # [0] = output (str | None), [1] = shutdown_requested (bool)
    result_box: list[str | bool | None]


class _JsonOperationError(Exception):
    """Structured operation failure sent back to socket clients as JSON."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status: int = status


class VcpiServer:
    """Headless VcpiCore daemon with a Unix socket control interface."""

    def __init__(self, host: VcpiCore, sock_path: Path = DEFAULT_SOCK_PATH):
        self.host = host
        self.sock_path = sock_path
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self._server_sock: socket.socket | None = None
        self._running = False
        # Commands enqueued by reader threads, executed on the main thread.
        self._cmd_queue: queue.Queue[_CommandRequest] = queue.Queue()

    # ------------------------------------------------------------------

    def start(self):
        """Bind the Unix socket and accept connections in a loop.

        The main thread alternates between accepting new connections and
        executing queued commands so that all CLI work (including native
        plugin loading) runs on the main thread.
        """
        # Remove stale socket file
        if self.sock_path.exists():
            self.sock_path.unlink()

        try:
            self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            fallback = Path("/tmp") / f"vcpi-{os.getuid()}" / "vcpi.sock"
            raise PermissionError(
                f"cannot create socket directory '{self.sock_path.parent}'. "
                f"Use --sock with a writable path (for example: {fallback})"
            ) from exc

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(str(self.sock_path))
        self._server_sock.listen(4)
        self._server_sock.settimeout(0.05)

        # Allow non-root users in the same group to connect
        os.chmod(str(self.sock_path), 0o770)

        self._running = True
        logger.info("listening on %s", self.sock_path)

        try:
            while self._running:
                # 1. Accept new connections (non-blocking, short timeout)
                try:
                    conn, _ = self._server_sock.accept()
                    t = threading.Thread(target=self._handle_client,
                                         args=(conn,), daemon=True)
                    t.start()
                except socket.timeout:
                    pass
                except OSError:
                    break

                # 2. Drain command queue — execute on main thread
                self._drain_commands()
        finally:
            self.stop()

    def _drain_commands(self):
        """Execute all pending commands on the main thread."""
        while True:
            try:
                req = self._cmd_queue.get_nowait()
            except queue.Empty:
                return
            try:
                output, shutdown = self._run_command(req.line, req.client_id)
                req.result_box[:] = [output, shutdown]
            except Exception as exc:
                req.result_box[:] = [f"Error: {exc}\n", False]
            finally:
                req.result.set()

    def stop(self):
        """Shut down the server and clean up."""
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass

    def shutdown(self):
        """Stop the server and shut down the host exactly once."""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        self.stop()

        try:
            self.host.shutdown()
        except Exception:
            logger.exception("host shutdown failed")

    # ------------------------------------------------------------------

    def _submit_command(self, line: str, client_id: str) -> tuple[str | None, bool]:
        """Enqueue a command for main-thread execution and block for the result."""
        evt = threading.Event()
        box: list[str | bool | None] = [None, False]
        self._cmd_queue.put(_CommandRequest(line, client_id, evt, box))
        _ = evt.wait()
        output, shutdown = box[0], box[1]
        if output is not None and not isinstance(output, str):
            raise RuntimeError("command output must be a string or None")
        if not isinstance(shutdown, bool):
            raise RuntimeError("shutdown flag must be a boolean")
        return output, shutdown

    def _handle_client(self, conn: socket.socket):
        """Serve one connected CLI client (reader thread — I/O only)."""
        client_id = f"client-{id(conn):x}"
        logger.info("%s connected", client_id)
        try:
            rfile = conn.makefile("r", encoding="utf-8", errors="replace")
            wfile = conn.makefile("w", encoding="utf-8")

            # Send the banner on connect
            banner = (HostCLI.intro or "").lstrip("\n")
            wfile.write(banner + "\n")
            wfile.write(END_OF_RESPONSE + "\n")
            wfile.flush()

            for line in rfile:
                line = line.rstrip("\n")

                if not line:
                    wfile.write(END_OF_RESPONSE + "\n")
                    wfile.flush()
                    continue

                # Submit to main thread and wait for result
                output, shutdown_requested = self._submit_command(line, client_id)

                if output is None:
                    # quit / exit / EOF  -> close this client
                    wfile.write("[Host] Disconnected.\n")
                    wfile.write(END_OF_RESPONSE + "\n")
                    wfile.flush()
                    logger.info("%s requested disconnect", client_id)
                    break

                wfile.write(output)
                if output and not output.endswith("\n"):
                    wfile.write("\n")
                wfile.write(END_OF_RESPONSE + "\n")
                wfile.flush()

                if shutdown_requested:
                    logger.info("%s requested daemon shutdown", client_id)
                    self.shutdown()
                    break

        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("%s connection error: %s", client_id, e)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            logger.info("%s disconnected", client_id)

    def _run_command(self, line: str, client_id: str) -> tuple[str | None, bool]:
        """Execute one CLI command and capture its printed output.

        Returns the captured output string, or ``None`` if the command
        requests a disconnect (quit/exit/EOF). The boolean indicates
        whether daemon shutdown was requested.

        Must be called on the main thread.
        """
        if line.startswith(JSON_REQUEST_PREFIX):
            output = self._run_json_request(line[len(JSON_REQUEST_PREFIX):], client_id)
            return output, False

        buf = io.StringIO()

        # Build a throw-away HostCLI that prints into our buffer.
        cli = HostCLI(self.host, stdout=buf, owns_host=False)
        cli.use_rawinput = False

        # cmd.Cmd.onecmd() returns True when the command wants to exit.
        stop = cli.onecmd(line)

        output = buf.getvalue()
        shutdown_requested = bool(getattr(cli, "_shutdown_requested", False))

        if shutdown_requested:
            logger.info("cli %s -> shutdown", client_id)
            return output, True

        # quit/exit/EOF tell cmd.Cmd to stop -- but for the *server* we
        # only disconnect this client, we never shut down the host.
        if stop:
            logger.info("cli %s -> disconnect", client_id)
            return None, False

        return output, False

    # ------------------------------------------------------------------

    @staticmethod
    def _json_response(payload: dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":")) + "\n"

    def _run_json_request(self, request_line: str, client_id: str) -> str:
        """Execute one typed JSON operation and return one JSON response line.

        This method is called from ``_run_command()``, which itself only runs
        inside ``_drain_commands()`` on the daemon main thread.
        """
        try:
            request = json.loads(request_line)
            if not isinstance(request, dict):
                raise _JsonOperationError("JSON request must be an object")

            operation = request.get("op")
            if not isinstance(operation, str) or not operation.strip():
                raise _JsonOperationError("op must be a non-empty string")

            payload = request.get("payload", {})
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise _JsonOperationError("payload must be an object")

            response = self._handle_json_operation(operation.strip(), payload)
            logger.info("json %s -> %s", client_id, operation.strip())
            return self._json_response(response)
        except json.JSONDecodeError as exc:
            return self._json_response({"ok": False, "status": 400, "error": str(exc)})
        except _JsonOperationError as exc:
            return self._json_response({"ok": False, "status": exc.status, "error": str(exc)})
        except Exception as exc:
            logger.exception("json %s failed", client_id)
            return self._json_response({"ok": False, "status": 500, "error": str(exc)})

    def _handle_json_operation(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        match operation:
            case "status":
                return {"ok": True, "status": self._status_payload()}
            case "slots":
                return {"ok": True, "slots": self._slots_payload()}
            case "audio.start":
                device = self._optional_audio_device(payload)
                self.host.start_audio(device)
                return {"ok": True, "status": self._status_payload()}
            case "audio.stop":
                self.host.stop_audio()
                return {"ok": True, "status": self._status_payload()}
            case "slot.gain":
                idx = self._slot_index_from_payload(payload)
                gain = self._gain_from_payload(payload)
                slot = self._loaded_slot(idx)
                slot.gain = gain
                return {"ok": True, "slot": self._slot_payload(idx, slot)}
            case "slot.mute":
                idx = self._slot_index_from_payload(payload)
                slot = self._loaded_slot(idx)
                slot.muted = self._slot_bool_from_payload(payload, "muted", slot.muted)
                self.host.refresh_mixer_leds([idx])
                return {"ok": True, "slot": self._slot_payload(idx, slot)}
            case "slot.solo":
                idx = self._slot_index_from_payload(payload)
                slot = self._loaded_slot(idx)
                slot.solo = self._slot_bool_from_payload(payload, "solo", slot.solo)
                self.host.refresh_mixer_leds([idx])
                return {"ok": True, "slot": self._slot_payload(idx, slot)}
            case _:
                raise _JsonOperationError(f"unknown operation: {operation}")

    @staticmethod
    def _slot_index_from_payload(payload: dict[str, Any]) -> int:
        value = payload.get("slot")
        if isinstance(value, bool) or not isinstance(value, int):
            raise _JsonOperationError(f"slot must be an integer 1-{NUM_SLOTS}")
        if not 1 <= value <= NUM_SLOTS:
            raise _JsonOperationError(f"slot must be 1-{NUM_SLOTS}")
        return value - 1

    @staticmethod
    def _gain_from_payload(payload: dict[str, Any]) -> float:
        value = payload.get("gain")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _JsonOperationError("gain must be a number between 0.0 and 1.0")
        gain = float(value)
        if not 0.0 <= gain <= 1.0:
            raise _JsonOperationError("gain must be between 0.0 and 1.0")
        return gain

    @staticmethod
    def _bool_from_payload(payload: dict[str, Any], key: str, default: bool) -> bool:
        if key not in payload:
            return default
        value = payload[key]
        if not isinstance(value, bool):
            raise _JsonOperationError(f"{key} must be a boolean")
        return value

    @classmethod
    def _slot_bool_from_payload(cls, payload: dict[str, Any], key: str, current: bool) -> bool:
        if key in payload:
            return cls._bool_from_payload(payload, key, current)
        if "toggle" in payload:
            toggle = payload["toggle"]
            if not isinstance(toggle, bool):
                raise _JsonOperationError("toggle must be a boolean")
            return not current if toggle else current
        return not current

    @staticmethod
    def _optional_audio_device(payload: dict[str, Any]) -> str | int | None:
        if "device" not in payload or payload["device"] is None:
            return None
        value = payload["device"]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise _JsonOperationError("device must be a string, integer, or null")
        return value

    def _loaded_slot(self, idx: int) -> InstrumentSlot:
        slot = self.host.engine.slots[idx]
        if slot is None:
            raise _JsonOperationError(f"slot {idx + 1} is empty")
        return slot

    def _status_payload(self) -> dict[str, Any]:
        routing = {str(ch + 1): slot_idx + 1 for ch, slot_idx in sorted(self.host.channel_map.items())}
        return {
            "sample_rate": self.host.sample_rate,
            "buffer_size": self.host.buffer_size,
            "session": {
                "path": str(self.host.session_path),
                "loaded_name": self.host.loaded_session_name,
                "loaded_path": str(self.host.loaded_session_path) if self.host.loaded_session_path else None,
            },
            "audio": {
                "running": self.host.engine.running,
                "output": self.host.audio_output_name,
                "master_gain": self.host.engine.master_gain,
            },
            "midi": {
                "inputs": self.host.midi_input_names,
                "mixer_input": self.host.mixer_midi_name,
                "mixer_output": self.host.mixer_midi_out_name,
                "routing": routing,
            },
            "link": {
                "enabled": self.host.link.enabled,
                "bpm": self.host.link.bpm,
            },
            "slots_loaded": sum(1 for slot in self.host.engine.slots if slot is not None),
        }

    def _slots_payload(self) -> list[dict[str, Any]]:
        return [self._slot_payload(idx, slot) for idx, slot in enumerate(self.host.engine.slots)]

    def _midi_channels_for_slot(self, idx: int, slot: InstrumentSlot | None) -> list[int]:
        channels = {ch for ch, slot_idx in self.host.channel_map.items() if slot_idx == idx}
        if slot is not None:
            channels.update(slot.midi_channels)
        return sorted(ch + 1 for ch in channels)

    def _slot_payload(self, idx: int, slot: InstrumentSlot | None) -> dict[str, Any]:
        midi_channels = self._midi_channels_for_slot(idx, slot)
        if slot is None:
            return {
                "slot": idx + 1,
                "loaded": False,
                "name": None,
                "path": None,
                "display_label": None,
                "source_type": None,
                "vcv_patch_path": None,
                "gain": None,
                "muted": False,
                "solo": False,
                "enabled": False,
                "midi_channels": midi_channels,
                "effects": 0,
            }
        return {
            "slot": idx + 1,
            "loaded": True,
            "name": slot.name,
            "path": slot.path,
            "display_label": slot.display_label,
            "source_type": slot.source_type,
            "vcv_patch_path": slot.vcv_patch_path or None,
            "gain": slot.gain,
            "muted": slot.muted,
            "solo": slot.solo,
            "enabled": slot.enabled,
            "midi_channels": midi_channels,
            "effects": len(slot.effects),
        }


def run_server(host: VcpiCore, sock_path: str | None = None):
    """Convenience entry point used by ``main.py``."""
    path = Path(sock_path) if sock_path else DEFAULT_SOCK_PATH
    server = VcpiServer(host, path)

    def _shutdown(signum, frame):
        del signum, frame
        logger.info("shutting down")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    server.start()
