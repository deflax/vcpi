"""Literal-text tables for cached routing and control views."""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from vcpi.models import NUM_TRACKS


def _section_id(view: str) -> str:
    return f"track{view}" if view in map(str, range(1, NUM_TRACKS + 1)) else view


_STATE_COLOURS = {
    "healthy": "green",
    "warning": "yellow",
    "error": "red",
    "neutral": "dim",
}


def _cell_text(cell: str, state: str | None = None) -> Text:
    """Apply explicit snapshot semantics without interpreting literal cell text."""
    return Text(cell, style=_STATE_COLOURS.get(state, ""))


def _section_table(section: dict, width: int) -> Table:
    columns = section["columns"]
    # Stack genuinely narrow tables instead of squeezing every column to letters.
    stacked = width < len(columns) * 10
    compact = section["id"] == "overview" and not stacked
    table = Table(
        title=Text(section["title"]),
        box=None if compact else box.SIMPLE, border_style="cyan",
        padding=(0, 0 if stacked else 1), expand=True,
    )
    states = section.get("cell_states", [])

    def cell_state(row: int, column: int) -> str | None:
        if row < len(states) and column < len(states[row]):
            return states[row][column]
        return None

    if stacked:
        table.add_column(Text("Details"), overflow="fold", min_width=1)
        for row_index, row in enumerate(section["rows"]):
            text = Text()
            for index, (column, cell) in enumerate(zip(columns, row)):
                if index:
                    text.append("\n")
                text.append(column, style="bold cyan")
                text.append(": ")
                text.append_text(_cell_text(cell, cell_state(row_index, index)))
            table.add_row(text)
    else:
        for index, column in enumerate(columns):
            table.add_column(
                Text(column), style="cyan" if index == 0 else None,
                overflow="ellipsis" if compact else "fold", no_wrap=compact,
                min_width=1, max_width=max(10, width // 4) if index == 0 else None,
            )
        for row_index, row in enumerate(section["rows"]):
            table.add_row(*(
                _cell_text(cell, cell_state(row_index, index))
                for index, cell in enumerate(row)
            ))
    return table


def display_flow(console: Console, snapshot: dict, view: str = "overview") -> None:
    """Render one section, a numbered track, or all sections from a snapshot."""
    section_id = _section_id(view)
    sections = [
        section for section in snapshot["sections"]
        if view == "all" or section["id"] == section_id
    ]
    if not sections:
        raise ValueError(f"No loaded show target: {view}")
    console.print(Text(snapshot["title"], style="bold cyan"))
    for section in sections:
        if section.get("description"):
            console.print(Text(section["description"], style="dim"))
        console.print(_section_table(section, console.width))

