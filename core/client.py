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
import readline  # noqa: F401  (enables line-editing in input())
import socket
import sys
from pathlib import Path

from core.paths import DEFAULT_SOCK_PATH

# Must match the sentinel used by server.py
END_OF_RESPONSE = "\x00"


HELP_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Fallback for tab completion in case help parsing fails.
FALLBACK_COMMANDS = (
    "audio_start",
    "audio_stop",
    "audio_devices",
    "deps",
    "exit",
    "gain",
    "graph",
    "help",
    "link",
    "load",
    "master",
    "midi_keys",
    "midi_mix",
    "midi_mix_out",
    "midi_ports_in",
    "midi_ports_out",
    "midi_seq",
    "mute",
    "note",
    "params",
    "quit",
    "restore",
    "route",
    "routing",
    "save",
    "set",
    "shutdown",
    "slots",
    "solo",
    "status",
    "tempo",
    "unload",
    "unlink",
    "unroute",
)

LOAD_TYPES = ("vst", "wav", "vcv", "fx")


def _filter_prefix(values: list[str], prefix: str) -> list[str]:
    wanted = prefix.lower()
    return sorted(v for v in values if v.lower().startswith(wanted))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _samples_root() -> Path:
    cwd_samples = Path.cwd() / "samples"
    if cwd_samples.exists() and cwd_samples.is_dir():
        return cwd_samples
    return _repo_root() / "samples"


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


def _complete_load_args(text: str, prefix_tokens: list[str]) -> list[str]:
    arg_index = len(prefix_tokens) - 1
    args_before = prefix_tokens[1:]

    if arg_index == 0:
        return _filter_prefix(list(LOAD_TYPES), text)

    if not args_before:
        return []

    mode = args_before[0].lower()
    slots = [str(i) for i in range(1, 9)]

    if mode == "wav":
        if arg_index == 1:
            return _filter_prefix(slots, text)
        if arg_index == 2:
            return _filter_prefix(_sample_pack_names(), text)
        if arg_index == 3 and len(args_before) >= 3:
            return _filter_prefix(_sample_names(args_before[2]), text)
        return []

    if mode == "vcv":
        if arg_index == 1:
            return _filter_prefix(slots, text)
        if arg_index == 2:
            return _filter_prefix(_vcv_patch_names(), text)
        return []

    if mode == "vst":
        if arg_index == 1:
            return _filter_prefix(slots, text)
        if arg_index == 2:
            return _filter_prefix(_vst_names(), text)
        return []

    if mode == "fx":
        if arg_index == 1:
            return _filter_prefix(_vst_names(), text)
        if arg_index == 2:
            return _filter_prefix(["master", *slots], text)
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

    # Read and display the welcome banner
    _read_response(rfile)

    # Enable readline tab-completion from live server command names.
    command_names = _fetch_command_names(rfile, wfile)
    _configure_tab_completion(command_names)

    try:
        while True:
            try:
                line = input("vcpi> ")
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

            if not _read_response(rfile):
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


def _configure_tab_completion(command_names: list[str]):
    """Enable command and load-argument completion on Tab."""
    names = sorted(set(command_names))
    if not names:
        return

    def _complete(text: str, state: int):
        line = readline.get_line_buffer()
        begidx = readline.get_begidx()
        prefix_tokens = line[:begidx].split()

        if not prefix_tokens:
            matches = _filter_prefix(names, text)
        elif prefix_tokens[0] == "load":
            matches = _complete_load_args(text, prefix_tokens)
        else:
            return None

        if len(matches) == 1:
            matches = [matches[0] + " "]

        return matches[state] if state < len(matches) else None

    readline.parse_and_bind("tab: complete")
    readline.set_completer(_complete)


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
            # New vcpi help format prints one command per line, followed by
            # a summary; only the first token is the command name.
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


def _read_response(rfile) -> bool:
    """Read lines from the server until the end-of-response sentinel.

    Prints each line to stdout.  Returns ``False`` if the connection
    was closed before a sentinel was seen.
    """
    lines = _read_response_lines(rfile)
    if lines is None:
        return False
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
