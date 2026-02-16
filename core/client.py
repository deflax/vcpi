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

import readline  # noqa: F401  (enables line-editing in input())
import socket
import sys
from pathlib import Path

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

    # Read and display the welcome banner
    _read_response(rfile)

    try:
        while True:
            try:
                line = input("vcpi> ")
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D or Ctrl-C -> disconnect gracefully
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

            if line.strip().lower() in {"quit", "exit"}:
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


def _read_response(rfile) -> bool:
    """Read lines from the server until the end-of-response sentinel.

    Prints each line to stdout.  Returns ``False`` if the connection
    was closed before a sentinel was seen.
    """
    while True:
        line = rfile.readline()
        if not line:
            # EOF -- server closed the connection
            return False
        line = line.rstrip("\n")
        if line == END_OF_RESPONSE:
            return True
        print(line)
