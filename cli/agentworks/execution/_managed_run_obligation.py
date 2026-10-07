"""Canonical non-secret run identity for managed lifecycle obligations."""

from __future__ import annotations

import json

from agentworks.errors import ValidationError

from ._managed_runs import ManagedRunIdentity


def encode_managed_run_obligation(run_id: str) -> bytes:
    """Encode only one canonical run ID, with no request or route data."""
    identity = ManagedRunIdentity(run_id)
    return json.dumps({"run_id": identity.run_id, "version": 1}, sort_keys=True, separators=(",", ":")).encode("ascii")


def decode_managed_run_obligation(payload: bytes) -> ManagedRunIdentity:
    """Validate one persisted recovery identity at the cross-execution boundary."""
    if type(payload) is not bytes or len(payload) != 57:
        raise ValidationError("Managed run obligation payload is invalid")
    try:
        value = json.loads(payload)
        if type(value) is not dict or set(value) != {"run_id", "version"} or value["version"] != 1:
            raise ValueError
        identity = ManagedRunIdentity(value["run_id"])
        if encode_managed_run_obligation(identity.run_id) != payload:
            raise ValueError
    except (TypeError, ValueError, UnicodeError):
        raise ValidationError("Managed run obligation payload is invalid") from None
    return identity
