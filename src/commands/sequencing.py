"""Sequencing command handlers."""

from vcpi.sequencer import NUM_SEQ_BANKS, midi_to_note_name
from vcpi.link import validate_bpm
from .context import track_index


def seq(ctx, arg):
    """Sequencer: seq | seq <bank> <notes...> | seq link <bank> <track> | seq cut <track> | seq clear <bank>

    Examples:
      seq                  -- show all sequence banks
      seq 1 d c b a        -- set bank 1 to D C B A (4 notes per bar)
      seq 1 c              -- set bank 1 to just C (1 note per bar)
      seq 3 C#5 Bb4        -- sharps/flats and octave suffixes work
      seq clear 1          -- clear bank 1
      seq link 1 5         -- attach sequence bank 1 to track 5
      seq cut 5            -- remove sequences from track 5
    """
    parts = ctx.tokens(arg, "seq [bank [notes...]] | seq link <bank> <track> | seq cut <track> | seq clear <bank>")
    if parts is None:
        return

    # No args: display all banks
    if not parts:
        seq = ctx.host.sequencer
        any_bank = False
        for bi, bank in enumerate(seq.banks):
            if bank is None:
                continue
            any_bank = True
            notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
            if bank.linked_track is not None:
                link_str = f" -> track {bank.linked_track + 1}"
            else:
                link_str = ""
            ctx.emit(f"  seq {bi + 1}: {notes_str}{link_str}")
        if not any_bank:
            ctx.emit("  No sequences defined. Use: seq <bank> <note> [note ...]")
        return

    # --- seq link <bank> <track> -----------------------------------------
    if parts[0].lower() == "link":
        if len(parts) != 3:
            ctx.emit("Usage: seq link <bank 1-16> <track 1-8>")
            return
        try:
            bi = int(parts[1]) - 1
            if not 0 <= bi < NUM_SEQ_BANKS:
                raise ValueError(f"bank must be 1-{NUM_SEQ_BANKS}")
        except ValueError as e:
            ctx.emit(f"Error: {e}")
            return
        try:
            si = track_index(int(parts[2]))
        except ValueError as e:
            ctx.emit(f"Error: {e}")
            return
        try:
            ctx.host.sequencer.link(bi, si)
            ctx.emit(f"  seq {bi + 1} -> track {si + 1}")
        except ValueError as e:
            ctx.emit(f"Error: {e}")
        return

    # --- seq cut <track> -------------------------------------------------
    if parts[0].lower() == "cut":
        if len(parts) != 2:
            ctx.emit("Usage: seq cut <track 1-8>")
            return
        try:
            si = track_index(int(parts[1]))
        except ValueError as e:
            ctx.emit(f"Error: {e}")
            return
        ctx.host.sequencer.detach_track(si)
        ctx.emit(f"  track {si + 1}: sequences detached")
        return

    # --- seq clear <bank> -----------------------------------------------
    if parts[0].lower() == "clear":
        if len(parts) != 2:
            ctx.emit(f"Usage: seq clear <bank 1-{NUM_SEQ_BANKS}>")
            return
        try:
            bank_num = int(parts[1])
            bi = bank_num - 1
            if not 0 <= bi < NUM_SEQ_BANKS:
                raise ValueError
        except ValueError:
            ctx.emit(f"Error: bank must be 1-{NUM_SEQ_BANKS}")
            return
        ctx.host.sequencer.clear_bank(bi)
        ctx.emit(f"  seq {bank_num}: cleared")
        return

    # --- remaining forms need a bank number as first arg ----------------
    try:
        bank_num = int(parts[0])
        bi = bank_num - 1
        if not 0 <= bi < NUM_SEQ_BANKS:
            raise ValueError
    except ValueError:
        ctx.emit(f"Error: bank must be 1-{NUM_SEQ_BANKS}")
        return

    # seq <bank> -- show single bank
    if len(parts) == 1:
        bank = ctx.host.sequencer.banks[bi]
        if bank is None:
            ctx.emit(f"  seq {bank_num}: (empty)")
        else:
            notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
            if bank.linked_track is not None:
                link_str = f" -> track {bank.linked_track + 1}"
            else:
                link_str = ""
            ctx.emit(f"  seq {bank_num}: {notes_str}{link_str}")
        return

    # seq <bank> <note> [note ...]
    note_names = parts[1:]
    try:
        bank = ctx.host.sequencer.set_bank(bi, note_names)
    except ValueError as e:
        ctx.emit(f"Error: {e}")
        return

    notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
    n = len(bank.notes)
    if n == 1:
        timing = "1 note/bar"
    else:
        timing = f"{n} notes/bar (1/{n} spacing)"
    ctx.emit(f"  seq {bank_num}: {notes_str}  ({timing})")


def ableton(ctx, arg):
    """Ableton Link commands: ableton link [bpm] | ableton cut

    Subcommands:
      ableton link [bpm]  -- enable Ableton Link (optionally set BPM)
      ableton cut         -- disable Ableton Link
    """
    parts = ctx.tokens(arg, "ableton link [bpm] | ableton cut")
    if parts is None:
        return
    if not parts:
        ctx.emit("Usage: ableton link [bpm] | ableton cut")
        return

    sub = parts[0]
    if (sub == "link" and len(parts) > 2) or (sub != "link" and len(parts) != 1):
        ctx.emit("Usage: ableton link [bpm] | ableton cut")
        return

    if sub == "link":
        if len(parts) > 1:
            try:
                bpm = validate_bpm(parts[1])
            except ValueError as error:
                ctx.emit(f"Error: {error}")
                return
        else:
            bpm = None
        try:
            ctx.host.start_link(bpm)
            ctx.emit(f"  Link enabled at {ctx.host.link.bpm:.1f} BPM")
        except Exception as e:
            ctx.emit(f"Error: {e}")

    elif sub == "cut":
        ctx.host.stop_link()
        ctx.emit("  Link disabled")

    else:
        ctx.emit(f"Unknown ableton subcommand: {sub}")


def tempo(ctx, arg):
    """Get/set tempo: tempo [bpm]"""
    parts = ctx.tokens(arg, "tempo [bpm]")
    if parts is None:
        return
    if len(parts) > 1:
        ctx.emit("Usage: tempo [bpm]")
        return
    if parts:
        try:
            ctx.host.link.bpm = validate_bpm(parts[0])
        except ValueError as error:
            ctx.emit(f"Error: {error}")
            return
    ctx.emit(f"  {ctx.host.link.bpm:.1f} BPM")
