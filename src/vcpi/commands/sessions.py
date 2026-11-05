"""Sessions command handlers."""

from . import assets


def save(ctx, arg):
    """Save session: save [<name>]

    Saves the current session to sessions/<name>.json.
    If no name is given, saves to the previously loaded session.
    The automatic session at ~/.config/vcpi/session.json is only
    written on shutdown.
    """
    parts = ctx.tokens(arg, "save [name]")
    if parts is None:
        return
    if len(parts) > 1:
        ctx.emit("Usage: save [name]; quote names containing spaces")
        return
    name = parts[0] if parts else ""
    if not name:
        if ctx.host.loaded_session_path is None:
            ctx.emit("Usage: save <name>  (no session loaded)")
            return
        path = ctx.host.loaded_session_path
    else:
        path = assets.resolve_session_path(name)
    try:
        ctx.host.save_session(str(path))
        ctx.emit(f"  Saved to {path}")
    except Exception as e:
        ctx.emit(f"Error: {e}")


def load(ctx, arg):
    """Load session: load <name>

    Loads from sessions/<name>.json and sets it as the active
    session (shown in ``status``).  The auto-save file at
    ~/.config/vcpi/session.json is updated so that shutdown
    preserves the loaded state, but the named session file is
    only written when you explicitly run ``save``.
    """
    parts = ctx.tokens(arg, "load <name>")
    if parts is None:
        return
    if len(parts) != 1 or not parts[0]:
        ctx.emit("Usage: load <name>; quote names containing spaces")
        return
    name = parts[0]
    path = assets.resolve_session_path(name)
    if not path.exists():
        ctx.emit(f"Error: session not found: {path}")
        return
    try:
        ctx.host.restore_session(str(path))
        ctx.host.refresh_mixer_leds()
        # Track the loaded session for save-without-args and status.
        ctx.host.loaded_session_name = name
        ctx.host.loaded_session_path = path
        # Update the auto-save session so shutdown preserves this state.
        ctx.host.save_session()
        ctx.emit(f"  Loaded from {path}")
    except Exception as e:
        ctx.emit(f"Error: {e}")
