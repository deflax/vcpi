"""Unix socket server for vcpi.

Runs VcpiCore headless and accepts CLI connections over a Unix domain socket.
Each connected client gets its own HostCLI session sharing the same core
instance.  The protocol is line-oriented text:

  client -> server:  one command per line (UTF-8)
  server -> client:  output lines, terminated by a sentinel line

The sentinel is a NUL byte on its own line (``\\x00\\n``).  The client reads
lines until it sees the sentinel, then prints everything before it and
prompts for the next command.

Multiple clients may connect simultaneously; a threading lock serialises
command execution so the shared core state stays consistent.
"""

from __future__ import annotations

import io
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from core.host import VcpiCore
from core.cli import HostCLI
from core.paths import DEFAULT_SOCK_PATH

# Sentinel that marks the end of a command's output.
END_OF_RESPONSE = "\x00"


class VcpiServer:
    """Headless VcpiCore daemon with a Unix socket control interface."""

    def __init__(self, host: VcpiCore, sock_path: Path = DEFAULT_SOCK_PATH):
        self.host = host
        self.sock_path = sock_path
        self._lock = threading.Lock()
        self._server_sock: socket.socket | None = None
        self._running = False

    # ------------------------------------------------------------------

    def start(self):
        """Bind the Unix socket and accept connections in a loop."""
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

        # Allow non-root users in the same group to connect
        os.chmod(str(self.sock_path), 0o770)

        self._running = True
        print(f"[Server] Listening on {self.sock_path}")

        try:
            while self._running:
                try:
                    conn, _ = self._server_sock.accept()
                except OSError:
                    break
                t = threading.Thread(target=self._handle_client, args=(conn,),
                                     daemon=True)
                t.start()
        finally:
            self.stop()

    def stop(self):
        """Shut down the server and clean up."""
        self._running = False
        if self._server_sock:
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

    # ------------------------------------------------------------------

    def _handle_client(self, conn: socket.socket):
        """Serve one connected CLI client."""
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

                # Execute the command while holding the lock so concurrent
                # clients don't trample each other.
                output = self._run_command(line)

                if output is None:
                    # quit / exit / EOF  -> close this client
                    wfile.write("[Host] Disconnected.\n")
                    wfile.write(END_OF_RESPONSE + "\n")
                    wfile.flush()
                    break

                wfile.write(output)
                if output and not output.endswith("\n"):
                    wfile.write("\n")
                wfile.write(END_OF_RESPONSE + "\n")
                wfile.flush()

        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _run_command(self, line: str) -> str | None:
        """Execute one CLI command and capture its printed output.

        Returns the captured output string, or ``None`` if the command
        requests a disconnect (quit/exit/EOF).
        """
        with self._lock:
            buf = io.StringIO()

            # Build a throw-away HostCLI that prints into our buffer.
            cli = HostCLI(self.host, stdout=buf, owns_host=False)
            cli.use_rawinput = False

            # cmd.Cmd.onecmd() returns True when the command wants to exit.
            stop = cli.onecmd(line)

            output = buf.getvalue()

        # quit/exit/EOF tell cmd.Cmd to stop -- but for the *server* we
        # only disconnect this client, we never shut down the host.
        if stop:
            # If the user typed "quit" don't actually shut down the host.
            # The host only shuts down when the daemon process exits.
            return None

        return output


def run_server(host: VcpiCore, sock_path: str | None = None):
    """Convenience entry point used by ``main.py``."""
    path = Path(sock_path) if sock_path else DEFAULT_SOCK_PATH
    server = VcpiServer(host, path)

    def _shutdown(signum, frame):
        print("\n[Server] Shutting down...")
        server.stop()
        host.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    server.start()
