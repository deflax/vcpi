"""Full signal-flow ASCII graph for vcpi.

Renders all 8 slots with MIDI routing, instrument, per-slot FX chains,
gain/mute/solo state, master effects, and master gain in a single diagram.
"""

from __future__ import annotations

from pathlib import Path

from core.models import NUM_SLOTS


def _plugin_name(plugin) -> str:
    """Best-effort short name for a pedalboard plugin."""
    name = getattr(plugin, "name", None)
    if name:
        return name
    path = getattr(plugin, "path_to_plugin_file", None)
    if path:
        return Path(path).stem
    return type(plugin).__name__


def _fx_chain_str(effects: list) -> str:
    if not effects:
        return ""
    names = [_plugin_name(fx) for fx in effects]
    return " -> ".join(names)


def _gain_bar(gain: float, width: int = 10) -> str:
    """Tiny horizontal bar for gain level."""
    filled = round(gain * width)
    filled = max(0, min(width, filled))
    return "#" * filled + "-" * (width - filled)


def render_signal_flow(engine, channel_map: dict) -> str:
    """Return an ASCII signal-flow diagram of the entire mixer.

    Parameters
    ----------
    engine : AudioEngine
        The running audio engine (provides slots, master_effects, master_gain).
    channel_map : dict
        MIDI channel (0-15) -> slot index (0-7) routing map.
    """

    # -- build reverse map: slot_index -> sorted MIDI channels ---------------
    routes_by_slot: dict[int, list[int]] = {}
    for ch, slot_idx in channel_map.items():
        routes_by_slot.setdefault(slot_idx, []).append(ch)
    for chs in routes_by_slot.values():
        chs.sort()

    any_solo = engine.any_solo()

    # -- collect row data for each slot --------------------------------------
    #
    # Each slot row looks like:
    #   [S01] ch01,ch02 -> Dexed -> DragonflyHall -> Delay  gain [####------] 0.40  M S
    #                      ^inst    ^----- fx chain -----^
    #
    # Empty slots:
    #   [S01] (empty)

    slot_lines: list[str] = []
    for i in range(NUM_SLOTS):
        slot = engine.slots[i]
        num = i + 1

        if slot is None:
            slot_lines.append(f"  [S{num}] (empty)")
            continue

        # MIDI channels routed to this slot
        chs = routes_by_slot.get(i, [])
        ch_str = ",".join(f"ch{c + 1:02d}" for c in chs) if chs else "---"

        # Instrument name
        inst_name = _plugin_name(slot.plugin)

        # FX chain
        fx_str = _fx_chain_str(slot.effects)

        # Signal chain: inst (-> fx1 -> fx2 ...)
        chain = inst_name
        if fx_str:
            chain += f" -> {fx_str}"

        # Audibility
        audible = (not slot.muted) and (not any_solo or slot.solo)

        # Flags
        flags = ""
        if slot.muted:
            flags += " M"
        if slot.solo:
            flags += " S"

        aud_mark = " " if audible else "x"
        bar = _gain_bar(slot.gain)

        slot_lines.append(
            f"  [{aud_mark}S{num}] {ch_str:<12} -> {chain}"
        )
        slot_lines.append(
            f"         gain [{bar}] {slot.gain:.2f}{flags}"
        )

    # -- master section ------------------------------------------------------
    master_lines: list[str] = []
    if engine.master_effects:
        fx_str = _fx_chain_str(engine.master_effects)
        master_lines.append(f"  Master FX: {fx_str}")

    master_bar = _gain_bar(engine.master_gain)
    master_lines.append(
        f"  Master   : [{master_bar}] {engine.master_gain:.2f}"
    )

    # -- compose the box -----------------------------------------------------
    title = "vcpi Signal Flow"
    all_content = slot_lines + [""] + master_lines

    body_width = max(len(title), max(len(ln) for ln in all_content))
    border = "+" + "-" * (body_width + 2) + "+"

    lines = [
        border,
        f"| {title:^{body_width}} |",
        border,
    ]
    for ln in all_content:
        lines.append(f"| {ln:<{body_width}} |")
    lines.append(border)

    return "\n".join(lines)
