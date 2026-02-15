"""CLI client that connects to a running LinkVST server over a Unix socket.

Usage::

    python -m linkvst cli [--sock /run/linkvst.sock]

Or directly::

    python -c "from linkvst.client import connect; connect()"
"""

from __future__ import annotations

import readline  # noqa: F401  (enables line-editing in input())
import socket
import sys
from pathlib import Path

DEFAULT_SOCK_PATH = Path("/run/linkvst/linkvst.sock")

# Must match the sentinel used by server.py
END_OF_RESPONSE = "\x00"


def connect(sock_path: str | Path | None = None):
    """Connect to the LinkVST server and run an interactive REPL."""
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
                line = input("linkvst> ")
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D or Ctrl-C -> disconnect gracefully
                print()
                break

            wfile.write(line + "\n")
            wfile.flush()

            if not _read_response(rfile):
                # Server closed the connection (e.g. after quit)
                break

    finally:
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
