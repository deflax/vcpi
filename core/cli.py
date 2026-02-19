"""Interactive command-line interface for vcpi.

All slot numbers and MIDI channels are presented 1-based to the user
(slots 1-8, MIDI channels 1-16) and converted to 0-based internally.
"""

from __future__ import annotations

import cmd
import inspect
import os
from pathlib import Path

from core.deps import HAS_PEDALBOARD, HAS_LINK, HAS_RTMIDI, HAS_MIDO, HAS_SOUNDDEVICE, sd
from core.host import VcpiCore
from core.midi import list_midi_input_ports, list_midi_output_ports
from core.models import NUM_SLOTS
from graph.signal_flow import render_signal_flow
from graph.plugin_info import render_plugin_info
from graph.knobs import render_knobs


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
    intro = r"""
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
Type 'help' for available commands.
Slots are numbered 1-8.  MIDI channels are numbered 1-16.
"""
    prompt = "vcpi> "
    LOAD_TYPES = ("vst", "wav", "vcv", "fx")

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

    def _audio_backend_label(self) -> str:
        """Best-effort host-audio backend label (ALSA/JACK/Pulse/PipeWire/etc.)."""
        if not HAS_SOUNDDEVICE:
            return "sounddevice unavailable"

        try:
            device_index = None

            stream = self.host.engine._stream
            if stream is not None:
                stream_device = getattr(stream, "device", None)
                if isinstance(stream_device, (tuple, list)):
                    if len(stream_device) >= 2 and isinstance(stream_device[1], int):
                        device_index = stream_device[1]
                    elif len(stream_device) == 1 and isinstance(stream_device[0], int):
                        device_index = stream_device[0]
                elif isinstance(stream_device, int):
                    device_index = stream_device

            if not isinstance(device_index, int):
                default_device = sd.default.device
                if isinstance(default_device, (tuple, list)):
                    if len(default_device) >= 2 and isinstance(default_device[1], int):
                        device_index = default_device[1]
                    elif len(default_device) == 1 and isinstance(default_device[0], int):
                        device_index = default_device[0]
                elif isinstance(default_device, int):
                    device_index = default_device

            if not isinstance(device_index, int) or device_index < 0:
                return "unknown"

            device_info = sd.query_devices(device_index)
            hostapi_index = device_info.get("hostapi")
            if not isinstance(hostapi_index, int):
                return "unknown"

            hostapi_name = sd.query_hostapis(hostapi_index).get("name", "unknown")
            device_name = device_info.get("name")
            if device_name:
                return f"{hostapi_name} ({device_name})"
            return str(hostapi_name)
        except Exception:
            return "unknown"

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
        for name, summary in entries:
            self._print(f"  {name:<{width}}  {summary}")
        self._print("Tip: use 'help <command>' for detailed usage.")

    # -- plugins -------------------------------------------------------------

    def _load_usage(self) -> str:
        return (
            "load vst <slot 1-8> <path|vst_name> [name] | "
            "load wav <slot 1-8> <pack> <sample> [name] | "
            "load fx <path|vst_name> [slot 1-8|master] [name] | "
            "load vcv <slot 1-8> <patch_name[.vcv]> [name]"
        )

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parent.parent

    def _samples_root(self) -> Path:
        cwd_samples = Path.cwd() / "samples"
        if cwd_samples.exists() and cwd_samples.is_dir():
            return cwd_samples
        return self._repo_root() / "samples"

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

    def complete_load(self, text, line, begidx, endidx):
        del endidx
        prefix_tokens = line[:begidx].split()
        if not prefix_tokens or prefix_tokens[0] != "load":
            return []

        arg_index = len(prefix_tokens) - 1
        args_before = prefix_tokens[1:]

        if arg_index == 0:
            return self._filter_prefix(list(self.LOAD_TYPES), text)

        if not args_before:
            return []

        mode = args_before[0]

        if mode == "wav":
            if arg_index == 1:
                return self._filter_prefix([str(i) for i in range(1, NUM_SLOTS + 1)], text)
            if arg_index == 2:
                return self._filter_prefix(self._sample_pack_names(), text)
            if arg_index == 3 and len(args_before) >= 3:
                return self._filter_prefix(self._sample_names(args_before[2]), text)
            return []

        if mode == "vcv":
            if arg_index == 1:
                return self._filter_prefix([str(i) for i in range(1, NUM_SLOTS + 1)], text)
            if arg_index == 2:
                return self._filter_prefix(self._vcv_patch_names(), text)
            return []

        if mode == "vst":
            if arg_index == 1:
                return self._filter_prefix([str(i) for i in range(1, NUM_SLOTS + 1)], text)
            if arg_index == 2:
                return self._filter_prefix(self._vst_names(), text)
            return []

        if mode == "fx":
            if arg_index == 1:
                return self._filter_prefix(self._vst_names(), text)
            if arg_index == 2:
                targets = ["master", *[str(i) for i in range(1, NUM_SLOTS + 1)]]
                return self._filter_prefix(targets, text)
            return []

        return []

    def _complete_slot_fx(self, text, prefix_tokens):
        """Shared completion for info/knobs: <slot 1-8> [fx <index>] | master [index]."""
        arg_index = len(prefix_tokens) - 1

        if arg_index == 0:
            targets = ["master", *[str(i) for i in range(1, NUM_SLOTS + 1)]]
            return self._filter_prefix(targets, text)

        args = prefix_tokens[1:]
        if not args:
            return []

        if args[0] == "master":
            # master <fx_index>
            if arg_index == 1:
                n = len(self.host.engine.master_effects)
                return self._filter_prefix([str(i) for i in range(1, n + 1)], text)
            return []

        # <slot> [fx <fx_index>]
        if arg_index == 1:
            return self._filter_prefix(["fx"], text)
        if arg_index == 2 and len(args) >= 2 and args[1] == "fx":
            try:
                idx = _slot_to_internal(int(args[0]))
                slot = self.host.engine.slots[idx]
                if slot and slot.effects:
                    n = len(slot.effects)
                    return self._filter_prefix([str(i) for i in range(1, n + 1)], text)
            except (ValueError, IndexError):
                pass
        return []

    def complete_info(self, text, line, begidx, endidx):
        del endidx
        prefix_tokens = line[:begidx].split()
        if not prefix_tokens or prefix_tokens[0] != "info":
            return []
        return self._complete_slot_fx(text, prefix_tokens)

    def complete_knobs(self, text, line, begidx, endidx):
        del endidx
        prefix_tokens = line[:begidx].split()
        if not prefix_tokens or prefix_tokens[0] != "knobs":
            return []
        return self._complete_slot_fx(text, prefix_tokens)

    def do_load(self, arg):
        """Load instrument/effect/VCV/WAV: load vst <slot 1-8> <path|vst_name> [name] | load wav <slot 1-8> <pack> <sample> [name] | load fx <path|vst_name> [slot 1-8|master] [name] | load vcv <slot 1-8> <patch_name[.vcv]> [name]"""
        text = arg.strip()
        if not text:
            self._print(f"Usage: {self._load_usage()}")
            return

        mode = text.split(maxsplit=1)[0].lower()

        if mode not in self.LOAD_TYPES:
            if mode.isdigit():
                self._print("Error: instrument loads now use 'load vst <slot 1-8> <path|vst_name> [name]'")
            else:
                self._print("Error: load type must be one of: vst, wav, vcv, fx")
            self._print(f"Usage: {self._load_usage()}")
            return

        if mode == "vst":
            parts = text.split(maxsplit=3)
            if len(parts) < 3:
                self._print("Usage: load vst <slot 1-8> <path|vst_name> [name]")
                return
            try:
                idx = _slot_to_internal(int(parts[1]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return

            path_token = parts[2]
            name = parts[3] if len(parts) > 3 else None
            try:
                path = self._resolve_vst_token(path_token)
                slot = self.host.load_instrument(idx, path, name)
                self._print(f"  slot {parts[1]} = {slot.name}")
                self._print(f"  vst      : {slot.path}")
            except Exception as e:
                self._print(f"Error: {e}")
            return

        if mode == "vcv":
            parts = text.split(maxsplit=3)
            if len(parts) < 3:
                self._print("Usage: load vcv <slot 1-8> <patch_name[.vcv]> [name]")
                return
            try:
                idx = _slot_to_internal(int(parts[1]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return

            patch_name = parts[2]
            name = parts[3] if len(parts) > 3 else None
            try:
                slot, patch_result, cardinal_path, patch_path = self.host.load_vcv_patch(
                    idx,
                    patch_name,
                    name=name,
                )
            except Exception as e:
                self._print(f"Error: {e}")
                return

            self._print(f"  slot {parts[1]} = {slot.name}")
            self._print(f"  patch    : {patch_path}")
            self._print(f"  cardinal : {cardinal_path}")
            self._print(f"  result   : {patch_result}")
            self._print(f"  route with: route <channel 1-16> {parts[1]}")
            return

        if mode == "wav":
            parts = text.split(maxsplit=4)
            if len(parts) < 4:
                self._print("Usage: load wav <slot 1-8> <pack> <sample> [name]")
                return

            try:
                idx = _slot_to_internal(int(parts[1]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return

            pack_name = parts[2].strip().strip("/")
            sample_name = parts[3].strip()
            display_name = parts[4].strip() if len(parts) > 4 else None

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

            wav_path = Path("samples") / pack_path / sample_path

            try:
                slot = self.host.load_wav(idx, str(wav_path), display_name)
            except Exception as e:
                self._print(f"Error: {e}")
                return

            self._print(f"  slot {parts[1]} = {slot.name}")
            self._print(f"  wav      : {slot.path}")
            self._print(f"  route with: route <channel 1-16> {parts[1]}")
            return

        if mode == "fx":
            parts = text.split(maxsplit=3)
            if len(parts) < 2:
                self._print("Usage: load fx <path|vst_name> [slot 1-8|master] [name]")
                return
            path_token = parts[1]
            target = parts[2] if len(parts) > 2 else "master"
            name = parts[3] if len(parts) > 3 else None
            try:
                slot_idx = None if target == "master" else _slot_to_internal(int(target))
                path = self._resolve_vst_token(path_token)
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                self.host.load_effect(path, slot_idx, name)
            except Exception as e:
                self._print(f"Error: {e}")
            return

    def do_unload(self, arg):
        """Unload instrument/effect: unload <slot 1-8> | unload fx <slot 1-8|master> <fx_index>"""
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: unload <slot 1-8> | unload fx <slot 1-8|master> <fx_index>")
            return

        if parts[0] == "fx":
            if len(parts) < 3:
                self._print("Usage: unload fx <slot 1-8|master> <fx_index>")
                return
            try:
                slot_idx = None if parts[1] == "master" else _slot_to_internal(int(parts[1]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                fx_idx = int(parts[2]) - 1
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

        if len(parts) != 1:
            self._print("Usage: unload <slot 1-8> | unload fx <slot 1-8|master> <fx_index>")
            return

        try:
            idx = _slot_to_internal(int(parts[0]))
        except ValueError as e:
            self._print(f"Error: {e}")
            return

        try:
            removed = self.host.remove_instrument(idx)
            self.host.refresh_mixer_leds([idx])
            self._print(f"  slot {parts[0]} cleared ({removed.name})")
        except Exception as e:
            self._print(f"Error: {e}")

    def do_params(self, arg):
        """Show params: params <slot 1-8>  or  params master <fx_index>"""
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: params <slot 1-8> | params master <fx_index>")
            return
        if parts[0] == "master":
            if len(parts) > 2:
                self._print("Usage: params master <fx_index>")
                return
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
        else:
            try:
                idx = _slot_to_internal(int(parts[0]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            slot = self.host.engine.slots[idx]
            if slot is None:
                self._print("  Empty slot")
                return
            plugin = slot.plugin
        params = getattr(plugin, "parameters", {})
        for name in params:
            try:
                val = getattr(plugin, name)
                rng = getattr(params[name], "range", None)
                if rng is not None and len(rng) >= 2:
                    self._print(
                        f"  {name} = {val}  (range {rng[0]:.3f} .. {rng[1]:.3f})"
                    )
                else:
                    self._print(f"  {name} = {val}")
            except Exception:
                self._print(f"  {name} = ???")

    def do_set(self, arg):
        """Set param: set <slot 1-8> <name> <value>  or  set master <fx> <name> <value>"""
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: set <slot 1-8> <name> <value>")
            return
        if parts[0] == "master":
            if len(parts) < 4:
                self._print("Usage: set master <fx_index> <name> <value>")
                return
            try:
                fx_idx = int(parts[1]) - 1
            except ValueError:
                self._print("Error: fx_index must be a positive integer")
                return
            if not 0 <= fx_idx < len(self.host.engine.master_effects):
                self._print("Error: fx_index out of range")
                return
            plugin = self.host.engine.master_effects[fx_idx]
            pname, pval = parts[2], parts[3]
        else:
            if len(parts) < 3:
                self._print("Usage: set <slot 1-8> <name> <value>")
                return
            try:
                idx = _slot_to_internal(int(parts[0]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            slot = self.host.engine.slots[idx]
            if slot is None:
                self._print("  Empty slot")
                return
            plugin = slot.plugin
            pname, pval = parts[1], parts[2]
        try:
            pval = float(pval)
        except ValueError:
            if pval.lower() in ("true", "false"):
                pval = pval.lower() == "true"
        try:
            setattr(plugin, pname, pval)
            self._print(f"  {pname} = {pval}")
        except Exception as e:
            self._print(f"Error: {e}")

    # -- gain / mute / solo --------------------------------------------------

    def do_gain(self, arg):
        """Set slot gain: gain <slot 1-8> <0.0-1.0>"""
        parts = arg.strip().split()
        if len(parts) < 2:
            self._print("Usage: gain <slot 1-8> <value>")
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

        slot.gain = gain
        self._print(f"  gain = {slot.gain:.2f}")

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

        slot.muted = not slot.muted
        self.host.refresh_mixer_leds([idx])
        self._print(f"  {slot.name}: {'MUTED' if slot.muted else 'unmuted'}")

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

        slot.solo = not slot.solo
        self.host.refresh_mixer_leds([idx])
        self._print(f"  {slot.name}: {'SOLO' if slot.solo else 'unsolo'}")

    def do_master(self, arg):
        """Set master gain: master <0.0-1.0>"""
        if not arg.strip():
            self._print(f"  master gain = {self.host.engine.master_gain:.2f}")
            return
        try:
            self.host.engine.master_gain = float(arg.strip())
        except ValueError:
            self._print("Error: master gain must be a number")

    # -- routing -------------------------------------------------------------

    def do_route(self, arg):
        """Route MIDI channel to slot: route <ch 1-16> <slot 1-8>"""
        parts = arg.strip().split()
        if len(parts) < 2:
            self._print("Usage: route <channel 1-16> <slot 1-8>")
            return
        try:
            ch = _ch_to_internal(int(parts[0]))
            idx = _slot_to_internal(int(parts[1]))
        except ValueError as e:
            self._print(f"Error: {e}")
            return
        self.host.route(ch, idx)
        self._print(f"  ch {parts[0]} -> slot {parts[1]}")

    def do_unroute(self, arg):
        """Unroute MIDI channel: unroute <ch 1-16>"""
        try:
            ch = _ch_to_internal(int(arg.strip()))
        except ValueError as e:
            self._print(f"Error: {e}")
            return
        self.host.unroute(ch)

    def do_mixer(self, arg):
        """Show full signal-flow diagram: mixer"""
        if arg.strip():
            self._print("Usage: mixer")
            return
        self._print(render_signal_flow(self.host.engine, self.host.channel_map))

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

    def do_knobs(self, arg):
        """Show parameter knobs: knobs <slot 1-8> | knobs <slot 1-8> fx <fx_index> | knobs master [fx_index]"""
        parts = arg.strip().split()
        if not parts:
            self._print("Usage: knobs <slot 1-8> | knobs <slot 1-8> fx <fx_index> | knobs master [fx_index]")
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

            # knobs <slot> fx <fx_index>  -- show knobs for a slot effect
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

        self._print(render_knobs(plugin, label))

    # -- audio ---------------------------------------------------------------

    def do_audio_start(self, arg):
        """Start audio: audio_start [device]"""
        dev = arg.strip() or None
        if dev and dev.isdigit():
            dev = int(dev)
        try:
            self.host.start_audio(dev)
        except Exception as e:
            self._print(f"Error: {e}")

    def do_audio_stop(self, arg):
        """Stop audio."""
        self.host.stop_audio()

    def do_audio_devices(self, arg):
        """List audio devices: audio_devices"""
        if HAS_SOUNDDEVICE:
            self._print(sd.query_devices())
        else:
            self._print("  sounddevice not installed")

    # -- MIDI ports ----------------------------------------------------------

    def do_midi_ports_in(self, arg):
        """List MIDI input ports: midi_ports_in"""
        ports = list_midi_input_ports()
        if not ports:
            self._print("  No MIDI input ports found.")
            return
        for i, name in enumerate(ports):
            self._print(f"  [{i}] {name}")

    def do_midi_ports_out(self, arg):
        """List MIDI output ports: midi_ports_out"""
        ports = list_midi_output_ports()
        if not ports:
            self._print("  No MIDI output ports found.")
            return
        for i, name in enumerate(ports):
            self._print(f"  [{i}] {name}")

    def do_midi_in(self, arg):
        """Open a MIDI input port: midi_in <port_index>"""
        if not arg.strip():
            self._print("Usage: midi_in <port_index>")
            return
        try:
            ctrl = self.host.open_midi_input(arg.strip())
            self._print(f"  Opened: {ctrl.port_name}")
        except Exception as e:
            self._print(f"Error: {e}")

    def do_midi_in_close(self, arg):
        """Close a MIDI input: midi_in_close <index 1-N>"""
        if not arg.strip():
            self._print("Usage: midi_in_close <index>")
            return
        try:
            idx = int(arg.strip()) - 1
            self.host.close_midi_input(idx)
            self._print("  Closed.")
        except Exception as e:
            self._print(f"Error: {e}")

    def do_midi_ins(self, arg):
        """List open MIDI inputs."""
        if not self.host.midi_inputs:
            self._print("  No MIDI inputs open.")
            return
        for i, ctrl in enumerate(self.host.midi_inputs):
            self._print(f"  [{i + 1}] {ctrl.port_name or ctrl.label}")

    def do_midi_mix(self, arg):
        """Open Akai MIDI Mix input: midi_mix <port_index>"""
        if not arg.strip():
            self._print("Usage: midi_mix <port_index>")
            return
        try:
            self.host.open_mixer_midi(int(arg.strip()))
        except Exception as e:
            self._print(f"Error: {e}")

    def do_midi_mix_out(self, arg):
        """Open Akai MIDI Mix output (LED feedback): midi_mix_out <port_index>"""
        if not arg.strip():
            self._print("Usage: midi_mix_out <port_index>")
            return
        try:
            self.host.open_mixer_midi_out(int(arg.strip()))
        except Exception as e:
            self._print(f"Error: {e}")

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

    # -- link ----------------------------------------------------------------

    def do_link(self, arg):
        """Enable Link: link [bpm]"""
        if arg.strip():
            try:
                bpm = float(arg.strip())
            except ValueError:
                self._print("Error: bpm must be a number")
                return
        else:
            bpm = None
        try:
            self.host.start_link(bpm)
        except Exception as e:
            self._print(f"Error: {e}")

    def do_unlink(self, arg):
        """Disable Link."""
        self.host.stop_link()

    def do_tempo(self, arg):
        """Get/set tempo: tempo [bpm]"""
        if arg.strip():
            try:
                self.host.link.bpm = float(arg.strip())
            except ValueError:
                self._print("Error: bpm must be a number")
                return
        self._print(f"  {self.host.link.bpm:.1f} BPM")

    # -- session -------------------------------------------------------------

    def do_save(self, arg):
        """Save session: save [path]"""
        path = arg.strip() or None
        try:
            self.host.save_session(path)
        except Exception as e:
            self._print(f"Error: {e}")

    def do_restore(self, arg):
        """Restore session: restore [path]"""
        path = arg.strip() or None
        try:
            self.host.restore_session(path)
            self.host.refresh_mixer_leds()
        except Exception as e:
            self._print(f"Error: {e}")

    # -- status --------------------------------------------------------------

    def do_status(self, arg):
        """Overall status."""
        self._print("=== vcpi Status ===")
        self._print(f"  Audio  : {'RUNNING' if self.host.engine.running else 'STOPPED'}"
                    f"  (sr={self.host.sample_rate} buf={self.host.buffer_size})")
        self._print(f"  Backend: {self._audio_backend_label()}")
        if self.host.midi_inputs:
            for i, ctrl in enumerate(self.host.midi_inputs):
                self._print(f"  MIDI IN[{i + 1}]: {ctrl.port_name or ctrl.label}")
        else:
            self._print("  MIDI IN: (none)")
        self._print(f"  MIDIMix IN : {self.host.mixer_midi_name or 'closed'}")
        self._print(f"  MIDIMix OUT: {self.host.mixer_midi_out_name or 'closed'}")
        self._print(f"  Session: {self.host.session_path}")
        lk = self.host.link
        if lk.enabled:
            self._print(f"  Link  : {lk.bpm:.1f} BPM  ({lk.num_peers} peers)")
        else:
            self._print("  Link  : disabled")

        self._print()

    def do_deps(self, arg):
        """Check dependencies."""
        for name, ok in [("pedalboard", HAS_PEDALBOARD), ("aalink", HAS_LINK),
                         ("python-rtmidi", HAS_RTMIDI), ("mido", HAS_MIDO),
                         ("sounddevice", HAS_SOUNDDEVICE)]:
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
