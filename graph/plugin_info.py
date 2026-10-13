"""Plugin info panel renderer for vcpi.

Displays cached metadata captured by the audio owner process:
name, vendor, category, version, latency, parameter count, etc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.audio_state import PluginSnapshot


def _metadata_text(value, default: str = "n/a") -> str:
    """Format cached metadata, preserving the missing-field convention."""
    return default if value is None else str(value)


def render_plugin_info(plugin: PluginSnapshot, label: str = "") -> str:
    """Return an ASCII info box for a cached plugin snapshot.

    Parameters
    ----------
    plugin : PluginSnapshot
        Plain metadata published by the audio owner while stopped.
    label : str, optional
        An optional heading (e.g. "Slot 1" or "Master FX 2").
    """
    # -- gather metadata -----------------------------------------------------
    name = _metadata_text(plugin.name, "Unknown")
    descriptive = _metadata_text(plugin.descriptive_name)
    vendor = _metadata_text(plugin.manufacturer_name)
    category = _metadata_text(plugin.category)
    version = _metadata_text(plugin.version)
    identifier = _metadata_text(plugin.identifier)
    latency = _metadata_text(plugin.reported_latency_samples, "0")
    plugin_type = _metadata_text(plugin.info_type, "Unknown")
    path = _metadata_text(plugin.path_to_plugin_file, "")

    # -- parameters summary --------------------------------------------------
    params = plugin.parameters
    param_count = len(params)
    automatable = sum(param.is_automatable for param in params.values())
    boolean_count = sum(param.is_boolean for param in params.values())
    discrete_count = sum(param.is_discrete for param in params.values())

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
