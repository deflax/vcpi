"""Interactive command-line interface for LinkVST.

All slot numbers and MIDI channels are presented 1-based to the user
(slots 1-8, MIDI channels 1-16) and converted to 0-based internally.
"""

from __future__ import annotations

import cmd
from pathlib import Path

from linkvst.deps import HAS_PEDALBOARD, HAS_LINK, HAS_RTMIDI, HAS_MIDO, HAS_SOUNDDEVICE, sd
from linkvst.midi import MidiPort
from linkvst.models import NUM_SLOTS
from linkvst.host import VSTHost


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
============================================================
  LinkVST  -  Python VST3 Host + Ableton Link
  Beatstep Pro (sequencer)  |  MIDI Mix (mixer)
============================================================
Type 'help' for available commands.
Slots are numbered 1-8.  MIDI channels are numbered 1-16.
"""
    prompt = "linkvst> "

    def __init__(self, host: VSTHost):
        super().__init__()
        self.host = host

    # -- plugins -------------------------------------------------------------

    def do_load(self, arg):
        """Load instrument: load <slot 1-8> <path> [name]"""
        parts = arg.strip().split(maxsplit=2)
        if len(parts) < 2:
            print("Usage: load <slot 1-8> <path> [name]")
            return
        try:
            idx = _slot_to_internal(int(parts[0]))
        except ValueError as e:
            print(f"Error: {e}")
            return
        path = parts[1]
        name = parts[2] if len(parts) > 2 else None
        try:
            slot = self.host.load_instrument(idx, path, name)
            print(f"  slot {parts[0]} = {slot.name}")
        except Exception as e:
            print(f"Error: {e}")

    def do_load_fx(self, arg):
        """Load effect: load_fx <path> [slot 1-8|master] [name]"""
        parts = arg.strip().split(maxsplit=2)
        if not parts:
            print("Usage: load_fx <path> [slot 1-8|master] [name]")
            return
        path = parts[0]
        target = parts[1] if len(parts) > 1 else "master"
        name = parts[2] if len(parts) > 2 else None
        try:
            slot_idx = None if target == "master" else _slot_to_internal(int(target))
        except ValueError as e:
            print(f"Error: {e}")
            return
        try:
            self.host.load_effect(path, slot_idx, name)
        except Exception as e:
            print(f"Error: {e}")

    def do_remove_fx(self, arg):
        """Remove effect: remove_fx <slot 1-8|master> <fx_index>"""
        parts = arg.strip().split()
        if len(parts) < 2:
            print("Usage: remove_fx <slot 1-8|master> <fx_index>")
            return
        try:
            slot_idx = None if parts[0] == "master" else _slot_to_internal(int(parts[0]))
        except ValueError as e:
            print(f"Error: {e}")
            return
        fx_idx = int(parts[1])
        try:
            self.host.remove_effect(slot_idx, fx_idx)
            print("  Removed.")
        except Exception as e:
            print(f"Error: {e}")

    def do_slots(self, arg):
        """Show all 8 instrument slots."""
        any_solo = self.host.engine.any_solo()
        for i, slot in enumerate(self.host.engine.slots):
            display_num = i + 1
            if slot is None:
                print(f"  [{display_num}] (empty)")
                continue
            flags = []
            if slot.muted:
                flags.append("M")
            if slot.solo:
                flags.append("S")
            chs = ",".join(str(c + 1) for c in sorted(slot.midi_channels)) or "-"
            audible = (not slot.muted) and (not any_solo or slot.solo)
            aud_mark = " " if audible else "x"
            print(f"  [{display_num}] {aud_mark} {slot.name:<20} ch={chs:<8} "
                  f"gain={slot.gain:.2f}  {''.join(flags)}")
            for j, fx in enumerate(slot.effects):
                print(f"        fx[{j + 1}] {Path(fx.path_to_plugin_file).stem}")
        if self.host.engine.master_effects:
            print("  master bus:")
            for j, fx in enumerate(self.host.engine.master_effects):
                print(f"    fx[{j + 1}] {Path(fx.path_to_plugin_file).stem}")
        print(f"  master gain: {self.host.engine.master_gain:.2f}")

    def do_params(self, arg):
        """Show params: params <slot 1-8>  or  params master <fx_index>"""
        parts = arg.strip().split()
        if not parts:
            print("Usage: params <slot 1-8> | params master <fx_index>")
            return
        if parts[0] == "master":
            fx_idx = int(parts[1]) - 1 if len(parts) > 1 else 0
            plugin = self.host.engine.master_effects[fx_idx]
        else:
            try:
                idx = _slot_to_internal(int(parts[0]))
            except ValueError as e:
                print(f"Error: {e}")
                return
            slot = self.host.engine.slots[idx]
            if slot is None:
                print("  Empty slot")
                return
            plugin = slot.plugin
        for name in plugin.parameters:
            try:
                val = getattr(plugin, name)
                rng = plugin.parameters[name].range
                print(f"  {name} = {val}  (range {rng[0]:.3f} .. {rng[1]:.3f})")
            except Exception:
                print(f"  {name} = ???")

    def do_set(self, arg):
        """Set param: set <slot 1-8> <name> <value>  or  set master <fx> <name> <value>"""
        parts = arg.strip().split()
        if len(parts) < 3:
            print("Usage: set <slot 1-8> <name> <value>")
            return
        if parts[0] == "master":
            fx_idx = int(parts[1]) - 1
            plugin = self.host.engine.master_effects[fx_idx]
            pname, pval = parts[2], parts[3]
        else:
            try:
                idx = _slot_to_internal(int(parts[0]))
            except ValueError as e:
                print(f"Error: {e}")
                return
            slot = self.host.engine.slots[idx]
            if slot is None:
                print("  Empty slot")
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
            print(f"  {pname} = {pval}")
        except Exception as e:
            print(f"Error: {e}")

    # -- gain / mute / solo --------------------------------------------------

    def do_gain(self, arg):
        """Set slot gain: gain <slot 1-8> <0.0-1.0>"""
        parts = arg.strip().split()
        if len(parts) < 2:
            print("Usage: gain <slot 1-8> <value>")
            return
        try:
            idx = _slot_to_internal(int(parts[0]))
        except ValueError as e:
            print(f"Error: {e}")
            return
        slot = self.host.engine.slots[idx]
        if slot:
            slot.gain = float(parts[1])
            print(f"  gain = {slot.gain:.2f}")

    def do_mute(self, arg):
        """Toggle mute: mute <slot 1-8>"""
        try:
            idx = _slot_to_internal(int(arg.strip()))
        except ValueError as e:
            print(f"Error: {e}")
            return
        slot = self.host.engine.slots[idx]
        if slot:
            slot.muted = not slot.muted
            print(f"  {slot.name}: {'MUTED' if slot.muted else 'unmuted'}")

    def do_solo(self, arg):
        """Toggle solo: solo <slot 1-8>"""
        try:
            idx = _slot_to_internal(int(arg.strip()))
        except ValueError as e:
            print(f"Error: {e}")
            return
        slot = self.host.engine.slots[idx]
        if slot:
            slot.solo = not slot.solo
            print(f"  {slot.name}: {'SOLO' if slot.solo else 'unsolo'}")

    def do_master(self, arg):
        """Set master gain: master <0.0-1.0>"""
        if not arg.strip():
            print(f"  master gain = {self.host.engine.master_gain:.2f}")
            return
        self.host.engine.master_gain = float(arg.strip())

    # -- routing -------------------------------------------------------------

    def do_route(self, arg):
        """Route MIDI channel to slot: route <ch 1-16> <slot 1-8>"""
        parts = arg.strip().split()
        if len(parts) < 2:
            print("Usage: route <channel 1-16> <slot 1-8>")
            return
        try:
            ch = _ch_to_internal(int(parts[0]))
            idx = _slot_to_internal(int(parts[1]))
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.host.route(ch, idx)
        print(f"  ch {parts[0]} -> slot {parts[1]}")

    def do_unroute(self, arg):
        """Unroute MIDI channel: unroute <ch 1-16>"""
        try:
            ch = _ch_to_internal(int(arg.strip()))
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.host.unroute(ch)

    def do_routing(self, arg):
        """Show MIDI routing."""
        if not self.host._channel_map:
            print("  No routes.")
            return
        for ch in sorted(self.host._channel_map):
            idx = self.host._channel_map[ch]
            slot = self.host.engine.slots[idx]
            name = slot.name if slot else "(empty)"
            print(f"  ch {ch + 1} -> slot {idx + 1} ({name})")

    # -- audio ---------------------------------------------------------------

    def do_audio_start(self, arg):
        """Start audio: audio_start [device]"""
        dev = arg.strip() or None
        if dev and dev.isdigit():
            dev = int(dev)
        try:
            self.host.start_audio(dev)
        except Exception as e:
            print(f"Error: {e}")

    def do_audio_stop(self, arg):
        """Stop audio."""
        self.host.stop_audio()

    def do_devices(self, arg):
        """List audio devices."""
        if HAS_SOUNDDEVICE:
            print(sd.query_devices())
        else:
            print("  sounddevice not installed")

    # -- MIDI ports ----------------------------------------------------------

    def do_midi_ports(self, arg):
        """List MIDI input ports."""
        ports = MidiPort.list_ports()
        if not ports:
            print("  No MIDI input ports found.")
            return
        for i, name in enumerate(ports):
            print(f"  [{i}] {name}")

    def do_midi_seq(self, arg):
        """Open Beatstep Pro MIDI: midi_seq <port_index>"""
        a = arg.strip()
        try:
            self.host.open_sequencer_midi(int(a) if a else None)
        except Exception as e:
            print(f"Error: {e}")

    def do_midi_mix(self, arg):
        """Open Akai MIDI Mix: midi_mix <port_index>"""
        if not arg.strip():
            print("Usage: midi_mix <port_index>")
            return
        try:
            self.host.open_mixer_midi(int(arg.strip()))
        except Exception as e:
            print(f"Error: {e}")

    def do_note(self, arg):
        """Test note: note <slot 1-8> <midi_note> [vel] [dur_ms]"""
        parts = arg.strip().split()
        if len(parts) < 2:
            print("Usage: note <slot 1-8> <note> [velocity] [dur_ms]")
            return
        try:
            idx = _slot_to_internal(int(parts[0]))
        except ValueError as e:
            print(f"Error: {e}")
            return
        n = int(parts[1])
        v = int(parts[2]) if len(parts) > 2 else 100
        d = float(parts[3]) / 1000.0 if len(parts) > 3 else 0.3
        self.host.send_note(idx, n, v, d)

    # -- link ----------------------------------------------------------------

    def do_link(self, arg):
        """Enable Link: link [bpm]"""
        bpm = float(arg.strip()) if arg.strip() else None
        try:
            self.host.start_link(bpm)
        except Exception as e:
            print(f"Error: {e}")

    def do_unlink(self, arg):
        """Disable Link."""
        self.host.stop_link()

    def do_tempo(self, arg):
        """Get/set tempo: tempo [bpm]"""
        if arg.strip():
            self.host.link.bpm = float(arg.strip())
        print(f"  {self.host.link.bpm:.1f} BPM")

    # -- session -------------------------------------------------------------

    def do_save(self, arg):
        """Save session: save [path]"""
        path = arg.strip() or None
        try:
            self.host.save_session(path)
        except Exception as e:
            print(f"Error: {e}")

    def do_restore(self, arg):
        """Restore session: restore [path]"""
        path = arg.strip() or None
        try:
            self.host.restore_session(path)
        except Exception as e:
            print(f"Error: {e}")

    # -- status --------------------------------------------------------------

    def do_status(self, arg):
        """Overall status."""
        print("=== LinkVST Status ===")
        print(f"  Audio  : {'RUNNING' if self.host.engine.running else 'STOPPED'}"
              f"  (sr={self.host.sample_rate} buf={self.host.buffer_size})")
        print(f"  BSP    : {self.host.midi_seq.name or 'closed'}")
        print(f"  MIDIMix: {self.host.midi_mix.name or 'closed'}")
        print(f"  Session: {self.host.session_path}")
        lk = self.host.link
        if lk.enabled:
            print(f"  Link  : {lk.bpm:.1f} BPM  ({lk.num_peers} peers)")
        else:
            print("  Link  : disabled")
        print()
        self.do_slots("")

    def do_deps(self, arg):
        """Check dependencies."""
        for name, ok in [("pedalboard", HAS_PEDALBOARD), ("aalink", HAS_LINK),
                         ("python-rtmidi", HAS_RTMIDI), ("mido", HAS_MIDO),
                         ("sounddevice", HAS_SOUNDDEVICE)]:
            print(f"  {name}: {'OK' if ok else 'MISSING'}")

    def do_quit(self, arg):
        """Exit."""
        self.host.shutdown()
        return True

    do_exit = do_quit
    do_EOF = do_quit
