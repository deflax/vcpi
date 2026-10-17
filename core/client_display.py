"""Terminal-only presentation of daemon responses; the wire protocol stays text."""

from __future__ import annotations

import re

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text


def _logo_colour(column: int, row: int, width: int, height: int) -> str:
    """Diagonal blend with broad yellow/purple ends and a narrow green centre."""
    position = (0.8 * column / max(width - 1, 1)
                + 0.2 * row / max(height - 1, 1) - 0.1) / 0.8
    yellow, green, purple = (255, 224, 55), (0, 198, 0), (132, 4, 217)
    if position <= 0.28:
        return "#ffe037"
    if position >= 0.72:
        return "#8404d9"
    if position <= 0.5:
        start, end = yellow, green
        fraction = (position - 0.28) / 0.22
    else:
        start, end = green, purple
        fraction = (position - 0.5) / 0.22
    return "#{:02x}{:02x}{:02x}".format(
        *(int(a + (b - a) * fraction) for a, b in zip(start, end))
    )


def _table(title: str, *columns: str) -> Table:
    table = Table(title=Text(title), box=box.SIMPLE, border_style="cyan")
    for column in columns:
        table.add_column(
            column, style="cyan" if column == columns[0] else None, overflow="fold",
        )
    return table


def _help(console: Console, lines: list[str]) -> None:
    table = _table("Available commands", "Category", "Command", "Description")
    table.columns[0].style = "bold"
    table.columns[1].no_wrap = True
    colours = ("cyan", "green", "yellow", "magenta",
               "bright_blue", "bright_cyan", "bright_green", "bright_magenta")
    group_index = 0
    colour = colours[0]
    category = ""
    notes: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "Available commands:":
            continue
        if not line.startswith(" ") and stripped.endswith(":"):
            category = stripped[:-1]
            colour = colours[group_index % len(colours)]
            group_index += 1
        elif line.startswith("  "):
            name, _, summary = stripped.partition(" ")
            table.add_row(Text(category, style=colour), Text(name, style=colour),
                          Text(summary.strip()))
            category = ""
        else:
            notes.append(line)
    if table.row_count:
        console.print(table)
    for note in notes:
        console.print(Text(note))


def _info_table(lines: list[str], title: str) -> Table | None:
    """Recognize complete status/info boxes without swallowing error responses."""
    if len(lines) < 3 or not re.fullmatch(r"\+-+\+", lines[0]):
        return None
    table = _table(title, "Field", "Value")
    for line in lines:
        if re.fullmatch(r"\+-+\+", line):
            continue
        if not (line.startswith("| ") and line.endswith(" |")):
            return None
        body = line[2:-2].strip()
        if not body:
            table.add_section()
        elif " : " in body:
            key, value = body.split(" : ", 1)
            table.add_row(Text(key.strip()), Text(value))
        else:
            table.add_row(Text(body, style="bold"), Text(""))
    return table


def _get_table(lines: list[str]) -> Table | None:
    if not lines:
        return None
    table = _table("Parameters", "Parameter", "Value", "Range / choices")
    for line in lines:
        if not line.startswith("  ") or " = " not in line:
            return None
        name, value = line.strip().split(" = ", 1)
        range_text = ""
        match = re.search(r"  \((?:range|choices) (.*)\)$", value)
        if match:
            range_text = match.group(1)
            value = value[:match.start()]
        table.add_row(Text(name), Text(value), Text(range_text))
    return table


def display_response(console: Console, command: str, lines: list[str]) -> None:
    """Render selected views, treating all server/plugin text as literal text."""
    tokens = command.strip().split()
    name = tokens[0] if tokens else ""
    if tokens == ["help"] and lines and lines[0] == "Available commands:":
        _help(console, lines)
        return
    table = None
    if name in ("status", "info"):
        table = _info_table(lines, "Status" if name == "status" else "Plugin info")
    elif name == "get":
        table = _get_table(lines)
    if table is not None:
        console.print(table)
        return
    logo_lines = []
    if name in ("", "about"):
        logo_lines = [
            line for line in lines if line.strip() and set(line) <= set(" █░")
        ]
    logo_width = max((len(line) for line in logo_lines), default=0)
    logo_row = 0
    # Preserve spacing and width for diagrams, including on narrow terminals.
    for line in lines:
        text = Text(line)
        if logo_width and line.strip() and set(line) <= set(" █░"):
            for column, char in enumerate(line):
                if char in "█░":
                    colour = _logo_colour(column, logo_row, logo_width, len(logo_lines))
                    text.stylize(colour, column, column + 1)
            logo_row += 1
        if line.lstrip().startswith(("Error:", "***")):
            text.stylize("bold red")
        elif name == "flow":
            text.stylize("cyan")
        console.print(text, soft_wrap=True)
