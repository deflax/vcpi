"""Track source, deck transport, and effect command handlers."""

from pathlib import Path

from . import assets
from .context import track_index, source_args, parse_named_source
from .help import HELP_TOPICS


TRACK_TYPES = ("deck", "vcv", "pd", "fx", "clear")


def track_usage():
    return (HELP_TOPICS["track"][1]
            + "\nUse 'help track <type>' for source-specific options and examples.")


def track(ctx, arg):
    """Load or clear track sources and effects, or control a deck."""
    text = arg.strip()
    if not text:
        ctx.emit(f"Usage: {track_usage()}")
        return

    parts = ctx.tokens(text, track_usage())
    if parts is None:
        return
    target = parts[0]

    # Determine track index (or None for master).
    if target == "master":
        track_num = None
    else:
        try:
            track_num = int(target)
            track_index(track_num)  # validate
        except ValueError as e:
            ctx.emit(f"Error: {e}")
            ctx.emit(f"Usage: {track_usage()}")
            return

    if len(parts) < 2:
        ctx.emit(f"Usage: {track_usage()}")
        return

    mode = parts[1].lower()

    if mode == "clear" and track_num is not None:
        return clear_source(ctx, track_num, parts)
    if mode == "fx":
        return effect(ctx, track_num, parts, text)
    if track_num is None:
        ctx.emit("Error: master only supports 'fx' subcommand")
        ctx.emit(f"Usage: {track_usage()}")
        return
    handlers = {"deck": deck, "vcv": vcv, "pd": pd}
    handler = handlers.get(mode)
    if handler is None:
        ctx.emit(f"Error: unknown subcommand '{mode}'")
        ctx.emit(f"Usage: {track_usage()}")
        return
    return handler(ctx, track_num, parts, text)


def clear_source(ctx, track_num, parts):
    if len(parts) != 2:
        ctx.emit("Usage: track <1-16> clear")
        return
    idx = track_index(track_num)
    try:
        removed = ctx.host.remove_instrument(idx)
        ctx.host.refresh_mixer_leds([idx])
        ctx.emit(f"  track {track_num} cleared ({removed.name})")
    except Exception as e:
        ctx.emit(f"Error: {e}")
    return


def effect(ctx, track_num, parts, text):
    if len(parts) < 3:
        ctx.emit(f"Usage: track <1-16|master> fx <path|vst_name> [--name <label>] | track <1-16|master> fx clear <fx_index>")
        return

    track_idx = None if track_num is None else track_index(track_num)

    # track <num|master> fx clear <fx_index>
    if parts[2].lower() == "clear":
        if len(parts) != 4:
            ctx.emit(f"Usage: track <1-16|master> fx clear <fx_index>")
            return
        try:
            fx_idx = int(parts[3]) - 1
        except ValueError:
            ctx.emit("Error: fx_index must be a positive integer")
            return
        if fx_idx < 0:
            ctx.emit("Error: fx_index must be >= 1")
            return
        try:
            ctx.host.remove_effect(track_idx, fx_idx)
            ctx.emit("  Removed.")
        except Exception as e:
            ctx.emit(f"Error: {e}")
        return

    # track <num|master> fx <path|vst_name> [--name <label>]
    try:
        path_token, name = parse_named_source(text.split(maxsplit=2)[2])
        path = assets.resolve_vst_token(path_token)
    except ValueError as e:
        ctx.emit(f"Error: {e}")
        return
    try:
        ctx.host.load_effect(path, track_idx, name)
    except Exception as e:
        ctx.emit(f"Error: {e}")
    return


def deck(ctx, track_num, parts, text):
    idx = track_index(track_num)
    try:
        tokens = parts[2:]
        if not tokens:
            raise ValueError("expected audio path, reanalyze, seek <seconds>, cue [set], or phase reset")
        action = tokens[0]
        if action in ("reanalyze", "seek", "cue", "phase"):
            track = ctx.host.engine.tracks[idx]
            if track is None or track.source_type != "deck":
                raise ValueError(f"Track {track_num} is not a deck")
            if tokens == ["reanalyze"]:
                ctx.host.engine.deck_command(idx, "reanalyze")
                ctx.emit("  Reanalysis accepted; deck paused at its current position. "
                         f"Check show {track_num} for completion.")
            elif tokens == ["phase", "reset"]:
                ctx.host.engine.deck_command(idx, "phase_reset")
                ctx.emit("  phase offset reset accepted")
            elif action == "seek" and len(tokens) == 2:
                ctx.host.engine.deck_command(idx, "seek", float(tokens[1]))
                ctx.emit("  seek accepted")
            elif tokens in (["cue"], ["cue", "set"]):
                command = "cue_set" if len(tokens) == 2 else "cue"
                ctx.host.engine.deck_command(idx, command)
                ctx.emit(f"  {command} accepted")
            else:
                raise ValueError("expected reanalyze, seek <seconds>, cue [set], or phase reset")
        else:
            if tokens.count("--no-sync-filter") > 1:
                raise ValueError("--no-sync-filter may only be supplied once")
            sync_filter = "--no-sync-filter" not in tokens
            bpm_hint = None
            if "--bpm-hint" in tokens:
                if tokens.count("--bpm-hint") != 1:
                    raise ValueError("--bpm-hint may only be supplied once")
                marker = tokens.index("--bpm-hint")
                if marker + 1 >= len(tokens) or tokens[marker + 1].startswith("--"):
                    raise ValueError("--bpm-hint requires an expected median BPM (60–200)")
                try:
                    bpm_hint = float(tokens[marker + 1])
                except ValueError:
                    raise ValueError("--bpm-hint requires an expected median BPM (60–200)") from None
                tokens = tokens[:marker] + tokens[marker + 2:]
            tokens = [token for token in tokens if token != "--no-sync-filter"]
            sources, name = source_args(tokens, 1)
            track = ctx.host.load_deck(
                idx, str(Path(sources[0]).expanduser()), name,
                bpm_hint=bpm_hint, sync_filter=sync_filter)
            ctx.emit(f"  track {track_num} = {track.name}")
            ctx.emit(f"  Use show {track_num} for playback state or "
                     f"show {track_num} --details for the playlist.")
    except Exception as error:
        ctx.emit(f"Error: {error}")
    return


def vcv(ctx, track_num, parts, text):
    idx = track_index(track_num)
    try:
        sources, name = source_args(parts[2:], 1)
        patch_name = sources[0]
        track, patch_result, cardinal_path, patch_path = ctx.host.load_vcv_patch(
            idx, patch_name, name=name,
        )
    except Exception as e:
        ctx.emit(f"Error: {e}")
        return
    ctx.emit(f"  track {track_num} = {track.name}")
    ctx.emit(f"  patch    : {patch_path}")
    ctx.emit(f"  cardinal : {cardinal_path}")
    ctx.emit(f"  result   : {patch_result}")
    ctx.emit(f"  route with: midi link <channel 1-16> {track_num}")
    return


def pd(ctx, track_num, parts, text):
    idx = track_index(track_num)
    rest = text.split(maxsplit=2)
    if len(rest) < 3:
        ctx.emit("Usage: track <1-16> pd <patch_name[.pd]> [--name <label>]")
        return
    try:
        patch_name, name = parse_named_source(rest[2])
        track, patch_result, plugdata_path, patch_path = ctx.host.load_pd_patch(
            idx, patch_name, name=name,
        )
    except Exception as e:
        ctx.emit(f"Error: {e}")
        return
    ctx.emit(f"  track {track_num} = {track.name}")
    ctx.emit(f"  patch    : {patch_path}")
    ctx.emit(f"  plugdata : {plugdata_path}")
    ctx.emit(f"  result   : {patch_result}")
    ctx.emit(f"  route with: midi link <channel 1-16> {track_num}")
    return
