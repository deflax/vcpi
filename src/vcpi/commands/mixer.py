"""Mixer command handlers."""

from .context import track_index


def focus(ctx, arg):
    """Read or select the shared focused track."""
    parts = ctx.tokens(arg, "focus [1-16]")
    if parts is None:
        return
    if len(parts) > 1:
        ctx.emit("Usage: focus [1-16]")
        return
    try:
        if parts:
            ctx.host.track_focus.select(track_index(int(parts[0])))
        index, _ = ctx.host.track_focus.snapshot()
        ctx.emit(f"Focused track: {index + 1}")
    except Exception as error:
        ctx.emit(f"Error: {error}")


def gain(ctx, arg):
    """Read or set track/master gain."""
    parts = ctx.tokens(arg, "gain <track 1-16|master> [value]")
    if parts is None:
        return
    if not 1 <= len(parts) <= 2:
        ctx.emit("Usage: gain <track 1-16|master> [value]")
        return
    try:
        idx = None if parts[0] == "master" else track_index(int(parts[0]))
        track = None if idx is None else ctx.host.engine.tracks[idx]
        if idx is not None and track is None:
            raise ValueError(f"track {parts[0]} is empty")
        if len(parts) == 2:
            gain = float(parts[1])
            if not 0.0 <= gain <= 1.0:
                raise ValueError("gain must be between 0.0 and 1.0")
            if idx is None:
                ctx.host.engine.set_master_gain(gain)
            else:
                track = ctx.host.engine.set_track_state(idx, gain=gain)
        gain = ctx.host.engine.master_gain if idx is None else track.gain
        ctx.emit(f"  {parts[0]} gain = {gain:.2f}")
    except Exception as error:
        ctx.emit(f"Error: {error}")


def _switch_track(ctx, arg, command, field):
    usage = f"{command} <track 1-16> [on|off|toggle]"
    parts = ctx.tokens(arg, usage)
    if parts is None:
        return
    if not 1 <= len(parts) <= 2 or (len(parts) == 2 and parts[1] not in ("on", "off", "toggle")):
        ctx.emit(f"Usage: {usage}")
        return
    try:
        idx = track_index(int(parts[0]))
        track = ctx.host.engine.tracks[idx]
        if track is None:
            raise ValueError(f"track {parts[0]} is empty")
        mode = parts[1] if len(parts) == 2 else "toggle"
        value = not getattr(track, field) if mode == "toggle" else mode == "on"
        track = ctx.host.engine.set_track_state(idx, **{field: value})
        ctx.host.refresh_mixer_leds([idx])
        ctx.emit(f"  {track.name}: {command} {'on' if getattr(track, field) else 'off'}")
    except Exception as error:
        ctx.emit(f"Error: {error}")


def mute(ctx, arg):
    """Set or toggle track mute."""
    _switch_track(ctx, arg, "mute", "muted")


def solo(ctx, arg):
    """Set or toggle track solo."""
    _switch_track(ctx, arg, "solo", "solo")
