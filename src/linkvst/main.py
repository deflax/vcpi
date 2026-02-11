"""Entry point and argument parsing for LinkVST."""

from __future__ import annotations

import argparse
import signal
import sys

from linkvst.host import VSTHost
from linkvst.cli import HostCLI


def main():
    ap = argparse.ArgumentParser(
        description="LinkVST - Python VST3 Host + Ableton Link")
    ap.add_argument("--sr", type=int, default=44100, help="Sample rate")
    ap.add_argument("--buf", type=int, default=512, help="Buffer size")
    ap.add_argument("--bpm", type=float, default=120.0, help="Initial BPM")
    ap.add_argument("--link", action="store_true", help="Enable Link on start")
    ap.add_argument("--seq-midi", type=int, default=None,
                    help="Beatstep Pro MIDI port index")
    ap.add_argument("--mix-midi", type=int, default=None,
                    help="MIDI Mix port index")
    ap.add_argument("--output", default=None, help="Audio output device")
    ap.add_argument("--session", default=None,
                    help="Session file path (default: ~/.config/linkvst/session.json)")
    ap.add_argument("--no-restore", action="store_true",
                    help="Skip restoring the previous session on startup")
    args = ap.parse_args()

    host = VSTHost(sample_rate=args.sr, buffer_size=args.buf,
                   session_path=args.session)
    host.link._bpm = args.bpm

    # Restore previous session (plugins, routing, params, gains)
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

    signal.signal(signal.SIGINT, lambda *_: (host.shutdown(), sys.exit(0)))

    cli = HostCLI(host)
    try:
        cli.cmdloop()
    except KeyboardInterrupt:
        host.shutdown()


if __name__ == "__main__":
    main()
