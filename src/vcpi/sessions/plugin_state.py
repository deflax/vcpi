"""Native plugin state snapshots and restoration."""

from __future__ import annotations

import base64
import binascii

from vcpi.state_transport import NATIVE_STATE_LIMIT, validate_native_state


def _saved_plugin_state(plugin) -> str:
    """Encode only a chunk captured by the explicit native save barrier."""
    if plugin is None or plugin.native_state is None:
        raise RuntimeError("native plugin state was not captured; refusing parameter-only save")
    return base64.b64encode(validate_native_state(plugin.native_state)).decode("ascii")


def _restore_plugin_state(host, track_index, entry, fx_index=None):
    """Restore the required native chunk without replaying parameters."""
    if "native_state" not in entry:
        raise ValueError("VST3 entry is missing native_state")
    encoded = entry["native_state"]
    if not isinstance(encoded, str):
        raise ValueError("native_state must be a base64 string")
    if len(encoded) > 4 * ((NATIVE_STATE_LIMIT + 2) // 3):
        raise ValueError("native_state exceeds the bounded state allowance")
    try:
        state = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("native_state is not valid base64") from error
    host.engine.load_plugin_state(
        track_index, validate_native_state(state), fx_index=fx_index)
