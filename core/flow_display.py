"""Literal-text flow tables and a client-local, read-only routing explorer."""

from __future__ import annotations

from collections.abc import Callable
from io import StringIO
import re

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.output import Output
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text


def _section_id(view: str) -> str:
    return f"slot{view}" if view in "12345678" and len(view) == 1 else view


_STATE_COLOURS = {
    "green": (
        r"\b(?:on|true|active|connected|running|ready)\b"
        r"|\beligible for output\b|\b(?:Link )?enabled\b"
    ),
    "red": (
        r"\b(?:off|false|stopped|failed|failure|muted|disabled|blocked|solo-excluded)\b"
        r"|\b(?:slot|master) gain zero\b"
    ),
    "yellow": (
        r"\b(?:inactive|disconnected|unlinked|unbound|undefined|unknown|empty|"
        r"unavailable|pending)\b|\bnot (?:opened|cached|reported)\b"
        r"|\bnone connected\b|\bno (?:instrument parameter|enum values)\b"
    ),
}
_STATE_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE), colour)
    for colour, pattern in _STATE_COLOURS.items()
]
_CONTROL_VALUE = re.compile(
    r"^(true|false)\b|\b(?:LED|mute|solo|enabled)\s+(on|off|true|false)\b",
    re.IGNORECASE,
)


def _cell_text(cell: str, column: str) -> Text:
    """Colour state spans, never interpret daemon/plugin strings as markup."""
    text = Text(cell)
    if "state" in column.lower():
        for pattern, colour in _STATE_PATTERNS:
            for match in pattern.finditer(cell):
                # A reported absence of failure is healthy, not a red failure.
                if match.group().lower() == "failure" and cell[match.end():].startswith(": none"):
                    continue
                text.stylize(colour, match.start(), match.end())
    elif "value" in column.lower() or "range" in column.lower():
        for match in _CONTROL_VALUE.finditer(cell):
            group = 1 if match.group(1) is not None else 2
            value = match.group(group).lower()
            text.stylize("green" if value in ("on", "true") else "red",
                         match.start(group), match.end(group))
    return text


def _section_table(section: dict, width: int) -> Table:
    columns = section["columns"]
    # Preserve every cell even when the terminal cannot fit all column borders.
    stacked = width < len(columns) * 4 + 1
    table = Table(
        title=Text(section["title"]), box=box.SIMPLE, border_style="cyan",
        padding=(0, 0 if stacked else 1), expand=True,
    )
    if stacked:
        table.add_column(Text("Details"), overflow="fold", min_width=1)
        for row in section["rows"]:
            text = Text()
            for index, (column, cell) in enumerate(zip(columns, row)):
                if index:
                    text.append("\n")
                text.append(column, style="bold cyan")
                text.append(": ")
                text.append_text(_cell_text(cell, column))
            table.add_row(text)
    else:
        for index, column in enumerate(columns):
            table.add_column(
                Text(column), style="cyan" if index == 0 else None,
                overflow="fold", min_width=1,
            )
        for row in section["rows"]:
            table.add_row(*(_cell_text(cell, column)
                            for column, cell in zip(columns, row)))
    return table


def display_flow(console: Console, snapshot: dict, view: str = "overview") -> None:
    """Render one section, a numbered slot, or all sections from a snapshot."""
    section_id = _section_id(view)
    sections = [
        section for section in snapshot["sections"]
        if view == "all" or section["id"] == section_id
    ]
    if not sections:
        raise ValueError(f"Unknown flow section: {view}")
    console.print(Text(snapshot["title"], style="bold cyan"))
    for section in sections:
        console.print(_section_table(section, console.width))


class _FlowControl(UIControl):
    """Give the window an actual cursor to follow through rendered table lines."""

    def __init__(self, render: Callable[[int], list]) -> None:
        self.render = render
        self.cursor_line = 0
        self.lines: list = [[]]

    def is_focusable(self) -> bool:
        return True

    def create_content(self, width: int, height: int) -> UIContent:
        self.lines = self.render(max(1, width)) or [[]]
        self.cursor_line = min(self.cursor_line, len(self.lines) - 1)
        return UIContent(
            get_line=lambda index: self.lines[index],
            line_count=len(self.lines),
            cursor_position=Point(x=0, y=self.cursor_line),
            show_cursor=False,
        )

    def move(self, delta: int) -> None:
        self.cursor_line = max(0, min(len(self.lines) - 1, self.cursor_line + delta))


def explore_flow(
    fetch_snapshot: Callable[[], dict], *, input: Input | None = None,
    output: Output | None = None,
) -> None:
    """Explore snapshots without polling or changing daemon routing state.

    Only entry and the explicit refresh key call ``fetch_snapshot``. A failed
    refresh leaves the previous snapshot, section, and scroll position intact.
    Input/output injection supports real prompt_toolkit pipe-input callers.
    """
    snapshot: dict | None = None
    selected_id = "overview"
    error = ""
    revision = 0
    cache_key: tuple | None = None
    cached_lines: list = []

    def sections() -> list[dict]:
        return snapshot["sections"] if snapshot is not None else []

    def refresh() -> None:
        nonlocal snapshot, selected_id, error, revision
        try:
            replacement = fetch_snapshot()
        except (OSError, ValueError) as exc:
            error = f"Refresh failed: {exc}"
            return
        snapshot = replacement
        revision += 1
        error = ""
        available = sections()
        if available and not any(s["id"] == selected_id for s in available):
            selected_id = available[0]["id"]

    refresh()

    def render(width: int) -> list:
        nonlocal cache_key, cached_lines
        key = (width, revision, selected_id)
        if key != cache_key:
            stream = StringIO()
            console = Console(
                file=stream, force_terminal=True, width=width,
                color_system="standard", highlight=False,
            )
            if snapshot is None or not sections():
                console.print(Text("No snapshot available. Press r to retry."))
            else:
                display_flow(console, snapshot, selected_id)
            cached_lines = list(split_lines(to_formatted_text(
                ANSI(stream.getvalue().rstrip("\n")),
            )))
            cache_key = key
        return cached_lines

    control = _FlowControl(render)
    content_window = Window(
        content=control, wrap_lines=True, always_hide_cursor=True,
        height=Dimension(min=1), right_margins=[ScrollbarMargin()],
    )

    def footer():
        available = sections()
        index = next(
            (i for i, section in enumerate(available) if section["id"] == selected_id),
            None,
        )
        current = (
            f"{available[index]['title']} [{index + 1}/{len(available)}]"
            if index is not None else "No snapshot"
        )
        fragments = [("bold", current + "\n")]
        if error:
            fragments.append(("fg:ansired bold", error + "\n"))
        fragments.extend([
            ("", "←/→ Tab/Shift-Tab: sections | o/m/c/a 1–8 9: jump\n"),
            ("", "↑/↓ PgUp/PgDn Home/End: scroll\n"),
            ("", "r: refresh (manual only) | q Esc Ctrl-C/Ctrl-D: back"),
        ])
        return fragments

    footer_window = Window(
        content=FormattedTextControl(footer), wrap_lines=True,
        dont_extend_height=True, style="reverse",
    )
    bindings = KeyBindings()

    def select(section_id: str) -> None:
        nonlocal selected_id
        if any(section["id"] == section_id for section in sections()):
            selected_id = section_id
            control.cursor_line = 0
            content_window.vertical_scroll = 0

    def cycle(delta: int) -> None:
        available = sections()
        if not available:
            return
        index = next(
            (i for i, section in enumerate(available) if section["id"] == selected_id), 0,
        )
        select(available[(index + delta) % len(available)]["id"])

    @bindings.add("left")
    @bindings.add("s-tab")
    def previous_section(event) -> None:
        cycle(-1)

    @bindings.add("right")
    @bindings.add("tab")
    def next_section(event) -> None:
        cycle(1)

    jump_keys = {
        "o": "overview", "m": "midi", "c": "controls", "a": "audio",
        **{str(number): f"slot{number}" for number in range(1, 9)},
        "9": "master",
    }
    for key, section_id in jump_keys.items():
        @bindings.add(key)
        def jump(event, target=section_id) -> None:
            select(target)

    @bindings.add("up")
    def up(event) -> None:
        control.move(-1)

    @bindings.add("down")
    def down(event) -> None:
        control.move(1)

    def page_height() -> int:
        info = content_window.render_info
        return max(1, info.window_height - 1) if info is not None else 1

    @bindings.add("pageup")
    def page_up(event) -> None:
        control.move(-page_height())

    @bindings.add("pagedown")
    def page_down(event) -> None:
        control.move(page_height())

    @bindings.add("home")
    def home(event) -> None:
        control.cursor_line = 0

    @bindings.add("end")
    def end(event) -> None:
        control.cursor_line = len(control.lines) - 1

    @bindings.add("r")
    def reload_snapshot(event) -> None:
        refresh()

    @bindings.add("q")
    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("c-d")
    def exit_explorer(event) -> None:
        event.app.exit()

    app = Application(
        layout=Layout(HSplit([content_window, footer_window]), focused_element=control),
        key_bindings=bindings, full_screen=True, erase_when_done=True,
        input=input, output=output,
    )
    try:
        app.run()
    except (EOFError, KeyboardInterrupt):
        # A closed pipe or terminal interrupt should return to the owning REPL.
        return
