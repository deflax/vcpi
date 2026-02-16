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

import re
import readline  # noqa: F401  (enables line-editing in input())
import socket
import sys
from pathlib import Path

from core.paths import DEFAULT_SOCK_PATH

# Must match the sentinel used by server.py
END_OF_RESPONSE = "\x00"


HELP_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Fallback for tab completion in case help parsing fails.
FALLBACK_COMMANDS = (
    "audio_start",
    "audio_stop",
    "audio_devices",
    "deps",
    "exit",
    "gain",
    "graph",
    "help",
    "link",
    "load",
    "master",
    "midi_keys",
    "midi_mix",
    "midi_mix_out",
    "midi_ports_in",
    "midi_ports_out",
    "midi_seq",
    "mute",
    "note",
    "params",
    "quit",
    "restore",
    "route",
    "routing",
    "save",
    "set",
    "shutdown",
    "slots",
    "solo",
    "status",
    "tempo",
    "unload",
    "unlink",
    "unroute",
)


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

    # Read and display the welcome banner
    _read_response(rfile)

    # Enable readline tab-completion from live server command names.
    command_names = _fetch_command_names(rfile, wfile)
    _configure_tab_completion(command_names)

    try:
        while True:
            try:
                line = input("vcpi> ")
            except KeyboardInterrupt:
                # Ctrl-C: cancel current input line and keep the client open.
                print()
                continue
            except EOFError:
                # Ctrl-D -> disconnect gracefully
                print()
                break

            try:
                wfile.write(line + "\n")
                wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Server already closed the socket.
                break

            if not _read_response(rfile):
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


def _configure_tab_completion(command_names: list[str]):
    """Enable command-name completion on Tab for the input prompt."""
    names = sorted(set(command_names))
    if not names:
        return

    def _complete(text: str, state: int):
        line = readline.get_line_buffer()
        begidx = readline.get_begidx()

        # Complete only the command token (first word).
        if line[:begidx].strip():
            return None

        matches = [name for name in names if name.startswith(text)]
        if len(matches) == 1:
            matches = [matches[0] + " "]

        return matches[state] if state < len(matches) else None

    readline.parse_and_bind("tab: complete")
    readline.set_completer(_complete)


def _fetch_command_names(rfile, wfile) -> list[str]:
    """Fetch command names from server help output for completion."""
    try:
        wfile.write("help\n")
        wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return list(FALLBACK_COMMANDS)

    lines = _read_response_lines(rfile)
    if lines is None:
        return list(FALLBACK_COMMANDS)

    parsed = _parse_help_commands(lines)
    return parsed or list(FALLBACK_COMMANDS)


def _parse_help_commands(lines: list[str]) -> list[str]:
    """Extract command tokens from cmd.Cmd style help output."""
    commands: list[str] = []
    seen: set[str] = set()

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

        if set(line) <= {"=", "-"}:
            continue

        for token in line.split():
            if token == "EOF":
                continue
            if not HELP_TOKEN_RE.fullmatch(token):
                continue
            if token in seen:
                continue
            seen.add(token)
            commands.append(token)

    return commands


def _read_response(rfile) -> bool:
    """Read lines from the server until the end-of-response sentinel.

    Prints each line to stdout.  Returns ``False`` if the connection
    was closed before a sentinel was seen.
    """
    lines = _read_response_lines(rfile)
    if lines is None:
        return False
    for line in lines:
        print(line)
    return True


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
