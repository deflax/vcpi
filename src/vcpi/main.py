"""Entry point and argument parsing for vcpi.

Subcommands
-----------
serve   Run the host as a headless daemon with a Unix socket interface.
cli     Connect to a running server and open an interactive session.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import TYPE_CHECKING

from vcpi.logging_setup import configure_logging
from vcpi.link import validate_bpm
from vcpi.paths import DEFAULT_SOCK_PATH, check_pidfile, write_pidfile, remove_pidfile
from vcpi.native_output import native_output

if TYPE_CHECKING:
    from vcpi.host import VcpiCore


logger = logging.getLogger(__name__)


# -- shared helpers ----------------------------------------------------------

def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _device_selector(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("device index or name must not be empty")
    return value


def _add_host_args(parser: argparse.ArgumentParser):
    """Add grouped startup settings; None means preserve restored state."""
    audio = parser.add_argument_group("Audio setup")
    audio.add_argument("--sample-rate", type=_positive_int, default=44100,
                       metavar="HZ", help="Startup sample rate in Hz (default: 44100)")
    audio.add_argument("--buffer-size", type=_positive_int, default=512,
                       metavar="FRAMES", help="Startup buffer size in frames (default: 512)")
    audio.add_argument("--output", type=_device_selector, metavar="INDEX_OR_NAME",
                       help="Start audio on this output, overriding the saved device; "
                            "zero-based index or quoted name (default: restore saved output)")

    tempo = parser.add_argument_group(
        "Tempo and synchronization",
        "Omitted Link options preserve saved state (default: disabled).")
    tempo.add_argument("--bpm", type=validate_bpm, metavar="BPM",
                       help="Positive finite beats/minute (default: saved tempo or 120)")
    link = tempo.add_mutually_exclusive_group()
    link.add_argument("--link", dest="link", action="store_true",
                      help="Enable Ableton Link, overriding saved state")
    link.add_argument("--no-link", dest="link", action="store_false",
                      help="Disable Ableton Link, overriding saved state")
    parser.set_defaults(link=None)

    midi = parser.add_argument_group("MIDI connections (zero-based hardware indexes or names)")
    midi.add_argument("--midi-input", nargs="+", action="extend", type=_device_selector,
                      metavar="INDEX_OR_NAME",
                      help="Open one or more MIDI inputs; repeat to accumulate. "
                           "Replaces saved inputs when supplied (default: saved inputs or none)")
    midi.add_argument("--midimix-input", type=_device_selector, metavar="INDEX_OR_NAME",
                      help="MIDI Mix input (default: saved connection)")
    midi.add_argument("--midimix-output", type=_device_selector, metavar="INDEX_OR_NAME",
                      help="MIDI Mix LED-feedback output (default: saved connection)")

    session = parser.add_argument_group("Session restoration")
    session.add_argument("--session", metavar="PATH",
                         help="Session file (default: ~/.config/vcpi/session.json)")
    session.add_argument("--no-restore", action="store_true",
                         help="Skip saved setup; use defaults and explicit options")


def _boot_host(args) -> "VcpiCore":
    """Create a VcpiCore from parsed arguments and optionally restore state."""
    from vcpi.host import VcpiCore

    host = VcpiCore(sample_rate=args.sample_rate, buffer_size=args.buffer_size,
                    session_path=args.session)
    try:
        overridden = {
            name for name in (
                "bpm", "link", "midi_input", "midimix_input", "midimix_output", "output"
            ) if getattr(args, name) is not None
        }
        if args.bpm is not None:
            host.link.bpm = args.bpm
        if not args.no_restore:
            try:
                host.restore_session(skip_settings=overridden)
            except Exception as e:
                logger.warning("session restore failed: %s", e)

        if args.output is not None:
            host.preferred_output_device = args.output
            try:
                host.start_audio()
            except Exception as e:
                logger.warning("audio startup failed for '%s': %s", args.output, e)
        if args.link is not None:
            try:
                if args.link:
                    host.start_link()
                else:
                    host.stop_link()
            except Exception as e:
                logger.warning("link startup failed: %s", e)

        for port in args.midi_input or ():
            try:
                host.open_midi_input(port)
            except Exception as e:
                logger.warning("MIDI input startup failed for '%s': %s", port, e)

        if args.midimix_input is not None:
            try:
                host.open_mixer_midi(args.midimix_input)
            except Exception as e:
                logger.warning("MIDI Mix startup failed: %s", e)

        if args.midimix_output is not None:
            try:
                host.open_mixer_midi_out(args.midimix_output)
            except Exception as e:
                logger.warning("MIDI Mix OUT startup failed: %s", e)

        return host
    except BaseException:
        try:
            host.shutdown(save=False)
        except BaseException:
            logger.exception("startup cleanup failed")
        raise


# -- subcommand handlers -----------------------------------------------------

def _cmd_serve(args):
    """Run the host as a headless server."""
    from vcpi.commands.help import VCPI_ASCII_LOGO
    from vcpi.server import run_server

    # Prevent accidental duplicate instances.
    existing_pid = check_pidfile()
    if existing_pid is not None:
        print(
            f"Error: vcpi server is already running (PID {existing_pid}).\n"
            "Connect with:  ./vcpi\n"
            "Then stop it interactively with:  shutdown",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\n{VCPI_ASCII_LOGO}\n", flush=True)

    with native_output("daemon"):
        level = configure_logging()
        logger.info("vcpi server starting (log level: %s)", logging.getLevelName(level))

        # Install before booting: unwind active RPCs instead of re-entering their
        # locks from a signal handler. Resource cleanup happens in finally blocks.
        def interrupt(signum, frame):
            del signum, frame
            raise SystemExit(0)

        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)

        write_pidfile()
        host = None
        try:
            host = _boot_host(args)
            run_server(host, args.socket)
        finally:
            try:
                if host is not None:
                    host.shutdown()
            finally:
                remove_pidfile()


def _cmd_cli(args):
    """Connect to a running server."""
    from vcpi.client import connect

    connect(args.socket)


# -- main --------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="vcpi - Native VST3 Host + Ableton Link", allow_abbrev=False)
    sub = ap.add_subparsers(dest="command")

    # -- serve ---------------------------------------------------------------
    sp_serve = sub.add_parser(
        "serve",
        help="Run headless with a Unix socket control interface",
        description="Restore the host setup, reopen saved audio output, and serve commands.",
        epilog="Explicit options override saved settings; omitted options preserve them. "
               "Sample rate and buffer size are startup-only, never restored. "
               "Without a saved or explicit output, use audio start in the interactive CLI. "
               "Quote device names or paths containing spaces.",
        allow_abbrev=False)
    _add_host_args(sp_serve)
    sp_serve.add_argument(
        "--socket", default=None, metavar="PATH",
        help=f"Unix socket path (default: {DEFAULT_SOCK_PATH})")
    sp_serve.set_defaults(func=_cmd_serve)

    # -- cli -----------------------------------------------------------------
    sp_cli = sub.add_parser(
        "cli",
        help="Connect to a running vcpi server",
        description="Open an interactive command session with a running vcpi server.",
        epilog="Type help for commands; shutdown stops the server; quit disconnects.",
        allow_abbrev=False)
    sp_cli.add_argument(
        "--socket", default=None, metavar="PATH",
        help=f"Unix socket path (default: {DEFAULT_SOCK_PATH})")
    sp_cli.set_defaults(func=_cmd_cli)
    return ap


def main():
    ap = _build_parser()

    args = ap.parse_args()
    if args.command is None:
        ap.error("a command is required: serve or cli")
    args.func(args)


if __name__ == "__main__":
    main()
