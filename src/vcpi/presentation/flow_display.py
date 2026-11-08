"""Literal-text flow tables and a client-local, read-only routing explorer."""

from __future__ import annotations

from collections.abc import Callable
from io import StringIO

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
from prompt_toolkit.utils import get_cwidth
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text


def _section_id(view: str) -> str:
    return f"track{view}" if view in "12345678" and len(view) == 1 else view


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

    def section_by_id(section_id: str) -> dict | None:
        return next((section for section in sections() if section["id"] == section_id), None)

    def parent_id(section: dict) -> str | None:
        parent = section.get("parent")
        return parent if parent and section_by_id(parent) is not None else None

    def roots() -> list[dict]:
        available = [section for section in sections() if parent_id(section) is None]
        order = ["overview", "midi", "controls", "audio",
                 *(f"track{number}" for number in range(1, 9)), "master"]
        return sorted(
            available,
            key=lambda section: order.index(section["id"]) if section["id"] in order else len(order),
        )

    def siblings() -> list[dict]:
        current = section_by_id(selected_id)
        parent = parent_id(current) if current is not None else None
        return ([section for section in sections() if parent_id(section) == parent]
                if parent is not None else roots())

    def children() -> list[dict]:
        return [section for section in sections() if parent_id(section) == selected_id]

    def ancestry() -> list[dict]:
        branch = []
        current = section_by_id(selected_id)
        seen = set()
        while current is not None and current["id"] not in seen:
            branch.append(current)
            seen.add(current["id"])
            parent = parent_id(current)
            current = section_by_id(parent) if parent is not None else None
        return list(reversed(branch))

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

    jump_keys = {
        "o": "overview", "m": "midi", "c": "controls", "a": "audio",
        **{str(number): f"track{number}" for number in range(1, 9)},
        "9": "master",
    }
    shortcuts = {section_id: key for key, section_id in jump_keys.items()}

    def tabs():
        width = app.output.get_size().columns
        labels = {"overview": "Overview", "midi": "MIDI", "controls": "Controls",
                  "audio": "Audio", "master": "Master"}
        if width < 100:
            labels.update(overview="View", controls="Ctrl", master="Mstr")
        branch = ancestry()
        root_id = branch[0]["id"] if branch else selected_id
        items = []
        for section in roots():
            section_id = section["id"]
            shortcut = shortcuts.get(section_id)
            if section_id.startswith("track") and shortcut is not None:
                label = shortcut
            else:
                label = labels.get(section_id, section["title"])
                if shortcut is not None:
                    label = f"{shortcut}:{label}"
            selected = section_id == root_id
            style = ("bg:ansicyan fg:ansiblack bold"
                     if selected else "fg:ansicyan bold")
            items.append((style, f"[{label}]" if selected else f" {label} "))
        # Keep the active root visible on narrow terminals, without wrapping tabs.
        active = next((index for index, section in enumerate(roots())
                       if section["id"] == root_id), 0)
        start, end = 0, len(items)
        while end - start > 1 and sum(get_cwidth(text) for _, text in items[start:end]) + 2 > width:
            if active - start > end - active - 1:
                start += 1
            else:
                end -= 1
        return ([("", "‹" if start else "")] + items[start:end]
                + [("", "›" if end < len(items) else "")])

    tabs_window = Window(
        content=FormattedTextControl(tabs), wrap_lines=False, height=1,
    )

    def footer():
        available = siblings()
        index = next(
            (i for i, section in enumerate(available) if section["id"] == selected_id),
            None,
        )
        current = " > ".join(section["title"] for section in ancestry()) or "No snapshot"
        if index is not None:
            current += f" [{index + 1}/{len(available)}]"
        child_count = len(children())
        if child_count:
            current += f" | Enter: {child_count} children"
        fragments = [("bold", current + "\n")]
        if error:
            fragments.append(("fg:ansired bold", error + "\n"))
        fragments.extend([
            ("", "←/→ Tab: siblings  Enter: children  Backspace: parent  o/m/c/a 1–9: root\n"),
            ("", "↑/↓ PgUp/PgDn Home/End: scroll  r: refresh  q/Esc/Ctrl-C/Ctrl-D: exit"),
        ])
        return fragments

    footer_window = Window(
        content=FormattedTextControl(footer), wrap_lines=False,
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
        available = siblings()
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

    for key, section_id in jump_keys.items():
        @bindings.add(key)
        def jump(event, target=section_id) -> None:
            select(target)

    @bindings.add("enter")
    def enter_child(event) -> None:
        available = children()
        if available:
            select(available[0]["id"])

    @bindings.add("backspace")
    def return_to_parent(event) -> None:
        current = section_by_id(selected_id)
        parent = parent_id(current) if current is not None else None
        if parent is not None:
            select(parent)

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
        layout=Layout(
            HSplit([tabs_window, content_window, footer_window]),
            focused_element=control,
        ),
        key_bindings=bindings, full_screen=True, erase_when_done=True,
        input=input, output=output,
    )
    try:
        app.run()
    except (EOFError, KeyboardInterrupt):
        # A closed pipe or terminal interrupt should return to the owning REPL.
        return
