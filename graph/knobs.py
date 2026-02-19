"""ASCII parameter knob/slider view for vcpi.

Renders every parameter of a plugin as a horizontal slider bar with
value, units, and range information.
"""

from __future__ import annotations


# Bar width in characters (the filled/empty portion).
BAR_WIDTH = 20


def _param_bar(raw_value: float, width: int = BAR_WIDTH) -> str:
    """Render a 0-1 raw value as a horizontal slider bar."""
    raw = max(0.0, min(1.0, raw_value))
    filled = round(raw * width)
    return "#" * filled + "-" * (width - filled)


def _format_value(plugin, pname: str, params: dict) -> tuple[str, str, str]:
    """Return (display_value, units, type_hint) for a parameter.

    Falls back gracefully when attributes are missing (e.g. WavSamplerPlugin).
    """
    param_obj = params.get(pname)
    if param_obj is None:
        return "???", "", "?"

    # Current value through plugin attribute
    try:
        val = getattr(plugin, pname)
    except Exception:
        val = None

    # Units / label
    units = ""
    try:
        units = getattr(param_obj, "label", "") or ""
        if not units:
            units = getattr(param_obj, "units", "") or ""
    except Exception:
        pass

    # Type hint
    try:
        ptype = getattr(param_obj, "type", None)
        if ptype is bool:
            type_hint = "bool"
        elif ptype is str:
            type_hint = "enum"
        else:
            type_hint = "float"
    except Exception:
        type_hint = "float"

    # Display value: prefer the plugin's own string_value representation
    display = ""
    try:
        raw_param = plugin._get_parameter(pname)
        if raw_param is not None:
            sv = getattr(raw_param, "string_value", None)
            if sv:
                display = sv
    except Exception:
        pass

    if not display:
        if val is not None:
            try:
                display = f"{float(val):.4g}" if isinstance(val, (int, float)) else str(val)
            except (TypeError, ValueError):
                display = str(val)
        else:
            display = "???"

    return str(display), str(units), type_hint


def render_knobs(plugin, label: str = "") -> str:
    """Return an ASCII knob/slider view for all parameters of *plugin*.

    Parameters
    ----------
    plugin : pedalboard ExternalPlugin / WavSamplerPlugin / any
        The plugin object whose parameters to display.
    label : str, optional
        Optional heading text (e.g. "Slot 1: Dexed").
    """
    params = getattr(plugin, "parameters", {})
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
        display, units, type_hint = _format_value(plugin, pname, params)

        # Raw value (0-1) for the bar
        raw_val = 0.0
        try:
            raw_param = plugin._get_parameter(pname)
            if raw_param is not None:
                raw_val = float(getattr(raw_param, "raw_value", 0.0))
        except Exception:
            # Fallback: compute from range
            try:
                val = getattr(plugin, pname)
                rng = getattr(param_obj, "range", None)
                if rng and len(rng) >= 2 and rng[1] != rng[0]:
                    raw_val = (float(val) - rng[0]) / (rng[1] - rng[0])
                    raw_val = max(0.0, min(1.0, raw_val))
            except Exception:
                pass

        # Range text
        rng = getattr(param_obj, "range", None)
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
            try:
                valid_values = list(getattr(param_obj, "valid_values", []))
            except Exception:
                pass

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
