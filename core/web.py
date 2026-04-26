"""Small stdlib HTTP bridge for a running vcpi Unix socket daemon.

The web process is intentionally a client: it never runs ``HostCLI`` or the
audio/core engine directly.  API requests connect to the existing daemon socket
and send the same line-oriented protocol used by ``core.client``.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import socket
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from core.client import (
    END_OF_RESPONSE,
    FALLBACK_COMMANDS,
)
from core.paths import DEFAULT_SOCK_PATH


DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765
MAX_COMMAND_BODY_BYTES = 64 * 1024
DEFAULT_DAEMON_TIMEOUT_SECONDS = 60.0
JSON_REQUEST_PREFIX = "__vcpi_json__ "
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
HELP_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SLOT_ACTION_RE = re.compile(r"^/api/slots/([^/]+)/(gain|mute|solo|clear|unload)$")
CSRF_META_TAG = "__VCPI_CSRF_META__"
MIN_BPM = 20.0
MAX_BPM = 300.0

logger = logging.getLogger(__name__)


class _TextLineReader(Protocol):
    def readline(self) -> str:
        """Return the next text line or an empty string at EOF."""
        ...


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  __VCPI_CSRF_META__
  <title>vcpi</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <main>
    <h1>vcpi</h1>
    <p>Phase 1 web bridge connected to the vcpi daemon.</p>
    <form id="command-form">
      <label for="command">Command</label>
      <div class="command-row">
        <input id="command" name="command" autocomplete="off" placeholder="status">
        <button type="submit">Send</button>
      </div>
    </form>
    <pre id="output" aria-live="polite"></pre>
  </main>
  <script src="/app.js"></script>
</body>
</html>
"""


APP_JS = """const form = document.getElementById('command-form');
const input = document.getElementById('command');
const output = document.getElementById('output');
const csrfToken = document.querySelector('meta[name="vcpi-csrf-token"]')?.content || '';

async function sendCommand(command) {
  const response = await fetch('/api/command', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-VCPI-CSRF': csrfToken,
    },
    body: JSON.stringify({command}),
  });
  const data = await response.json();
  output.textContent = data.output ? data.output.join('\n') : data.error || '';
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const command = input.value.trim();
  if (command) {
    sendCommand(command).catch((error) => {
      output.textContent = String(error);
    });
  }
});
"""


STYLE_CSS = """body {
  background: #111;
  color: #eee;
  font: 16px/1.4 system-ui, sans-serif;
  margin: 0;
}

main {
  margin: 2rem auto;
  max-width: 56rem;
  padding: 0 1rem;
}

.command-row {
  display: flex;
  gap: 0.5rem;
}

input {
  flex: 1;
  min-width: 0;
}

input,
button {
  font: inherit;
  padding: 0.5rem 0.75rem;
}

pre {
  background: #000;
  border: 1px solid #333;
  overflow: auto;
  padding: 1rem;
  white-space: pre-wrap;
}
"""


def _web_asset(filename: str, fallback: str) -> str:
    """Return a static web asset when present, otherwise use embedded fallback."""
    try:
        return (WEB_DIR / filename).read_text(encoding="utf-8")
    except OSError:
        return fallback


def _inject_csrf_meta(html: str, token: str) -> str:
    tag = f'<meta name="vcpi-csrf-token" content="{escape(token, quote=True)}">'
    if CSRF_META_TAG in html:
        return html.replace(CSRF_META_TAG, tag)
    if "</head>" in html:
        return html.replace("</head>", f"  {tag}\n</head>", 1)
    return html


class CommandResult:
    """Output from one daemon command."""

    def __init__(self, output: list[str], banner: list[str]):
        self.output: list[str] = output
        self.banner: list[str] = banner


class JsonOperationResult:
    """Structured output from one daemon JSON operation."""

    def __init__(self, payload: dict[str, object], banner: list[str]):
        self.payload: dict[str, object] = payload
        self.banner: list[str] = banner


def _socket_path(sock_path: str | Path | None) -> Path:
    return Path(sock_path) if sock_path else DEFAULT_SOCK_PATH


def _first_token(command: str) -> str:
    parts = command.strip().split(maxsplit=1)
    return parts[0].lower() if parts else ""


def _validate_command(command: object, allow_shutdown: bool) -> str:
    if not isinstance(command, str):
        raise ValueError("command must be a string")

    line = command.strip()
    if not line:
        raise ValueError("command must not be empty")
    if "\n" in line or "\r" in line:
        raise ValueError("command must be a single line")
    if _first_token(line) == "shutdown" and not allow_shutdown:
        raise PermissionError("shutdown is disabled for this web backend")
    return line


def execute_command(
    command: str,
    sock_path: str | Path | None = None,
    *,
    allow_shutdown: bool = False,
    daemon_timeout: float = DEFAULT_DAEMON_TIMEOUT_SECONDS,
) -> CommandResult:
    """Send one command line to the daemon and return its response lines."""
    line = _validate_command(command, allow_shutdown)
    path = _socket_path(sock_path)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(daemon_timeout)
        sock.connect(str(path))
        rfile = sock.makefile("r", encoding="utf-8", errors="replace")
        wfile = sock.makefile("w", encoding="utf-8")
        try:
            banner = _read_response_lines(rfile)
            if banner is None:
                raise ConnectionError("daemon closed connection before banner")

            _ = wfile.write(line + "\n")
            wfile.flush()

            output = _read_response_lines(rfile)
            if output is None:
                raise ConnectionError("daemon closed connection before response")
            return CommandResult(output=output, banner=banner)
        finally:
            try:
                wfile.close()
            finally:
                rfile.close()


def execute_json_operation(
    operation: str,
    payload: dict[str, object] | None = None,
    sock_path: str | Path | None = None,
    *,
    daemon_timeout: float = DEFAULT_DAEMON_TIMEOUT_SECONDS,
) -> JsonOperationResult:
    """Send one typed JSON operation to the daemon and return its payload."""
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation must be a non-empty string")
    if "\n" in operation or "\r" in operation:
        raise ValueError("operation must be a single line")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    request = json.dumps(
        {"op": operation.strip(), "payload": payload},
        separators=(",", ":"),
    )
    line = JSON_REQUEST_PREFIX + request
    path = _socket_path(sock_path)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(daemon_timeout)
        sock.connect(str(path))
        rfile = sock.makefile("r", encoding="utf-8", errors="replace")
        wfile = sock.makefile("w", encoding="utf-8")
        try:
            banner = _read_response_lines(rfile)
            if banner is None:
                raise ConnectionError("daemon closed connection before banner")

            _ = wfile.write(line + "\n")
            wfile.flush()

            output = _read_response_lines(rfile)
            if output is None:
                raise ConnectionError("daemon closed connection before JSON response")
            if not output:
                raise ConnectionError("daemon returned an empty JSON response")

            raw_payload = "\n".join(output)
            parsed = cast(object, json.loads(raw_payload))
            if not isinstance(parsed, dict):
                raise ConnectionError("daemon JSON response must be an object")
            return JsonOperationResult(cast(dict[str, object], parsed), banner=banner)
        finally:
            try:
                wfile.close()
            finally:
                rfile.close()


def _probe_daemon(
    sock_path: str | Path | None = None,
    *,
    daemon_timeout: float = DEFAULT_DAEMON_TIMEOUT_SECONDS,
) -> list[str]:
    path = _socket_path(sock_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(daemon_timeout)
        sock.connect(str(path))
        rfile = sock.makefile("r", encoding="utf-8", errors="replace")
        try:
            banner = _read_response_lines(rfile)
            if banner is None:
                raise ConnectionError("daemon closed connection before banner")
            return banner
        finally:
            rfile.close()


def _read_response_lines(rfile: _TextLineReader) -> list[str] | None:
    """Read one daemon protocol response and return lines before sentinel."""
    lines: list[str] = []
    while True:
        raw_line = rfile.readline()
        if not raw_line:
            return None
        line = raw_line.rstrip("\n")
        if line == END_OF_RESPONSE:
            return lines
        lines.append(line)


def _parse_help_commands(lines: list[str]) -> list[str]:
    """Extract command tokens from cmd.Cmd style help output."""
    commands: list[str] = []
    seen: set[str] = set()
    in_custom_help = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        lower = line.lower()
        if (
            lower.startswith("documented commands")
            or lower.startswith("undocumented commands")
            or lower.startswith("miscellaneous help topics")
        ):
            continue

        if lower.startswith("available commands"):
            in_custom_help = True
            continue

        if lower.startswith("tip:") or lower.startswith("no commands available"):
            continue

        if set(line) <= {"=", "-"}:
            continue

        if in_custom_help:
            tokens = line.split()
            if not tokens:
                continue
            token = tokens[0]
            if token == "EOF" or not HELP_TOKEN_RE.fullmatch(token):
                continue
            if token not in seen:
                seen.add(token)
                commands.append(token)
            continue

        for token in line.split():
            if token == "EOF" or not HELP_TOKEN_RE.fullmatch(token):
                continue
            if token in seen:
                continue
            seen.add(token)
            commands.append(token)

    return commands


def _send_json(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    payload: dict[str, object],
) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _ = handler.wfile.write(body)


def _send_text(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    content_type: str,
    text: str,
) -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _ = handler.wfile.write(body)


def _http_status_from_payload(payload: dict[str, object]) -> HTTPStatus:
    if payload.get("ok") is True:
        return HTTPStatus.OK
    status = payload.get("status")
    if isinstance(status, int):
        try:
            return HTTPStatus(status)
        except ValueError:
            pass
    return HTTPStatus.INTERNAL_SERVER_ERROR


class VcpiWebServer(ThreadingHTTPServer):
    """HTTP server carrying daemon connection settings for handlers."""

    daemon_threads: bool = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        sock_path: str | Path | None = None,
        *,
        allow_shutdown: bool = False,
        daemon_timeout: float = DEFAULT_DAEMON_TIMEOUT_SECONDS,
    ):
        super().__init__(server_address, handler_class)
        self.sock_path: Path = _socket_path(sock_path)
        self.allow_shutdown: bool = allow_shutdown
        self.daemon_timeout: float = daemon_timeout
        self.csrf_token: str = secrets.token_urlsafe(32)


class VcpiWebHandler(BaseHTTPRequestHandler):
    """Explicit-route HTTP handler for the Phase 1 web backend."""

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            _send_text(
                self,
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                _inject_csrf_meta(_web_asset("index.html", INDEX_HTML), self.vcpi_server.csrf_token),
            )
        elif path == "/app.js":
            _send_text(
                self,
                HTTPStatus.OK,
                "text/javascript; charset=utf-8",
                _web_asset("app.js", APP_JS),
            )
        elif path == "/style.css":
            _send_text(
                self,
                HTTPStatus.OK,
                "text/css; charset=utf-8",
                _web_asset("style.css", STYLE_CSS),
            )
        elif path == "/api/health":
            self._handle_health()
        elif path == "/api/commands":
            self._handle_commands()
        elif path == "/api/status":
            self._handle_json_get("status")
        elif path == "/api/slots":
            self._handle_json_get("slots")
        elif path == "/api/sessions":
            self._handle_json_get("sessions")
        elif path == "/api/audio/devices":
            self._handle_json_get("audio.devices")
        elif path == "/api/flow":
            self._handle_json_get("flow")
        else:
            _send_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/command":
            self._handle_command()
        elif path == "/api/audio/start":
            self._handle_audio_start()
        elif path == "/api/audio/stop":
            self._handle_json_post("audio.stop", {})
        elif path == "/api/tempo":
            self._handle_tempo_set()
        elif path == "/api/link/start":
            self._handle_link_start()
        elif path == "/api/link/stop":
            self._handle_json_post("link.stop", {})
        elif path == "/api/master/gain":
            self._handle_master_gain()
        elif path == "/api/session/save":
            self._handle_session_save()
        elif path == "/api/session/load":
            self._handle_session_load()
        elif path.startswith("/api/slots/"):
            self._handle_slot_action(path)
        else:
            _send_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    @property
    def vcpi_server(self) -> VcpiWebServer:
        return cast(VcpiWebServer, self.server)

    def _handle_health(self) -> None:
        try:
            banner = _probe_daemon(
                self.vcpi_server.sock_path,
                daemon_timeout=self.vcpi_server.daemon_timeout,
            )
        except TimeoutError as exc:
            _send_json(self, HTTPStatus.GATEWAY_TIMEOUT, {"ok": False, "error": str(exc)})
            return
        except (ConnectionError, ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            _send_json(
                self,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "socket": str(self.vcpi_server.sock_path),
                    "error": str(exc),
                },
            )
            return

        _send_json(
            self,
            HTTPStatus.OK,
            {"ok": True, "socket": str(self.vcpi_server.sock_path), "banner": banner},
        )

    def _handle_commands(self) -> None:
        try:
            result = execute_command(
                "help",
                self.vcpi_server.sock_path,
                allow_shutdown=True,
                daemon_timeout=self.vcpi_server.daemon_timeout,
            )
        except TimeoutError as exc:
            _send_json(
                self,
                HTTPStatus.GATEWAY_TIMEOUT,
                {"ok": False, "commands": list(FALLBACK_COMMANDS), "error": str(exc)},
            )
            return
        except (ConnectionError, ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            _send_json(
                self,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "commands": list(FALLBACK_COMMANDS), "error": str(exc)},
            )
            return

        commands = _parse_help_commands(result.output) or list(FALLBACK_COMMANDS)
        _send_json(self, HTTPStatus.OK, {"ok": True, "commands": commands})

    def _handle_command(self) -> None:
        try:
            self._validate_post_security()
            payload = self._read_json_body()
            command = payload.get("command")
            if not isinstance(command, str):
                raise ValueError("command must be a string")
            result = execute_command(
                command,
                self.vcpi_server.sock_path,
                allow_shutdown=self.vcpi_server.allow_shutdown,
                daemon_timeout=self.vcpi_server.daemon_timeout,
            )
        except PermissionError as exc:
            _send_json(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
            return
        except TimeoutError as exc:
            _send_json(self, HTTPStatus.GATEWAY_TIMEOUT, {"ok": False, "error": str(exc)})
            return
        except json.JSONDecodeError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except (ConnectionError, ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            _send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
            return

        _send_json(
            self,
            HTTPStatus.OK,
            {"ok": True, "command": command, "output": result.output},
        )

    def _handle_json_get(self, operation: str) -> None:
        try:
            result = execute_json_operation(
                operation,
                {},
                self.vcpi_server.sock_path,
                daemon_timeout=self.vcpi_server.daemon_timeout,
            )
        except TimeoutError as exc:
            _send_json(self, HTTPStatus.GATEWAY_TIMEOUT, {"ok": False, "error": str(exc)})
            return
        except json.JSONDecodeError as exc:
            _send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
            return
        except (ConnectionError, ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            _send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
            return

        _send_json(self, _http_status_from_payload(result.payload), result.payload)

    def _handle_json_post(self, operation: str, payload: dict[str, object] | None = None) -> None:
        try:
            self._validate_post_security()
            if payload is None:
                payload = self._read_optional_json_body()
            result = execute_json_operation(
                operation,
                payload,
                self.vcpi_server.sock_path,
                daemon_timeout=self.vcpi_server.daemon_timeout,
            )
        except PermissionError as exc:
            _send_json(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
            return
        except TimeoutError as exc:
            _send_json(self, HTTPStatus.GATEWAY_TIMEOUT, {"ok": False, "error": str(exc)})
            return
        except json.JSONDecodeError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except (ConnectionError, ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            _send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
            return

        _send_json(self, _http_status_from_payload(result.payload), result.payload)

    def _handle_audio_start(self) -> None:
        payload = self._read_secure_optional_json_body()
        if payload is None:
            return
        device = payload.get("device")
        if isinstance(device, bool) or (
            device is not None and not isinstance(device, (str, int))
        ):
            _send_json(
                self,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "device must be a string, integer, or null"},
            )
            return
        self._handle_json_post("audio.start", payload)

    def _handle_tempo_set(self) -> None:
        try:
            payload = self._read_secure_optional_json_body()
            if payload is None:
                return
            self._validate_bpm_payload(payload)
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        self._handle_json_post("tempo.set", payload)

    def _handle_link_start(self) -> None:
        try:
            payload = self._read_secure_optional_json_body()
            if payload is None:
                return
            self._validate_bpm_payload(payload, required=False)
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        self._handle_json_post("link.start", payload)

    def _handle_master_gain(self) -> None:
        try:
            payload = self._read_secure_optional_json_body()
            if payload is None:
                return
            self._validate_gain_payload(payload)
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        self._handle_json_post("master.gain", payload)

    def _handle_session_save(self) -> None:
        payload = self._read_secure_optional_json_body()
        if payload is None:
            return
        try:
            if "path" in payload:
                raise ValueError("path is not accepted for session operations")
            if "name" in payload:
                payload = dict(payload)
                payload["name"] = self._validate_session_name(payload["name"])
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._handle_json_post("session.save", payload)

    def _handle_session_load(self) -> None:
        payload = self._read_secure_optional_json_body()
        if payload is None:
            return
        try:
            if "path" in payload:
                raise ValueError("path is not accepted for session operations")
            payload = dict(payload)
            payload["name"] = self._validate_session_name(payload.get("name"))
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._handle_json_post("session.load", payload)

    def _handle_slot_action(self, path: str) -> None:
        match = SLOT_ACTION_RE.fullmatch(path)
        if match is None:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "slot route must be /api/slots/{1-8}/{gain|mute|solo|clear|unload}"})
            return

        try:
            slot = self._validate_slot_number(match.group(1))
            body = self._read_secure_optional_json_body()
            if body is None:
                return
            action = match.group(2)
            payload = dict(body)
            payload["slot"] = slot
            if action == "gain":
                self._validate_gain_payload(payload)
                operation = "slot.gain"
            elif action == "mute":
                self._validate_optional_bool_payload(payload, "muted")
                operation = "slot.mute"
            elif action == "solo":
                self._validate_optional_bool_payload(payload, "solo")
                operation = "slot.solo"
            else:
                operation = f"slot.{action}"
        except json.JSONDecodeError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except PermissionError as exc:
            _send_json(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        self._handle_json_post(operation, payload)

    def _validate_post_security(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise PermissionError("Content-Type must be application/json")

        supplied_token = self.headers.get("X-VCPI-CSRF", "")
        if not secrets.compare_digest(supplied_token, self.vcpi_server.csrf_token):
            raise PermissionError("missing or invalid CSRF token")

        origin = self.headers.get("Origin")
        if origin is None:
            return

        origin_parts = urlsplit(origin)
        if origin_parts.scheme not in {"http", "https"}:
            raise PermissionError("invalid Origin header")

        request_host = self.headers.get("Host", "")
        if origin_parts.netloc.lower() != request_host.lower():
            raise PermissionError("cross-origin command requests are not allowed")

    def _read_json_body(self) -> dict[str, object]:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            raise ValueError("Content-Length is required")
        try:
            size = int(content_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if size < 0:
            raise ValueError("Content-Length must not be negative")
        if size > MAX_COMMAND_BODY_BYTES:
            raise ValueError("request body is too large")

        raw = self.rfile.read(size)
        payload = cast(object, json.loads(raw.decode("utf-8")))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return cast(dict[str, object], payload)

    def _read_secure_optional_json_body(self) -> dict[str, object] | None:
        try:
            self._validate_post_security()
            return self._read_optional_json_body()
        except PermissionError as exc:
            _send_json(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
        except json.JSONDecodeError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        return None

    def _read_optional_json_body(self) -> dict[str, object]:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return {}
        try:
            size = int(content_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if size < 0:
            raise ValueError("Content-Length must not be negative")
        if size > MAX_COMMAND_BODY_BYTES:
            raise ValueError("request body is too large")
        if size == 0:
            return {}

        raw = self.rfile.read(size)
        payload = cast(object, json.loads(raw.decode("utf-8")))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return cast(dict[str, object], payload)

    @staticmethod
    def _validate_session_name(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("name must be a string")
        name = value.strip()
        if name.lower().endswith(".json"):
            name = name[:-5]
        if not name:
            raise ValueError("name must not be empty")
        if ".." in name:
            raise ValueError("name must not contain '..'")
        if any(separator in name for separator in ("/", "\\")):
            raise ValueError("name must not contain path separators")
        if Path(name).is_absolute():
            raise ValueError("name must not be an absolute path")
        if not SESSION_NAME_RE.fullmatch(name):
            raise ValueError(
                "name must start with a letter or number and contain only letters, numbers, dots, underscores, or hyphens"
            )
        return name

    @staticmethod
    def _validate_slot_number(raw_slot: str) -> int:
        try:
            slot = int(raw_slot)
        except ValueError as exc:
            raise ValueError("slot must be an integer 1-8") from exc
        if not 1 <= slot <= 8:
            raise ValueError("slot must be 1-8")
        return slot

    @staticmethod
    def _validate_gain_payload(payload: dict[str, object]) -> None:
        gain = payload.get("gain")
        if isinstance(gain, bool) or not isinstance(gain, (int, float)):
            raise ValueError("gain must be a number between 0.0 and 1.0")
        if not 0.0 <= float(gain) <= 1.0:
            raise ValueError("gain must be between 0.0 and 1.0")

    @staticmethod
    def _validate_bpm_payload(payload: dict[str, object], *, required: bool = True) -> None:
        bpm = payload.get("bpm")
        if bpm is None:
            if required:
                raise ValueError(f"bpm must be a number between {MIN_BPM:.1f} and {MAX_BPM:.1f}")
            return
        if isinstance(bpm, bool) or not isinstance(bpm, (int, float)):
            raise ValueError(f"bpm must be a number between {MIN_BPM:.1f} and {MAX_BPM:.1f}")
        if not MIN_BPM <= float(bpm) <= MAX_BPM:
            raise ValueError(f"bpm must be between {MIN_BPM:.1f} and {MAX_BPM:.1f}")

    @staticmethod
    def _validate_optional_bool_payload(payload: dict[str, object], key: str) -> None:
        value = payload.get(key)
        if key in payload and not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        toggle = payload.get("toggle")
        if "toggle" in payload and not isinstance(toggle, bool):
            raise ValueError("toggle must be a boolean")


def run_web(
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    sock_path: str | Path | None = None,
    *,
    allow_shutdown: bool = False,
    daemon_timeout: float = DEFAULT_DAEMON_TIMEOUT_SECONDS,
    allow_remote: bool = False,
) -> None:
    """Run the local web bridge until interrupted."""
    if not allow_remote and not _is_loopback_host(host):
        raise ValueError(
            "refusing to bind the web console to a non-loopback host without --allow-remote"
        )
    server = VcpiWebServer(
        (host, port),
        VcpiWebHandler,
        sock_path,
        allow_shutdown=allow_shutdown,
        daemon_timeout=daemon_timeout,
    )
    logger.info(
        "vcpi web bridge listening on http://%s:%s (socket: %s)",
        host,
        port,
        server.sock_path,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def serve_web(
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    sock_path: str | Path | None = None,
    *,
    allow_shutdown: bool = False,
    daemon_timeout: float = DEFAULT_DAEMON_TIMEOUT_SECONDS,
    allow_remote: bool = False,
) -> None:
    """Compatibility entry point for ``core.main``."""
    run_web(
        host,
        port,
        sock_path,
        allow_shutdown=allow_shutdown,
        daemon_timeout=daemon_timeout,
        allow_remote=allow_remote,
    )
