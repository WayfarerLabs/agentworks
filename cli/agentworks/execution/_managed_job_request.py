"""Portable, closed v1 request assets for an independent managed job.

This file and its two stdlib-only wire dependencies are bundled as exact source
and run on Python 3.11 without an installed agentworks package.
Limits bound memory before parsing or dispatch: control 32 KiB, environment
64 KiB, source and stdin 16 MiB each. Capture uses the v1 16 MiB ceiling.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import cast

from . import _managed_job_wire as wire

VERSION = 1
MAX_LAUNCH_BYTES = wire.MAX_MANAGED_JOB_FACT_BYTES
MAX_CONTROL_BYTES = 32768
MAX_ENVIRONMENT_BYTES = 65536
MAX_SOURCE_BYTES = 16777216
MAX_STDIN_BYTES = 16777216
MAX_CAPTURE_PREFIX_BYTES = wire.MAX_CAPTURE_PREFIX_BYTES_V1
MAX_TEXT_BYTES = 4096
MAX_ARGV = 256
MAX_ENVIRONMENT_ENTRIES = 256

_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_RUN = re.compile(r"[0-9a-f]{32}\Z")
_RESERVED_ENV = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD"})
_ASSETS = ("request-launch", "request-control", "request-environment", "request-source", "request-stdin")


class RequestError(ValueError):
    """A request is unsupported, malformed, or incomplete."""


@dataclass(frozen=True, slots=True, repr=False)
class ManagedJobRequest:
    launch: bytes
    kind: str
    argv: tuple[str, ...]
    cwd: str | None
    output_mode: str
    capture_prefix_bytes: int | None
    environment: tuple[tuple[str, str], ...]
    source: bytes
    stdin: bytes


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def _decode(data: bytes, maximum: int) -> dict[str, object]:
    if type(data) is not bytes or len(data) > maximum:
        raise RequestError("invalid request asset size or type")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or _json(value) != data:
            raise RequestError("noncanonical request asset")
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise RequestError("invalid request asset") from None
    return cast("dict[str, object]", value)


def _text(value: object, *, nonempty: bool = False) -> str:
    if type(value) is not str or "\0" in value or (nonempty and not value):
        raise RequestError("invalid request text")
    try:
        if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise RequestError("request text exceeds bound")
    except UnicodeError:
        raise RequestError("invalid request text") from None
    return value


def _path(value: object) -> str:
    path = _text(value, nonempty=True)
    if not path.startswith("/") or (path != "/" and any(part in ("", ".", "..") for part in path[1:].split("/"))):
        raise RequestError("noncanonical working directory")
    return path


def decode_request_launch(data: bytes) -> dict[str, object]:
    """Validate the complete canonical launch fact and independent owner."""
    try:
        value = wire.decode_fact(data)
    except wire.ManagedJobWireError:
        raise RequestError("invalid launch") from None
    if value["kind"] != "launch" or value["lifetime"] != "independent":
        raise RequestError("unsupported launch")
    owner = cast("dict[str, object]", value["owner"])
    shell = cast("dict[str, object]", value["shell"])
    if owner["kind"] != "resource" or shell["interactive"]:
        raise RequestError("unsupported launch ownership or shell")
    return value


def decode_control(data: bytes) -> dict[str, object]:
    """Validate a canonical control leaf before full asset binding."""
    value = _decode(data, MAX_CONTROL_BYTES)
    run_id = value.get("run_id")
    if (
        set(value) != {"version", "run_id", "kind", "argv", "cwd", "output", "environment", "source", "stdin"}
        or type(value["version"]) is not int
        or value["version"] != VERSION
        or type(run_id) is not str
        or _RUN.fullmatch(run_id) is None
    ):
        raise RequestError("invalid control identity or fields")
    return value


def encode_environment(entries: tuple[tuple[str, str], ...]) -> bytes:
    """Encode bounded environment values separately from control and launcher."""
    if type(entries) is not tuple or len(entries) > MAX_ENVIRONMENT_ENTRIES:
        raise RequestError("invalid environment")
    values: dict[str, str] = {}
    for entry in entries:
        if type(entry) is not tuple or len(entry) != 2:
            raise RequestError("invalid environment entry")
        name, value = entry
        if (
            type(name) is not str
            or _NAME.fullmatch(name) is None
            or name in _RESERVED_ENV
            or name.startswith("_agw_")
            or name in values
        ):
            raise RequestError("invalid environment name")
        values[name] = _text(value)
    data = _json({"version": VERSION, "values": values})
    if len(data) > MAX_ENVIRONMENT_BYTES:
        raise RequestError("environment exceeds bound")
    return data


def decode_environment(data: bytes) -> tuple[tuple[str, str], ...]:
    value = _decode(data, MAX_ENVIRONMENT_BYTES)
    if set(value) != {"version", "values"} or type(value["version"]) is not int or value["version"] != VERSION:
        raise RequestError("invalid environment version")
    entries = value["values"]
    if type(entries) is not dict:
        raise RequestError("invalid environment values")
    result = tuple(entries.items())
    if encode_environment(result) != data:
        raise RequestError("noncanonical environment")
    return result


def _metadata(data: bytes) -> dict[str, object]:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def encode_request(request: ManagedJobRequest) -> dict[str, bytes]:
    """Validate a caller request and return only the five fixed asset leaves."""
    if type(request) is not ManagedJobRequest:
        raise RequestError("invalid request")
    launch = decode_request_launch(request.launch)
    shell = cast("dict[str, object]", launch["shell"])
    if request.kind not in ("command", "script") or type(request.kind) is not str:
        raise RequestError("invalid invocation kind")
    if type(request.argv) is not tuple or len(request.argv) > MAX_ARGV:
        raise RequestError("invalid argv")
    argv = tuple(_text(arg, nonempty=index == 0) for index, arg in enumerate(request.argv))
    if request.kind == "command":
        if (
            not argv
            or request.source != b""
            or shell != {"requested": None, "resolved_executable": None, "login": False, "interactive": False}
        ):
            raise RequestError("command and shell mismatch")
    elif (
        argv
        or shell["requested"] not in ("sh", "bash", "user_default")
        or not isinstance(shell["resolved_executable"], str)
    ):
        raise RequestError("script and shell mismatch")
    else:
        _path(shell["resolved_executable"])
    cwd = None if request.cwd is None else _path(request.cwd)
    if type(request.output_mode) is not str or request.output_mode not in (
        "capture",
        "discard",
        "sensitivity-suppressed",
    ):
        raise RequestError("invalid output mode")
    limit = request.capture_prefix_bytes
    if request.output_mode == "capture":
        if type(limit) is not int or not 0 <= limit <= MAX_CAPTURE_PREFIX_BYTES:
            raise RequestError("invalid capture ceiling")
    elif limit is not None:
        raise RequestError("unexpected capture ceiling")
    environment = encode_environment(request.environment)
    for data, maximum in ((request.source, MAX_SOURCE_BYTES), (request.stdin, MAX_STDIN_BYTES)):
        if type(data) is not bytes or len(data) > maximum:
            raise RequestError("raw request asset exceeds bound")
    if request.kind == "script":
        try:
            source_text = request.source.decode("utf-8")
        except UnicodeError:
            raise RequestError("invalid script source") from None
        if "\0" in source_text:
            raise RequestError("invalid script source")
    control = _json(
        {
            "version": VERSION,
            "run_id": launch["run_id"],
            "kind": request.kind,
            "argv": argv,
            "cwd": cwd,
            "output": {"mode": request.output_mode, "prefix_bytes": limit},
            "environment": _metadata(environment),
            "source": _metadata(request.source),
            "stdin": _metadata(request.stdin),
        }
    )
    if len(control) > MAX_CONTROL_BYTES:
        raise RequestError("control exceeds bound")
    return dict(zip(_ASSETS, (request.launch, control, environment, request.source, request.stdin), strict=True))


def decode_request(assets: dict[str, bytes]) -> ManagedJobRequest:
    """Require a complete fixed set and verify every binding before use."""
    if type(assets) is not dict or set(assets) != set(_ASSETS):
        raise RequestError("incomplete request assets")
    launch = decode_request_launch(assets["request-launch"])
    control = decode_control(assets["request-control"])
    if control["run_id"] != launch["run_id"]:
        raise RequestError("request identity mismatch")
    output = control["output"]
    if type(output) is not dict or set(output) != {"mode", "prefix_bytes"}:
        raise RequestError("invalid output shape")
    argv = control["argv"]
    if type(argv) is not list:
        raise RequestError("invalid argv")
    request = ManagedJobRequest(
        assets["request-launch"],
        cast("str", control["kind"]),
        tuple(argv),
        cast("str | None", control["cwd"]),
        cast("str", output["mode"]),
        cast("int | None", output["prefix_bytes"]),
        decode_environment(assets["request-environment"]),
        assets["request-source"],
        assets["request-stdin"],
    )
    if encode_request(request) != assets:
        raise RequestError("noncanonical request")
    return request
