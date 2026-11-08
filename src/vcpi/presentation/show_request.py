"""Shared parsing for cached inspection in plain and terminal clients."""

from __future__ import annotations

from dataclasses import dataclass
import shlex

from vcpi.models import NUM_TRACKS


SHOW_TARGETS = ("all", "midi", "controls", "audio",
                *(str(index) for index in range(1, NUM_TRACKS + 1)), "master")
SHOW_CONTROLLERS = ("midimix", "beatstep")
SHOW_USAGE = ("Usage: show [all|midi|controls|audio|1-16|master] [--details] | "
              "show controls [midimix|beatstep] [--details] | "
              "show <1-16|master> fx <fx_index> [--details] | show browse [--details]")


@dataclass(frozen=True)
class ShowRequest:
    view: str = "overview"
    details: bool = False
    browse: bool = False


def resolve_show(arguments: list[str]) -> ShowRequest:
    """Resolve shell tokens once, regardless of the eventual renderer."""
    details = "--details" in arguments
    if arguments.count("--details") > 1:
        raise ValueError(SHOW_USAGE)
    tokens = [token for token in arguments if token != "--details"]
    if not tokens:
        return ShowRequest(details=details)
    target = tokens[0]
    if tokens == ["browse"]:
        return ShowRequest(details=details, browse=True)
    if len(tokens) == 1 and target in SHOW_TARGETS:
        view = f"track{target}" if target.isdecimal() else target
        return ShowRequest(view, details)
    if len(tokens) == 2 and target == "controls" and tokens[1] in SHOW_CONTROLLERS:
        return ShowRequest(f"controls{tokens[1]}", details)
    if (len(tokens) == 3 and target in (*map(str, range(1, NUM_TRACKS + 1)), "master")
            and tokens[1] == "fx"):
        try:
            index = int(tokens[2])
        except ValueError:
            raise ValueError("fx_index must be a positive integer") from None
        if index < 1:
            raise ValueError("fx_index must be a positive integer")
        prefix = "master" if target == "master" else f"track{target}"
        return ShowRequest(f"{prefix}fx{index}", details)
    raise ValueError(SHOW_USAGE)


def parse_show(argument: str) -> ShowRequest:
    return resolve_show(shlex.split(argument))
