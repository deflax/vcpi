"""Bounded multipart control frames for explicit opaque plugin-state transfers.

Ordinary RPC frames retain their existing limits. Only state capture/load may
use the separate, finite native-state allowance; each pipe frame stays bounded.
"""

import pickle
from dataclasses import replace

NATIVE_STATE_LIMIT = 128 * 1024 * 1024
NATIVE_MESSAGE_LIMIT = 512 * 1024 * 1024
_PART_LIMIT = 64 * 1024
_MULTIPART = "vcpi-native-state-v1"


def validate_native_state(state):
    if not isinstance(state, bytes):
        raise TypeError("native plugin state must be bytes")
    if not state:
        raise ValueError("native plugin state is empty")
    if len(state) > NATIVE_STATE_LIMIT:
        raise ValueError(f"native plugin state exceeds {NATIVE_STATE_LIMIT} bytes")
    return state


def validate_native_message(message, ordinary_limit):
    """Keep native bytes separate from the ordinary metadata safety budget."""
    metadata = dict(message)
    if message.get("operation") == "load_plugin_state":
        arguments = dict(message["arguments"])
        validate_native_state(arguments["state"])
        arguments["state"] = None
        metadata["arguments"] = arguments
    elif "state" in message:
        state = dict(message["state"])

        def strip(plugin):
            if plugin.native_state is not None:
                validate_native_state(plugin.native_state)
            return replace(plugin, native_state=None)

        # Slot-owner replies contain one slot record, audio-owner replies all.
        def slot_record(record):
            if record is None:
                return None
            return {**record, "plugin": strip(record["plugin"]),
                    "effects": [strip(effect) for effect in record["effects"]]}

        if "slots" in state:
            state["slots"] = [slot_record(record) for record in state["slots"]]
            state["master_effects"] = [strip(effect) for effect in state["master_effects"]]
        else:
            state = slot_record(state)
        metadata["state"] = state
    else:
        raise ValueError("native state transfer requires an explicit capture/load envelope")
    if len(pickle.dumps(metadata, protocol=pickle.HIGHEST_PROTOCOL)) > ordinary_limit:
        raise ValueError(f"native state metadata exceeds {ordinary_limit} bytes")


def encode_native_message(message, ordinary_limit):
    validate_native_message(message, ordinary_limit)
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > NATIVE_MESSAGE_LIMIT:
        raise ValueError(f"native state message exceeds {NATIVE_MESSAGE_LIMIT} bytes")
    return data


def send_frame(connection, data, ordinary_limit):
    if len(data) <= ordinary_limit:
        connection.send_bytes(data)
        return
    if len(data) > NATIVE_MESSAGE_LIMIT:
        raise ValueError(f"native state message exceeds {NATIVE_MESSAGE_LIMIT} bytes")
    connection.send_bytes(pickle.dumps((_MULTIPART, len(data))))
    view = memoryview(data)
    for start in range(0, len(data), _PART_LIMIT):
        connection.send_bytes(view[start:start + _PART_LIMIT])


def receive_frame(connection, ordinary_limit, *, native_state=False):
    data = connection.recv_bytes(ordinary_limit)
    message = pickle.loads(data)
    if not (isinstance(message, tuple) and len(message) == 2
            and message[0] == _MULTIPART):
        return message
    size = message[1]
    if not native_state:
        raise ValueError("native state multipart frame is not permitted here")
    if type(size) is not int or not ordinary_limit < size <= NATIVE_MESSAGE_LIMIT:
        raise ValueError("invalid native state multipart size")
    # Allocate once, only on a control thread, with a strict aggregate bound.
    payload = bytearray(size)
    position = 0
    while position < size:
        part = connection.recv_bytes(min(_PART_LIMIT, size - position))
        if not part:
            raise ValueError("empty native state multipart frame")
        payload[position:position + len(part)] = part
        position += len(part)
    return pickle.loads(payload)
