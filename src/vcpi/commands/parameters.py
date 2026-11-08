"""Parameters command handlers."""

import shlex
from .context import track_index


def parameter_target(host, parts, *, require_master_index=False):
    """Resolve a cached plugin and leave parameter/value arguments unconsumed."""
    if not parts:
        raise ValueError("missing plugin target")
    fx_idx = None
    if parts[0] == "master":
        idx = None
        consumed = 1
        fx_idx = 0
        if len(parts) > 1 and (require_master_index or parts[1].lstrip("+-").isdigit()):
            try:
                fx_idx = int(parts[1]) - 1
            except ValueError:
                raise ValueError("fx_index must be a positive integer") from None
            consumed = 2
        elif require_master_index:
            raise ValueError("missing master fx_index")
        effects = host.engine.master_effects
        if not effects:
            raise ValueError("No master effects loaded")
        if not 0 <= fx_idx < len(effects):
            raise ValueError("fx_index out of range")
        plugin = effects[fx_idx]
    else:
        idx = track_index(int(parts[0]))
        track = host.engine.tracks[idx]
        if track is None:
            raise ValueError(f"Track {parts[0]} is empty")
        consumed = 1
        plugin = track.plugin
        if len(parts) > 1 and parts[1] == "fx":
            if len(parts) < 3:
                raise ValueError("missing track fx_index")
            try:
                fx_idx = int(parts[2]) - 1
            except ValueError:
                raise ValueError("fx_index must be a positive integer") from None
            if not 0 <= fx_idx < len(track.effects):
                raise ValueError("fx_index out of range")
            plugin = track.effects[fx_idx]
            consumed = 3
    if plugin is None:
        raise ValueError("No plugin loaded")
    return idx, fx_idx, plugin, parts[consumed:]


def get(ctx, arg):
    """Read cached plugin parameters, including units, ranges, and choices.

    get <track 1-16> [name | <listing flag>]
    get <track 1-16> fx <fx_index> [name | <listing flag>]
    get master [fx_index] [name | <listing flag>]

    Listing flags: --controls (default), --settings, --midi, --unused, --all.
    Use one flag or one exact parameter name, not both.
    Master defaults to its first effect. Reads use cached metadata only.
    Examples: get 1; get 1 --all; get 1 fx 2; get master 1 cutoff
    """
    try:
        idx, fx_idx, plugin, remaining = parameter_target(ctx.host, shlex.split(arg))
        groups = {
            "controls": "Controls:",
            "settings": "Plugin settings:",
            "midi": "MIDI parameters:",
            "unused": "Unused parameters:",
        }
        group = "controls"
        if len(remaining) > 1:
            raise ValueError("expected one parameter name or one listing flag, not both")
        if remaining and remaining[0] in (
                "--controls", "--settings", "--midi", "--unused", "--all"):
            group = remaining[0][2:]
            remaining = []
        if remaining:
            name = remaining[0]
            if name not in plugin.parameters:
                raise ValueError(f"unknown parameter '{name}'")
            sections = [(None, [(name, plugin.parameters[name])])]
        elif group == "all":
            sections = [(heading, plugin.parameter_items(key))
                        for key, heading in groups.items()]
        else:
            sections = [(None, plugin.parameter_items(group))]
        for heading, parameters in sections:
            if heading:
                ctx.emit(heading)
            found = False
            for name, parameter in parameters:
                found = True
                value = plugin.values.get(name, parameter.string_value or "???")
                units = f" {parameter.units}" if parameter.units else ""
                details = ""
                if parameter.kind == "enum" and parameter.valid_values:
                    details = f"  (choices {', '.join(parameter.valid_values)})"
                elif parameter.kind in ("float", "int") and len(parameter.range) >= 2:
                    low, high = parameter.range[:2]
                    if low is not None and high is not None:
                        details = f"  (range {low:.4g} .. {high:.4g})"
                ctx.emit(f"  {name} = {value}{units}{details}")
            if not found:
                ctx.emit("  No parameters")
    except ValueError as error:
        ctx.emit(f"Error: {error}")
        ctx.emit("Usage: get <track> [fx <index>] [name | <listing flag>] | "
                    "get master [index] [name | <listing flag>]")
        ctx.emit("Listing flags: --controls (default), --settings, --midi, --unused, --all")


def set(ctx, arg):
    """Set a plugin parameter and show its actual applied value.

    set <track 1-16> <name> <value>
    set <track 1-16> fx <fx_index> <name> <value>
    set master <fx_index> <name> <value>

    Boolean values accept true/false; quote enum values containing spaces.
    Unused plugin slots cannot be set; explicit MIDI parameter names are allowed.
    Examples: set 1 cutoff 1000; set 1 fx 2 bypass true
              set master 1 mode "Quiet Mode"
    """
    try:
        idx, fx_idx, plugin, remaining = parameter_target(ctx.host, 
            shlex.split(arg), require_master_index=True)
        if len(remaining) != 2:
            raise ValueError("expected a parameter name and one value")
        pname, pval = remaining
        parameter = plugin.parameters.get(pname)
        if parameter is None:
            raise ValueError(f"unknown parameter '{pname}'")
        if plugin.parameter_group(pname) == "unused":
            raise ValueError(f"parameter '{pname}' is an unused plugin slot and cannot be set")
        if parameter.kind != "enum":
            try:
                pval = float(pval)
            except ValueError:
                if pval.lower() in ("true", "false"):
                    pval = pval.lower() == "true"
    except ValueError as error:
        ctx.emit(f"Error: {error}")
        ctx.emit("Usage: set <track> [fx <index>] <name> <value> | set master <index> <name> <value>")
        return
    try:
        actual = ctx.host.engine.set_parameter(idx, pname, pval, fx_index=fx_idx)
        if (idx is not None and fx_idx is None and pname == "track_select"
                and ctx.host.engine.tracks[idx].source_type == "deck"):
            ctx.emit(f"  track_select request accepted = {actual}; use deck status for applied selection")
        else:
            ctx.emit(f"  {pname} = {actual}")
    except Exception as error:
        ctx.emit(f"Error: {error}")
