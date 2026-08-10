"""Shared, presentation-free JSON v1 primitives for CLI commands.

This module deliberately owns only the contract shared by the operational
commands that support machine output.  Command services remain responsible for
building their own safe fact records and for selecting collection order.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.origin import Origin
    from agentworks.resources.kind import InstanceRef
    from agentworks.resources.reference import ReferenceEntry


type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class OutputFormat(StrEnum):
    """The complete set of local output formats supported by JSON v1."""

    HUMAN = "human"
    JSON = "json"


class MachineOutputCommand(StrEnum):
    """The closed set of command identifiers in the JSON v1 contract."""

    RESOURCE_LIST = "resource.list"
    RESOURCE_KINDS = "resource.kinds"
    RESOURCE_DESCRIBE = "resource.describe"
    VM_LIST = "vm.list"
    VM_DESCRIBE = "vm.describe"
    WORKSPACE_LIST = "workspace.list"
    WORKSPACE_DESCRIBE = "workspace.describe"
    AGENT_LIST = "agent.list"
    AGENT_DESCRIBE = "agent.describe"
    SESSION_LIST = "session.list"
    SESSION_DESCRIBE = "session.describe"
    CONSOLE_LIST = "console.list"
    CONSOLE_DESCRIBE = "console.describe"
    SECRET_LIST = "secret.list"
    SECRET_DESCRIBE = "secret.describe"
    DOCTOR = "doctor"


def encode_json_envelope(command: MachineOutputCommand, data: JsonObject) -> bytes:
    """Return the single UTF-8 JSON v1 document for an already-safe fact object.

    Encoding completes before any caller-owned stream is written, so invalid or
    unserializable facts cannot leave a partial JSON document on stdout.
    """
    if not isinstance(command, MachineOutputCommand):
        raise TypeError("command must be a MachineOutputCommand")
    if not isinstance(data, dict):
        raise TypeError("data must be an object")

    envelope: JsonObject = {
        "schema_version": 1,
        "command": command.value,
        "data": data,
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return _escape_terminal_control_characters(encoded).encode("utf-8") + b"\n"


def _escape_terminal_control_characters(document: str) -> str:
    """Escape DEL and C1 controls that terminals could interpret specially."""
    return "".join(
        f"\\u{ord(character):04x}" if 0x7F <= ord(character) <= 0x9F else character for character in document
    )


def write_json_envelope(command: MachineOutputCommand, data: JsonObject, stream: BinaryIO) -> None:
    """Write one JSON v1 document directly to a byte stream.

    Callers pass stdout's binary buffer, never the presentation output handler,
    so terminal formatting cannot enter a machine-readable response.
    """
    document = encode_json_envelope(command, data)
    offset = 0
    while offset < len(document):
        written = stream.write(document[offset:])
        if written is None or written <= 0:
            raise OSError("JSON output stream made no progress")
        offset += written


def project_origin(origin: Origin | None) -> JsonObject | None:
    """Project safe, stable provenance fields without a display rendering."""
    if origin is None:
        return None

    if origin.variant == "operator-declared":
        if not isinstance(origin.file, Path) or type(origin.line) is not int:
            raise AssertionError("operator-declared origins require a file and line")
        return {
            "variant": origin.variant,
            "file": str(origin.file),
            "line": origin.line,
            "source": None,
            "source_resource": None,
            "plugin": None,
        }
    if origin.variant == "auto-declared":
        if not (
            isinstance(origin.source, tuple)
            and len(origin.source) == 2
            and all(isinstance(part, str) for part in origin.source)
        ):
            raise AssertionError("auto-declared origins require a two-string source resource")
        return {
            "variant": origin.variant,
            "file": None,
            "line": None,
            "source": None,
            "source_resource": {"kind": origin.source[0], "name": origin.source[1]},
            "plugin": None,
        }
    if origin.variant == "built-in":
        if not isinstance(origin.source, str):
            raise AssertionError("built-in origins require a code source")
        return {
            "variant": origin.variant,
            "file": None,
            "line": None,
            "source": origin.source,
            "source_resource": None,
            "plugin": None,
        }
    if origin.variant == "system-plugin":
        if not isinstance(origin.plugin, str) or not isinstance(origin.source, str):
            raise AssertionError("system-plugin origins require a plugin and code source")
        return {
            "variant": origin.variant,
            "file": None,
            "line": None,
            "source": origin.source,
            "source_resource": None,
            "plugin": origin.plugin,
        }
    raise AssertionError(f"unhandled Origin variant: {origin.variant!r}")


def project_reference(reference: ReferenceEntry) -> JsonObject:
    """Project one inbound graph entry without display-derived declarer data."""
    if not (
        isinstance(reference.source, tuple)
        and len(reference.source) == 2
        and all(isinstance(part, str) for part in reference.source)
    ):
        raise AssertionError("reference sources require a two-string identity")
    if not isinstance(reference.usage, str):
        raise AssertionError("reference usage must be a string")

    declared_by_kind: str | None = None
    declared_by_name: str | None = None
    if reference.declared_by is not None:
        if not (
            isinstance(reference.declared_by, tuple)
            and len(reference.declared_by) == 2
            and all(isinstance(part, str) for part in reference.declared_by)
        ):
            raise AssertionError("reference declarers require a two-string identity")
        declared_by_kind, declared_by_name = reference.declared_by

    return {
        "source_kind": reference.source[0],
        "source_name": reference.source[1],
        "usage": reference.usage,
        "declared_by_kind": declared_by_kind,
        "declared_by_name": declared_by_name,
    }


def project_references(references: Sequence[ReferenceEntry]) -> list[JsonObject]:
    """Project graph entries in their supplied order, retaining duplicates."""
    return [project_reference(reference) for reference in references]


def project_instance_reference(reference: InstanceRef) -> JsonObject:
    """Project one current instance reference without grouping it for display."""
    if not isinstance(reference.instance_kind, str) or not isinstance(reference.instance_name, str):
        raise AssertionError("instance references require string kind and name")
    return {"kind": reference.instance_kind, "name": reference.instance_name}


def project_instance_references(references: Sequence[InstanceRef]) -> list[JsonObject]:
    """Project instance references in their supplied order, retaining duplicates."""
    return [project_instance_reference(reference) for reference in references]
