"""ASCII parameter knob/slider view for vcpi.

Renders every parameter of a plugin as a horizontal slider bar with
value, units, and range information.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.audio_state import ParameterSnapshot, PluginSnapshot


# Bar width in characters (the filled/empty portion).
BAR_WIDTH = 20


def _param_bar(raw_value: float, width: int = BAR_WIDTH) -> str:
    """Render a 0-1 raw value as a horizontal slider bar."""
    raw = max(0.0, min(1.0, raw_value))
    filled = round(raw * width)
    return "#" * filled + "-" * (width - filled)


def _format_value(
    plugin: PluginSnapshot, pname: str, param: ParameterSnapshot,
) -> tuple[str, str, str]:
    """Return (display_value, units, type_hint) from cached parameter data."""
    val = plugin.values.get(pname)
    units = param.label or param.units or ""
    display = param.string_value
    if not display:
        if val is None:
            display = "???"
        elif isinstance(val, (int, float)):
            display = f"{float(val):.4g}"
        else:
            display = str(val)
    return str(display), str(units), param.kind


def render_knobs(plugin: PluginSnapshot, label: str = "") -> str:
    """Return an ASCII knob/slider view for a cached plugin snapshot.

    Parameters
    ----------
    plugin : PluginSnapshot
        Plain parameter metadata and values published by the audio owner.
    label : str, optional
        Optional heading text (e.g. "Slot 1: Dexed").
    """
    params = plugin.parameters
    if not params:
        heading = label or "Parameters"
        border = "+" + "-" * (len(heading) + 2) + "+"
        return "\n".join([
            border,
            f"| {heading} |",
            border,
            f"| {'(no parameters)':<{len(heading)}} |",
            border,
        ])

    # -- collect row data ----------------------------------------------------
    rows: list[dict] = []
    for pname in params:
        param_obj = params[pname]
        display, units, type_hint = _format_value(plugin, pname, param_obj)

        # Preserve the plugin's cached normalized slider position.
        raw_val = param_obj.raw_value

        # Range text
        rng = param_obj.range
        if rng and len(rng) >= 2 and rng[0] is not None and rng[1] is not None:
            try:
                range_text = f"{float(rng[0]):.4g} .. {float(rng[1]):.4g}"
            except (TypeError, ValueError):
                range_text = f"{rng[0]} .. {rng[1]}"
        else:
            range_text = ""

        # For enum/string parameters, show valid values instead of bar
        valid_values: list[str] = []
        if type_hint == "enum":
            valid_values = list(param_obj.valid_values)

        rows.append({
            "name": pname,
            "bar": _param_bar(raw_val),
            "display": display,
            "units": units,
            "range": range_text,
            "type": type_hint,
            "valid_values": valid_values,
        })

    # -- compute column widths -----------------------------------------------
    name_w = max(len(r["name"]) for r in rows)
    disp_w = max(len(f'{r["display"]} {r["units"]}') for r in rows)

    # -- render lines --------------------------------------------------------
    heading = label or "Parameters"
    content_lines: list[str] = []

    for r in rows:
        val_str = r["display"]
        if r["units"]:
            val_str += f" {r['units']}"

        if r["type"] == "enum" and r["valid_values"]:
            # Show current value + valid options instead of bar
            opts = ", ".join(r["valid_values"][:8])
            if len(r["valid_values"]) > 8:
                opts += ", ..."
            line = f"  {r['name']:<{name_w}}  = {val_str:<{disp_w}}  [{opts}]"
        elif r["type"] == "bool":
            line = f"  {r['name']:<{name_w}}  [{r['bar']}]  {val_str}"
        else:
            line = f"  {r['name']:<{name_w}}  [{r['bar']}]  {val_str:<{disp_w}}"
            if r["range"]:
                line += f"  ({r['range']})"

        content_lines.append(line)

    body_width = max(len(heading), max(len(ln) for ln in content_lines))
    border = "+" + "-" * (body_width + 2) + "+"

    out = [border, f"| {heading:^{body_width}} |", border]
    for ln in content_lines:
        out.append(f"| {ln:<{body_width}} |")
    out.append(border)

    return "\n".join(out)
