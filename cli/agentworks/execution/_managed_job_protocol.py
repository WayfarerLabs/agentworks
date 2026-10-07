"""Typed host mapping for private canonical managed-job facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from agentworks.errors import ValidationError
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_runs import (
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution.models import Shell

MAX_MANAGED_JOB_FACT_BYTES = wire.MAX_MANAGED_JOB_FACT_BYTES


class ManagedJobFactError(ValueError):
    """One private job fact violates the canonical wire protocol."""


class StreamName(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class StreamDisposition(StrEnum):
    COMPLETE_CAPTURE = "complete-capture"
    TRUNCATED_CAPTURE = "truncated-capture"
    DISCARDED = "discarded"
    SUPPRESSED = "sensitivity-suppressed"


@dataclass(frozen=True, slots=True)
class WorkloadWaitFact:
    """The main process was reaped with exactly one terminal status."""

    identity: ManagedRunIdentity
    unit_name: str
    receipt_sha256: str
    exit_code: int | None = None
    signal: int | None = None

    def __post_init__(self) -> None:
        _validate_typed_fact(self)


@dataclass(frozen=True, slots=True)
class StreamEndFact:
    """One terminal stream with a disposition and retained-byte evidence."""

    identity: ManagedRunIdentity
    unit_name: str
    receipt_sha256: str
    stream: StreamName
    retained_bytes: int
    retained_sha256: str
    disposition: StreamDisposition

    def __post_init__(self) -> None:
        _validate_typed_fact(self)


@dataclass(frozen=True, slots=True)
class BoundaryEmptyFact:
    """Positive observation that the exact owned workload boundary was empty."""

    identity: ManagedRunIdentity
    unit_name: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        _validate_typed_fact(self)


type ManagedJobFact = ManagedRunReceipt | WorkloadWaitFact | StreamEndFact | BoundaryEmptyFact


def _receipt_object(receipt: ManagedRunReceipt) -> dict[str, object]:
    if type(receipt) is not ManagedRunReceipt or receipt.unit_name != receipt.identity.unit_name:
        raise ManagedJobFactError("invalid realized launch receipt")
    spec = receipt.spec
    return {
        "version": wire.VERSION,
        "kind": "launch",
        "run_id": receipt.identity.run_id,
        "unit": receipt.unit_name,
        "target": {
            "kind": spec.target.kind.value,
            "name": spec.target.name,
            "incarnation": spec.target.incarnation,
            "boot_id": spec.target.boot_id,
        },
        "workload": {"euid": spec.workload.euid, "egid": spec.workload.egid, "groups": list(spec.workload.groups)},
        "shell": {
            "requested": spec.shell.requested.value if spec.shell.requested is not None else None,
            "resolved_executable": spec.shell.resolved_executable,
            "login": spec.shell.login,
            "interactive": spec.shell.interactive,
        },
        "owner": {"kind": spec.owner.kind.value, "owner_id": spec.owner.owner_id},
        "lifetime": spec.lifetime.value,
        "profile_revision": spec.managed_profile_revision,
        "receipt_namespace": spec.receipt_namespace,
        "receipt_protocol_version": spec.receipt_protocol_version,
    }


def _fact_object(fact: WorkloadWaitFact | StreamEndFact | BoundaryEmptyFact) -> dict[str, object]:
    if type(fact.identity) is not ManagedRunIdentity:
        raise ManagedJobFactError("invalid managed-job fact identity")
    value: dict[str, object] = {
        "version": wire.VERSION,
        "run_id": fact.identity.run_id,
        "unit": fact.unit_name,
        "receipt_sha256": fact.receipt_sha256,
    }
    if isinstance(fact, WorkloadWaitFact):
        value.update({"kind": "wait", "exit_code": fact.exit_code, "signal": fact.signal})
    elif isinstance(fact, StreamEndFact):
        if type(fact.stream) is not StreamName or type(fact.disposition) is not StreamDisposition:
            raise ManagedJobFactError("invalid stream or disposition")
        value.update(
            {
                "kind": "stream-end",
                "stream": fact.stream.value,
                "retained_bytes": fact.retained_bytes,
                "retained_sha256": fact.retained_sha256,
                "disposition": fact.disposition.value,
            }
        )
    else:
        value["kind"] = "boundary-empty"
    return value


def _validate_typed_fact(fact: WorkloadWaitFact | StreamEndFact | BoundaryEmptyFact) -> None:
    try:
        wire.encode_fact(_fact_object(fact))
    except wire.ManagedJobWireError as exc:
        raise ManagedJobFactError(str(exc)) from None


def encode_managed_job_fact(fact: ManagedJobFact) -> bytes:
    """Map one typed fact to the portable canonical codec."""
    if isinstance(fact, ManagedRunReceipt):
        value = _receipt_object(fact)
    elif isinstance(fact, WorkloadWaitFact | StreamEndFact | BoundaryEmptyFact):
        value = _fact_object(fact)
    else:
        raise ManagedJobFactError("unsupported managed-job fact")
    try:
        return wire.encode_fact(value)
    except wire.ManagedJobWireError as exc:
        raise ManagedJobFactError(str(exc)) from None


def managed_launch_receipt_sha256(receipt: ManagedRunReceipt) -> str:
    """Digest the exact canonical launch receipt used by later facts."""
    try:
        return wire.launch_sha256(_receipt_object(receipt))
    except wire.ManagedJobWireError as exc:
        raise ManagedJobFactError(str(exc)) from None


def _decode_receipt(value: dict[str, Any]) -> ManagedRunReceipt:
    target = value["target"]
    workload = value["workload"]
    shell = value["shell"]
    owner = value["owner"]
    requested = shell["requested"]
    spec = ManagedRunSpec(
        ManagedTargetIdentity(
            ManagedTargetKind(target["kind"]), target["name"], target["incarnation"], target["boot_id"]
        ),
        IdentityExpectation(workload["euid"], workload["egid"], tuple(workload["groups"])),
        ManagedShellIdentity(
            None if requested is None else Shell(requested),
            shell["resolved_executable"],
            shell["login"],
            shell["interactive"],
        ),
        ManagedRunOwner(ManagedRunOwnerKind(owner["kind"]), owner["owner_id"]),
        ManagedRunLifetime(value["lifetime"]),
        value["profile_revision"],
        value["receipt_namespace"],
        value["receipt_protocol_version"],
    )
    return ManagedRunReceipt(ManagedRunIdentity(value["run_id"]), value["unit"], spec)


def decode_managed_job_fact(data: bytes) -> ManagedJobFact:
    """Decode validated primitive bytes into typed host facts."""
    try:
        value = cast("dict[str, Any]", wire.decode_fact(data))
        kind = value["kind"]
        if kind == "launch":
            return _decode_receipt(value)
        run = ManagedRunIdentity(value["run_id"])
        unit = value["unit"]
        digest = value["receipt_sha256"]
        if kind == "wait":
            return WorkloadWaitFact(run, unit, digest, value["exit_code"], value["signal"])
        if kind == "stream-end":
            return StreamEndFact(
                run,
                unit,
                digest,
                StreamName(value["stream"]),
                value["retained_bytes"],
                value["retained_sha256"],
                StreamDisposition(value["disposition"]),
            )
        return BoundaryEmptyFact(run, unit, digest)
    except (wire.ManagedJobWireError, KeyError, TypeError, ValueError, ValidationError, AttributeError):
        raise ManagedJobFactError("invalid managed-job fact") from None


def fact_matches_receipt(
    fact: WorkloadWaitFact | StreamEndFact | BoundaryEmptyFact, receipt: ManagedRunReceipt
) -> bool:
    """Check the complete binding before trusting a post-launch observation."""
    return (
        fact.identity == receipt.identity
        and fact.unit_name == receipt.identity.unit_name
        and fact.receipt_sha256 == managed_launch_receipt_sha256(receipt)
    )
