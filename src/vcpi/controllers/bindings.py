"""Cached controller expectations, not hardware-map verification."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal


@dataclass(frozen=True)
class ControlBinding:
    controller: str
    control: str
    message: str
    target: str
    value: str
    availability: Literal["mapped", "disconnected", "unbound", "unsupported", "unavailable"]
    reason: str = ""
    track: int | None = None
    range: str = ""
    mode: str = ""
    feedback: str = ""


def cached_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def supported_parameter(parameter) -> bool:
    if parameter.kind == "bool":
        return True
    if parameter.kind == "enum":
        return bool(parameter.valid_values)
    bounds = parameter.range
    return (len(bounds) == 2
            and all(isinstance(bound, (int, float)) and math.isfinite(bound)
                    for bound in bounds)
            and bounds[0] <= bounds[1]
            and math.isfinite(bounds[1] - bounds[0]))


def parameter_bounds(parameter) -> tuple[float, float]:
    if parameter.kind == "bool":
        return (0.0, 1.0)
    if parameter.kind == "enum":
        return (0.0, float(max(0, len(parameter.valid_values) - 1)))
    return (float(parameter.range[0]), float(parameter.range[1]))


def parameter_value(plugin, name, parameter) -> str:
    value = cached_value(plugin.values[name]) if name in plugin.values else "(not cached)"
    if parameter.string_value and parameter.string_value != value:
        value += f" ({parameter.string_value})"
    return value + (f" {parameter.units}" if parameter.units else "")


def parameter_range(parameter) -> str:
    if parameter.kind == "bool":
        return "false / true"
    if parameter.kind == "enum":
        if not parameter.valid_values:
            return "no enum values"
        return (f"indices 0-{len(parameter.valid_values) - 1}: "
                + ", ".join(parameter.valid_values))
    return " .. ".join(cached_value(bound) for bound in parameter.range) or "unknown"


def input_availability(input_name):
    return (("mapped", "") if input_name else
            ("disconnected", "controller input disconnected"))
