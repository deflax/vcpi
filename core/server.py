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
import logging
import os
import queue
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import NamedTuple

from core.host import VcpiCore
from core.cli import HostCLI
from core.paths import DEFAULT_SOCK_PATH

# Sentinel that marks the end of a command's output.
END_OF_RESPONSE = "\x00"


logger = logging.getLogger(__name__)


class _CommandRequest(NamedTuple):
    """A command submitted by a reader thread for main-thread execution."""
    line: str
    client_id: str
    result: threading.Event
    # Mutable container so the main thread can store the result.
    # [0] = output (str | None), [1] = shutdown_requested (bool)
    result_box: list


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
        box: list = [None, False]
        self._cmd_queue.put(_CommandRequest(line, client_id, evt, box))
        evt.wait()
        return box[0], box[1]

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
