"""Cached plugin metadata rows shared by track and effect inspection.

Reads only metadata captured by the audio owner process:
name, vendor, category, version, latency, parameter count, etc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.audio_state import PluginSnapshot


def _metadata_text(value, default: str = "n/a") -> str:
    """Format cached metadata, preserving the missing-field convention."""
    return default if value is None else str(value)


def plugin_metadata_rows(plugin: PluginSnapshot) -> list[tuple[str, str]]:
    """Return metadata fields without accessing a live native plugin."""
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
    group_counts = {"controls": 0, "settings": 0, "midi": 0, "unused": 0}
    for parameter_name in params:
        group_counts[plugin.parameter_group(parameter_name)] += 1
    automatable = sum(param.is_automatable for param in params.values())
    boolean_count = sum(param.is_boolean for param in params.values())
    discrete_count = sum(param.is_discrete for param in params.values())

    # -- compose rows --------------------------------------------------------
    rows: list[tuple[str, str]] = []

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
    rows.append(("Reported parameters", str(param_count)))
    rows.append(("Sound controls", str(group_counts["controls"])))
    rows.append(("Plugin settings", str(group_counts["settings"])))
    rows.append(("MIDI parameters", str(group_counts["midi"])))
    rows.append(("Unused parameters", str(group_counts["unused"])))
    if param_count > 0:
        rows.append(("Automatable parameters (reported)", str(automatable)))
        rows.append(("Boolean parameters (reported)", str(boolean_count)))
        rows.append(("Discrete parameters (reported)", str(discrete_count)))

    return rows
