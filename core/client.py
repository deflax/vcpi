"""CLI client that connects to a running vcpi server over a Unix socket.

Usage::

    python main.py cli [--sock PATH]

Default PATH is auto-selected:
  $XDG_RUNTIME_DIR/vcpi/vcpi.sock
  fallback: /tmp/vcpi-<uid>/vcpi.sock
  root fallback: /run/vcpi/vcpi.sock

Or directly::

    python -c "from core.client import connect; connect()"
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console

from core.client_display import display_response
from core.flow_display import display_flow, explore_flow
from core.paths import DEFAULT_SOCK_PATH

# Must match the sentinel used by server.py
END_OF_RESPONSE = "\x00"


def connect(sock_path: str | Path | None = None):
    """Connect to the vcpi server and run an interactive REPL."""
    path = Path(sock_path) if sock_path else DEFAULT_SOCK_PATH

    if not path.exists():
        print(f"Error: socket {path} not found. Is the server running?",
              file=sys.stderr)
        sys.exit(1)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except (ConnectionRefusedError, OSError) as e:
        print(f"Error: cannot connect to {path}: {e}", file=sys.stderr)
        sys.exit(1)

    rfile = sock.makefile("r", encoding="utf-8", errors="replace")
    wfile = sock.makefile("w", encoding="utf-8")

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    console = Console() if interactive else None

    try:
        # Keep socket cleanup active during banner and completion requests too.
        if not _read_response(rfile, console):
            return
        session = (
            PromptSession(
                completer=CommandCompleter(rfile, wfile),
                history=InMemoryHistory(),
                complete_while_typing=False,
            )
            if interactive else None
        )
        while True:
            try:
                line = session.prompt("vcpi> ") if session else input("vcpi> ")
            except KeyboardInterrupt:
                # Ctrl-C: cancel current input line and keep the client open.
                print()
                continue
            except EOFError:
                # Ctrl-D -> disconnect gracefully
                print()
                break

            # Rich flow views use the same cached snapshot as the text command.
            # Navigation stays client-local; the daemon never owns terminal input.
            tokens = line.strip().split()
            if console is not None and tokens and tokens[0] == "flow":
                views = {"all", "midi", "controls", "audio", "master",
                         *(str(i) for i in range(1, 9))}
                view = tokens[1] if len(tokens) == 2 else "overview"
                if len(tokens) <= 2 and (len(tokens) == 1 or view in views or view == "explore"):
                    try:
                        fetch = lambda: _fetch_flow_snapshot(rfile, wfile)
                        if view == "explore":
                            explore_flow(fetch)
                        else:
                            display_flow(console, fetch(), view)
                    except (OSError, ValueError) as error:
                        console.print(f"Error: {error}", markup=False, style="bold red")
                    continue

            try:
                wfile.write(line + "\n")
                wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Server already closed the socket.
                break

            if not _read_response(rfile, console, line):
                # Server closed the connection (e.g. after quit)
                break

            if line.strip().lower() in {"quit", "exit", "shutdown"}:
                # Explicit disconnect command completed.
                break

    finally:
        try:
            wfile.close()
        except OSError:
            pass
        try:
            rfile.close()
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class CommandCompleter(Completer):
    """Request the daemon's cached-state completions over the command stream."""

    def __init__(self, rfile, wfile):
        super().__init__()
        self.rfile = rfile
        self.wfile = wfile

    def get_completions(
        self, document: Document, complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        before = document.text_before_cursor
        try:
            self.wfile.write("\x01" + json.dumps(before) + "\n")
            self.wfile.flush()
            # Always consume the entire framed response, including errors,
            # before deciding whether it contains usable suggestions.
            lines = _read_response_lines(self.rfile)
        except (OSError, ValueError):
            return

        if lines is None or len(lines) != 1:
            return
        try:
            response = json.loads(lines[0])
        except (ValueError, TypeError):
            return
        if not isinstance(response, dict) or set(response) != {"start_position", "matches"}:
            return
        start = response["start_position"]
        matches = response["matches"]
        if type(start) is not int or not -len(before) <= start <= 0:
            return
        if not isinstance(matches, list) or any(not isinstance(name, str) for name in matches):
            return

        for name in matches:
            yield Completion(name + " ", start_position=start, display=name)


def _read_response(rfile, console: Console | None = None, command: str = "") -> bool:
    """Display one response; non-interactive clients preserve the wire text."""
    lines = _read_response_lines(rfile)
    if lines is None:
        return False
    if console is not None:
        display_response(console, command, lines)
    else:
        for line in lines:
            print(line)
    return True


def _fetch_flow_snapshot(rfile, wfile) -> dict:
    """Consume a complete response before reporting a malformed flow snapshot."""
    wfile.write("\x02\n")
    wfile.flush()
    lines = _read_response_lines(rfile)
    if lines is None:
        raise OSError("server disconnected")
    try:
        snapshot = json.loads("\n".join(lines))
    except ValueError as error:
        raise ValueError("\n".join(lines) or "empty flow response") from error
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("title"), str):
        raise ValueError("invalid flow snapshot")
    sections = snapshot.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("invalid flow sections")
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("invalid flow section")
        if any(not isinstance(section.get(key), str) for key in ("id", "title")):
            raise ValueError("invalid flow section heading")
        columns, rows = section.get("columns"), section.get("rows")
        if (not isinstance(columns, list) or not columns
                or any(not isinstance(column, str) for column in columns)
                or not isinstance(rows, list)
                or any(not isinstance(row, list) or len(row) != len(columns)
                       or any(not isinstance(cell, str) for cell in row) for row in rows)):
            raise ValueError("invalid flow table")
    return snapshot


def _read_response_lines(rfile) -> list[str] | None:
    """Read one protocol response and return all lines before sentinel."""
    lines: list[str] = []
    while True:
        line = rfile.readline()
        if not line:
            # EOF -- server closed the connection
            return None
        line = line.rstrip("\n")
        if line == END_OF_RESPONSE:
            return lines
        lines.append(line)
