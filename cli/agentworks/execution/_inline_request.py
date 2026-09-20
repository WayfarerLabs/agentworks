"""Closed stdlib-only request schema for the private Linux inline helper."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_CAPTURE_BYTES = 4_096
MAX_MANIFEST_BYTES = 32_768
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_LOWER_HEX = frozenset("0123456789abcdef")
_RESERVED_ENV = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD"})
_FIELDS = frozenset(
    {"argv", "cwd", "env", "identity", "kind", "nonce", "output", "shell", "source", "stdin", "version"}
)


class ManifestErrorCode(StrEnum):
    INVALID = "invalid"
    OVERSIZED = "oversized"


class ManifestError(ValueError):
    """A closed request failure that never retains untrusted manifest values."""

    def __init__(self, code: ManifestErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class InvocationKind(StrEnum):
    COMMAND = "command"
    SCRIPT = "script"


class ScriptShell(StrEnum):
    SH = "sh"
    BASH = "bash"
    USER_DEFAULT = "user_default"


class OutputMode(StrEnum):
    CAPTURE = "capture"
    DISCARD = "discard"
    SUPPRESS = "suppress"


@dataclass(frozen=True, slots=True)
class IdentityExpectation:
    euid: int
    egid: int
    groups: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class InlineManifest:
    nonce: str
    kind: InvocationKind
    argv: tuple[str, ...] = field(repr=False)
    source: bytes | None = field(repr=False)
    shell: ScriptShell | None
    env: tuple[tuple[str, str], ...] = field(repr=False)
    cwd: str | None = field(repr=False)
    stdin: bytes = field(repr=False)
    output_mode: OutputMode
    capture_limit: int
    identity: IdentityExpectation


def _invalid() -> ManifestError:
    return ManifestError(ManifestErrorCode.INVALID)


def _text(value: object, *, nonempty: bool = False) -> str:
    if type(value) is not str or "\0" in value or (nonempty and not value):
        raise _invalid()
    failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise _invalid()
    return value


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode_b64(value: object) -> bytes:
    if type(value) is not str:
        raise _invalid()
    failed = False
    decoded = b""
    encoded = b""
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error):
        failed = True
    if failed:
        raise _invalid()
    if base64.b64encode(decoded) != encoded:
        raise _invalid()
    return decoded


def _decode_text(value: object, *, nonempty: bool = False) -> str:
    failed = False
    decoded = ""
    try:
        decoded = _decode_b64(value).decode("utf-8")
    except UnicodeDecodeError:
        failed = True
    if failed:
        raise _invalid()
    return _text(decoded, nonempty=nonempty)


def _environment(values: object) -> tuple[tuple[str, str], ...]:
    if type(values) is not list:
        raise _invalid()
    environment: list[tuple[str, str]] = []
    seen: set[str] = set()
    previous_name: str | None = None
    for entry in values:
        if type(entry) is not list or len(entry) != 2:
            raise _invalid()
        name, encoded_value = entry
        if (
            type(name) is not str
            or _ENV_NAME.fullmatch(name) is None
            or name in seen
            or name in _RESERVED_ENV
            or name.startswith("_agw_")
            or (previous_name is not None and name < previous_name)
        ):
            raise _invalid()
        seen.add(name)
        previous_name = name
        environment.append((name, _decode_text(encoded_value)))
    return tuple(environment)


def _identity(value: object) -> IdentityExpectation:
    if type(value) is not dict or set(value) != {"egid", "euid", "groups"}:
        raise _invalid()
    euid = value["euid"]
    egid = value["egid"]
    groups = value["groups"]
    if (
        type(euid) is not int
        or euid < 0
        or type(egid) is not int
        or egid < 0
        or type(groups) is not list
        or not groups
        or any(type(group) is not int or group < 0 for group in groups)
        or groups != sorted(set(groups))
        or egid not in groups
    ):
        raise _invalid()
    return IdentityExpectation(euid, egid, tuple(groups))


def _output(value: object) -> tuple[OutputMode, int]:
    if type(value) is not dict or set(value) != {"limit", "mode"}:
        raise _invalid()
    failed = False
    mode = OutputMode.CAPTURE
    try:
        mode = OutputMode(value["mode"])
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise _invalid()
    limit = value["limit"]
    if type(limit) is not int or not 0 <= limit <= MAX_CAPTURE_BYTES:
        raise _invalid()
    if mode is not OutputMode.CAPTURE and limit != 0:
        raise _invalid()
    return mode, limit


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _reject_constant(_constant: str) -> None:
    raise _invalid()


def decode_manifest(data: bytes) -> InlineManifest:
    """Validate one untrusted canonical manifest from the carrier input boundary."""
    if type(data) is not bytes:
        raise _invalid()
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError(ManifestErrorCode.OVERSIZED)
    failed = False
    value: Any = None
    try:
        value = json.loads(
            data.decode("ascii"),
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, ManifestError):
        failed = True
    if failed:
        raise _invalid()
    if type(value) is not dict or set(value) != _FIELDS or _json_bytes(value) != data:
        raise _invalid()
    if type(value["version"]) is not int or value["version"] != 1:
        raise _invalid()
    nonce = value["nonce"]
    if type(nonce) is not str or len(nonce) != 32 or any(character not in _LOWER_HEX for character in nonce):
        raise _invalid()
    failed = False
    kind = InvocationKind.COMMAND
    try:
        kind = InvocationKind(value["kind"])
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise _invalid()
    if type(value["argv"]) is not list:
        raise _invalid()
    argv = tuple(_decode_text(argument, nonempty=index == 0) for index, argument in enumerate(value["argv"]))
    source = None if value["source"] is None else _decode_b64(value["source"])
    if source is not None:
        failed = False
        try:
            _text(source.decode("utf-8"))
        except UnicodeDecodeError:
            failed = True
        if failed:
            raise _invalid()
    failed = False
    shell: ScriptShell | None = None
    try:
        shell = None if value["shell"] is None else ScriptShell(value["shell"])
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise _invalid()
    if kind is InvocationKind.COMMAND:
        if not argv or source is not None or shell is not None:
            raise _invalid()
    elif argv or source is None or shell is None:
        raise _invalid()
    cwd = None if value["cwd"] is None else _decode_text(value["cwd"], nonempty=True)
    if cwd is not None and not cwd.startswith("/"):
        raise _invalid()
    output_mode, capture_limit = _output(value["output"])
    return InlineManifest(
        nonce=nonce,
        kind=kind,
        argv=argv,
        source=source,
        shell=shell,
        env=_environment(value["env"]),
        cwd=cwd,
        stdin=_decode_b64(value["stdin"]),
        output_mode=output_mode,
        capture_limit=capture_limit,
        identity=_identity(value["identity"]),
    )


def encode_manifest(manifest: InlineManifest) -> bytes:
    """Encode trusted host state, then apply the same closed boundary validation."""
    value = {
        "argv": [_b64(argument.encode("utf-8")) for argument in manifest.argv],
        "cwd": None if manifest.cwd is None else _b64(manifest.cwd.encode("utf-8")),
        "env": [[name, _b64(item.encode("utf-8"))] for name, item in manifest.env],
        "identity": {
            "egid": manifest.identity.egid,
            "euid": manifest.identity.euid,
            "groups": list(manifest.identity.groups),
        },
        "kind": manifest.kind.value,
        "nonce": manifest.nonce,
        "output": {"limit": manifest.capture_limit, "mode": manifest.output_mode.value},
        "shell": None if manifest.shell is None else manifest.shell.value,
        "source": None if manifest.source is None else _b64(manifest.source),
        "stdin": _b64(manifest.stdin),
        "version": 1,
    }
    encoded = _json_bytes(value)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ManifestError(ManifestErrorCode.OVERSIZED)
    decode_manifest(encoded)
    return encoded
