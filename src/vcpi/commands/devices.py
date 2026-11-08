"""Devices command handlers."""

import math
from vcpi.midi import list_midi_input_ports, list_midi_output_ports
from vcpi.presentation.status import format_output_device, format_realtime
from .context import track_index, channel_index


def audio(ctx, arg):
    """Audio commands: audio start [device] | audio stop | audio devices | audio diagnose

    Subcommands:
      audio start [device]  -- start audio engine
      audio stop            -- stop audio engine
      audio devices         -- list available output devices
      audio diagnose        -- inspect MIDI delivery and lifetime signal peaks
    """
    parts = ctx.tokens(arg, "audio start [device] | audio stop | audio devices | audio diagnose")
    if parts is None:
        return
    if not parts:
        ctx.emit("Usage: audio start [device] | audio stop | audio devices | audio diagnose")
        return

    sub = parts[0]
    if (sub == "start" and len(parts) > 2) or (sub != "start" and len(parts) != 1):
        ctx.emit("Usage: audio start [device] | audio stop | audio devices | audio diagnose")
        return

    if sub == "start":
        dev = " ".join(parts[1:]) if len(parts) > 1 else None
        if dev and dev.isdigit():
            dev = int(dev)
        try:
            ctx.host.start_audio(dev)
            engine = ctx.host.engine
            device = format_output_device(engine.audio_output_name, engine.audio_output_index)
            ctx.emit(f"  Audio started: {device} | backend={engine.backend_label}")
        except Exception as e:
            ctx.emit(f"Error: {e}")

    elif sub == "stop":
        ctx.host.stop_audio()

    elif sub == "devices":
        devices = ctx.host.engine.audio_devices
        if not devices:
            ctx.emit("  No audio devices available.")
        for device in devices:
            engine = ctx.host.engine
            selected = device["index"] == engine.audio_output_index
            marker = (" [active]" if engine.running else " [last opened]") if selected else ""
            ctx.emit(
                f"  [{device['index']}] {device['name']} "
                f"({device['max_input_channels']} in, "
                f"{device['max_output_channels']} out; {device['hostapi_name']}){marker}"
            )

    elif sub == "diagnose":
        try:
            diagnosis = ctx.host.engine.request("diagnose")
        except Exception as e:
            ctx.emit(f"Error: {e}")
            return
        output = diagnosis["output"]
        ctx.emit(
            f"  Output: {format_output_device(output['output_name'], output['output_index'])} "
            f"| backend={output['backend_label']} "
            f"| running={output['running']} "
            f"| master gain={output['master_gain']:.3f}"
        )
        ctx.emit(f"  Output realtime: {format_realtime(output['realtime'])}")
        ctx.emit(
            f"  Callbacks={output['callback_count']} "
            f"| render errors={output['render_errors']} "
            f"| underruns={output['underruns']} "
            f"| deadline misses={output['deadline_misses']}"
        )
        ctx.emit("  Peaks are absolute-sample high-water marks, not current levels.")
        ctx.emit(
            f"  Output peak since audio-owner creation "
            f"(final device samples): {output['output_peak']:.6g}"
        )
        if not diagnosis["tracks"]:
            ctx.emit("  No loaded tracks.")
        for track in diagnosis["tracks"]:
            ctx.emit(
                f"  Track {track['index']}: {track['name']} "
                f"| gain={track['gain']:.3f} "
                f"| muted={track['muted']} | solo={track['solo']}"
            )
            ctx.emit(
                f"    MIDI sent={track['midi_sent']} "
                f"consumed={track['midi_consumed']} pending={track['midi_pending']} "
                f"| missing blocks={track['misses']}"
            )
            ctx.emit(
                f"    Audio peak since track creation "
                f"(before mixer gain/mute/solo): {track['audio_peak']:.6g}"
            )
            ctx.emit(f"    Worker failure: {track['worker_failure'] or 'none'}")
            ctx.emit(f"    Realtime: {format_realtime(track['realtime'])}")

    else:
        ctx.emit(f"Unknown audio subcommand: {sub}")


def midi(ctx, arg):
    """MIDI commands: midi ports input | midi ports output | midi input <port> | midi input close <index> | midi link <ch> <track> | midi cut <ch>

    Subcommands:
      midi ports input       -- list MIDI input ports
      midi ports output      -- list MIDI output ports
      midi input <port>      -- open a MIDI input port
      midi input close <idx> -- close a MIDI input by index
      midi link <ch> <track>  -- route MIDI channel to track
      midi cut <ch>          -- remove MIDI channel route
    """
    parts = ctx.tokens(arg, "midi ports input|output | midi input <port> | midi input close <number> | midi link <ch> <track> | midi cut <ch>")
    if parts is None:
        return
    if not parts:
        ctx.emit(
            "Usage: midi ports input | midi ports output | "
            "midi input <port> | midi input close <index> | "
            "midi link <ch> <track> | midi cut <ch>"
        )
        return

    sub = parts[0].lower()

    # --- midi ports input / midi ports output ---------------------------
    if sub == "ports":
        if len(parts) != 2:
            ctx.emit("Usage: midi ports input | midi ports output")
            return
        direction = parts[1].lower()
        if direction == "input":
            ports = list_midi_input_ports()
            if not ports:
                ctx.emit("  No MIDI input ports found.")
                return
            for i, name in enumerate(ports):
                ctx.emit(f"  [{i}] {name}")
        elif direction == "output":
            ports = list_midi_output_ports()
            if not ports:
                ctx.emit("  No MIDI output ports found.")
                return
            for i, name in enumerate(ports):
                ctx.emit(f"  [{i}] {name}")
        else:
            ctx.emit("Usage: midi ports input | midi ports output")
        return

    # --- midi input <port> / midi input close <index> -------------------
    if sub == "input":
        if len(parts) < 2:
            ctx.emit("Usage: midi input <port> | midi input close <index>")
            return
        if parts[1].lower() == "close":
            if len(parts) != 3:
                ctx.emit("Usage: midi input close <index>")
                return
            try:
                idx = int(parts[2]) - 1
                if not 0 <= idx < len(ctx.host.midi_inputs):
                    raise ValueError("active input number must be 1 through the number of open inputs")
                ctx.host.close_midi_input(idx)
                ctx.emit("  Closed.")
            except Exception as e:
                ctx.emit(f"Error: {e}")
        else:
            if len(parts) != 2:
                ctx.emit("Usage: midi input <port index|name>; quote names containing spaces")
                return
            try:
                ctrl = ctx.host.open_midi_input(parts[1])
                ctx.emit(f"  Opened: {ctrl.port_name}")
            except Exception as e:
                ctx.emit(f"Error: {e}")
        return

    # --- midi link <ch> <track> ------------------------------------------
    if sub == "link":
        if len(parts) != 3:
            ctx.emit("Usage: midi link <channel 1-16> <track 1-16>")
            return
        try:
            ch = channel_index(int(parts[1]))
            idx = track_index(int(parts[2]))
        except ValueError as e:
            ctx.emit(f"Error: {e}")
            return
        ctx.host.route(ch, idx)
        ctx.emit(f"  ch {parts[1]} -> track {parts[2]}")
        return

    # --- midi cut <ch> --------------------------------------------------
    if sub == "cut":
        if len(parts) != 2:
            ctx.emit("Usage: midi cut <channel 1-16>")
            return
        try:
            ch = channel_index(int(parts[1]))
        except ValueError as e:
            ctx.emit(f"Error: {e}")
            return
        ctx.host.unroute(ch)
        ctx.emit(f"  ch {parts[1]} unlinked")
        return

    ctx.emit(
        "Unknown midi subcommand. Use: ports, input, link, cut"
    )


def midimix(ctx, arg):
    """Open MIDI Mix control input or LED feedback output."""
    parts = ctx.tokens(arg, "midimix input|output <port index|name>")
    if parts is None:
        return
    if len(parts) != 2 or parts[0].lower() not in ("input", "output"):
        ctx.emit("Usage: midimix input|output <port index|name>")
        return
    try:
        port = int(parts[1]) if parts[1].isdigit() else parts[1]
        if parts[0].lower() == "input":
            ctx.host.open_mixer_midi(port)
        else:
            ctx.host.open_mixer_midi_out(port)
    except Exception as error:
        ctx.emit(f"Error: {error}")


def beatstep(ctx, arg):
    """Connect the supported BeatStep Pro controller without MIDI learn."""
    usage = "beatstep [input [port index|name]|close]"
    parts = ctx.tokens(arg, usage)
    if parts is None:
        return
    try:
        if not parts:
            ctx.emit(f"  BeatStep Pro: {ctx.host.beatstep.input_port_name or 'closed'}")
        elif parts == ["close"]:
            ctx.host.close_beatstep_midi()
            ctx.emit("  BeatStep Pro closed; automatic reconnection disabled.")
        elif parts[0] == "input" and len(parts) <= 2:
            ctx.host.open_beatstep_midi(parts[1] if len(parts) == 2 else None)
            ctx.emit(f"  BeatStep Pro opened: {ctx.host.beatstep.input_port_name}")
        else:
            raise ValueError(usage)
    except Exception as error:
        ctx.emit(f"Error: {error}")


def note(ctx, arg):
    """Send a timed MIDI test note."""
    usage = "note <track 1-16> <midi_note> [--velocity <0..127>] [--duration-ms <milliseconds>]"
    parts = ctx.tokens(arg, usage)
    if parts is None:
        return
    try:
        if len(parts) < 2:
            raise ValueError("track and MIDI note are required")
        idx = track_index(int(parts[0]))
        note = int(parts[1])
        options = {}
        for offset in range(2, len(parts), 2):
            flag = parts[offset]
            if flag not in ("--velocity", "--duration-ms") or flag in options or offset + 1 == len(parts):
                raise ValueError("expected each note option once with one value")
            options[flag] = parts[offset + 1]
        velocity = int(options.get("--velocity", "100"))
        duration = float(options.get("--duration-ms", "300"))
        if not 0 <= note <= 127 or not 0 <= velocity <= 127:
            raise ValueError("MIDI note and velocity must be integers from 0 to 127")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration-ms must be a positive finite number")
        ctx.host.send_note(idx, note, velocity, duration / 1000.0)
    except (ValueError, OverflowError) as error:
        ctx.emit(f"Error: {error}")
        ctx.emit(f"Usage: {usage}")
