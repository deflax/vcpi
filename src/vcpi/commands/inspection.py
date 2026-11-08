"""Inspection command handlers."""

import logging
from vcpi.deps import HAS_LINK, HAS_RTMIDI, HAS_MIDO
from vcpi.logging_setup import set_log_level
from vcpi.processes import render_processes
from vcpi.presentation.signal_flow import build_flow_snapshot, render_flow_snapshot
from vcpi.presentation.show_request import parse_show
from vcpi.presentation.status import render_status
from .help import VCPI_ASCII_LOGO


def show(ctx, arg):
    """Inspect cached routing: show [target] [--details]

    Bare show prints a compact overview. show browse opens the interactive
    explorer; piped output uses the same overview. Use show controls
    [midimix|beatstep] for mappings, or --details for metadata and diagnostics.
    """
    try:
        request = parse_show(arg)
        snapshot = build_flow_snapshot(ctx.host, details=request.details)
        ctx.emit(render_flow_snapshot(snapshot, None if request.view == "all" else request.view))
    except ValueError as error:
        ctx.emit(f"Error: {error}")


def debug(ctx, arg):
    """Show or toggle daemon debug logging: debug [on|off]

    debug on enables DEBUG logging in the daemon and live audio workers.
    debug off returns logging to INFO. New workers inherit the setting.
    Changes last until the daemon exits; logs stay in its terminal/service log.
    Raw native/plugin output is discarded unless debug is on, then shown with
    [daemon], [audio], or [track N] prefixes. No per-process log files are kept.
    For startup diagnostics, launch with LOG_LEVEL=DEBUG ./vcpid.
    """
    mode = arg.strip().lower()
    if mode not in ("", "on", "off"):
        ctx.emit("Usage: debug [on|off]")
        return
    if mode:
        level = "DEBUG" if mode == "on" else "INFO"
        try:
            ctx.host.engine.request("set_log_level", level=level)
        except Exception as error:
            ctx.emit(f"Error: {error}")
            return
        set_log_level(level)
    level = logging.getLogger().getEffectiveLevel()
    ctx.emit(f"  Debug logging: {'on' if level <= logging.DEBUG else 'off'} "
                f"(log level: {logging.getLevelName(level)})")


def about(ctx, arg):
    """Show vcpi logo and project info: about"""
    if arg.strip():
        ctx.emit("Usage: about")
        return

    ctx.emit(VCPI_ASCII_LOGO)
    ctx.emit()
    ctx.emit("vcpi - Native VST3 host with Ableton Link tempo sync")
    ctx.emit("License: AGPL-3.0 (see LICENSE); no warranty.")
    ctx.emit("Redistribution is permitted under the license.")
    ctx.emit("Source: https://github.com/deflax/vcpi")


def status(ctx, arg):
    """Overall status: status"""
    if arg.strip():
        ctx.emit("Usage: status")
        return
    ctx.emit(render_status(ctx.host))


def ps(ctx, arg):
    """Show processes and threads: ps [--all] [--cpu | --mem]

    Includes the daemon and all descendants (audio, tracks, helpers), not
    the separate CLI client. Samples CPU over ~200 ms; 100% is one vcpi.
    Process rows show total CPU, resident memory (MiB), state and thread
    count. Thread rows show native TIDs and CPU, not separate memory.
    Idle generically named threads are hidden; ps --all shows every thread.
    Main threads and known native render threads remain visible.
    Sort descending by CPU (including threads) or process RSS.
    """
    options = ctx.tokens(arg, "ps [--all] [--cpu | --mem]")
    if options is None:
        return
    if (any(option not in ("--all", "--cpu", "--mem") for option in options)
            or len(options) != len(set(options))
            or ("--cpu" in options and "--mem" in options)):
        ctx.emit("Usage: ps [--all] [--cpu | --mem]")
        return
    sort_by = "cpu" if "--cpu" in options else "mem" if "--mem" in options else None
    try:
        ctx.emit(render_processes(ctx.host, show_all="--all" in options,
                                     sort_by=sort_by))
    except (OSError, RuntimeError) as exc:
        ctx.emit(f"Error collecting process resources: {exc}")


def deps(ctx, arg):
    """Check dependencies."""
    if arg.strip():
        ctx.emit("Usage: deps")
        return
    native = ctx.host.engine.native_dependencies
    for name, ok in [("native audio (JUCE/PortAudio)", native["native"]),
                     ("aalink", HAS_LINK), ("python-rtmidi", HAS_RTMIDI),
                     ("mido", HAS_MIDO)]:
        ctx.emit(f"  {name}: {'OK' if ok else 'MISSING'}")
