"""Interactive command-line interface for vcpi.

All slot numbers and MIDI channels are presented 1-based to the user
(slots 1-8, MIDI channels 1-16) and converted to 0-based internally.
"""

from __future__ import annotations

import cmd
import inspect
import logging
import os
import shlex
from pathlib import Path

from core.completion import complete_line
from core.deps import HAS_LINK, HAS_RTMIDI, HAS_MIDO
from core.host import VcpiCore
from core.midi import list_midi_input_ports, list_midi_output_ports
from core.models import NUM_SLOTS
from core.logging_setup import set_log_level
from core.sequencer import NUM_SEQ_BANKS, midi_to_note_name
from core.processes import render_processes
from graph.signal_flow import build_flow_snapshot, render_flow_snapshot
from graph.plugin_info import render_plugin_info
from graph.status import format_output_device, format_realtime, render_status


VCPI_ASCII_LOGO = r"""
                                 ███
                                ░░░
 █████ █████  ██████  ████████  ████
░░███ ░░███  ███░░███░░███░░███░░███
 ░███  ░███ ░███ ░░░  ░███ ░███ ░███
 ░░███ ███  ░███  ███ ░███ ░███ ░███
  ░░█████   ░░██████  ░███████  █████
   ░░░░░     ░░░░░░   ░███░░░  ░░░░░
                      ░███
                      █████
                     ░░░░░
""".strip("\n")


def _slot_to_internal(user_slot: int) -> int:
    """Convert 1-based user slot to 0-based index, with validation."""
    if not 1 <= user_slot <= NUM_SLOTS:
        raise ValueError(f"slot must be 1-{NUM_SLOTS}")
    return user_slot - 1


def _ch_to_internal(user_ch: int) -> int:
    """Convert 1-based user MIDI channel to 0-based, with validation."""
    if not 1 <= user_ch <= 16:
        raise ValueError("MIDI channel must be 1-16")
    return user_ch - 1


class HostCLI(cmd.Cmd):
    intro = (
        "\n"
        f"{VCPI_ASCII_LOGO}\n"
        "Type 'help' for available commands.\n"
        "Slots are numbered 1-8.  MIDI channels are numbered 1-16."
    )
    prompt = "vcpi> "
    SLOT_TYPES = ("vst", "wav", "vcv", "fx", "clear")

    def __init__(self, host: VcpiCore, stdout=None, owns_host: bool = True):
        super().__init__(stdout=stdout)
        self.host = host
        # When True, quit/exit will call host.shutdown().
        # Set to False when running behind the socket server (the server
        # manages the host lifecycle).
        self._owns_host = owns_host
        # Set by `shutdown` so the socket server can terminate the daemon.
        self._shutdown_requested = False

    # -- helper for redirectable output --------------------------------------

    def _print(self, *args, **kwargs):
        """Print to self.stdout so output is captured in server mode."""
        kwargs.setdefault("file", self.stdout)
        print(*args, **kwargs)

    @staticmethod
    def _doc_summary(doc: str | None) -> str:
        """Condense a command docstring to a short one-line summary."""
        if not doc:
            return "-"

        first = doc.strip().splitlines()[0].strip()
        if ":" in first:
            first = first.split(":", 1)[0].strip()
        return first.rstrip(".") or "-"

    def _command_entries(self) -> list[tuple[str, str]]:
        """Return sorted (command, summary) rows for help output."""
        entries: list[tuple[str, str]] = []

        for attr in dir(self.__class__):
            if not attr.startswith("do_"):
                continue

            command = attr[3:]
            if not command or command == "EOF" or command.startswith("_"):
                continue

            handler = getattr(self, attr, None)
            if not callable(handler):
                continue

            if command == "exit":
                summary = "Alias for quit"
            else:
                summary = self._doc_summary(inspect.getdoc(handler))

            entries.append((command, summary))

        entries.sort(key=lambda item: item[0])
        return entries

    def do_help(self, arg):
        """Show help: help [command]"""
        topic = arg.strip()
        if topic:
            command = topic.split()[0]
            if command == "EOF":
                self._print("No help for 'EOF'")
                return

            handler = getattr(self, f"do_{command}", None)
            if not callable(handler):
                self._print(f"No help for '{command}'")
                return

            doc = inspect.getdoc(handler)
            if doc:
                self._print(doc)
                if command == "exit":
                    self._print("Alias of: quit")
            else:
                self._print(f"No help for '{command}'")
            return

        entries = self._command_entries()
        if not entries:
            self._print("No commands available.")
            return

        width = max(len(name) for name, _ in entries)
        self._print("Available commands:")
        groups = (
            ("Instruments & effects", {"slot", "get", "set", "info"}),
            ("Volume, mute & solo", {"gain", "mute", "solo"}),
            ("Audio devices & output", {"audio"}),
            ("MIDI ports & routing", {"midi", "midimix", "note"}),
            ("Sequencing, tempo & Link", {"seq", "tempo", "ableton"}),
            ("Save & load sessions", {"save", "load"}),
            ("Status, signal & logging", {"status", "ps", "flow", "deps", "debug"}),
            ("Help, exit & shutdown", {"help", "about", "quit", "exit", "shutdown"}),
        )
        remaining = dict(entries)
        for title, commands in groups:
            rows = [(name, summary) for name, summary in entries if name in commands]
            if not rows:
                continue
            self._print(f"\n{title}:")
            for name, summary in rows:
                self._print(f"  {name:<{width}}  {summary}")
                remaining.pop(name)
        if remaining:
            self._print("\nOther commands:")
            for name, summary in remaining.items():
                self._print(f"  {name:<{width}}  {summary}")
        self._print()
        self._print("Tip: use 'help <command>' for detailed usage.")

    # -- plugins -------------------------------------------------------------

    def _slot_usage(self) -> str:
        return (
            "slot <1-8> vst <path|vst_name> [--name <label>] | "
            "slot <1-8> wav <pack> <sample> [name] | "
            "slot <1-8> vcv <patch_name[.vcv]> [name] | "
            "slot <1-8|master> fx <path|vst_name> [--name <label>] | "
            "slot <1-8> clear | "
            "slot <1-8|master> fx clear <fx_index>"
        )

    @staticmethod
    def _parse_vst_args(text: str) -> tuple[str, str | None]:
        tokens = shlex.split(text)
        name = None
        if "--name" in tokens:
            marker = tokens.index("--name")
            name = " ".join(tokens[marker + 1:])
            if not name or "--name" in tokens[marker + 1:]:
                raise ValueError("--name requires a single display label")
            tokens = tokens[:marker]
        plugin = " ".join(tokens)
        if not plugin:
            raise ValueError("VST path/name is required")
        return plugin, name

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parent.parent

    def _samples_root(self) -> Path:
        cwd_samples = Path.cwd() / "sampler" / "samples"
        if cwd_samples.exists() and cwd_samples.is_dir():
            return cwd_samples
        return self._repo_root() / "sampler" / "samples"

    def _patches_root(self) -> Path:
        patches_dir = self.host.patches_dir
        if not patches_dir.is_absolute():
            patches_dir = Path.cwd() / patches_dir
        return patches_dir

    def _vst_search_dirs(self) -> list[Path]:
        env_tokens: list[str] = []
        for key in ("VST3_PATH", "VST_PATH"):
            raw = os.environ.get(key)
            if raw:
                env_tokens.extend(part for part in raw.split(os.pathsep) if part)

        candidates = [
            *env_tokens,
            str(self._repo_root() / "vst3"),
            str(Path.cwd() / "vst3"),
            str(Path.cwd()),
            str(self._repo_root()),
            "~/.vst3",
            "/usr/lib/vst3",
            "/usr/local/lib/vst3",
            "~/Library/Audio/Plug-Ins/VST3",
            "/Library/Audio/Plug-Ins/VST3",
        ]

        found: list[Path] = []
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
                found.append(resolved)
        return found

    def _iter_vst_plugin_paths(self) -> list[Path]:
        plugins: dict[str, Path] = {}
        for base in self._vst_search_dirs():
            try:
                entries = list(base.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.suffix.lower() != ".vst3":
                    continue
                plugins.setdefault(str(entry), entry)
        return sorted(plugins.values(), key=lambda p: p.name.lower())

    def _resolve_vst_token(self, token: str) -> str:
        token = token.strip()
        if not token:
            raise ValueError("VST path/name is required")

        path = Path(token).expanduser()
        if path.exists():
            return str(path)

        if token.startswith(("/", "./", "../", "~")):
            return str(path)

        key = token.lower()
        exact: list[Path] = []
        prefix: list[Path] = []

        for candidate in self._iter_vst_plugin_paths():
            stem = candidate.stem.lower()
            name = candidate.name.lower()

            if key in {stem, name} or (not key.endswith(".vst3") and f"{key}.vst3" == name):
                exact.append(candidate)
                continue

            if stem.startswith(key) or name.startswith(key):
                prefix.append(candidate)

        if len(exact) == 1:
            return str(exact[0])
        if len(exact) > 1:
            preview = ", ".join(sorted({p.stem for p in exact})[:5])
            raise ValueError(f"ambiguous VST name '{token}' (matches: {preview})")

        if len(prefix) == 1:
            return str(prefix[0])
        if len(prefix) > 1:
            preview = ", ".join(sorted({p.stem for p in prefix})[:5])
            raise ValueError(f"ambiguous VST name '{token}' (matches: {preview})")

        return token

    @staticmethod
    def _filter_prefix(values: list[str], prefix: str) -> list[str]:
        wanted = prefix.lower()
        return sorted(v for v in values if v.lower().startswith(wanted))

    def _sample_pack_names(self) -> list[str]:
        root = self._samples_root()
        if not root.exists() or not root.is_dir():
            return []
        packs: list[str] = []
        for entry in root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                packs.append(entry.name)
        return sorted(packs)

    def _sample_names(self, pack_name: str) -> list[str]:
        pack = Path(pack_name.strip().strip("/"))
        if pack.is_absolute() or ".." in pack.parts:
            return []

        pack_dir = self._samples_root() / pack
        if not pack_dir.exists() or not pack_dir.is_dir():
            return []

        samples: list[str] = []
        for wav in pack_dir.glob("*.wav"):
            samples.append(wav.stem)
        return sorted(samples)

    def _vcv_patch_names(self) -> list[str]:
        root = self._patches_root()
        if not root.exists() or not root.is_dir():
            return []

        patches: list[str] = []
        for patch_file in root.rglob("*.vcv"):
            try:
                rel = patch_file.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel.lower().endswith(".vcv"):
                rel = rel[:-4]
            patches.append(rel)
        return sorted(patches)

    def _vst_names(self) -> list[str]:
        return sorted({path.stem for path in self._iter_vst_plugin_paths()})

    def cmdloop(self, intro=None):
        """Let readline replace a whole completion span, including plugin words."""
        if not self.use_rawinput or not self.completekey:
            return super().cmdloop(intro)
        try:
            import readline
        except ImportError:
            return super().cmdloop(intro)
        delimiters = readline.get_completer_delims()
        try:
            readline.set_completer_delims("\n")
            return super().cmdloop(intro)
        finally:
            readline.set_completer_delims(delimiters)

    def complete(self, text, state):
        """Bridge cmd.Cmd's readline API to the shared daemon grammar."""
        if state == 0:
            import readline

            before = readline.get_line_buffer()[:readline.get_endidx()]
            result = complete_line(self, before)
            start = len(before) + result["start_position"]
            preserved = before[readline.get_begidx():start]
            self.completion_matches = [
                preserved + match + " " for match in result["matches"]
            ]
        try:
            return self.completion_matches[state]
        except IndexError:
            return None

    def do_slot(self, arg):
        """Slot management: slot <1-8> vst <path|name> [--name <label>] | slot <1-8> wav <pack> <sample> [name] | slot <1-8> vcv <patch> [name] | slot <1-8|master> fx <path|name> [--name <label>] | slot <1-8> clear | slot <1-8|master> fx clear <fx_index>"""
        text = arg.strip()
        if not text:
            self._print(f"Usage: {self._slot_usage()}")
            return

        parts = text.split()
        target = parts[0]

        # Determine slot index (or None for master).
        if target == "master":
            slot_num = None
        else:
            try:
                slot_num = int(target)
                _slot_to_internal(slot_num)  # validate
            except ValueError as e:
                self._print(f"Error: {e}")
                self._print(f"Usage: {self._slot_usage()}")
                return

        if len(parts) < 2:
            self._print(f"Usage: {self._slot_usage()}")
            return

        mode = parts[1].lower()

        # -- slot <num> clear ------------------------------------------------
        if mode == "clear" and slot_num is not None:
            idx = _slot_to_internal(slot_num)
            try:
                removed = self.host.remove_instrument(idx)
                self.host.refresh_mixer_leds([idx])
                self._print(f"  slot {slot_num} cleared ({removed.name})")
            except Exception as e:
                self._print(f"Error: {e}")
            return

        # -- slot <num|master> fx ... ----------------------------------------
        if mode == "fx":
            if len(parts) < 3:
                self._print(f"Usage: slot <1-8|master> fx <path|vst_name> [--name <label>] | slot <1-8|master> fx clear <fx_index>")
                return

            slot_idx = None if slot_num is None else _slot_to_internal(slot_num)

            # slot <num|master> fx clear <fx_index>
            if parts[2].lower() == "clear":
                if len(parts) < 4:
                    self._print(f"Usage: slot <1-8|master> fx clear <fx_index>")
                    return
                try:
                    fx_idx = int(parts[3]) - 1
                except ValueError:
                    self._print("Error: fx_index must be a positive integer")
                    return
                if fx_idx < 0:
                    self._print("Error: fx_index must be >= 1")
                    return
                try:
                    self.host.remove_effect(slot_idx, fx_idx)
                    self._print("  Removed.")
                except Exception as e:
                    self._print(f"Error: {e}")
                return

            # slot <num|master> fx <path|vst_name> [--name <label>]
            try:
                path_token, name = self._parse_vst_args(text.split(maxsplit=2)[2])
                path = self._resolve_vst_token(path_token)
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                self.host.load_effect(path, slot_idx, name)
            except Exception as e:
                self._print(f"Error: {e}")
            return

        # Remaining modes require a numeric slot.
        if slot_num is None:
            self._print("Error: master only supports 'fx' subcommand")
            self._print(f"Usage: {self._slot_usage()}")
            return

        idx = _slot_to_internal(slot_num)

        # -- slot <num> vst <path|name> [--name <label>] ----------------------
        if mode == "vst":
            rest = text.split(maxsplit=2)
            if len(rest) < 3:
                self._print(f"Usage: slot <1-8> vst <path|vst_name> [--name <label>]")
                return
            try:
                path_token, name = self._parse_vst_args(rest[2])
                path = self._resolve_vst_token(path_token)
                slot = self.host.load_instrument(idx, path, name)
                self._print(f"  slot {slot_num} = {slot.name}")
                self._print(f"  vst      : {slot.path}")
            except Exception as e:
                self._print(f"Error: {e}")
            return

        # -- slot <num> vcv <patch> [name] -----------------------------------
        if mode == "vcv":
            rest = text.split(maxsplit=3)
            if len(rest) < 3:
                self._print(f"Usage: slot <1-8> vcv <patch_name[.vcv]> [name]")
                return
            patch_name = rest[2]
            name = rest[3] if len(rest) > 3 else None
            try:
                slot, patch_result, cardinal_path, patch_path = self.host.load_vcv_patch(
                    idx, patch_name, name=name,
                )
            except Exception as e:
                self._print(f"Error: {e}")
                return
            self._print(f"  slot {slot_num} = {slot.name}")
            self._print(f"  patch    : {patch_path}")
            self._print(f"  cardinal : {cardinal_path}")
            self._print(f"  result   : {patch_result}")
            self._print(f"  route with: midi link <channel 1-16> {slot_num}")
            return

        # -- slot <num> wav <pack> <sample> [name] ---------------------------
        if mode == "wav":
            rest = text.split(maxsplit=4)
            if len(rest) < 4:
                self._print(f"Usage: slot <1-8> wav <pack> <sample> [name]")
                return
            pack_name = rest[2].strip().strip("/")
            sample_name = rest[3].strip()
            display_name = rest[4].strip() if len(rest) > 4 else None

            if not pack_name:
                self._print("Error: pack name is required")
                return
            if not sample_name:
                self._print("Error: sample name is required")
                return

            pack_path = Path(pack_name)
            if pack_path.is_absolute() or ".." in pack_path.parts:
                self._print("Error: invalid pack name")
                return

            wav_file = sample_name if sample_name.lower().endswith(".wav") else f"{sample_name}.wav"
            sample_path = Path(wav_file)
            if sample_path.is_absolute() or ".." in sample_path.parts:
                self._print("Error: invalid sample name")
                return

            wav_path = Path("sampler") / "samples" / pack_path / sample_path

            try:
                slot = self.host.load_wav(idx, str(wav_path), display_name)
            except Exception as e:
                self._print(f"Error: {e}")
                return

            self._print(f"  slot {slot_num} = {slot.name}")
            self._print(f"  wav      : {slot.path}")
            self._print(f"  route with: midi link <channel 1-16> {slot_num}")
            return

        self._print(f"Error: unknown subcommand '{mode}'")
        self._print(f"Usage: {self._slot_usage()}")

    def _parameter_target(self, parts, *, require_master_index=False):
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
            effects = self.host.engine.master_effects
            if not effects:
                raise ValueError("No master effects loaded")
            if not 0 <= fx_idx < len(effects):
                raise ValueError("fx_index out of range")
            plugin = effects[fx_idx]
        else:
            idx = _slot_to_internal(int(parts[0]))
            slot = self.host.engine.slots[idx]
            if slot is None:
                raise ValueError(f"Slot {parts[0]} is empty")
            consumed = 1
            plugin = slot.plugin
            if len(parts) > 1 and parts[1] == "fx":
                if len(parts) < 3:
                    raise ValueError("missing slot fx_index")
                try:
                    fx_idx = int(parts[2]) - 1
                except ValueError:
                    raise ValueError("fx_index must be a positive integer") from None
                if not 0 <= fx_idx < len(slot.effects):
                    raise ValueError("fx_index out of range")
                plugin = slot.effects[fx_idx]
                consumed = 3
        if plugin is None:
            raise ValueError("No plugin loaded")
        return idx, fx_idx, plugin, parts[consumed:]

    def do_get(self, arg):
        """Read cached plugin parameters, including units, ranges, and choices.

        get <slot 1-8> [name]
        get <slot 1-8> fx <fx_index> [name]
        get master [fx_index] [name]

        Omit name to list all parameters; master defaults to its first effect.
        MIDI CC helpers are hidden. This command never calls live plugin getters.
        Examples: get 1; get 1 cutoff; get 1 fx 2; get master 1 cutoff
        """
        try:
            _, _, plugin, remaining = self._parameter_target(shlex.split(arg))
            if len(remaining) > 1:
                raise ValueError("expected at most one parameter name")
            parameters = {name: param for name, param in plugin.parameters.items()
                          if not param.is_midi_cc_helper}
            if remaining:
                name = remaining[0]
                if name not in parameters:
                    raise ValueError(f"unknown parameter '{name}'")
                parameters = {name: parameters[name]}
            if not parameters:
                self._print("  No parameters")
            for name, parameter in parameters.items():
                value = plugin.values.get(name, parameter.string_value or "???")
                units = f" {parameter.units}" if parameter.units else ""
                details = ""
                if parameter.kind == "enum" and parameter.valid_values:
                    details = f"  (choices {', '.join(parameter.valid_values)})"
                elif parameter.kind == "float" and len(parameter.range) >= 2:
                    low, high = parameter.range[:2]
                    if low is not None and high is not None:
                        details = f"  (range {low:.4g} .. {high:.4g})"
                self._print(f"  {name} = {value}{units}{details}")
        except ValueError as error:
            self._print(f"Error: {error}")
            self._print("Usage: get <slot> [fx <index>] [name] | get master [index] [name]")

    def do_set(self, arg):
        """Set a plugin parameter and show its actual applied value.

        set <slot 1-8> <name> <value>
        set <slot 1-8> fx <fx_index> <name> <value>
        set master <fx_index> <name> <value>

        Boolean values accept true/false; quote enum values containing spaces.
        Examples: set 1 cutoff 1000; set 1 fx 2 bypass true
                  set master 1 mode "Quiet Mode"
        """
        try:
            idx, fx_idx, plugin, remaining = self._parameter_target(
                shlex.split(arg), require_master_index=True)
            if len(remaining) != 2:
                raise ValueError("expected a parameter name and one value")
            pname, pval = remaining
            parameter = plugin.parameters.get(pname)
            if parameter is None:
                raise ValueError(f"unknown parameter '{pname}'")
            if parameter.kind != "enum":
                try:
                    pval = float(pval)
                except ValueError:
                    if pval.lower() in ("true", "false"):
                        pval = pval.lower() == "true"
        except ValueError as error:
            self._print(f"Error: {error}")
            self._print("Usage: set <slot> [fx <index>] <name> <value> | set master <index> <name> <value>")
            return
        try:
            actual = self.host.engine.set_parameter(idx, pname, pval, fx_index=fx_idx)
            self._print(f"  {pname} = {actual}")
        except Exception as error:
            self._print(f"Error: {error}")

    # -- gain / mute / solo --------------------------------------------------


    def do_gain(self, arg):
        """Set gain: gain <slot 1-8> <0.0-1.0> | gain master [0.0-1.0]"""
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: gain <slot 1-8> <value> | gain master [value]")
            return

        if parts[0] == "master":
            if len(parts) < 2:
                self._print(f"  master gain = {self.host.engine.master_gain:.2f}")
                return
            try:
                gain = float(parts[1])
            except ValueError:
                self._print("Error: gain must be a number")
                return
            if not 0.0 <= gain <= 1.0:
                self._print("Error: gain must be between 0.0 and 1.0")
                return
            try:
                self.host.engine.set_master_gain(gain)
                self._print(f"  master gain = {self.host.engine.master_gain:.2f}")
            except Exception as e:
                self._print(f"Error: {e}")
            return

        if len(parts) < 2:
            self._print("Usage: gain <slot 1-8> <value> | gain master [value]")
            return
        try:
            idx = _slot_to_internal(int(parts[0]))
        except ValueError as e:
            self._print(f"Error: {e}")
            return
        slot = self.host.engine.slots[idx]
        if slot is None:
            self._print(f"Error: slot {parts[0]} is empty")
            return

        try:
            gain = float(parts[1])
        except ValueError:
            self._print("Error: gain must be a number")
            return

        if not 0.0 <= gain <= 1.0:
            self._print("Error: gain must be between 0.0 and 1.0")
            return

        try:
            slot = self.host.engine.set_slot_state(idx, gain=gain)
            self._print(f"  gain = {slot.gain:.2f}")
        except Exception as e:
            self._print(f"Error: {e}")

    def do_mute(self, arg):
        """Toggle mute: mute <slot 1-8>"""
        token = arg.strip()
        if not token:
            self._print("Usage: mute <slot 1-8>")
            return
        try:
            idx = _slot_to_internal(int(token))
        except ValueError as e:
            self._print(f"Error: {e}")
            return
        slot = self.host.engine.slots[idx]
        if slot is None:
            self._print(f"Error: slot {token} is empty")
            return

        try:
            slot = self.host.engine.set_slot_state(idx, muted=not slot.muted)
            self.host.refresh_mixer_leds([idx])
            self._print(f"  {slot.name}: {'MUTED' if slot.muted else 'unmuted'}")
        except Exception as e:
            self._print(f"Error: {e}")

    def do_solo(self, arg):
        """Toggle solo: solo <slot 1-8>"""
        token = arg.strip()
        if not token:
            self._print("Usage: solo <slot 1-8>")
            return
        try:
            idx = _slot_to_internal(int(token))
        except ValueError as e:
            self._print(f"Error: {e}")
            return
        slot = self.host.engine.slots[idx]
        if slot is None:
            self._print(f"Error: slot {token} is empty")
            return

        try:
            slot = self.host.engine.set_slot_state(idx, solo=not slot.solo)
            self.host.refresh_mixer_leds([idx])
            self._print(f"  {slot.name}: {'SOLO' if slot.solo else 'unsolo'}")
        except Exception as e:
            self._print(f"Error: {e}")

    # -- routing / MIDI ------------------------------------------------------

    def do_flow(self, arg):
        """Explore cached signal routing: flow [all|midi|controls|audio|1-8|master|explore]

        With no argument, show the compact overview. Named views and slot
        numbers show a detailed table; all prints every table. explore opens
        the full-screen read-only explorer in the interactive client only.
        No hardware discovery, owner requests, or routing changes are performed.
        """
        parts = arg.strip().split()
        views = {"all", "midi", "controls", "audio", "master", "explore"}
        if len(parts) > 1 or (parts and parts[0] not in views
                              and parts[0] not in {str(i) for i in range(1, NUM_SLOTS + 1)}):
            self._print("Usage: flow [all|midi|controls|audio|1-8|master|explore]")
            return
        view = parts[0] if parts else "overview"
        if view == "explore":
            self._print("flow explore requires the interactive client; use flow all for plain output.")
            return
        snapshot = build_flow_snapshot(self.host)
        section_id = f"slot{view}" if view.isdecimal() else view
        self._print(render_flow_snapshot(snapshot, None if view == "all" else section_id))

    def do_info(self, arg):
        """Show plugin info: info <slot 1-8> | info <slot 1-8> fx <fx_index> | info master <fx_index>"""
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: info <slot 1-8> | info <slot 1-8> fx <fx_index> | info master <fx_index>")
            return

        if parts[0] == "master":
            if not self.host.engine.master_effects:
                self._print("  No master effects loaded")
                return
            try:
                fx_idx = int(parts[1]) - 1 if len(parts) > 1 else 0
            except ValueError:
                self._print("Error: fx_index must be a positive integer")
                return
            if not 0 <= fx_idx < len(self.host.engine.master_effects):
                self._print("Error: fx_index out of range")
                return
            plugin = self.host.engine.master_effects[fx_idx]
            label = f"Master FX {fx_idx + 1}"
        else:
            try:
                idx = _slot_to_internal(int(parts[0]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            slot = self.host.engine.slots[idx]
            if slot is None:
                self._print(f"  Slot {parts[0]} is empty")
                return

            # info <slot> fx <fx_index>  -- show info for a slot effect
            if len(parts) >= 2 and parts[1] == "fx":
                if not slot.effects:
                    self._print(f"  Slot {parts[0]} has no effects")
                    return
                try:
                    fx_idx = int(parts[2]) - 1 if len(parts) > 2 else 0
                except ValueError:
                    self._print("Error: fx_index must be a positive integer")
                    return
                if not 0 <= fx_idx < len(slot.effects):
                    self._print("Error: fx_index out of range")
                    return
                plugin = slot.effects[fx_idx]
                label = f"Slot {parts[0]} FX {fx_idx + 1}"
            else:
                plugin = slot.plugin
                label = f"Slot {parts[0]}: {slot.name}"

        self._print(render_plugin_info(plugin, label))

    # -- audio ---------------------------------------------------------------

    def do_audio(self, arg):
        """Audio commands: audio start [device] | audio stop | audio devices | audio diagnose

        Subcommands:
          audio start [device]  -- start audio engine
          audio stop            -- stop audio engine
          audio devices         -- list available output devices
          audio diagnose        -- inspect MIDI delivery and lifetime signal peaks
        """
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: audio start [device] | audio stop | audio devices | audio diagnose")
            return

        sub = parts[0]

        if sub == "start":
            dev = " ".join(parts[1:]) if len(parts) > 1 else None
            if dev and dev.isdigit():
                dev = int(dev)
            try:
                self.host.start_audio(dev)
                engine = self.host.engine
                device = format_output_device(engine.audio_output_name, engine.audio_output_index)
                self._print(f"  Audio started: {device} | backend={engine.backend_label}")
            except Exception as e:
                self._print(f"Error: {e}")

        elif sub == "stop":
            self.host.stop_audio()

        elif sub == "devices":
            devices = self.host.engine.audio_devices
            if not devices:
                self._print("  No audio devices available.")
            for device in devices:
                engine = self.host.engine
                selected = device["index"] == engine.audio_output_index
                marker = (" [active]" if engine.running else " [last opened]") if selected else ""
                self._print(
                    f"  [{device['index']}] {device['name']} "
                    f"({device['max_input_channels']} in, "
                    f"{device['max_output_channels']} out; {device['hostapi_name']}){marker}"
                )

        elif sub == "diagnose":
            try:
                diagnosis = self.host.engine.request("diagnose")
            except Exception as e:
                self._print(f"Error: {e}")
                return
            output = diagnosis["output"]
            self._print(
                f"  Output: {format_output_device(output['output_name'], output['output_index'])} "
                f"| backend={output['backend_label']} "
                f"| running={output['running']} "
                f"| master gain={output['master_gain']:.3f}"
            )
            self._print(f"  Output realtime: {format_realtime(output['realtime'])}")
            self._print(
                f"  Callbacks={output['callback_count']} "
                f"| render errors={output['render_errors']} "
                f"| underruns={output['underruns']} "
                f"| deadline misses={output['deadline_misses']}"
            )
            self._print("  Peaks are absolute-sample high-water marks, not current levels.")
            self._print(
                f"  Output peak since audio-owner creation "
                f"(final device samples): {output['output_peak']:.6g}"
            )
            if not diagnosis["slots"]:
                self._print("  No loaded slots.")
            for slot in diagnosis["slots"]:
                self._print(
                    f"  Slot {slot['index']}: {slot['name']} "
                    f"| gain={slot['gain']:.3f} "
                    f"| muted={slot['muted']} | solo={slot['solo']}"
                )
                self._print(
                    f"    MIDI sent={slot['midi_sent']} "
                    f"consumed={slot['midi_consumed']} pending={slot['midi_pending']} "
                    f"| missing blocks={slot['misses']}"
                )
                self._print(
                    f"    Audio peak since slot creation "
                    f"(before mixer gain/mute/solo): {slot['audio_peak']:.6g}"
                )
                self._print(f"    Worker failure: {slot['worker_failure'] or 'none'}")
                self._print(f"    Realtime: {format_realtime(slot['realtime'])}")

        else:
            self._print(f"Unknown audio subcommand: {sub}")

    # -- MIDI (unified) ------------------------------------------------------

    def do_midi(self, arg):
        """MIDI commands: midi ports input | midi ports output | midi input <port> | midi input close <index> | midi link <ch> <slot> | midi cut <ch>

        Subcommands:
          midi ports input       -- list MIDI input ports
          midi ports output      -- list MIDI output ports
          midi input <port>      -- open a MIDI input port
          midi input close <idx> -- close a MIDI input by index
          midi link <ch> <slot>  -- route MIDI channel to slot
          midi cut <ch>          -- remove MIDI channel route
        """
        parts = arg.strip().split()
        if not parts:
            self._print(
                "Usage: midi ports input | midi ports output | "
                "midi input <port> | midi input close <index> | "
                "midi link <ch> <slot> | midi cut <ch>"
            )
            return

        sub = parts[0].lower()

        # --- midi ports input / midi ports output ---------------------------
        if sub == "ports":
            if len(parts) < 2:
                self._print("Usage: midi ports input | midi ports output")
                return
            direction = parts[1].lower()
            if direction == "input":
                ports = list_midi_input_ports()
                if not ports:
                    self._print("  No MIDI input ports found.")
                    return
                for i, name in enumerate(ports):
                    self._print(f"  [{i}] {name}")
            elif direction == "output":
                ports = list_midi_output_ports()
                if not ports:
                    self._print("  No MIDI output ports found.")
                    return
                for i, name in enumerate(ports):
                    self._print(f"  [{i}] {name}")
            else:
                self._print("Usage: midi ports input | midi ports output")
            return

        # --- midi input <port> / midi input close <index> -------------------
        if sub == "input":
            if len(parts) < 2:
                self._print("Usage: midi input <port> | midi input close <index>")
                return
            if parts[1].lower() == "close":
                if len(parts) < 3:
                    self._print("Usage: midi input close <index>")
                    return
                try:
                    idx = int(parts[2]) - 1
                    self.host.close_midi_input(idx)
                    self._print("  Closed.")
                except Exception as e:
                    self._print(f"Error: {e}")
            else:
                try:
                    ctrl = self.host.open_midi_input(parts[1])
                    self._print(f"  Opened: {ctrl.port_name}")
                except Exception as e:
                    self._print(f"Error: {e}")
            return

        # --- midi link <ch> <slot> ------------------------------------------
        if sub == "link":
            if len(parts) < 3:
                self._print("Usage: midi link <channel 1-16> <slot 1-8>")
                return
            try:
                ch = _ch_to_internal(int(parts[1]))
                idx = _slot_to_internal(int(parts[2]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            self.host.route(ch, idx)
            self._print(f"  ch {parts[1]} -> slot {parts[2]}")
            return

        # --- midi cut <ch> --------------------------------------------------
        if sub == "cut":
            if len(parts) < 2:
                self._print("Usage: midi cut <channel 1-16>")
                return
            try:
                ch = _ch_to_internal(int(parts[1]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            self.host.unroute(ch)
            self._print(f"  ch {parts[1]} unlinked")
            return

        self._print(
            "Unknown midi subcommand. Use: ports, input, link, cut"
        )

    def do_midimix(self, arg):
        """Akai MIDI Mix: midimix input <port> | midimix output <port>

        Subcommands:
          midimix input <port>   -- open MIDI Mix control input
          midimix output <port>  -- open MIDI Mix LED feedback output
        """
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: midimix input <port> | midimix output <port>")
            return

        sub = parts[0].lower()

        if sub == "input":
            if len(parts) < 2:
                self._print("Usage: midimix input <port_index>")
                return
            try:
                self.host.open_mixer_midi(int(parts[1]))
            except Exception as e:
                self._print(f"Error: {e}")
            return

        if sub == "output":
            if len(parts) < 2:
                self._print("Usage: midimix output <port_index>")
                return
            try:
                self.host.open_mixer_midi_out(int(parts[1]))
            except Exception as e:
                self._print(f"Error: {e}")
            return

        self._print("Usage: midimix input <port> | midimix output <port>")

    def do_note(self, arg):
        """Test note: note <slot 1-8> <midi_note> [vel] [dur_ms]"""
        parts = arg.strip().split()
        if len(parts) < 2:
            self._print("Usage: note <slot 1-8> <note> [velocity] [dur_ms]")
            return
        try:
            idx = _slot_to_internal(int(parts[0]))
        except ValueError as e:
            self._print(f"Error: {e}")
            return
        try:
            n = int(parts[1])
            v = int(parts[2]) if len(parts) > 2 else 100
            d = float(parts[3]) / 1000.0 if len(parts) > 3 else 0.3
        except ValueError:
            self._print("Error: note/velocity must be integers, dur_ms must be numeric")
            return
        if not (0 <= n <= 127):
            self._print(f"Warning: note {n} clamped to {max(0, min(127, n))}")
        if not (0 <= v <= 127):
            self._print(f"Warning: velocity {v} clamped to {max(0, min(127, v))}")
        self.host.send_note(idx, n, v, d)

    # -- sequencer -----------------------------------------------------------

    def do_seq(self, arg):
        """Sequencer: seq | seq <bank> <notes...> | seq link <bank> <slot> | seq cut <slot> | seq clear <bank>

        Examples:
          seq                  -- show all sequence banks
          seq 1 d c b a        -- set bank 1 to D C B A (4 notes per bar)
          seq 1 c              -- set bank 1 to just C (1 note per bar)
          seq 3 C#5 Bb4        -- sharps/flats and octave suffixes work
          seq clear 1          -- clear bank 1
          seq link 1 5         -- attach sequence bank 1 to slot 5
          seq cut 5            -- remove sequences from slot 5
        """
        parts = arg.strip().split()

        # No args: display all banks
        if not parts:
            seq = self.host.sequencer
            any_bank = False
            for bi, bank in enumerate(seq.banks):
                if bank is None:
                    continue
                any_bank = True
                notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
                if bank.linked_slot is not None:
                    link_str = f" -> slot {bank.linked_slot + 1}"
                else:
                    link_str = ""
                self._print(f"  seq {bi + 1}: {notes_str}{link_str}")
            if not any_bank:
                self._print("  No sequences defined. Use: seq <bank> <note> [note ...]")
            return

        # --- seq link <bank> <slot> -----------------------------------------
        if parts[0].lower() == "link":
            if len(parts) < 3:
                self._print("Usage: seq link <bank 1-16> <slot 1-8>")
                return
            try:
                bi = int(parts[1]) - 1
                if not 0 <= bi < NUM_SEQ_BANKS:
                    raise ValueError(f"bank must be 1-{NUM_SEQ_BANKS}")
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                si = _slot_to_internal(int(parts[2]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                self.host.sequencer.link(bi, si)
                self._print(f"  seq {bi + 1} -> slot {si + 1}")
            except ValueError as e:
                self._print(f"Error: {e}")
            return

        # --- seq cut <slot> -------------------------------------------------
        if parts[0].lower() == "cut":
            if len(parts) < 2:
                self._print("Usage: seq cut <slot 1-8>")
                return
            try:
                si = _slot_to_internal(int(parts[1]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            self.host.sequencer.detach_slot(si)
            self._print(f"  slot {si + 1}: sequences detached")
            return

        # --- seq clear <bank> -----------------------------------------------
        if parts[0].lower() == "clear":
            if len(parts) < 2:
                self._print(f"Usage: seq clear <bank 1-{NUM_SEQ_BANKS}>")
                return
            try:
                bank_num = int(parts[1])
                bi = bank_num - 1
                if not 0 <= bi < NUM_SEQ_BANKS:
                    raise ValueError
            except ValueError:
                self._print(f"Error: bank must be 1-{NUM_SEQ_BANKS}")
                return
            self.host.sequencer.clear_bank(bi)
            self._print(f"  seq {bank_num}: cleared")
            return

        # --- remaining forms need a bank number as first arg ----------------
        try:
            bank_num = int(parts[0])
            bi = bank_num - 1
            if not 0 <= bi < NUM_SEQ_BANKS:
                raise ValueError
        except ValueError:
            self._print(f"Error: bank must be 1-{NUM_SEQ_BANKS}")
            return

        # seq <bank> -- show single bank
        if len(parts) == 1:
            bank = self.host.sequencer.banks[bi]
            if bank is None:
                self._print(f"  seq {bank_num}: (empty)")
            else:
                notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
                if bank.linked_slot is not None:
                    link_str = f" -> slot {bank.linked_slot + 1}"
                else:
                    link_str = ""
                self._print(f"  seq {bank_num}: {notes_str}{link_str}")
            return

        # seq <bank> <note> [note ...]
        note_names = parts[1:]
        try:
            bank = self.host.sequencer.set_bank(bi, note_names)
        except ValueError as e:
            self._print(f"Error: {e}")
            return

        notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
        n = len(bank.notes)
        if n == 1:
            timing = "1 note/bar"
        else:
            timing = f"{n} notes/bar (1/{n} spacing)"
        self._print(f"  seq {bank_num}: {notes_str}  ({timing})")

    # -- link ----------------------------------------------------------------

    def do_ableton(self, arg):
        """Ableton Link commands: ableton link [bpm] | ableton cut

        Subcommands:
          ableton link [bpm]  -- enable Ableton Link (optionally set BPM)
          ableton cut         -- disable Ableton Link
        """
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: ableton link [bpm] | ableton cut")
            return

        sub = parts[0]

        if sub == "link":
            if len(parts) > 1:
                try:
                    bpm = float(parts[1])
                except ValueError:
                    self._print("Error: bpm must be a number")
                    return
            else:
                bpm = None
            try:
                self.host.start_link(bpm)
                self._print(f"  Link enabled at {self.host.link.bpm:.1f} BPM")
            except Exception as e:
                self._print(f"Error: {e}")

        elif sub == "cut":
            self.host.stop_link()
            self._print("  Link disabled")

        else:
            self._print(f"Unknown ableton subcommand: {sub}")

    def do_tempo(self, arg):
        """Get/set tempo: tempo [bpm]"""
        if arg.strip():
            try:
                self.host.link.bpm = float(arg.strip())
            except ValueError:
                self._print("Error: bpm must be a number")
                return
        self._print(f"  {self.host.link.bpm:.1f} BPM")

    def do_debug(self, arg):
        """Show or toggle daemon debug logging: debug [on|off]

        debug on enables DEBUG logging in the daemon and live audio workers.
        debug off returns logging to INFO. New workers inherit the setting.
        Changes last until the daemon exits; logs stay in its terminal/service log.
        Raw native/plugin output is discarded unless debug is on, then shown with
        [daemon], [audio], or [slot N] prefixes. No per-process log files are kept.
        For startup diagnostics, launch with LOG_LEVEL=DEBUG ./vcpid.
        """
        mode = arg.strip().lower()
        if mode not in ("", "on", "off"):
            self._print("Usage: debug [on|off]")
            return
        if mode:
            level = "DEBUG" if mode == "on" else "INFO"
            try:
                self.host.engine.request("set_log_level", level=level)
            except Exception as error:
                self._print(f"Error: {error}")
                return
            set_log_level(level)
        level = logging.getLogger().getEffectiveLevel()
        self._print(f"  Debug logging: {'on' if level <= logging.DEBUG else 'off'} "
                    f"(log level: {logging.getLevelName(level)})")


    # -- session -------------------------------------------------------------

    def _sessions_root(self) -> Path:
        """Return the sessions directory (CWD or repo root)."""
        cwd_sessions = Path.cwd() / "sessions"
        if cwd_sessions.exists() and cwd_sessions.is_dir():
            return cwd_sessions
        return self._repo_root() / "sessions"

    def _resolve_session_path(self, name: str) -> Path:
        """Resolve a session name to a full path in the sessions directory."""
        if name.lower().endswith(".json"):
            name = name[:-5]
        return self._sessions_root() / f"{name}.json"

    def _session_names(self) -> list[str]:
        """List available session names for tab completion."""
        root = self._sessions_root()
        if not root.exists() or not root.is_dir():
            return []
        return sorted(
            f.stem for f in root.glob("*.json")
            if f.is_file() and not f.name.startswith(".")
        )


    def do_save(self, arg):
        """Save session: save [<name>]

        Saves the current session to sessions/<name>.json.
        If no name is given, saves to the previously loaded session.
        The automatic session at ~/.config/vcpi/session.json is only
        written on shutdown.
        """
        name = arg.strip()
        if not name:
            if self.host.loaded_session_path is None:
                self._print("Usage: save <name>  (no session loaded)")
                return
            path = self.host.loaded_session_path
        else:
            path = self._resolve_session_path(name)
        try:
            self.host.save_session(str(path))
            self._print(f"  Saved to {path}")
        except Exception as e:
            self._print(f"Error: {e}")

    def do_load(self, arg):
        """Load session: load <name>

        Loads from sessions/<name>.json and sets it as the active
        session (shown in ``status``).  The auto-save file at
        ~/.config/vcpi/session.json is updated so that shutdown
        preserves the loaded state, but the named session file is
        only written when you explicitly run ``save``.
        """
        name = arg.strip()
        if not name:
            self._print("Usage: load <name>")
            return
        path = self._resolve_session_path(name)
        if not path.exists():
            self._print(f"Error: session not found: {path}")
            return
        try:
            self.host.restore_session(str(path))
            self.host.refresh_mixer_leds()
            # Track the loaded session for save-without-args and status.
            self.host.loaded_session_name = name
            self.host.loaded_session_path = path
            # Update the auto-save session so shutdown preserves this state.
            self.host.save_session()
            self._print(f"  Loaded from {path}")
        except Exception as e:
            self._print(f"Error: {e}")

    # -- status --------------------------------------------------------------

    def do_about(self, arg):
        """Show vcpi logo and project info: about"""
        if arg.strip():
            self._print("Usage: about")
            return

        self._print(VCPI_ASCII_LOGO)
        self._print()
        self._print("vcpi - Native VST3 host with Ableton Link tempo sync")
        self._print("License: AGPL-3.0 (see LICENSE); no warranty.")
        self._print("Redistribution is permitted under the license.")
        self._print("Source: https://github.com/deflax/vcpi")

    def do_status(self, arg):
        """Overall status: status"""
        self._print(render_status(self.host))

    def do_ps(self, arg):
        """Show daemon processes and threads with resource usage: ps [all]

        Includes the daemon and all descendants (audio, slots, helpers), not
        the separate CLI client. Samples CPU over ~200 ms; 100% is one core.
        Process rows show total CPU, resident memory (MiB), state and thread
        count. Thread rows show native TIDs and CPU, not separate memory.
        Idle generically named threads are hidden; ps all shows every thread.
        Main threads and known native render threads remain visible.
        """
        mode = arg.strip()
        if mode not in ("", "all"):
            self._print("Usage: ps [all]")
            return
        try:
            self._print(render_processes(self.host, show_all=mode == "all"))
        except (OSError, RuntimeError) as exc:
            self._print(f"Error collecting process resources: {exc}")

    def do_deps(self, arg):
        """Check dependencies."""
        native = self.host.engine.native_dependencies
        for name, ok in [("native audio (JUCE/PortAudio)", native["native"]),
                         ("aalink", HAS_LINK), ("python-rtmidi", HAS_RTMIDI),
                         ("mido", HAS_MIDO)]:
            self._print(f"  {name}: {'OK' if ok else 'MISSING'}")

    def do_quit(self, arg):
        """Exit the current CLI session."""
        if self._owns_host:
            self.host.shutdown()
        return True

    def do_shutdown(self, arg):
        """Shutdown host/server process (for systemd restart)."""
        if arg.strip():
            self._print("Usage: shutdown")
            return

        self._shutdown_requested = True
        self._print("  Shutting down vcpi...")

        if self._owns_host:
            self.host.shutdown()

        return True

    do_exit = do_quit
    do_EOF = do_quit
