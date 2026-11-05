"""Small execution context and shared shell argument validation."""

from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Callable, TYPE_CHECKING

from vcpi.models import NUM_TRACKS

if TYPE_CHECKING:
    from vcpi.host import VcpiCore


@dataclass
class CommandContext:
    host: VcpiCore
    emit: Callable

    def tokens(self, arg, usage):
        try:
            return shlex.split(arg)
        except ValueError as error:
            self.emit(f"Error: {error}")
            self.emit(f"Usage: {usage}")
            return None


def track_index(user_track: int) -> int:
    """Convert 1-based user track to 0-based index, with validation."""
    if not 1 <= user_track <= NUM_TRACKS:
        raise ValueError(f"track must be 1-{NUM_TRACKS}")
    return user_track - 1


def channel_index(user_ch: int) -> int:
    """Convert 1-based user MIDI channel to 0-based, with validation."""
    if not 1 <= user_ch <= 16:
        raise ValueError("MIDI channel must be 1-16")
    return user_ch - 1


def source_args(tokens, count=None):
    """Separate a single explicit label from source arguments."""
    name = None
    if "--name" in tokens:
        marker = tokens.index("--name")
        if len(tokens) != marker + 2 or not tokens[-1] or tokens[-1].startswith("--"):
            raise ValueError("--name requires one label; quote labels containing spaces")
        name = tokens[-1]
        tokens = tokens[:marker]
    if any(token.startswith("--") for token in tokens):
        raise ValueError("unknown source option")
    if not tokens or (count is not None and len(tokens) != count):
        raise ValueError("incorrect source arguments; labels require --name")
    return tokens, name


def parse_named_source(text: str) -> tuple[str, str | None]:
    tokens, name = source_args(shlex.split(text))
    return " ".join(tokens), name

