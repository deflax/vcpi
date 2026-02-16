"""Interactive command-line interface for vcpi.

All slot numbers and MIDI channels are presented 1-based to the user
(slots 1-8, MIDI channels 1-16) and converted to 0-based internally.
"""

from __future__ import annotations

import cmd
from pathlib import Path

from core.deps import HAS_PEDALBOARD, HAS_LINK, HAS_RTMIDI, HAS_MIDO, HAS_SOUNDDEVICE, sd
from core.host import VcpiCore
from core.midi import list_midi_input_ports, list_midi_output_ports
from core.models import NUM_SLOTS
from graph.routes import render_route_graph


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

    # -- plugins -------------------------------------------------------------

    def do_load(self, arg):
        """Load instrument/effect/VCV: load <slot 1-8> <path> [name] | load fx <path> [slot 1-8|master] [name] | load vcv <slot 1-8> <patch_name[.vcv]> [name]"""
        text = arg.strip()
        if not text:
            self._print(
                "Usage: load <slot 1-8> <path> [name] | "
                "load fx <path> [slot 1-8|master] [name] | "
                "load vcv <slot 1-8> <patch_name[.vcv]> [name]"
            )
            return

        mode = text.split(maxsplit=1)[0]

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

        if mode == "fx":
            parts = text.split(maxsplit=3)
            if len(parts) < 2:
                self._print("Usage: load fx <path> [slot 1-8|master] [name]")
                return
            path = parts[1]
            target = parts[2] if len(parts) > 2 else "master"
            name = parts[3] if len(parts) > 3 else None
            try:
                slot_idx = None if target == "master" else _slot_to_internal(int(target))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                self.host.load_effect(path, slot_idx, name)
            except Exception as e:
                self._print(f"Error: {e}")
            return

        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            self._print("Usage: load <slot 1-8> <path> [name]")
            return
        try:
            idx = _slot_to_internal(int(parts[0]))
        except ValueError as e:
            self._print(f"Error: {e}")
            return
        path = parts[1]
        name = parts[2] if len(parts) > 2 else None
        try:
            slot = self.host.load_instrument(idx, path, name)
            self._print(f"  slot {parts[0]} = {slot.name}")
        except Exception as e:
            self._print(f"Error: {e}")

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

    def do_slots(self, arg):
        """Show all 8 instrument slots."""
        any_solo = self.host.engine.any_solo()
        for i, slot in enumerate(self.host.engine.slots):
            display_num = i + 1
            if slot is None:
                self._print(f"  [{display_num}] (empty)")
                continue
            flags = []
            if slot.muted:
                flags.append("M")
            if slot.solo:
                flags.append("S")
            chs = ",".join(str(c + 1) for c in sorted(slot.midi_channels)) or "-"
            audible = (not slot.muted) and (not any_solo or slot.solo)
            aud_mark = " " if audible else "x"
            self._print(f"  [{display_num}] {aud_mark} {slot.name:<20} ch={chs:<8} "
                        f"gain={slot.gain:.2f}  {''.join(flags)}")
            for j, fx in enumerate(slot.effects):
                self._print(f"        fx[{j + 1}] {Path(fx.path_to_plugin_file).stem}")
        if self.host.engine.master_effects:
            self._print("  master bus:")
            for j, fx in enumerate(self.host.engine.master_effects):
                self._print(f"    fx[{j + 1}] {Path(fx.path_to_plugin_file).stem}")
        self._print(f"  master gain: {self.host.engine.master_gain:.2f}")

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
        for name in plugin.parameters:
            try:
                val = getattr(plugin, name)
                rng = plugin.parameters[name].range
                self._print(f"  {name} = {val}  (range {rng[0]:.3f} .. {rng[1]:.3f})")
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

    def do_routing(self, arg):
        """Show MIDI routing."""
        if not self.host.channel_map:
            self._print("  No routes.")
            return
        for ch in sorted(self.host.channel_map):
            idx = self.host.channel_map[ch]
            slot = self.host.engine.slots[idx]
            name = slot.name if slot else "(empty)"
            self._print(f"  ch {ch + 1} -> slot {idx + 1} ({name})")

    def do_graph(self, arg):
        """Show ASCII route graph."""
        if arg.strip():
            self._print("Usage: graph")
            return
        self._print(render_route_graph(self.host))

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

    def do_midi_seq(self, arg):
        """Open Beatstep Pro MIDI: midi_seq <port_index>"""
        a = arg.strip()
        try:
            self.host.open_sequencer_midi(int(a) if a else None)
        except Exception as e:
            self._print(f"Error: {e}")

    def do_midi_keys(self, arg):
        """Open Novation 25 LE MIDI input: midi_keys <port_index>"""
        if not arg.strip():
            self._print("Usage: midi_keys <port_index>")
            return
        try:
            self.host.open_keyboard_midi(int(arg.strip()))
        except Exception as e:
            self._print(f"Error: {e}")

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
        self._print(f"  BSP    : {self.host.sequencer_midi_name or 'closed'}")
        self._print(f"  Keys   : {self.host.keyboard_midi_name or 'closed'}")
        self._print(f"  MIDIMix IN : {self.host.mixer_midi_name or 'closed'}")
        self._print(f"  MIDIMix OUT: {self.host.mixer_midi_out_name or 'closed'}")
        self._print(f"  Session: {self.host.session_path}")
        lk = self.host.link
        if lk.enabled:
            self._print(f"  Link  : {lk.bpm:.1f} BPM  ({lk.num_peers} peers)")
        else:
            self._print("  Link  : disabled")

        gain_tokens = []
        for i, slot in enumerate(self.host.engine.slots, start=1):
            if slot is None:
                gain_tokens.append(f"{i}:empty")
            else:
                gain_tokens.append(f"{i}:{slot.gain:.2f}")
        self._print("  Slot gains: " + "  ".join(gain_tokens))

        self._print()
        self.do_slots("")

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
