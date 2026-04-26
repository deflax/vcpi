"""Entry point and argument parsing for vcpi.

Subcommands
-----------
serve   Run the host as a headless daemon with a Unix socket interface.
cli     Connect to a running server and open an interactive session.
web     Start the local-only Phase 1 browser command console.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

from core.logging_setup import configure_logging
from core.paths import DEFAULT_SOCK_PATH, check_pidfile, write_pidfile, remove_pidfile

if TYPE_CHECKING:
    from core.host import VcpiCore


logger = logging.getLogger(__name__)


# -- shared helpers ----------------------------------------------------------

def _add_host_args(parser: argparse.ArgumentParser):
    """Add arguments used when starting a host instance."""
    parser.add_argument("--sr", type=int, default=44100, help="Sample rate")
    parser.add_argument("--buf", type=int, default=512, help="Buffer size")
    parser.add_argument("--bpm", type=float, default=120.0, help="Initial BPM")
    parser.add_argument("--link", action="store_true",
                        help="Enable Ableton Link on start")
    parser.add_argument("--midi-in", nargs="*", default=None,
                        help="MIDI input port index(es) to open")
    parser.add_argument("--mix-midi", type=int, default=None,
                        help="MIDI Mix port index")
    parser.add_argument("--mix-midi-out", type=int, default=None,
                        help="MIDI Mix output port index (LED feedback)")
    parser.add_argument("--output", default=None, help="Audio output device")
    parser.add_argument("--session", default=None,
                        help="Session file path "
                             "(default: ~/.config/vcpi/session.json)")
    parser.add_argument("--no-restore", action="store_true",
                        help="Skip restoring the previous session on startup")


def _boot_host(args) -> "VcpiCore":
    """Create a VcpiCore from parsed arguments and optionally restore state."""
    from core.host import VcpiCore

    host = VcpiCore(sample_rate=args.sr, buffer_size=args.buf,
                    session_path=args.session)
    host.link._bpm = args.bpm

    if not args.no_restore:
        try:
            host.restore_session()
        except Exception as e:
            logger.warning("session restore failed: %s", e)

    if args.link:
        try:
            host.start_link(args.bpm)
        except Exception as e:
            logger.warning("link startup failed: %s", e)

    if args.midi_in:
        for port in args.midi_in:
            try:
                host.open_midi_input(port)
            except Exception as e:
                logger.warning("MIDI input startup failed for '%s': %s", port, e)

    if args.mix_midi is not None:
        try:
            host.open_mixer_midi(args.mix_midi)
        except Exception as e:
            logger.warning("MIDI Mix startup failed: %s", e)

    if args.mix_midi_out is not None:
        try:
            host.open_mixer_midi_out(args.mix_midi_out)
        except Exception as e:
            logger.warning("MIDI Mix OUT startup failed: %s", e)

    return host


# -- subcommand handlers -----------------------------------------------------

def _cmd_serve(args):
    """Run the host as a headless server."""
    from core.server import run_server

    # Prevent accidental duplicate instances.
    existing_pid = check_pidfile()
    if existing_pid is not None:
        print(
            f"Error: vcpi server is already running (PID {existing_pid}).\n"
            "Connect with:  ./vcli\n"
            "Or stop it first:  ./vcli -c shutdown",
            file=sys.stderr,
        )
        sys.exit(1)

    level = configure_logging(default_level="WARNING")
    logger.info("vcpi server starting (log level: %s)", logging.getLevelName(level))

    write_pidfile()
    try:
        host = _boot_host(args)
        run_server(host, args.sock)
    finally:
        remove_pidfile()


def _cmd_cli(args):
    """Connect to a running server."""
    from core.client import connect

    connect(args.sock)


def _cmd_web(args):
    """Start the local-only Phase 1 browser command console."""
    try:
        from core import web as web_mod
    except ImportError as exc:
        raise SystemExit(
            "Error: web backend is not available yet. Add core/web.py with "
            "serve_web(host, port, sock_path, allow_shutdown)."
        ) from exc

    serve_web = getattr(web_mod, "serve_web", None) or getattr(web_mod, "run_web", None)
    if serve_web is None:
        raise SystemExit(
            "Error: core.web must export serve_web(host, port, sock_path, allow_shutdown)."
        )

    serve_web(
        args.host,
        args.port,
        args.sock,
        allow_shutdown=args.allow_shutdown,
        daemon_timeout=args.daemon_timeout,
        allow_remote=args.allow_remote,
    )

# -- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="vcpi - Python VST3 Host + Ableton Link")
    sub = ap.add_subparsers(dest="command")

    # -- serve ---------------------------------------------------------------
    sp_serve = sub.add_parser(
        "serve",
        help="Run headless with a Unix socket control interface")
    _add_host_args(sp_serve)
    sp_serve.add_argument(
        "--sock", default=None,
        help=f"Unix socket path (default: {DEFAULT_SOCK_PATH})")
    sp_serve.set_defaults(func=_cmd_serve)

    # -- cli -----------------------------------------------------------------
    sp_cli = sub.add_parser(
        "cli",
        help="Connect to a running vcpi server")
    sp_cli.add_argument(
        "--sock", default=None,
        help=f"Unix socket path (default: {DEFAULT_SOCK_PATH})")
    sp_cli.set_defaults(func=_cmd_cli)

    # -- web -----------------------------------------------------------------
    sp_web = sub.add_parser(
        "web",
        help="Start the local-only Phase 1 browser command console")
    sp_web.add_argument(
        "--host", default="127.0.0.1",
        help="HTTP bind host (default: 127.0.0.1)")
    sp_web.add_argument(
        "--port", type=int, default=8765,
        help="HTTP bind port (default: 8765)")
    sp_web.add_argument(
        "--sock", default=None,
        help=f"Unix socket path (default: {DEFAULT_SOCK_PATH})")
    sp_web.add_argument(
        "--allow-shutdown", action="store_true",
        help="Allow the browser UI to request daemon shutdown")
    sp_web.add_argument(
        "--daemon-timeout", type=float, default=60.0,
        help="Seconds to wait for daemon command responses (default: 60)")
    sp_web.add_argument(
        "--allow-remote", action="store_true",
        help="Allow binding the command console to non-loopback hosts")
    sp_web.set_defaults(func=_cmd_web)

    args = ap.parse_args()
    if args.command is None:
        ap.error("a command is required: serve, cli, or web")
    args.func(args)


if __name__ == "__main__":
    main()
