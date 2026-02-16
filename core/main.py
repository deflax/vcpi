"""Entry point and argument parsing for vcpi.

Subcommands
-----------
serve   Run the host as a headless daemon with a Unix socket interface.
cli     Connect to a running server and open an interactive session.
"""

from __future__ import annotations

import argparse
import logging

from core.host import VcpiCore
from core.logging_setup import configure_logging
from core.paths import DEFAULT_SOCK_PATH


logger = logging.getLogger(__name__)


# -- shared helpers ----------------------------------------------------------

def _add_host_args(parser: argparse.ArgumentParser):
    """Add arguments used when starting a host instance."""
    parser.add_argument("--sr", type=int, default=44100, help="Sample rate")
    parser.add_argument("--buf", type=int, default=512, help="Buffer size")
    parser.add_argument("--bpm", type=float, default=120.0, help="Initial BPM")
    parser.add_argument("--link", action="store_true",
                        help="Enable Ableton Link on start")
    parser.add_argument("--seq-midi", type=int, default=None,
                        help="Beatstep Pro MIDI port index")
    parser.add_argument("--mix-midi", type=int, default=None,
                        help="MIDI Mix port index")
    parser.add_argument("--output", default=None, help="Audio output device")
    parser.add_argument("--session", default=None,
                        help="Session file path "
                             "(default: ~/.config/vcpi/session.json)")
    parser.add_argument("--no-restore", action="store_true",
                        help="Skip restoring the previous session on startup")


def _boot_host(args) -> VcpiCore:
    """Create a VcpiCore from parsed arguments and optionally restore state."""
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

    if args.seq_midi is not None:
        try:
            host.open_sequencer_midi(args.seq_midi)
        except Exception as e:
            logger.warning("sequencer MIDI startup failed: %s", e)

    if args.mix_midi is not None:
        try:
            host.open_mixer_midi(args.mix_midi)
        except Exception as e:
            logger.warning("MIDI Mix startup failed: %s", e)

    return host


# -- subcommand handlers -----------------------------------------------------

def _cmd_serve(args):
    """Run the host as a headless server."""
    from core.server import run_server

    level = configure_logging(default_level="WARNING")
    logger.info("vcpi server starting (log level: %s)", logging.getLevelName(level))

    host = _boot_host(args)

    output_device = args.output
    if isinstance(output_device, str) and output_device.isdigit():
        output_device = int(output_device)
    try:
        host.start_audio(output_device)
    except Exception as e:
        logger.warning("audio auto-start failed: %s", e)

    run_server(host, args.sock)


def _cmd_cli(args):
    """Connect to a running server."""
    from core.client import connect

    connect(args.sock)

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

    args = ap.parse_args()
    if args.command is None:
        ap.error("a command is required: serve or cli")
    args.func(args)


if __name__ == "__main__":
    main()
