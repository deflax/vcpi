"""Plugin info panel renderer for vcpi.

Displays metadata exposed by pedalboard ExternalPlugin instances:
name, vendor, category, version, latency, parameter count, etc.
"""

from __future__ import annotations

from pathlib import Path


def _safe_attr(plugin, attr: str, default: str = "n/a") -> str:
    """Read an attribute with a fallback for plugins that don't expose it."""
    try:
        val = getattr(plugin, attr, None)
        if val is None:
            return default
        return str(val)
    except Exception:
        return default


def render_plugin_info(plugin, label: str = "") -> str:
    """Return an ASCII info box for a single plugin instance.

    Parameters
    ----------
    plugin : pedalboard ExternalPlugin / WavSamplerPlugin / any
        The plugin object to inspect.
    label : str, optional
        An optional heading (e.g. "Slot 1" or "Master FX 2").
    """
    # -- gather metadata -----------------------------------------------------
    name = _safe_attr(plugin, "name", type(plugin).__name__)
    descriptive = _safe_attr(plugin, "descriptive_name")
    vendor = _safe_attr(plugin, "manufacturer_name")
    category = _safe_attr(plugin, "category")
    version = _safe_attr(plugin, "version")
    identifier = _safe_attr(plugin, "identifier")
    latency = _safe_attr(plugin, "reported_latency_samples", "0")

    is_instrument = getattr(plugin, "is_instrument", False)
    plugin_type = "Instrument" if is_instrument else "Effect"

    path = _safe_attr(plugin, "path_to_plugin_file",
                      _safe_attr(plugin, "path", ""))

    # -- parameters summary --------------------------------------------------
    params = getattr(plugin, "parameters", {})
    param_count = len(params) if params else 0

    automatable = 0
    boolean_count = 0
    discrete_count = 0
    for pname in params:
        try:
            raw_param = plugin._get_parameter(pname)
        except Exception:
            raw_param = None
        if raw_param is not None:
            if getattr(raw_param, "is_automatable", False):
                automatable += 1
            if getattr(raw_param, "is_boolean", False):
                boolean_count += 1
            if getattr(raw_param, "is_discrete", False):
                discrete_count += 1

    # -- compose rows --------------------------------------------------------
    rows: list[tuple[str, str]] = []

    if label:
        rows.append(("", label))
        rows.append(("", ""))  # spacer

    rows.append(("Name", name))
    if descriptive != name and descriptive != "n/a":
        rows.append(("Desc", descriptive))
    rows.append(("Vendor", vendor))
    rows.append(("Category", category))
    rows.append(("Version", version))
    rows.append(("Type", plugin_type))
    if identifier != "n/a":
        rows.append(("ID", identifier))
    if path:
        rows.append(("Path", path))
    rows.append(("Latency", f"{latency} samples"))
    rows.append(("", ""))  # spacer
    rows.append(("Parameters", str(param_count)))
    if param_count > 0:
        rows.append(("  automatable", str(automatable)))
        rows.append(("  boolean", str(boolean_count)))
        rows.append(("  discrete", str(discrete_count)))

    # -- render box ----------------------------------------------------------
    key_width = max(len(k) for k, _ in rows) if rows else 0
    val_width = max(len(v) for _, v in rows) if rows else 0
    body_width = key_width + 3 + val_width  # " : " separator

    border = "+" + "-" * (body_width + 2) + "+"
    lines = [border]
    for key, val in rows:
        if not key and not val:
            lines.append(f"| {'':<{body_width}} |")
        elif not key:
            lines.append(f"| {val:^{body_width}} |")
        else:
            line_text = f"{key:<{key_width}} : {val}"
            lines.append(f"| {line_text:<{body_width}} |")
    lines.append(border)

    return "\n".join(lines)
