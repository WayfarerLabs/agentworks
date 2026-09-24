"""Canonical v1 managed-job facts shared by host and future target producer.

This standalone Python 3.11 module accepts only primitive fact objects. It has
no access to the application payload, the filesystem, or a service runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import cast
from uuid import UUID

MAX_MANAGED_JOB_FACT_BYTES = 4096
VERSION = 1

_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_INCARNATION = re.compile(r"v1:[0-9a-f]{64}\Z")
_SAFE_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]*\Z")
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()
_COMMON = frozenset({"version", "kind", "run_id", "unit", "receipt_sha256"})
_LAUNCH = frozenset(
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


class ManagedJobWireError(ValueError):
    """A primitive fact violates the canonical managed-job wire contract."""


def _object(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ManagedJobWireError("invalid managed-job fact fields")
    return value


def _integer(value: object, maximum: int, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _identity_text(value: object) -> bool:
    return type(value) is str and _SAFE_IDENTITY.fullmatch(value) is not None and len(value) <= 255


def _shell_path(value: object) -> bool:
    if type(value) is not str or value == "/" or not value.startswith("/") or not value.isprintable():
        return False
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        return False
    try:
        return len(value.encode("utf-8")) <= 255
    except UnicodeEncodeError:
        return False


def _validate_launch(value: dict[str, object]) -> None:
    _object(value, _LAUNCH)
    target = _object(value["target"], frozenset({"kind", "name", "incarnation", "boot_id"}))
    workload = _object(value["workload"], frozenset({"euid", "egid", "groups"}))
    shell = _object(value["shell"], frozenset({"requested", "resolved_executable", "login", "interactive"}))
    owner = _object(value["owner"], frozenset({"kind", "owner_id"}))

    if target["kind"] not in ("vm", "platform-host") or not _identity_text(target["name"]):
        raise ManagedJobWireError("invalid target identity")
    if type(target["incarnation"]) is not str or _INCARNATION.fullmatch(target["incarnation"]) is None:
        raise ManagedJobWireError("invalid target incarnation")
    boot_id = target["boot_id"]
    try:
        if type(boot_id) is not str or str(UUID(boot_id)) != boot_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise ManagedJobWireError("invalid target boot identity") from None

    groups = workload["groups"]
    if (
        not _integer(workload["euid"], 2**32 - 1)
        or not _integer(workload["egid"], 2**32 - 1)
        or type(groups) is not list
        or not groups
        or len(groups) > 65_536
        or any(not _integer(group, 2**32 - 1) for group in groups)
        or groups != sorted(set(groups))
        or workload["egid"] not in groups
    ):
        raise ManagedJobWireError("invalid workload identity")

    requested = shell["requested"]
    if type(shell["login"]) is not bool or type(shell["interactive"]) is not bool:
        raise ManagedJobWireError("invalid shell flags")
    if requested is None:
        if shell["resolved_executable"] is not None or shell["login"] or shell["interactive"]:
            raise ManagedJobWireError("invalid literal-command shell identity")
    elif requested not in ("sh", "bash", "user_default") or not _shell_path(shell["resolved_executable"]):
        raise ManagedJobWireError("invalid shell identity")

    owner_kind = owner["kind"]
    lifetime = value["lifetime"]
    if (owner_kind, lifetime) not in (("operation", "operation"), ("resource", "independent")):
        raise ManagedJobWireError("invalid owner lifetime")
    owner_id = owner["owner_id"]
    if owner_kind == "operation":
        if type(owner_id) is not str or _RUN_ID.fullmatch(owner_id) is None:
            raise ManagedJobWireError("invalid operation owner")
    elif not _identity_text(owner_id):
        raise ManagedJobWireError("invalid resource owner")
    if (
        type(value["profile_revision"]) is not int
        or value["profile_revision"] != 1
        or value["receipt_namespace"] != "agentworks-managed-runs-v1"
        or type(value["receipt_protocol_version"]) is not int
        or value["receipt_protocol_version"] != 1
    ):
        raise ManagedJobWireError("invalid receipt protocol")


def _validate_fact(value: object) -> dict[str, object]:
    if type(value) is not dict or type(value.get("version")) is not int or value["version"] != VERSION:
        raise ManagedJobWireError("invalid managed-job fact version")
    kind = value.get("kind")
    run_id = value.get("run_id")
    unit = value.get("unit")
    if (
        type(kind) is not str
        or type(run_id) is not str
        or _RUN_ID.fullmatch(run_id) is None
        or type(unit) is not str
        or unit != f"agw-managed-{run_id}.service"
    ):
        raise ManagedJobWireError("invalid managed-job fact identity")
    if kind == "launch":
        _validate_launch(value)
        return value
    digest = value.get("receipt_sha256")
    if type(digest) is not str or _HASH.fullmatch(digest) is None:
        raise ManagedJobWireError("invalid launch receipt digest")
    if kind == "wait":
        _object(value, _COMMON | {"exit_code", "signal"})
        exit_code = value["exit_code"]
        signal = value["signal"]
        if (
            (exit_code is None) == (signal is None)
            or (exit_code is not None and not _integer(exit_code, 255))
            or (signal is not None and not _integer(signal, 64, 1))
        ):
            raise ManagedJobWireError("invalid workload terminal status")
    elif kind == "stream-end":
        _object(value, _COMMON | {"stream", "retained_bytes", "retained_sha256", "disposition"})
        length = value["retained_bytes"]
        retained_hash = value["retained_sha256"]
        disposition = value["disposition"]
        if (
            value["stream"] not in ("stdout", "stderr")
            or not _integer(length, 2**63 - 1)
            or type(retained_hash) is not str
            or _HASH.fullmatch(retained_hash) is None
            or disposition not in ("complete-capture", "truncated-capture", "discarded", "sensitivity-suppressed")
            or (length == 0 and retained_hash != _EMPTY_HASH)
            or (disposition in ("discarded", "sensitivity-suppressed") and length != 0)
        ):
            raise ManagedJobWireError("invalid stream end")
    elif kind == "boundary-empty":
        _object(value, _COMMON)
    else:
        raise ManagedJobWireError("invalid managed-job fact kind")
    return value


def encode_fact(value: dict[str, object]) -> bytes:
    """Validate and encode one primitive fact in its sole canonical form."""
    try:
        checked = _validate_fact(value)
        data = json.dumps(checked, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ManagedJobWireError("invalid managed-job fact") from None
    if len(data) > MAX_MANAGED_JOB_FACT_BYTES:
        raise ManagedJobWireError("managed-job fact exceeds byte bound")
    return data


def decode_fact(data: bytes) -> dict[str, object]:
    """Reject oversize input before parsing and all alternate encodings after it."""
    if type(data) is not bytes or len(data) > MAX_MANAGED_JOB_FACT_BYTES:
        raise ManagedJobWireError("invalid managed-job fact bytes")
    try:
        value = json.loads(data.decode("ascii"))
        if encode_fact(value) != data:
            raise ManagedJobWireError("noncanonical managed-job fact")
        return cast("dict[str, object]", value)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ManagedJobWireError("invalid managed-job fact") from None


def launch_sha256(value: dict[str, object]) -> str:
    """Digest a validated canonical primitive launch fact."""
    if type(value) is not dict or value.get("kind") != "launch":
        raise ManagedJobWireError("expected launch fact")
    return hashlib.sha256(encode_fact(value)).hexdigest()
