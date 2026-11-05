"""Native plugin snapshots and conservative legacy parameter restoration."""

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING

from vcpi.state_transport import NATIVE_STATE_LIMIT, validate_native_state

if TYPE_CHECKING:
    from vcpi.host import VcpiCore

logger = logging.getLogger(__name__)


def _saved_plugin_state(plugin) -> str:
    """Encode only a chunk captured by the explicit native save barrier."""
    if plugin is None or plugin.native_state is None:
        raise RuntimeError("native plugin state was not captured; refusing parameter-only save")
    return base64.b64encode(validate_native_state(plugin.native_state)).decode("ascii")


def _apply_plugin_params(
    host: VcpiCore,
    track_index: int | None,
    params: dict[str, bool | str | int | float],
    fx_index: int | None = None,
):
    """Conservatively migrate v1 values using freshly loaded live descriptors."""
    if track_index is None:
        plugin = host.engine.master_effects[fx_index]
    else:
        track = host.engine.tracks[track_index]
        plugin = track.plugin if fx_index is None else track.effects[fx_index]
    descriptors = plugin.parameters if plugin is not None else {}
    safe_params = {}
    for name, value in params.items():
        descriptor = descriptors.get(name)
        if descriptor is None or not descriptor.is_automatable:
            logger.warning(
                "[session] skipped legacy param '%s': %s", name,
                "unavailable in live metadata" if descriptor is None else "not automatable",
            )
        else:
            safe_params[name] = value
    if not safe_params:
        return
    result = host.engine.set_parameters(track_index, safe_params, fx_index=fx_index)
    for name, error in result["errors"].items():
        logger.warning(
            "[session] could not restore param '%s' = %s: %s",
            name, params[name], error,
        )


def _restore_plugin_state(host, track_index, entry, version, fx_index=None, *, wav=False):
    """Native chunks take precedence; parameter replay is legacy-v1-only."""
    if wav:
        return
    if "native_state" in entry:
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
    elif version == 1:
        _apply_plugin_params(host, track_index, entry.get("params", {}), fx_index)
    else:
        raise ValueError(f"version {version} VST3 entry is missing native_state")
