"""Route graph rendering for vcpi."""

from __future__ import annotations


def render_route_graph(host) -> str:
    """Return an ASCII diagram of current MIDI channel routes."""
    title = "vcpi Route Graph"
    routes = dict(host.channel_map)

    if not routes:
        width = max(len(title), len("No active routes."))
        border = "+" + "-" * (width + 2) + "+"
        lines = [
            border,
            f"| {title:^{width}} |",
            border,
            f"| {'No active routes.':<{width}} |",
            border,
        ]
        return "\n".join(lines)

    routes_by_slot: dict[int, list[int]] = {}
    for channel, slot_index in routes.items():
        routes_by_slot.setdefault(slot_index, []).append(channel)

    rows: list[tuple[str, str]] = []
    for slot_index in sorted(routes_by_slot):
        channels = sorted(routes_by_slot[slot_index])
        left = ", ".join(f"ch{ch + 1:02d}" for ch in channels)

        slot = host.engine.slots[slot_index]
        slot_name = slot.name if slot else "(empty)"
        right = f"[S{slot_index + 1:02d}] {slot_name}"
        if slot:
            flags = "".join(
                flag for flag, enabled in (("M", slot.muted), ("S", slot.solo)) if enabled
            )
            if flags:
                right += f" ({flags})"

        rows.append((left, right))

    left_width = max(len(left) for left, _ in rows)
    right_width = max(len(right) for _, right in rows)
    row_template = f"{{left:<{left_width}}} -> {{right:<{right_width}}}"

    unrouted = [f"ch{ch:02d}" for ch in range(1, 17) if (ch - 1) not in routes]
    unrouted_text = ", ".join(unrouted) if unrouted else "(none)"

    body_width = max(
        len(title),
        max(len(row_template.format(left=left, right=right)) for left, right in rows),
        len(f"Unrouted: {unrouted_text}"),
    )
    border = "+" + "-" * (body_width + 2) + "+"

    lines = [
        border,
        f"| {title:^{body_width}} |",
        border,
    ]
    for left, right in rows:
        lines.append(f"| {row_template.format(left=left, right=right):<{body_width}} |")
    lines.extend(
        [
            border,
            f"| {'Unrouted: ' + unrouted_text:<{body_width}} |",
            border,
        ]
    )
    return "\n".join(lines)
