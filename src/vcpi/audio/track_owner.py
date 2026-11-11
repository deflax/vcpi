"""Track child service, native ownership, and parent-lifetime monitoring."""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import signal
import threading
import time
from multiprocessing.connection import wait as wait_connections

from vcpi.audio_state import capture_plugin
from vcpi.deck_bank import deck_status_frames, encode_deck_message
from vcpi.logging_setup import configure_logging, set_log_level
from vcpi.native_output import native_output
from vcpi.state_transport import (
    encode_native_message, receive_frame, send_frame, validate_native_message,
)

from .track_protocol import (
    CONTROL_LIMIT, PARAMETER_BATCH_LIMIT, STATE_LIMIT, TrackProcessError, _encode,
)


def _capture_native_plugin(plugin, values, readers, *, capture_native_state=False):
    snapshot = capture_plugin(plugin, values, readers,
                              capture_native_state=capture_native_state)
    # Native object addresses are unique only within a process. Snapshot
    # identities must not collide with another track or the owner's master FX.
    snapshot.token = (os.getpid() << 64) | snapshot.token
    return snapshot


def _track_state(owner, index, *, capture_native_state=False):
    track = owner.engine.tracks[index]
    values, readers = {}, {}
    if track.source_type == "deck":
        track.deck.update(track.plugin.native.deck_status())
        track.deck["status_time"] = time.monotonic()
    state = {
        "name": track.name,
        "path": track.path,
        "plugin": _capture_native_plugin(track.plugin, values, readers,
                                         capture_native_state=capture_native_state),
        "effects": [_capture_native_plugin(effect, values, readers,
                                           capture_native_state=capture_native_state)
                    for effect in track.effects],
        "midi_channels": set(track.midi_channels),
        "gain": float(track.gain),
        "muted": bool(track.muted),
        "solo": bool(track.solo),
        "enabled": bool(track.enabled),
        "source_type": track.source_type,
        "vcv_patch_path": track.vcv_patch_path,
        "pd_patch_path": track.pd_patch_path,
        "deck": dict(track.deck),
        "generation": track.generation,
    }
    owner.engine.parameter_values = values
    owner.engine.parameter_readers = readers
    return state


def _normalized_result(operation, result):
    if operation == "load_vcv":
        return {"patch_result": result[1], "cardinal_path": result[2]}
    if operation == "load_vcv_patch":
        return {"patch_result": result[1], "cardinal_path": result[2],
                "patch_path": str(result[3])}
    if operation == "load_pd":
        return {"patch_result": result[1], "plugdata_path": result[2]}
    if operation == "load_pd_patch":
        return {"patch_result": result[1], "plugdata_path": result[2],
                "patch_path": str(result[3])}
    return None


def _watch_parent(lifeline, transport_path, stopping):
    parent = mp.parent_process()
    watched = [lifeline]
    if parent is not None:
        watched.append(parent.sentinel)
    while not stopping.is_set():
        try:
            ready = wait_connections(watched, timeout=0.05)
        except OSError:
            if stopping.is_set():
                return
            ready = watched
        if stopping.is_set():
            return
        if parent is not None and parent.sentinel in ready:
            try:
                os.unlink(transport_path)
            except FileNotFoundError:
                pass
            os._exit(1)
        if lifeline in ready:
            if parent is None:
                try:
                    os.unlink(transport_path)
                except FileNotFoundError:
                    pass
                os._exit(1)
            watched.remove(lifeline)




def _execute(owner, index, operation, arguments):
    arguments = dict(arguments)
    requested_index = arguments.pop("track_index", index)
    if requested_index != index:
        raise ValueError("control targets a different track")
    if operation == "set_log_level":
        return {"ok": True, "result": set_log_level(**arguments), "updates": []}
    if operation == "set_parameter":
        result = owner.engine.set_parameter(track_index=index, **arguments)
    elif operation == "set_parameters":
        params = arguments.pop("params")
        fx_index = arguments.pop("fx_index", None)
        if arguments:
            raise TypeError("set_parameters takes only params and fx_index")
        if not isinstance(params, dict):
            raise TypeError("params must be a dict")
        if len(params) > PARAMETER_BATCH_LIMIT:
            raise ValueError(
                f"parameter batch exceeds {PARAMETER_BATCH_LIMIT} entries")
        values, errors = {}, {}
        for name, value in params.items():
            try:
                values[name] = owner.engine.set_parameter(
                    track_index=index, name=name, value=value, fx_index=fx_index)
            except (TimeoutError, BufferError, TrackProcessError):
                raise
            except Exception as error:
                errors[name] = str(error)
        result = {"values": values, "errors": errors}
    elif operation == "sync_state":
        if arguments:
            raise TypeError("sync_state takes no arguments")
        result = None
    elif operation == "deck_command":
        result = owner.engine.deck_command(index, **arguments)
    elif operation == "set_deck_clock":
        result = owner.engine.set_deck_clock(index, **arguments)
    elif operation == "load_plugin_state":
        owner.engine.load_plugin_state(track_index=index, **arguments)
        result = None
    elif operation in ("load_effect", "remove_effect", "update_deck_catalog"):
        getattr(owner, operation)(track_index=index, **arguments)
        result = None
    elif operation == "shutdown":
        if arguments:
            raise TypeError("shutdown takes no arguments")
        owner.shutdown()
        return {"ok": True, "result": None, "updates": []}
    else:
        raise ValueError(f"unknown track operation: {operation}")
    updates = list(owner.engine._applied_updates)
    owner.engine._applied_updates.clear()
    reply = {"ok": True, "result": result, "updates": updates}
    track = owner.engine.tracks[index]
    if track.source_type == "deck" and operation in {"set_parameter", "set_parameters", "deck_command"}:
        reply["deck"] = dict(track.plugin.native.deck_status())
        reply["deck"]["status_time"] = time.monotonic()
    if operation not in {"set_parameter", "set_parameters", "deck_command", "set_deck_clock"}:
        reply["state"] = _track_state(
            owner, index, capture_native_state=operation == "sync_state")
        if operation == "load_plugin_state":
            fx_index = arguments.get("fx_index")
            reply["result"] = (reply["state"]["plugin"] if fx_index is None
                               else reply["state"]["effects"][fx_index])
    return reply


def _track_owner(sample_rate, frames, index, operation, arguments,
                 transport_path, control, lifeline, output, status):
    with native_output(f"track {index + 1}", output):
        _run_track_owner(sample_rate, frames, index, operation, arguments,
                         transport_path, control, lifeline, status)


def _run_track_owner(sample_rate, frames, index, operation, arguments,
                     transport_path, control, lifeline, status):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_logging()
    owner = None
    transport = None
    ready = False
    stopping = threading.Event()
    parent_watch = threading.Thread(
        target=_watch_parent, args=(lifeline, transport_path, stopping),
        name="vcpi-track-parent", daemon=True)
    parent_watch.start()
    try:
        from vcpi.audio_deps import require_native
        from vcpi.plugin_host import PluginHost

        backend = require_native()
        transport = backend.Transport(transport_path, frames, False)
        if operation == "load_deck":
            request = receive_frame(control, CONTROL_LIMIT, native_state=True)
            if request.get("operation") != operation:
                raise ValueError("invalid initial deck catalog envelope")
            encode_deck_message(request, CONTROL_LIMIT)
            arguments = request["arguments"]
        owner = PluginHost(sample_rate=sample_rate, buffer_size=frames, track_mode=True)
        loaded = getattr(owner, operation)(track_index=index, **arguments)
        owner.engine.runner = backend.TrackRunner(
            transport, owner.engine.tracks[index].plugin.native, sample_rate, frames)
        owner.engine.runner.barrier()
        control.send_bytes(_encode({
            "ok": True, "result": _normalized_result(operation, loaded),
            "state": _track_state(owner, index), "updates": [],
        }, STATE_LIMIT))
        ready = True
        last_status = 0.0
        deck_metadata_key = None
        deck_parameters = None
        while True:
            now = time.monotonic()
            track = owner.engine.tracks[index]
            if track.source_type == "deck" and now - last_status >= 0.1:
                last_status = now
                try:
                    current = dict(track.plugin.native.deck_status())
                    current["status_time"] = now
                    metadata_key = (current.get("track_id"), current.get("duration"),
                                    tuple(entry["id"] for entry in track.deck["tracks"]))
                    if metadata_key != deck_metadata_key:
                        deck_parameters = capture_plugin(track.plugin, {}, {}).parameters
                        deck_metadata_key = metadata_key
                finally:
                    # A request may have arrived while metadata owned the plugin.
                    owner.engine.runner.wake()
                current["parameters"] = deck_parameters
                track.deck.update((key, value) for key, value in current.items()
                                  if key != "parameters")
                # A single pending snapshot bounds memory and never stalls DSP
                # or control if the output owner is busy staging another source.
                try:
                    status.put_nowait(deck_status_frames(current, CONTROL_LIMIT, STATE_LIMIT))
                except queue.Full:
                    pass
            if not control.poll(0.05):
                continue
            request = receive_frame(control, CONTROL_LIMIT, native_state=True)
            operation = request["operation"]
            if operation == "load_plugin_state":
                validate_native_message(request, CONTROL_LIMIT)
            elif operation == "update_deck_catalog":
                encode_deck_message(request, CONTROL_LIMIT)
            else:
                _encode(request, CONTROL_LIMIT)
            try:
                # DSP/control ownership is serialized by the native Plugin.
                # The barrier also orders snapshots after already-issued MIDI.
                owner.engine.runner.barrier()
                if operation == "shutdown":
                    stopping.set()
                runner = owner.engine.runner
                try:
                    reply = _execute(owner, index, operation, request["arguments"])
                finally:
                    # Resume a request deferred by native plugin/control ownership,
                    # even when a setter or state capture raises.
                    runner.wake()
                data = (encode_native_message(reply, STATE_LIMIT)
                        if operation == "sync_state" else _encode(reply, STATE_LIMIT))
            except Exception as error:
                fatal = isinstance(error, (TimeoutError, BufferError, TrackProcessError))
                data = _encode({"ok": False, "error": str(error),
                                "error_type": type(error).__name__,
                                "fatal": fatal}, STATE_LIMIT)
                control.send_bytes(data)
                if fatal:
                    return
            else:
                send_frame(control, data, STATE_LIMIT)
                if operation == "shutdown":
                    return
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        try:
            control.send_bytes(_encode({"ok": False, "error": str(error),
                                       "error_type": type(error).__name__,
                                       "fatal": ready or isinstance(
                                           error, (TimeoutError, BufferError, TrackProcessError))
                                       }, STATE_LIMIT))
        except (OSError, TrackProcessError):
            pass
    finally:
        # Normal teardown must run native cleanup and Python finalizers; a
        # closed lifeline must not make the watchdog short-circuit either.
        stopping.set()
        control.close()
        status.cancel_join_thread()
        status.close()
        try:
            if owner is not None:
                owner.shutdown()
            if transport is not None and hasattr(transport, "close"):
                transport.close()
        finally:
            lifeline.close()
