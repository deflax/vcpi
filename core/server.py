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
import math
import os
import queue
import re
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from core import deps
from core.host import VcpiCore
from core.cli import HostCLI
from core.models import NUM_SLOTS, InstrumentSlot
from core.paths import DEFAULT_SOCK_PATH
from graph.plugin_info import render_plugin_info

# Sentinel that marks the end of a command's output.
END_OF_RESPONSE = "\x00"
JSON_REQUEST_PREFIX = "__vcpi_json__ "
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_BPM = 20.0
MAX_BPM = 300.0
DEFAULT_NOTE_VELOCITY = 100
DEFAULT_NOTE_DURATION_MS = 300
MIN_NOTE_DURATION_MS = 1
MAX_NOTE_DURATION_MS = 5000
MAX_SLOT_PARAMETERS = 512
MAX_PARAMETER_STRING_LENGTH = 256


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
            case "sessions":
                return self._sessions_payload()
            case "audio.devices":
                return self._audio_devices_payload()
            case "flow":
                return {"ok": True, "flow": self._flow_payload()}
            case "slot.info":
                idx = self._slot_index_from_payload(payload)
                return self._slot_info_payload(idx)
            case "slot.params":
                idx = self._slot_index_from_payload(payload)
                return self._slot_params_payload(idx)
            case "slot.param.set":
                idx = self._slot_index_from_payload(payload)
                slot = self._loaded_slot(idx)
                target = self._parameter_target_from_payload(payload, slot)
                name = self._parameter_name_from_payload(payload)
                value = self._parameter_value_from_payload(payload)
                return self._slot_param_set_payload(idx, slot, target, name, value)
            case "audio.start":
                device = self._optional_audio_device(payload)
                self.host.start_audio(device)
                return {"ok": True, "status": self._status_payload()}
            case "audio.stop":
                self.host.stop_audio()
                return {"ok": True, "status": self._status_payload()}
            case "tempo.set":
                bpm = self._bpm_from_payload(payload)
                self.host.link.bpm = bpm
                return {"ok": True, "status": self._status_payload()}
            case "link.start":
                bpm = self._optional_bpm_from_payload(payload)
                self.host.start_link(bpm)
                return {"ok": True, "status": self._status_payload()}
            case "link.stop":
                self.host.stop_link()
                return {"ok": True, "status": self._status_payload()}
            case "midi.link":
                channel = self._midi_channel_from_payload(payload)
                idx = self._slot_index_from_payload(payload)
                self.host.route(channel - 1, idx)
                return {
                    "ok": True,
                    "route": {"channel": channel, "slot": idx + 1},
                    "status": self._status_payload(),
                    "slots": self._slots_payload(),
                }
            case "midi.cut":
                channel = self._midi_channel_from_payload(payload)
                self.host.unroute(channel - 1)
                return {
                    "ok": True,
                    "route": {"channel": channel, "slot": None},
                    "status": self._status_payload(),
                    "slots": self._slots_payload(),
                }
            case "slot.gain":
                idx = self._slot_index_from_payload(payload)
                gain = self._gain_from_payload(payload)
                slot = self._loaded_slot(idx)
                slot.gain = gain
                return {"ok": True, "slot": self._slot_payload(idx, slot)}
            case "slot.note":
                idx = self._slot_index_from_payload(payload)
                slot = self._loaded_slot(idx)
                note = self._midi_note_from_payload(payload)
                velocity = self._midi_velocity_from_payload(payload)
                duration_ms = self._note_duration_ms_from_payload(payload)
                self.host.send_note(idx, note, velocity, duration_ms / 1000.0)
                return {
                    "ok": True,
                    "slot": self._slot_payload(idx, slot),
                    "note": {
                        "note": note,
                        "velocity": velocity,
                        "duration_ms": duration_ms,
                    },
                }
            case "master.gain":
                gain = self._gain_from_payload(payload)
                self.host.engine.master_gain = gain
                return {"ok": True, "status": self._status_payload()}
            case "session.save":
                return self._save_session_from_payload(payload)
            case "session.load":
                return self._load_session_from_payload(payload)
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
            case "slot.clear" | "slot.unload":
                idx = self._slot_index_from_payload(payload)
                _ = self._loaded_slot(idx)
                _ = self.host.remove_instrument(idx)
                refresh = getattr(self.host, "refresh_mixer_leds", None)
                if callable(refresh):
                    _ = refresh([idx])
                status = self._status_payload()
                return {
                    "ok": True,
                    "slot": self._slot_payload(idx, self.host.engine.slots[idx]),
                    "status": status,
                    "slots": self._slots_payload(),
                }
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
    def _midi_channel_from_payload(payload: dict[str, Any]) -> int:
        value = payload.get("channel")
        if isinstance(value, bool) or not isinstance(value, int):
            raise _JsonOperationError("channel must be an integer 1-16")
        if not 1 <= value <= 16:
            raise _JsonOperationError("channel must be 1-16")
        return value

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
    def _int_range_from_payload(
        payload: dict[str, Any],
        key: str,
        minimum: int,
        maximum: int,
        *,
        default: int | None = None,
    ) -> int:
        if key not in payload:
            if default is None:
                raise _JsonOperationError(f"{key} must be an integer {minimum}-{maximum}")
            return default
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise _JsonOperationError(f"{key} must be an integer {minimum}-{maximum}")
        if not minimum <= value <= maximum:
            raise _JsonOperationError(f"{key} must be {minimum}-{maximum}")
        return value

    @classmethod
    def _midi_note_from_payload(cls, payload: dict[str, Any]) -> int:
        return cls._int_range_from_payload(payload, "note", 0, 127)

    @classmethod
    def _midi_velocity_from_payload(cls, payload: dict[str, Any]) -> int:
        return cls._int_range_from_payload(
            payload,
            "velocity",
            0,
            127,
            default=DEFAULT_NOTE_VELOCITY,
        )

    @classmethod
    def _note_duration_ms_from_payload(cls, payload: dict[str, Any]) -> int:
        return cls._int_range_from_payload(
            payload,
            "duration_ms",
            MIN_NOTE_DURATION_MS,
            MAX_NOTE_DURATION_MS,
            default=DEFAULT_NOTE_DURATION_MS,
        )

    @staticmethod
    def _bpm_from_payload(payload: dict[str, Any]) -> float:
        value = payload.get("bpm")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _JsonOperationError(f"bpm must be a number between {MIN_BPM:.1f} and {MAX_BPM:.1f}")
        bpm = float(value)
        if not MIN_BPM <= bpm <= MAX_BPM:
            raise _JsonOperationError(f"bpm must be between {MIN_BPM:.1f} and {MAX_BPM:.1f}")
        return bpm

    @classmethod
    def _optional_bpm_from_payload(cls, payload: dict[str, Any]) -> float | None:
        if "bpm" not in payload or payload["bpm"] is None:
            return None
        return cls._bpm_from_payload(payload)

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

    @staticmethod
    def _parameter_name_from_payload(payload: dict[str, Any]) -> str:
        value = payload.get("name")
        if not isinstance(value, str):
            raise _JsonOperationError("name must be a string")
        name = value.strip()
        if not name:
            raise _JsonOperationError("name must not be empty")
        return name

    @staticmethod
    def _parameter_value_from_payload(payload: dict[str, Any]) -> float:
        if "value" not in payload:
            raise _JsonOperationError("value must be a finite number")
        value = payload["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _JsonOperationError("value must be a finite number")
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            raise _JsonOperationError("value must be a finite number")
        return numeric_value

    @staticmethod
    def _parameter_target_from_payload(payload: dict[str, Any], slot: InstrumentSlot) -> tuple[str, int | None]:
        target = payload.get("target", "instrument")
        if target == "instrument":
            return "instrument", None
        if target != "effect":
            raise _JsonOperationError("target must be 'instrument' or 'effect'")

        effect_index = payload.get("effect")
        if isinstance(effect_index, bool) or not isinstance(effect_index, int):
            raise _JsonOperationError("effect must be an integer 1-N")

        effects = getattr(slot, "effects", [])
        try:
            effect_count = len(effects)
        except Exception:
            effect_count = 0
        if not 1 <= effect_index <= effect_count:
            raise _JsonOperationError("effect index is out of range")
        return "effect", effect_index - 1

    @staticmethod
    def _normalize_session_name(raw_name: object, *, required: bool) -> str | None:
        if raw_name is None:
            if required:
                raise _JsonOperationError("name is required")
            return None
        if not isinstance(raw_name, str):
            raise _JsonOperationError("name must be a string")

        name = raw_name.strip()
        if name.lower().endswith(".json"):
            name = name[:-5]
        if not name:
            raise _JsonOperationError("name must not be empty")
        if ".." in name:
            raise _JsonOperationError("name must not contain '..'")
        if any(separator in name for separator in ("/", "\\")):
            raise _JsonOperationError("name must not contain path separators")
        if Path(name).is_absolute():
            raise _JsonOperationError("name must not be an absolute path")
        if not SESSION_NAME_RE.fullmatch(name):
            raise _JsonOperationError(
                "name must start with a letter or number and contain only letters, numbers, dots, underscores, or hyphens"
            )
        return name

    def _session_cli(self) -> HostCLI:
        return HostCLI(self.host, stdout=io.StringIO(), owns_host=False)

    def _sessions_root(self) -> Path:
        root = self._session_cli()._sessions_root().expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _require_path_within_root(path: Path, root: Path) -> Path:
        resolved = path.expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise _JsonOperationError("session path must stay inside the session directory") from exc
        return resolved

    def _session_path_for_name(self, name: str) -> Path:
        root = self._sessions_root()
        return self._require_path_within_root(root / f"{name}.json", root)

    def _loaded_session_path(self) -> Path:
        raw_path = self.host.loaded_session_path
        if raw_path is None:
            raise _JsonOperationError("name is required when no session is loaded")
        root = self._sessions_root()
        return self._require_path_within_root(Path(raw_path), root)

    def _session_payload(self) -> dict[str, Any]:
        return {
            "path": str(self.host.session_path),
            "loaded_name": self.host.loaded_session_name,
            "loaded_path": str(self.host.loaded_session_path) if self.host.loaded_session_path else None,
        }

    def _sessions_payload(self) -> dict[str, object]:
        root = self._sessions_root()
        try:
            loaded_name = self._normalize_session_name(self.host.loaded_session_name, required=False)
        except _JsonOperationError:
            loaded_name = None
        loaded_path = self.host.loaded_session_path
        loaded_stem: str | None = None
        if loaded_path is not None:
            try:
                loaded_stem = self._require_path_within_root(Path(loaded_path), root).stem
            except _JsonOperationError:
                loaded_stem = None

        sessions: list[dict[str, object]] = []
        for path in root.glob("*.json"):
            filename = path.name
            if filename.startswith(".") or not path.is_file():
                continue
            try:
                name = self._normalize_session_name(filename, required=True)
            except _JsonOperationError:
                continue
            if name is None or f"{name}.json" != filename:
                continue
            loaded = name == loaded_name or name == loaded_stem
            sessions.append({"name": name, "filename": filename, "loaded": loaded})

        sessions.sort(key=lambda item: str(item["name"]))
        return {"ok": True, "sessions": sessions, "session": self._session_payload()}

    def _save_session_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "path" in payload:
            raise _JsonOperationError("path is not accepted for session operations")
        name = self._normalize_session_name(payload.get("name"), required=False)

        if name is None:
            path = self._loaded_session_path()
            if self.host.loaded_session_name is None:
                self.host.loaded_session_name = path.stem
            self.host.loaded_session_path = path
        else:
            path = self._session_path_for_name(name)
            self.host.loaded_session_name = name
            self.host.loaded_session_path = path

        self.host.save_session(str(path))
        status = self._status_payload()
        return {"ok": True, "session": status["session"], "status": status}

    def _load_session_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "path" in payload:
            raise _JsonOperationError("path is not accepted for session operations")
        name = self._normalize_session_name(payload.get("name"), required=True)
        if name is None:
            raise _JsonOperationError("name is required")

        path = self._session_path_for_name(name)
        if not path.exists() or not path.is_file():
            raise _JsonOperationError(f"session not found: {name}", status=404)

        self.host.restore_session(str(path))
        refresh = getattr(self.host, "refresh_mixer_leds", None)
        if callable(refresh):
            refresh()
        self.host.loaded_session_name = name
        self.host.loaded_session_path = path
        self.host.save_session()
        status = self._status_payload()
        return {
            "ok": True,
            "session": status["session"],
            "status": status,
            "slots": self._slots_payload(),
        }

    def _loaded_slot(self, idx: int) -> InstrumentSlot:
        slot = self.host.engine.slots[idx]
        if slot is None:
            raise _JsonOperationError(f"slot {idx + 1} is empty")
        return slot

    def _flow_payload(self) -> str:
        buf = io.StringIO()
        cli = HostCLI(self.host, stdout=buf, owns_host=False)
        cli.use_rawinput = False
        _ = cli.onecmd("flow")
        return buf.getvalue().rstrip("\n")

    def _slot_info_payload(self, idx: int) -> dict[str, Any]:
        slot = self.host.engine.slots[idx]
        slot_payload = self._slot_payload(idx, slot)
        if slot is None:
            return {
                "ok": True,
                "slot": slot_payload,
                "instrument": None,
                "effects": [],
                "rendered": "",
                "message": f"Slot {idx + 1} is empty",
            }

        instrument_label = f"Slot {idx + 1}: {slot.name}"
        instrument = self._plugin_info_payload(
            slot.plugin,
            instrument_label,
            kind="instrument",
            index=None,
        )
        effects = [
            self._plugin_info_payload(
                effect,
                f"Slot {idx + 1} FX {effect_idx + 1}",
                kind="effect",
                index=effect_idx + 1,
            )
            for effect_idx, effect in enumerate(slot.effects)
        ]
        return {
            "ok": True,
            "slot": slot_payload,
            "instrument": instrument,
            "effects": effects,
            "rendered": instrument["rendered"],
        }

    def _slot_params_payload(self, idx: int) -> dict[str, Any]:
        slot = self._loaded_slot(idx)
        label = f"Slot {idx + 1}: {slot.name}"
        instrument = self._plugin_params_group_payload(
            slot.plugin,
            label,
            kind="instrument",
            index=None,
        )
        effects = [
            self._plugin_params_group_payload(
                effect,
                f"Slot {idx + 1} FX {effect_idx + 1}",
                kind="effect",
                index=effect_idx + 1,
            )
            for effect_idx, effect in enumerate(slot.effects)
        ]
        payload: dict[str, Any] = {
            "ok": True,
            "slot": self._slot_payload(idx, slot),
            "parameters": instrument["parameters"],
            "count": instrument["count"],
            "rendered": instrument["rendered"],
            "instrument": instrument,
            "effects": effects,
        }
        if instrument.get("truncated"):
            payload["truncated"] = True
            payload["limit"] = MAX_SLOT_PARAMETERS
        return payload

    def _slot_param_set_payload(
        self,
        idx: int,
        slot: InstrumentSlot,
        target: tuple[str, int | None],
        name: str,
        value: float,
    ) -> dict[str, Any]:
        target_kind, effect_idx = target
        if target_kind == "effect":
            if effect_idx is None:
                raise _JsonOperationError("effect must be an integer 1-N")
            return self._slot_effect_param_set_payload(idx, slot, effect_idx, name, value)

        current = self._slot_params_payload(idx)
        parameter = self._find_parameter_payload(current["parameters"], name)
        if parameter is None:
            raise _JsonOperationError(f"unknown parameter: {name}")
        self._validate_numeric_parameter(parameter, name, value)

        enqueue_param_change = getattr(self.host.engine, "enqueue_param_change", None)
        if callable(enqueue_param_change):
            enqueue_param_change(idx, name, value)
        setattr(slot.plugin, name, value)

        refreshed = self._slot_params_payload(idx)
        refreshed_parameter = self._find_parameter_payload(refreshed["parameters"], name)
        if refreshed_parameter is None:
            refreshed_parameter = dict(parameter)
            refreshed_parameter["value"] = value
        refreshed["parameter"] = refreshed_parameter
        return refreshed

    def _slot_effect_param_set_payload(
        self,
        idx: int,
        slot: InstrumentSlot,
        effect_idx: int,
        name: str,
        value: float,
    ) -> dict[str, Any]:
        effect = slot.effects[effect_idx]
        current = self._slot_params_payload(idx)
        current_effects = current.get("effects", [])
        if not isinstance(current_effects, list) or effect_idx >= len(current_effects):
            raise _JsonOperationError("effect index is out of range")
        effect_payload = current_effects[effect_idx]
        if not isinstance(effect_payload, dict):
            raise _JsonOperationError("effect index is out of range")
        parameter = self._find_parameter_payload(effect_payload.get("parameters"), name)
        if parameter is None:
            raise _JsonOperationError(f"unknown parameter: {name}")
        self._validate_numeric_parameter(parameter, name, value)

        setattr(effect, name, value)

        refreshed = self._slot_params_payload(idx)
        refreshed_effect = refreshed["effects"][effect_idx]
        refreshed_parameter = self._find_parameter_payload(refreshed_effect.get("parameters"), name)
        if refreshed_parameter is None:
            refreshed_parameter = dict(parameter)
            refreshed_parameter["value"] = value
        refreshed["effect"] = refreshed_effect
        refreshed["parameter"] = refreshed_parameter
        return refreshed

    @staticmethod
    def _find_parameter_payload(parameters: object, name: str) -> dict[str, Any] | None:
        if not isinstance(parameters, list):
            return None
        for parameter in parameters:
            if isinstance(parameter, dict) and parameter.get("name") == name:
                return parameter
        return None

    @classmethod
    def _validate_numeric_parameter(cls, parameter: dict[str, Any], name: str, value: float) -> None:
        numeric_metadata_found = False

        if "value" in parameter:
            current_value = parameter["value"]
            if not cls._is_finite_number(current_value):
                raise _JsonOperationError(f"parameter is not numeric: {name}")
            numeric_metadata_found = True

        if "default" in parameter:
            default_value = parameter["default"]
            if not cls._is_finite_number(default_value):
                raise _JsonOperationError(f"parameter is not numeric: {name}")
            numeric_metadata_found = True

        minimum = cls._optional_numeric_bound(parameter, "minimum", name)
        maximum = cls._optional_numeric_bound(parameter, "maximum", name)
        if minimum is not None:
            numeric_metadata_found = True
            if value < minimum:
                raise _JsonOperationError(f"value must be >= {minimum:g} for parameter: {name}")
        if maximum is not None:
            numeric_metadata_found = True
            if value > maximum:
                raise _JsonOperationError(f"value must be <= {maximum:g} for parameter: {name}")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _JsonOperationError(f"parameter has invalid numeric range: {name}")

        if not numeric_metadata_found:
            raise _JsonOperationError(f"parameter is not numeric: {name}")

    @classmethod
    def _optional_numeric_bound(cls, parameter: dict[str, Any], key: str, name: str) -> float | None:
        if key not in parameter:
            return None
        value = parameter[key]
        if not cls._is_finite_number(value):
            raise _JsonOperationError(f"parameter has non-numeric range: {name}")
        return float(value)

    @staticmethod
    def _is_finite_number(value: object) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))

    def _plugin_info_payload(
        self,
        plugin: object,
        label: str,
        *,
        kind: str,
        index: int | None,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "index": index,
            "label": label,
            "name": self._safe_plugin_attr(plugin, "name", type(plugin).__name__),
            "descriptive_name": self._safe_plugin_attr(plugin, "descriptive_name"),
            "vendor": self._safe_plugin_attr(plugin, "manufacturer_name"),
            "category": self._safe_plugin_attr(plugin, "category"),
            "version": self._safe_plugin_attr(plugin, "version"),
            "identifier": self._safe_plugin_attr(plugin, "identifier"),
            "type": self._safe_plugin_attr(plugin, "info_type", "Unknown"),
            "latency_samples": self._safe_plugin_attr(plugin, "reported_latency_samples", 0),
            "parameters": {"count": self._safe_parameter_count(plugin)},
            "rendered": self._render_plugin_info(plugin, label),
        }

    def _plugin_parameters_payload(self, plugin: object) -> tuple[list[dict[str, Any]], int]:
        try:
            raw_parameters = getattr(plugin, "parameters", None)
        except Exception:
            return [], 0
        if not raw_parameters:
            return [], 0

        parameters: list[dict[str, Any]] = []
        total_count = 0
        declared_count = self._safe_collection_len(raw_parameters)
        for index, name, param_obj in self._iter_parameter_objects(raw_parameters):
            if len(parameters) >= MAX_SLOT_PARAMETERS:
                total_count = declared_count if declared_count is not None else index + 1
                break
            total_count = index + 1
            parameters.append(self._parameter_payload(plugin, name, index, param_obj))
        else:
            if declared_count is not None:
                total_count = max(total_count, declared_count)
        return parameters, total_count

    def _plugin_params_group_payload(
        self,
        plugin: object,
        label: str,
        *,
        kind: str,
        index: int | None,
    ) -> dict[str, Any]:
        parameters, total_count = self._plugin_parameters_payload(plugin)
        truncated = total_count > len(parameters)
        payload: dict[str, Any] = {
            "kind": kind,
            "index": index,
            "label": label,
            "name": self._safe_plugin_attr(plugin, "name", type(plugin).__name__),
            "parameters": parameters,
            "count": total_count,
            "rendered": self._render_parameters(label, parameters, truncated, total_count),
        }
        if truncated:
            payload["truncated"] = True
            payload["limit"] = MAX_SLOT_PARAMETERS
        return payload

    @staticmethod
    def _safe_collection_len(value: Any) -> int | None:
        try:
            length = len(value)
        except Exception:
            return None
        return length if length >= 0 else None

    def _iter_parameter_objects(self, raw_parameters: Any) -> Iterator[tuple[int, str, object]]:
        try:
            items_attr = getattr(raw_parameters, "items", None)
        except Exception:
            items_attr = None
        if callable(items_attr):
            try:
                iterable: Any = items_attr()
            except Exception:
                return
            for index, item in enumerate(iterable):
                try:
                    key, param_obj = item
                except Exception:
                    key, param_obj = index, item
                yield index, self._parameter_name(key, param_obj, index), param_obj
            return

        try:
            iterator = iter(raw_parameters)
        except (TypeError, ValueError):
            return
        except Exception:
            return

        for index, item in enumerate(iterator):
            yield index, self._parameter_name(None, item, index), item

    def _parameter_payload(
        self,
        plugin: object,
        name: str,
        index: int,
        param_obj: object,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "index": index}
        self._copy_optional_parameter_field(payload, param_obj, "id", ("id", "identifier"))
        self._copy_current_parameter_value(payload, plugin, name, param_obj)
        self._copy_optional_parameter_field(
            payload,
            param_obj,
            "default",
            ("default", "default_value", "default_raw_value"),
        )
        self._copy_parameter_range(payload, param_obj)
        self._copy_optional_parameter_field(payload, param_obj, "unit", ("unit", "units"))
        self._copy_optional_parameter_field(payload, param_obj, "label", ("label",))
        return payload

    @classmethod
    def _parameter_name(cls, key: object, param_obj: object, index: int) -> str:
        for candidate in (key, cls._parameter_object_value(param_obj, ("name", "id", "identifier"))):
            value = cls._safe_scalar(candidate)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(value)
        return f"param_{index + 1}"

    @classmethod
    def _copy_current_parameter_value(
        cls,
        payload: dict[str, Any],
        plugin: object,
        name: str,
        param_obj: object,
    ) -> None:
        value: Any = None
        value_found = False
        if name:
            try:
                value = getattr(plugin, name)
                value_found = True
            except Exception:
                value_found = False
        if not value_found:
            value = cls._parameter_object_value(param_obj, ("value", "current_value", "raw_value"))
        scalar = cls._safe_scalar(value)
        if scalar is not None:
            payload["value"] = scalar

    @classmethod
    def _copy_optional_parameter_field(
        cls,
        payload: dict[str, Any],
        param_obj: object,
        output_key: str,
        source_keys: tuple[str, ...],
    ) -> None:
        scalar = cls._safe_scalar(cls._parameter_object_value(param_obj, source_keys))
        if scalar is not None:
            payload[output_key] = scalar

    @classmethod
    def _copy_parameter_range(cls, payload: dict[str, Any], param_obj: object) -> None:
        range_value = cls._parameter_object_value(param_obj, ("range",))
        minimum: Any = None
        maximum: Any = None
        if isinstance(range_value, (list, tuple)) and len(range_value) >= 2:
            minimum, maximum = range_value[0], range_value[1]
        else:
            minimum = cls._parameter_object_value(param_obj, ("min", "minimum", "min_value"))
            maximum = cls._parameter_object_value(param_obj, ("max", "maximum", "max_value"))

        min_scalar = cls._safe_scalar(minimum)
        max_scalar = cls._safe_scalar(maximum)
        if min_scalar is not None:
            payload["minimum"] = min_scalar
        if max_scalar is not None:
            payload["maximum"] = max_scalar

    @staticmethod
    def _parameter_object_value(param_obj: object, keys: tuple[str, ...]) -> Any:
        for key in keys:
            if isinstance(param_obj, dict) and key in param_obj:
                value = param_obj[key]
                if value is not None:
                    return value
                continue
            try:
                value = getattr(param_obj, key)
            except Exception:
                continue
            if value is not None:
                return value
        return None

    @staticmethod
    def _safe_scalar(value: object) -> str | int | float | bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            if len(value) > MAX_PARAMETER_STRING_LENGTH:
                return value[:MAX_PARAMETER_STRING_LENGTH] + "..."
            return value
        return None

    @staticmethod
    def _render_parameters(
        label: str,
        parameters: list[dict[str, Any]],
        truncated: bool,
        total_count: int,
    ) -> str:
        lines = [label or "Parameters"]
        if not parameters:
            lines.append("  (no parameters)")
        for parameter in parameters:
            name = str(parameter.get("name", "?"))
            value = parameter.get("value", "?")
            unit = parameter.get("unit", "")
            line = f"  {name} = {value}"
            if unit:
                line += f" {unit}"
            if "minimum" in parameter or "maximum" in parameter:
                line += f"  ({parameter.get('minimum', '?')} .. {parameter.get('maximum', '?')})"
            lines.append(line)
        if truncated:
            lines.append(f"  ... capped at {MAX_SLOT_PARAMETERS} of {total_count} parameters")
        return "\n".join(lines)

    @staticmethod
    def _safe_plugin_attr(plugin: object, attr: str, default: Any = None) -> Any:
        try:
            value = getattr(plugin, attr, None)
        except Exception:
            return default
        if value is None:
            return default
        if isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _safe_parameter_count(plugin: object) -> int | None:
        try:
            parameters = getattr(plugin, "parameters", None)
        except Exception:
            return None
        if not parameters:
            return 0
        try:
            return len(parameters)
        except Exception:
            return None

    @staticmethod
    def _render_plugin_info(plugin: object, label: str) -> str:
        try:
            return render_plugin_info(plugin, label)
        except Exception:
            logger.debug("plugin info render failed", exc_info=True)
            return ""

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

    def _audio_devices_payload(self) -> dict[str, Any]:
        current = self.host.audio_output_name
        unavailable_payload = {
            "ok": True,
            "available": False,
            "current": current,
            "default_device": None,
            "devices": [],
        }
        if not deps.HAS_SOUNDDEVICE or deps.sd is None:
            return unavailable_payload

        try:
            raw_devices = deps.sd.query_devices()
        except Exception:
            logger.debug("audio device query failed", exc_info=True)
            return unavailable_payload

        default_device = self._output_device_index(getattr(deps.sd.default, "device", None))
        devices: list[dict[str, Any]] = []
        for idx, info in enumerate(raw_devices):
            if not isinstance(info, dict):
                continue
            try:
                output_channels = int(info.get("max_output_channels", 0))
            except (TypeError, ValueError):
                output_channels = 0
            if output_channels <= 0:
                continue

            name = str(info.get("name", "")).strip()
            if not name:
                continue

            devices.append(
                {
                    "id": idx,
                    "name": name,
                    "output_channels": output_channels,
                    "default": idx == default_device,
                    "selected": current == name,
                }
            )

        return {
            "ok": True,
            "available": True,
            "current": current,
            "default_device": default_device,
            "devices": devices,
        }

    @staticmethod
    def _output_device_index(device: object) -> int | None:
        if isinstance(device, int):
            return device
        if isinstance(device, (tuple, list)):
            if len(device) >= 2 and isinstance(device[1], int):
                return device[1]
            if len(device) == 1 and isinstance(device[0], int):
                return device[0]
        return None

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
