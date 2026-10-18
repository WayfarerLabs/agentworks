"""Bounded namespace custody, without account names, helper source or payloads."""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.execution._file_effect_gate import (
    FileEffectGateBinding,
    decode_file_effect_gate,
    encode_file_effect_gate,
)
from agentworks.execution._file_effect_gate_protocol import _decode_object, _json_bytes
from agentworks.execution._file_gate_namespace_protocol import NamespaceRequest, encode_namespace_request
from agentworks.execution._file_gate_setup import FileEffectGateSetup, file_effect_gate_path
from agentworks.execution._file_obligation import (
    _decode_bootstrap,
    _decode_runtime_selection,
    _decode_target,
    _encode_guest,
    _encode_identity_plan,
    _encode_runtime_selection,
    _encode_target,
)
from agentworks.execution._file_wire import valid_nonce
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS, _NumericGuestBootstrap
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id

NAMESPACE_OBLIGATION_VERSION = 1
MAX_NAMESPACE_OBLIGATION_BYTES = 16_384


@dataclass(frozen=True, slots=True, repr=False)
class NamespaceObligation:
    nonce: str
    uid: int
    gid: int
    target: ManagedTargetIdentity
    bootstrap: _NumericGuestBootstrap
    runtime_selection: RuntimeSelection
    setup: FileEffectGateSetup
    binding: FileEffectGateBinding | None = None


def encode_namespace_obligation(record: NamespaceObligation) -> bytes:
    """Encode and round-trip the exact recovery identity before dispatch."""
    data = _json_bytes(
        {
            "version": NAMESPACE_OBLIGATION_VERSION,
            "nonce": record.nonce,
            "uid": record.uid,
            "gid": record.gid,
            "target": _encode_target(record.target),
            "bootstrap": {
                "guest": _encode_guest(record.bootstrap.guest),
                "root_entry": _encode_identity_plan(record.bootstrap.root_entry),
            },
            "runtime": _encode_runtime_selection(record.runtime_selection),
            "setup": {"guest": _encode_guest(record.setup.guest), "path": record.setup.path},
            "binding": None if record.binding is None else encode_file_effect_gate(record.binding),
            "publication": "possible" if record.binding is not None else "not-admitted",
            "cleanup": "owned-temporary-possible" if record.binding is not None else "not-admitted",
        }
    )
    decode_namespace_obligation(data)
    return data


def decode_namespace_obligation(data: bytes) -> NamespaceObligation:
    """Reject corrupt, old, oversized or mismatched persisted custody."""
    value = _decode_object(data, maximum_bytes=MAX_NAMESPACE_OBLIGATION_BYTES)
    if set(value) != {
        "version",
        "nonce",
        "uid",
        "gid",
        "target",
        "bootstrap",
        "runtime",
        "setup",
        "binding",
        "publication",
        "cleanup",
    }:
        raise ValueError("invalid namespace obligation")
    bootstrap = _decode_bootstrap(value["bootstrap"])
    target = _decode_target(value["target"])
    runtime = _decode_runtime_selection(value["runtime"])
    binding = None if value["binding"] is None else decode_file_effect_gate(value["binding"])
    setup = FileEffectGateSetup.for_target(target, 0, bootstrap.guest)
    if (
        type(value["version"]) is not int
        or value["version"] != NAMESPACE_OBLIGATION_VERSION
        or not valid_nonce(value["nonce"])
        or type(value["uid"]) is not int
        or not 0 < value["uid"] < 2**32
        or type(value["gid"]) is not int
        or not 0 <= value["gid"] < 2**32
        or target.kind is not ManagedTargetKind.VM
        or target.boot_id != vm_guest_boot_id(bootstrap.guest)
        or runtime.target_os is not RuntimeTargetOS.LINUX
        or runtime.explicit_path not in (None, "/usr/bin/python3")
        or value["setup"] != {"guest": _encode_guest(setup.guest), "path": setup.path}
        or value["publication"] != ("possible" if binding is not None else "not-admitted")
        or value["cleanup"] != ("owned-temporary-possible" if binding is not None else "not-admitted")
    ):
        raise ValueError("invalid namespace obligation")
    if binding is not None:
        encode_namespace_request(
            NamespaceRequest(value["nonce"], value["uid"], value["gid"], bootstrap.root_entry.expected, binding, 0.0)
        )
        if (
            binding.guest != bootstrap.guest
            or binding.path != file_effect_gate_path(target, 0, bootstrap.guest)
            or binding.scope_name != target.name
        ):
            raise ValueError("invalid namespace obligation binding")
    return NamespaceObligation(value["nonce"], value["uid"], value["gid"], target, bootstrap, runtime, setup, binding)
