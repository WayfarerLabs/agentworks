"""Private, bounded wire facts for a future reconnectable managed-job service.

These codecs neither persist facts nor establish that a workload was launched.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentworks.errors import ValidationError
from agentworks.execution._helper_identity import IdentityExpectation, decode_identity
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

MAX_MANAGED_JOB_FACT_BYTES = 4096
_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()
_COMMON = frozenset({"version", "kind", "run_id", "unit", "receipt_sha256"})
_RECEIPT = frozenset(
    {
        "version",
        "kind",
        "run_id",
        "unit",
        "target",
        "workload",
        "shell",
        "owner",
        "lifetime",
        "profile_revision",
        "receipt_namespace",
        "receipt_protocol_version",
    }
)


class ManagedJobFactError(ValueError):
    """One private job fact violates the canonical wire protocol."""


class StreamName(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class StreamDisposition(StrEnum):
    """Closed stream outcome; only capture outcomes have a retained spool."""

    COMPLETE_CAPTURE = "complete-capture"
    TRUNCATED_CAPTURE = "truncated-capture"
    DISCARDED = "discarded"
    SUPPRESSED = "sensitivity-suppressed"


def _identity(run: ManagedRunIdentity, unit: str, receipt_sha256: str) -> None:
    if (
        type(run) is not ManagedRunIdentity
        or type(unit) is not str
        or unit != run.unit_name
        or type(receipt_sha256) is not str
        or _HASH.fullmatch(receipt_sha256) is None
    ):
        raise ManagedJobFactError("invalid managed-job fact binding")


@dataclass(frozen=True, slots=True)
class WorkloadWaitFact:
    """The main process was reaped with exactly one terminal status."""

    identity: ManagedRunIdentity
    unit_name: str
    receipt_sha256: str
    exit_code: int | None = None
    signal: int | None = None

    def __post_init__(self) -> None:
        _identity(self.identity, self.unit_name, self.receipt_sha256)
        if (self.exit_code is None) == (self.signal is None):
            raise ManagedJobFactError("wait fact requires exactly one terminal status")
        if self.exit_code is not None and (type(self.exit_code) is not int or not 0 <= self.exit_code <= 255):
            raise ManagedJobFactError("invalid workload exit code")
        if self.signal is not None and (type(self.signal) is not int or not 1 <= self.signal <= 64):
            raise ManagedJobFactError("invalid workload signal")


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
        _identity(self.identity, self.unit_name, self.receipt_sha256)
        if type(self.stream) is not StreamName or type(self.disposition) is not StreamDisposition:
            raise ManagedJobFactError("invalid stream or disposition")
        if type(self.retained_bytes) is not int or not 0 <= self.retained_bytes <= 2**63 - 1:
            raise ManagedJobFactError("invalid retained byte length")
        if type(self.retained_sha256) is not str or _HASH.fullmatch(self.retained_sha256) is None:
            raise ManagedJobFactError("invalid retained byte digest")
        if self.retained_bytes == 0 and self.retained_sha256 != _EMPTY_HASH:
            raise ManagedJobFactError("empty retained bytes have an inconsistent digest")
        if self.disposition in (StreamDisposition.DISCARDED, StreamDisposition.SUPPRESSED) and (
            self.retained_bytes != 0 or self.retained_sha256 != _EMPTY_HASH
        ):
            raise ManagedJobFactError("non-capture stream cannot retain output")


@dataclass(frozen=True, slots=True)
class BoundaryEmptyFact:
    """Positive observation that the exact owned workload boundary was empty."""

    identity: ManagedRunIdentity
    unit_name: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        _identity(self.identity, self.unit_name, self.receipt_sha256)


type ManagedJobFact = ManagedRunReceipt | WorkloadWaitFact | StreamEndFact | BoundaryEmptyFact


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _object(value: object, fields: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ManagedJobFactError("invalid managed-job fact fields")
    return value


def _receipt_object(receipt: ManagedRunReceipt) -> dict[str, object]:
    if type(receipt) is not ManagedRunReceipt or receipt.unit_name != receipt.identity.unit_name:
        raise ManagedJobFactError("invalid realized launch receipt")
    spec = receipt.spec
    return {
        "version": _VERSION,
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


def encode_managed_job_fact(fact: ManagedJobFact) -> bytes:
    """Encode one validated fact; the byte bound also limits receipt group lists."""
    if isinstance(fact, ManagedRunReceipt):
        value = _receipt_object(fact)
    elif isinstance(fact, WorkloadWaitFact | StreamEndFact | BoundaryEmptyFact):
        value = {
            "version": _VERSION,
            "run_id": fact.identity.run_id,
            "unit": fact.unit_name,
            "receipt_sha256": fact.receipt_sha256,
        }
        if isinstance(fact, WorkloadWaitFact):
            value.update({"kind": "wait", "exit_code": fact.exit_code, "signal": fact.signal})
        elif isinstance(fact, StreamEndFact):
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
    else:
        raise ManagedJobFactError("unsupported managed-job fact")
    try:
        encoded = _json_bytes(value)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ManagedJobFactError("invalid managed-job fact value") from None
    if len(encoded) > MAX_MANAGED_JOB_FACT_BYTES:
        raise ManagedJobFactError("managed-job fact exceeds byte bound")
    return encoded


def managed_launch_receipt_sha256(receipt: ManagedRunReceipt) -> str:
    """Digest the exact canonical launch receipt used by later facts."""
    return hashlib.sha256(encode_managed_job_fact(receipt)).hexdigest()


def _decode_receipt(value: dict[str, Any]) -> ManagedRunReceipt:
    _object(value, _RECEIPT)
    target = _object(value["target"], frozenset({"kind", "name", "incarnation", "boot_id"}))
    shell = _object(value["shell"], frozenset({"requested", "resolved_executable", "login", "interactive"}))
    owner = _object(value["owner"], frozenset({"kind", "owner_id"}))
    if type(shell["login"]) is not bool or type(shell["interactive"]) is not bool:
        raise ManagedJobFactError("invalid shell flags")
    for key in ("profile_revision", "receipt_protocol_version"):
        if type(value[key]) is not int:
            raise ManagedJobFactError("invalid receipt version")
    requested = shell["requested"]
    if requested is not None and type(requested) is not str:
        raise ManagedJobFactError("invalid shell selection")
    workload = decode_identity(value["workload"])
    spec = ManagedRunSpec(
        ManagedTargetIdentity(
            ManagedTargetKind(target["kind"]), target["name"], target["incarnation"], target["boot_id"]
        ),
        IdentityExpectation(workload.euid, workload.egid, workload.groups),
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
    receipt = ManagedRunReceipt(ManagedRunIdentity(value["run_id"]), value["unit"], spec)
    if receipt.unit_name != receipt.identity.unit_name:
        raise ManagedJobFactError("receipt unit does not derive from run")
    return receipt


def decode_managed_job_fact(data: bytes) -> ManagedJobFact:
    """Validate an untrusted canonical fact, including all nested fields."""
    if type(data) is not bytes or len(data) > MAX_MANAGED_JOB_FACT_BYTES:
        raise ManagedJobFactError("invalid managed-job fact bytes")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict:
            raise ValueError
        if type(value.get("version")) is not int or value["version"] != _VERSION or type(value.get("kind")) is not str:
            raise ValueError
        kind = value["kind"]
        if kind == "launch":
            fact: ManagedJobFact = _decode_receipt(value)
        else:
            common = _COMMON
            run = ManagedRunIdentity(value["run_id"])
            unit = value["unit"]
            digest = value["receipt_sha256"]
            if kind == "wait":
                _object(value, common | {"exit_code", "signal"})
                fact = WorkloadWaitFact(run, unit, digest, value["exit_code"], value["signal"])
            elif kind == "stream-end":
                _object(value, common | {"stream", "retained_bytes", "retained_sha256", "disposition"})
                fact = StreamEndFact(
                    run,
                    unit,
                    digest,
                    StreamName(value["stream"]),
                    value["retained_bytes"],
                    value["retained_sha256"],
                    StreamDisposition(value["disposition"]),
                )
            elif kind == "boundary-empty":
                _object(value, common)
                fact = BoundaryEmptyFact(run, unit, digest)
            else:
                raise ValueError
        if encode_managed_job_fact(fact) != data:
            raise ValueError
        return fact
    except (KeyError, TypeError, ValueError, ValidationError, UnicodeError, RecursionError, AttributeError):
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
