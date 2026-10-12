"""CLI client that connects to a running vcpi server over a Unix socket.

Usage::

    python main.py cli [--sock PATH]

Default PATH is auto-selected:
  $XDG_RUNTIME_DIR/vcpi/vcpi.sock
  fallback: /tmp/vcpi-<uid>/vcpi.sock
  root fallback: /run/vcpi/vcpi.sock

Or directly::

    python -c "from core.client import connect; connect()"
"""

from __future__ import annotations

import os
import re
import socket
import sys
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console

from core.client_display import display_response
from core.paths import DEFAULT_SOCK_PATH

# Must match the sentinel used by server.py
END_OF_RESPONSE = "\x00"


HELP_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Fallback for tab completion in case help parsing fails.
FALLBACK_COMMANDS = (
    "about",
    "audio",
    "deps",
    "exit",
    "gain",
    "help",
    "info",
    "knobs",
    "ableton",
    "midi",
    "midimix",
    "flow",
    "mute",
    "note",
    "params",
    "load",
    "quit",
    "save",
    "set",
    "shutdown",
    "slot",
    "solo",
    "status",
    "tempo",
)

SLOT_TYPES = ("vst", "wav", "vcv", "fx", "clear")


def _filter_prefix(values: list[str], prefix: str) -> list[str]:
    wanted = prefix.lower()
    return sorted(v for v in values if v.lower().startswith(wanted))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _samples_root() -> Path:
    cwd_samples = Path.cwd() / "sampler" / "samples"
    if cwd_samples.exists() and cwd_samples.is_dir():
        return cwd_samples
    return _repo_root() / "sampler" / "samples"


def _sample_pack_names() -> list[str]:
    root = _samples_root()
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def _sample_names(pack_name: str) -> list[str]:
    pack = Path(pack_name.strip().strip("/"))
    if pack.is_absolute() or ".." in pack.parts:
        return []

    pack_dir = _samples_root() / pack
    if not pack_dir.exists() or not pack_dir.is_dir():
        return []

    return sorted(wav.stem for wav in pack_dir.glob("*.wav"))


def _patches_root() -> Path:
    raw = os.environ.get("VCPI_PATCHES_DIR", "patches")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _vcv_patch_names() -> list[str]:
    root = _patches_root()
    if not root.exists() or not root.is_dir():
        return []

    out: list[str] = []
    for patch_file in root.rglob("*.vcv"):
        try:
            rel = patch_file.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel.lower().endswith(".vcv"):
            rel = rel[:-4]
        out.append(rel)
    return sorted(out)


def _vst_search_dirs() -> list[Path]:
    env_tokens: list[str] = []
    for key in ("VST3_PATH", "VST_PATH"):
        raw = os.environ.get(key)
        if raw:
            env_tokens.extend(part for part in raw.split(os.pathsep) if part)

    candidates = [
        *env_tokens,
        str(_repo_root() / "vst3"),
        str(Path.cwd() / "vst3"),
        str(Path.cwd()),
        str(_repo_root()),
        "~/.vst3",
        "/usr/lib/vst3",
        "/usr/local/lib/vst3",
        "~/Library/Audio/Plug-Ins/VST3",
        "/Library/Audio/Plug-Ins/VST3",
    ]

    out: list[Path] = []
    seen: set[Path] = set()
    for token in candidates:
        path = Path(token).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists() and resolved.is_dir():
            out.append(resolved)
    return out


def _vst_names() -> list[str]:
    names: set[str] = set()
    for base in _vst_search_dirs():
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() == ".vst3":
                names.add(entry.stem)
    return sorted(names)


def _sessions_root() -> Path:
    cwd_sessions = Path.cwd() / "sessions"
    if cwd_sessions.exists() and cwd_sessions.is_dir():
        return cwd_sessions
    return _repo_root() / "sessions"


def _session_names() -> list[str]:
    root = _sessions_root()
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        f.stem for f in root.glob("*.json")
        if f.is_file() and not f.name.startswith(".")
    )


def _complete_slot_fx_args(text: str, prefix_tokens: list[str]) -> list[str]:
    """Tab completion for info/knobs: <slot 1-8> [fx] | master."""
    arg_index = len(prefix_tokens) - 1
    slots = [str(i) for i in range(1, 9)]

    if arg_index == 0:
        return _filter_prefix(["master", *slots], text)

    args = prefix_tokens[1:]
    if not args:
        return []

    if args[0] == "master":
        return []

    # <slot> [fx]
    if arg_index == 1:
        return _filter_prefix(["fx"], text)

    return []


def _complete_slot_args(text: str, prefix_tokens: list[str]) -> list[str]:
    """Tab completion for the unified slot command."""
    arg_index = len(prefix_tokens) - 1
    args_before = prefix_tokens[1:]
    slots = [str(i) for i in range(1, 9)]

    # slot <slot|master>
    if arg_index == 0:
        return _filter_prefix(["master", *slots], text)

    if not args_before:
        return []

    target = args_before[0]

    if target == "master":
        # slot master fx ...
        if arg_index == 1:
            return _filter_prefix(["fx"], text)
        if arg_index == 2 and len(args_before) >= 2 and args_before[1] == "fx":
            return _filter_prefix(["clear", *_vst_names()], text)
        return []

    # slot <num> <subcommand> ...
    if arg_index == 1:
        return _filter_prefix(list(SLOT_TYPES), text)

    if len(args_before) < 2:
        return []

    mode = args_before[1].lower()

    if mode == "vst":
        if arg_index == 2:
            return _filter_prefix(_vst_names(), text)
        return []

    if mode == "vcv":
        if arg_index == 2:
            return _filter_prefix(_vcv_patch_names(), text)
        return []

    if mode == "wav":
        if arg_index == 2:
            return _filter_prefix(_sample_pack_names(), text)
        if arg_index == 3 and len(args_before) >= 3:
            return _filter_prefix(_sample_names(args_before[2]), text)
        return []

    if mode == "fx":
        if arg_index == 2:
            return _filter_prefix(["clear", *_vst_names()], text)
        return []

    return []


def connect(sock_path: str | Path | None = None):
    """Connect to the vcpi server and run an interactive REPL."""
    path = Path(sock_path) if sock_path else DEFAULT_SOCK_PATH

    if not path.exists():
        print(f"Error: socket {path} not found. Is the server running?",
              file=sys.stderr)
        sys.exit(1)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except (ConnectionRefusedError, OSError) as e:
        print(f"Error: cannot connect to {path}: {e}", file=sys.stderr)
        sys.exit(1)

    rfile = sock.makefile("r", encoding="utf-8", errors="replace")
    wfile = sock.makefile("w", encoding="utf-8")

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    console = Console() if interactive else None

    try:
        # Keep socket cleanup active during banner and completion discovery too.
        if not _read_response(rfile, console):
            return
        command_names = _fetch_command_names(rfile, wfile)
        session = (
            PromptSession(
                completer=CommandCompleter(command_names),
                history=InMemoryHistory(),
                complete_while_typing=False,
            )
            if interactive else None
        )
        while True:
            try:
                line = session.prompt("vcpi> ") if session else input("vcpi> ")
            except KeyboardInterrupt:
                # Ctrl-C: cancel current input line and keep the client open.
                print()
                continue
            except EOFError:
                # Ctrl-D -> disconnect gracefully
                print()
                break

            try:
                wfile.write(line + "\n")
                wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Server already closed the socket.
                break

            if not _read_response(rfile, console, line):
                # Server closed the connection (e.g. after quit)
                break

            if line.strip().lower() in {"quit", "exit", "shutdown"}:
                # Explicit disconnect command completed.
                break

    finally:
        try:
            wfile.close()
        except OSError:
            pass
        try:
            rfile.close()
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class CommandCompleter(Completer):
    """Complete commands and asset arguments using the existing discovery rules."""

    def __init__(self, command_names: list[str]):
        super().__init__()
        self.names: list[str] = sorted(set(command_names))

    def get_completions(
        self, document: Document, complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        before = document.text_before_cursor
        match = re.search(r"\S*$", before)
        text = match.group() if match else ""
        prefix_tokens = before[:len(before) - len(text)].split()

        if not prefix_tokens:
            matches = _filter_prefix(self.names, text)
        elif prefix_tokens[0] == "slot":
            matches = _complete_slot_args(text, prefix_tokens)
        elif prefix_tokens[0] in ("info", "knobs"):
            matches = _complete_slot_fx_args(text, prefix_tokens)
        elif prefix_tokens[0] in ("save", "load"):
            matches = _filter_prefix(_session_names(), text)
        else:
            return

        for name in matches:
            yield Completion(name + " ", start_position=-len(text), display=name)


def _fetch_command_names(rfile, wfile) -> list[str]:
    """Fetch command names from server help output for completion."""
    try:
        wfile.write("help\n")
        wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return list(FALLBACK_COMMANDS)

    lines = _read_response_lines(rfile)
    if lines is None:
        return list(FALLBACK_COMMANDS)

    parsed = _parse_help_commands(lines)
    return parsed or list(FALLBACK_COMMANDS)


def _parse_help_commands(lines: list[str]) -> list[str]:
    """Extract command tokens from cmd.Cmd style help output."""
    commands: list[str] = []
    seen: set[str] = set()
    in_custom_help = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        lower = line.lower()
        if (
            lower.startswith("documented commands")
            or lower.startswith("undocumented commands")
            or lower.startswith("miscellaneous help topics")
        ):
            continue

        if lower.startswith("available commands"):
            in_custom_help = True
            continue

        if lower.startswith("tip:"):
            continue

        if lower.startswith("no commands available"):
            continue

        if set(line) <= {"=", "-"}:
            continue

        if in_custom_help:
            if line.endswith(":"):
                continue
            # Group headings are skipped above; command rows contain a name
            # followed by a summary.
            token = line.split()[0]
            if token == "EOF":
                continue
            if not HELP_TOKEN_RE.fullmatch(token):
                continue
            if token in seen:
                continue
            seen.add(token)
            commands.append(token)
            continue

        for token in line.split():
            if token == "EOF":
                continue
            if not HELP_TOKEN_RE.fullmatch(token):
                continue
            if token in seen:
                continue
            seen.add(token)
            commands.append(token)

    return commands


def _read_response(rfile, console: Console | None = None, command: str = "") -> bool:
    """Display one response; non-interactive clients preserve the wire text."""
    lines = _read_response_lines(rfile)
    if lines is None:
        return False
    if console is not None:
        display_response(console, command, lines)
    else:
        for line in lines:
            print(line)
    return True


def _read_response_lines(rfile) -> list[str] | None:
    """Read one protocol response and return all lines before sentinel."""
    lines: list[str] = []
    while True:
        line = rfile.readline()
        if not line:
            # EOF -- server closed the connection
            return None
        line = line.rstrip("\n")
        if line == END_OF_RESPONSE:
            return lines
        lines.append(line)
