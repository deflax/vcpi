"""Interactive command shell; domain handlers use a small execution context.

Track numbers and MIDI channels are presented 1-based and converted internally.
"""

from __future__ import annotations

import cmd
from typing import TYPE_CHECKING

from . import devices, help, inspection, mixer, parameters, sequencing, sessions, tracks
from .completion import complete_line
from .context import CommandContext
from .help import VCPI_ASCII_LOGO

if TYPE_CHECKING:
    from vcpi.host import VcpiCore


class HostCLI(cmd.Cmd):
    intro = (
        "\n"
        f"{VCPI_ASCII_LOGO}\n"
        "Type 'help' for available commands.\n"
        "Tracks are numbered 1-8.  MIDI channels are numbered 1-16."
    )
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

    def _print(self, *args, **kwargs):
        """Print to self.stdout so output is captured in server mode."""
        kwargs.setdefault("file", self.stdout)
        print(*args, **kwargs)


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


    def do_quit(self, arg):
        """Exit the current CLI session."""
        if arg.strip():
            self._print("Usage: quit")
            return
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


    def do_help(self, arg):
        """Show help: help [command]"""
        return help.show_help(CommandContext(self.host, self._print), arg,
                              help.command_entries(self))

    def do_track(self, arg):
        """Load or clear track sources and effects, or control a deck."""
        return tracks.track(CommandContext(self.host, self._print), arg)

    def do_get(self, arg):
        """Read cached plugin parameters, including units, ranges, and choices."""
        return parameters.get(CommandContext(self.host, self._print), arg)

    def do_set(self, arg):
        """Set a plugin parameter and show its actual applied value."""
        return parameters.set(CommandContext(self.host, self._print), arg)

    def do_gain(self, arg):
        """Read or set track/master gain."""
        return mixer.gain(CommandContext(self.host, self._print), arg)

    def do_mute(self, arg):
        """Set or toggle track mute."""
        return mixer.mute(CommandContext(self.host, self._print), arg)

    def do_solo(self, arg):
        """Set or toggle track solo."""
        return mixer.solo(CommandContext(self.host, self._print), arg)

    def do_audio(self, arg):
        """Audio commands: audio start [device] | audio stop | audio devices | audio diagnose"""
        return devices.audio(CommandContext(self.host, self._print), arg)

    def do_midi(self, arg):
        """Manage MIDI ports and routing."""
        return devices.midi(CommandContext(self.host, self._print), arg)

    def do_midimix(self, arg):
        """Open MIDI Mix control input or LED feedback output."""
        return devices.midimix(CommandContext(self.host, self._print), arg)

    def do_note(self, arg):
        """Send a timed MIDI test note."""
        return devices.note(CommandContext(self.host, self._print), arg)

    def do_seq(self, arg):
        """Edit and route sequence banks."""
        return sequencing.seq(CommandContext(self.host, self._print), arg)

    def do_ableton(self, arg):
        """Ableton Link commands: ableton link [bpm] | ableton cut"""
        return sequencing.ableton(CommandContext(self.host, self._print), arg)

    def do_tempo(self, arg):
        """Get/set tempo: tempo [bpm]"""
        return sequencing.tempo(CommandContext(self.host, self._print), arg)

    def do_save(self, arg):
        """Save session: save [<name>]"""
        return sessions.save(CommandContext(self.host, self._print), arg)

    def do_load(self, arg):
        """Load session: load <name>"""
        return sessions.load(CommandContext(self.host, self._print), arg)

    def do_show(self, arg):
        """Inspect routing and plugin metadata: show [all|midi|controls|audio|1-8|master]"""
        return inspection.show(CommandContext(self.host, self._print), arg)

    def do_debug(self, arg):
        """Show or toggle daemon debug logging: debug [on|off]"""
        return inspection.debug(CommandContext(self.host, self._print), arg)

    def do_about(self, arg):
        """Show vcpi logo and project info: about"""
        return inspection.about(CommandContext(self.host, self._print), arg)

    def do_status(self, arg):
        """Overall status: status"""
        return inspection.status(CommandContext(self.host, self._print), arg)

    def do_ps(self, arg):
        """Show processes and threads: ps [--all] [--cpu | --mem]"""
        return inspection.ps(CommandContext(self.host, self._print), arg)

    def do_deps(self, arg):
        """Check dependencies."""
        return inspection.deps(CommandContext(self.host, self._print), arg)

    do_exit = do_quit
    do_EOF = do_quit
