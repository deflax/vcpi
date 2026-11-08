"""Track source, deck transport, and effect command handlers."""

from pathlib import Path

from . import assets
from .context import track_index, source_args, parse_named_source
from .help import HELP_TOPICS


TRACK_TYPES = ("vst", "wav", "deck", "vcv", "pd", "fx", "clear")


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
    handlers = {"deck": deck, "vst": vst, "vcv": vcv, "pd": pd, "wav": wav}
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
            raise ValueError("expected audio path, remove <index>, status, reanalyze, seek <seconds>, cue [set], or phase reset")
        action = tokens[0]
        if action in ("status", "reanalyze", "seek", "cue", "phase", "remove"):
            track = ctx.host.engine.tracks[idx]
            if track is None or track.source_type != "deck":
                raise ValueError(f"Track {track_num} is not a deck")
            if tokens == ["status"]:
                print_deck_status(ctx, track)
            elif tokens == ["reanalyze"]:
                ctx.host.engine.deck_command(idx, "reanalyze")
                ctx.emit("  Reanalysis accepted; deck paused at its current position. "
                         f"Check track {track_num} deck status for completion.")
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
            elif action == "remove" and len(tokens) == 2:
                track = ctx.host.remove_deck_entry(idx, int(tokens[1]))
                print_deck_status(ctx, track)
            else:
                raise ValueError("expected status, reanalyze, remove <index>, seek <seconds>, cue [set], or phase reset")
        else:
            sources, name = source_args(tokens, 1)
            track = ctx.host.load_deck(
                idx, str(Path(sources[0]).expanduser()), name)
            ctx.emit(f"  track {track_num} = {track.name}")
            print_deck_status(ctx, track)
    except Exception as error:
        ctx.emit(f"Error: {error}")
    return


def vst(ctx, track_num, parts, text):
    idx = track_index(track_num)
    rest = text.split(maxsplit=2)
    if len(rest) < 3:
        ctx.emit(f"Usage: track <1-16> vst <path|vst_name> [--name <label>]")
        return
    try:
        path_token, name = parse_named_source(rest[2])
        path = assets.resolve_vst_token(path_token)
        track = ctx.host.load_instrument(idx, path, name)
        ctx.emit(f"  track {track_num} = {track.name}")
        ctx.emit(f"  vst      : {track.path}")
    except Exception as e:
        ctx.emit(f"Error: {e}")
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


def wav(ctx, track_num, parts, text):
    idx = track_index(track_num)
    try:
        sources, display_name = source_args(parts[2:], 2)
    except ValueError as error:
        ctx.emit(f"Error: {error}")
        ctx.emit("Usage: track <1-16> wav <pack> <sample> [--name <label>]")
        return
    pack_name = sources[0].strip().strip("/")
    sample_name = sources[1].strip()

    if not pack_name:
        ctx.emit("Error: pack name is required")
        return
    if not sample_name:
        ctx.emit("Error: sample name is required")
        return

    pack_path = Path(pack_name)
    if pack_path.is_absolute() or ".." in pack_path.parts:
        ctx.emit("Error: invalid pack name")
        return

    wav_file = sample_name if sample_name.lower().endswith(".wav") else f"{sample_name}.wav"
    sample_path = Path(wav_file)
    if sample_path.is_absolute() or ".." in sample_path.parts:
        ctx.emit("Error: invalid sample name")
        return

    wav_path = assets.samples_root() / pack_path / sample_path

    try:
        track = ctx.host.load_wav(idx, str(wav_path), display_name)
    except Exception as e:
        ctx.emit(f"Error: {e}")
        return

    ctx.emit(f"  track {track_num} = {track.name}")
    ctx.emit(f"  wav      : {track.path}")
    ctx.emit(f"  route with: midi link <channel 1-16> {track_num}")
    return


def print_deck_status(ctx, track):
    """Render only the daemon snapshot, never synchronizing with a worker."""
    status = track.deck
    ctx.emit(f"  playlist_id = {status.get('bank_id', '(not cached)')}")
    for key in ("track_id", "track_title",
                "requested_track_id", "request_id", "state", "play",
                "position", "duration", "cue", "pitch", "nudge", "phase_offset", "bpm",
                "beat_offset", "beat_confidence", "sync", "sync_active",
                "sync_error", "playback_speed", "beat_phase_error", "error"):
        ctx.emit(f"  {key} = {status.get(key, '(not cached)')}")
    for ordinal, entry in enumerate(status.get("tracks", []), 1):
        ctx.emit(f"  {ordinal}: {entry.get('title') or entry['id']} — {entry['path']}")

