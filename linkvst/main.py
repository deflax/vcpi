"""Entry point and argument parsing for LinkVST.

Subcommands
-----------
serve   Run the host as a headless daemon with a Unix socket interface.
cli     Connect to a running server and open an interactive session.
(none)  Legacy all-in-one mode: host + interactive CLI in one process.
"""

from __future__ import annotations

import argparse
import signal
import sys

from linkvst.host import VSTHost
from linkvst.cli import HostCLI


# -- shared helpers ----------------------------------------------------------

def _add_host_args(parser: argparse.ArgumentParser):
    """Add arguments common to both ``serve`` and the legacy local mode."""
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
                             "(default: ~/.config/linkvst/session.json)")
    parser.add_argument("--no-restore", action="store_true",
                        help="Skip restoring the previous session on startup")


def _boot_host(args) -> VSTHost:
    """Create a VSTHost from parsed arguments and optionally restore state."""
    host = VSTHost(sample_rate=args.sr, buffer_size=args.buf,
                   session_path=args.session)
    host.link._bpm = args.bpm

    if not args.no_restore:
        try:
            host.restore_session()
        except Exception as e:
            print(f"Session restore: {e}")

    if args.link:
        try:
            host.start_link(args.bpm)
        except Exception as e:
            print(f"Link: {e}")

    if args.seq_midi is not None:
        try:
            host.open_sequencer_midi(args.seq_midi)
        except Exception as e:
            print(f"SEQ MIDI: {e}")

    if args.mix_midi is not None:
        try:
            host.open_mixer_midi(args.mix_midi)
        except Exception as e:
            print(f"MIDI Mix: {e}")

    return host


# -- subcommand handlers -----------------------------------------------------

def _cmd_serve(args):
    """Run the host as a headless server."""
    from linkvst.server import run_server

    host = _boot_host(args)
    run_server(host, args.sock)


def _cmd_cli(args):
    """Connect to a running server."""
    from linkvst.client import connect

    connect(args.sock)


def _cmd_local(args):
    """Legacy all-in-one mode (host + interactive CLI)."""
    host = _boot_host(args)

    signal.signal(signal.SIGINT, lambda *_: (host.shutdown(), sys.exit(0)))

    cli = HostCLI(host, owns_host=True)
    try:
        cli.cmdloop()
    except KeyboardInterrupt:
        host.shutdown()


# -- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="LinkVST - Python VST3 Host + Ableton Link")
    sub = ap.add_subparsers(dest="command")

    # -- serve ---------------------------------------------------------------
    sp_serve = sub.add_parser(
        "serve",
        help="Run headless with a Unix socket control interface")
    _add_host_args(sp_serve)
    sp_serve.add_argument(
        "--sock", default=None,
        help="Unix socket path (default: /run/linkvst/linkvst.sock)")
    sp_serve.set_defaults(func=_cmd_serve)

    # -- cli -----------------------------------------------------------------
    sp_cli = sub.add_parser(
        "cli",
        help="Connect to a running LinkVST server")
    sp_cli.add_argument(
        "--sock", default=None,
        help="Unix socket path (default: /run/linkvst/linkvst.sock)")
    sp_cli.set_defaults(func=_cmd_cli)

    # -- parse ---------------------------------------------------------------
    args = ap.parse_args()

    if args.command is None:
        # No subcommand -> legacy local mode.  Re-parse with host args.
        ap_local = argparse.ArgumentParser(
            description="LinkVST - Python VST3 Host + Ableton Link (local mode)")
        _add_host_args(ap_local)
        args = ap_local.parse_args()
        _cmd_local(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
