"""Bounded cleanup identity for one operation-owned prepared helper stage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ValidationError

from ._helper_identity import decode_identity
from ._helper_launcher import IdentityMode, IdentityPlan, _validate_plan
from ._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from ._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS, _NumericGuestBootstrap
from ._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id

PREPARED_HELPER_OBLIGATION_KIND = "prepared-helper-stage"
PREPARED_HELPER_PAYLOAD_VERSION = 1
_FIELDS = {"version", "target", "bootstrap", "runtime", "token"}


class PreparedHelperObligationError(ValueError):
    """Invalid persisted cleanup identity, without its contents."""


@dataclass(frozen=True, slots=True, repr=False)
class PreparedHelperObligation:
    """Cleanup authority only, never input bytes or activation authority."""

    target: ManagedTargetIdentity
    bootstrap: _NumericGuestBootstrap
    runtime: RuntimeSelection
    token: bytes


def encode_prepared_helper_obligation(record: PreparedHelperObligation) -> bytes:
    """Validate identity at the durable cleanup-record boundary."""
    try:
        target, bootstrap, runtime = record.target, record.bootstrap, record.runtime
        if (
            type(record) is not PreparedHelperObligation
            or type(target) is not ManagedTargetIdentity
            or target.kind is not ManagedTargetKind.VM
            or type(bootstrap) is not _NumericGuestBootstrap
            or type(runtime) is not RuntimeSelection
            or runtime.target_os is not RuntimeTargetOS.LINUX
            or runtime.explicit_path not in (None, "/usr/bin/python3")
            or type(record.token) is not bytes
            or len(record.token) != 16
            or _validate_plan(bootstrap.root_entry).euid != 0
            or vm_guest_boot_id(bootstrap.guest) != target.boot_id
        ):
            raise ValueError
        plan, guest = bootstrap.root_entry, bootstrap.guest
        data = _json(
            {
                "version": PREPARED_HELPER_PAYLOAD_VERSION,
                "target": {
                    "kind": target.kind.value,
                    "name": target.name,
                    "incarnation": target.incarnation,
                    "boot_id": target.boot_id,
                },
                "bootstrap": {
                    "root_entry": {
                        "mode": plan.mode.value,
                        "euid": plan.expected.euid,
                        "egid": plan.expected.egid,
                        "groups": list(plan.expected.groups),
                    },
                    "guest": {
                        "instance_marker": guest.instance_marker,
                        "boot_id": guest.boot_id,
                        "init_start_ticks": guest.init_start_ticks,
                    },
                },
                "runtime": {"target_os": runtime.target_os.value, "explicit_path": runtime.explicit_path},
                "token": record.token.hex(),
            }
        )
        if len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValueError
        return data
    except (AttributeError, TypeError, ValueError, ValidationError, RecursionError, OverflowError):
        raise PreparedHelperObligationError("invalid prepared helper cleanup identity") from None


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError
    return cast("dict[str, object]", value)


def decode_prepared_helper_obligation(payload: bytes) -> PreparedHelperObligation:
    """Refuse noncanonical or mismatched persisted cleanup authority."""
    try:
        if type(payload) is not bytes or len(payload) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValueError
        value = _object(json.loads(payload), _FIELDS)
        if type(value["version"]) is not int or value["version"] != PREPARED_HELPER_PAYLOAD_VERSION:
            raise ValueError
        target = _object(value["target"], {"kind", "name", "incarnation", "boot_id"})
        bootstrap = _object(value["bootstrap"], {"root_entry", "guest"})
        root = _object(bootstrap["root_entry"], {"mode", "euid", "egid", "groups"})
        guest = _object(bootstrap["guest"], {"instance_marker", "boot_id", "init_start_ticks"})
        runtime = _object(value["runtime"], {"target_os", "explicit_path"})
        token = value["token"]
        if type(token) is not str or len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError
        record = PreparedHelperObligation(
            ManagedTargetIdentity(
                ManagedTargetKind(cast("str", target["kind"])),
                cast("str", target["name"]),
                cast("str", target["incarnation"]),
                cast("str", target["boot_id"]),
            ),
            _NumericGuestBootstrap(
                IdentityPlan(
                    decode_identity({key: root[key] for key in ("euid", "egid", "groups")}),
                    IdentityMode(cast("str", root["mode"])),
                ),
                VMGuestIdentity(
                    cast("str", guest["instance_marker"]),
                    cast("str", guest["boot_id"]),
                    cast("int", guest["init_start_ticks"]),
                ),
            ),
            RuntimeSelection(
                RuntimeTargetOS(cast("str", runtime["target_os"])), cast("str | None", runtime["explicit_path"])
            ),
            bytes.fromhex(token),
        )
        if encode_prepared_helper_obligation(record) != payload:
            raise ValueError
        return record
    except (AttributeError, TypeError, ValueError, ValidationError, RecursionError, OverflowError):
        raise PreparedHelperObligationError("invalid prepared helper cleanup identity") from None
