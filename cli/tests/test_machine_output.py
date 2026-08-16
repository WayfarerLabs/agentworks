"""Contract tests for the narrow JSON v1 machine-output foundation."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import cast

import pytest

from agentworks.machine_output import (
    JsonValue,
    MachineOutputCommand,
    OutputFormat,
    encode_json_envelope,
    project_instance_references,
    project_origin,
    project_references,
    write_json_envelope,
)
from agentworks.origin import Origin
from agentworks.resources.kind import InstanceRef
from agentworks.resources.reference import ReferenceEntry


class _ShortWriteStream:
    """A raw stream that accepts a few bytes per call, as a non-blocking pipe does."""

    def __init__(self) -> None:
        self.document = bytearray()
        self.write_calls = 0

    def write(self, data: bytes) -> int:
        self.write_calls += 1
        accepted = min(3, len(data))
        self.document.extend(data[:accepted])
        return accepted


class _PartialThenZeroStream:
    """A stream that accepts one byte and then stalls forever."""

    def __init__(self) -> None:
        self.document = bytearray()
        self._wrote_once = False

    def write(self, data: bytes) -> int:
        if self._wrote_once:
            return 0
        self._wrote_once = True
        self.document.extend(data[:1])
        return 1


class _NoneWriteStream:
    """A raw stream reporting the ``EAGAIN`` shape, where ``write`` returns ``None``."""

    def write(self, data: bytes) -> None:
        return None


def test_output_formats_are_closed_to_human_and_json() -> None:
    assert list(OutputFormat) == [OutputFormat.HUMAN, OutputFormat.JSON]
    assert OutputFormat("human") is OutputFormat.HUMAN
    assert OutputFormat("json") is OutputFormat.JSON
    with pytest.raises(ValueError):
        OutputFormat("yaml")


def test_machine_output_commands_are_the_complete_v1_contract() -> None:
    assert [command.value for command in MachineOutputCommand] == [
        "resource.list",
        "resource.kinds",
        "resource.describe",
        "vm.list",
        "vm.describe",
        "workspace.list",
        "workspace.describe",
        "agent.list",
        "agent.describe",
        "session.list",
        "session.describe",
        "console.list",
        "console.describe",
        "secret.list",
        "secret.describe",
        "doctor",
    ]
    with pytest.raises(ValueError):
        MachineOutputCommand("resource.delete")


def test_envelope_is_utf8_deterministic_and_has_one_trailing_newline() -> None:
    data = {"empty": None, "enabled": True, "count": 3, "label": "snowman ☃"}

    first = encode_json_envelope(MachineOutputCommand.RESOURCE_LIST, data)
    second = encode_json_envelope(MachineOutputCommand.RESOURCE_LIST, data)

    assert first == second
    assert first.endswith(b"\n")
    assert not first.endswith(b"\n\n")
    assert b"\xef\xbb\xbf" not in first
    assert "☃".encode() in first
    assert first == (
        b'{"schema_version":1,"command":"resource.list","data":'
        b'{"empty":null,"enabled":true,"count":3,"label":"snowman \xe2\x98\x83"}}\n'
    )
    assert list(json.loads(first)) == ["schema_version", "command", "data"]


def test_envelope_escapes_del_and_c1_controls_without_changing_text() -> None:
    text = "~\x7f\u009b\u009f\u00a0☃"

    encoded = encode_json_envelope(MachineOutputCommand.DOCTOR, {"text": text})

    assert b"\\u007f" in encoded
    assert b"\\u009b" in encoded
    assert b"\\u009f" in encoded
    assert b"\x7f" not in encoded
    assert "\u009b".encode("utf-8") not in encoded
    assert "\u009f".encode("utf-8") not in encoded
    assert b"~" in encoded
    assert "\u00a0".encode("utf-8") in encoded
    assert "☃".encode() in encoded
    assert json.loads(encoded)["data"]["text"] == text


def test_writer_writes_directly_without_ansi_presentation_bytes() -> None:
    stream = io.BytesIO()

    write_json_envelope(
        MachineOutputCommand.DOCTOR,
        {"message": "\x1b[31mnot terminal formatting\x1b[0m"},
        stream,
    )

    assert b"\x1b" not in stream.getvalue()
    assert json.loads(stream.getvalue()) == {
        "schema_version": 1,
        "command": "doctor",
        "data": {"message": "\x1b[31mnot terminal formatting\x1b[0m"},
    }


def test_writer_retries_short_writes_until_the_document_is_complete() -> None:
    stream = _ShortWriteStream()
    expected = encode_json_envelope(MachineOutputCommand.DOCTOR, {"count": 3})

    write_json_envelope(MachineOutputCommand.DOCTOR, {"count": 3}, stream)

    assert bytes(stream.document) == expected
    assert stream.write_calls > 1


@pytest.mark.parametrize("build_stream", [_PartialThenZeroStream, _NoneWriteStream])
def test_writer_fails_when_the_stream_makes_no_progress(build_stream: type[object]) -> None:
    stream = build_stream()

    with pytest.raises(OSError):
        write_json_envelope(MachineOutputCommand.DOCTOR, {"count": 3}, stream)  # type: ignore[arg-type]

    if isinstance(stream, _PartialThenZeroStream):
        assert bytes(stream.document) == encode_json_envelope(MachineOutputCommand.DOCTOR, {"count": 3})[:1]


def test_unserializable_input_writes_no_partial_document() -> None:
    stream = io.BytesIO()
    with pytest.raises(TypeError):
        write_json_envelope(MachineOutputCommand.DOCTOR, {"bad": cast(JsonValue, object())}, stream)
    assert stream.getvalue() == b""

    with pytest.raises(ValueError):
        encode_json_envelope(MachineOutputCommand.DOCTOR, {"not_a_number": float("nan")})


def test_origin_projection_has_fixed_safe_order_and_variant_fields() -> None:
    origins = [
        Origin.operator_declared(file=Path("é/resources.yaml"), line=7),
        Origin.auto_declared(source=("vm-template", "default")),
        Origin.built_in(source="agentworks.manifests.builtin/vm-sites.yaml"),
        Origin.system_plugin(plugin="azure", source="agentworks.plugins.azure"),
    ]

    assert project_origin(None) is None
    projections = [project_origin(origin) for origin in origins]
    assert projections == [
        {
            "variant": "operator-declared",
            "file": "é/resources.yaml",
            "line": 7,
            "source": None,
            "source_resource": None,
            "plugin": None,
        },
        {
            "variant": "auto-declared",
            "file": None,
            "line": None,
            "source": None,
            "source_resource": {"kind": "vm-template", "name": "default"},
            "plugin": None,
        },
        {
            "variant": "built-in",
            "file": None,
            "line": None,
            "source": "agentworks.manifests.builtin/vm-sites.yaml",
            "source_resource": None,
            "plugin": None,
        },
        {
            "variant": "system-plugin",
            "file": None,
            "line": None,
            "source": "agentworks.plugins.azure",
            "source_resource": None,
            "plugin": "azure",
        },
    ]
    assert all(
        list(projection) == ["variant", "file", "line", "source", "source_resource", "plugin"]
        for projection in projections
        if projection is not None
    )


@pytest.mark.parametrize(
    "origin",
    [
        Origin(variant="operator-declared", file=None, line=7),
        Origin(variant="built-in", source=None),
        Origin(variant="auto-declared", source=()),
        Origin(variant="auto-declared", source=("kind", cast(str, 7))),
        Origin(variant="system-plugin", plugin=None, source="agentworks.plugins.example"),
        Origin(variant="system-plugin", plugin="example", source=None),
    ],
)
def test_origin_projection_rejects_variants_built_outside_their_factory(origin: Origin) -> None:
    """One dataclass carries four variants, so only the factories pin the shape."""
    with pytest.raises(AssertionError):
        project_origin(origin)


def test_reference_projections_preserve_graph_order_duplicates_and_nullable_declarers() -> None:
    entries = (
        ReferenceEntry(source=("vm-template", "base"), usage="a secret"),
        ReferenceEntry(
            source=("vm-template", "child"),
            usage="a secret",
            declared_by=("vm-template", "base"),
        ),
        ReferenceEntry(source=("vm-template", "base"), usage="a secret"),
    )

    assert project_references(entries) == [
        {
            "source_kind": "vm-template",
            "source_name": "base",
            "usage": "a secret",
            "declared_by_kind": None,
            "declared_by_name": None,
        },
        {
            "source_kind": "vm-template",
            "source_name": "child",
            "usage": "a secret",
            "declared_by_kind": "vm-template",
            "declared_by_name": "base",
        },
        {
            "source_kind": "vm-template",
            "source_name": "base",
            "usage": "a secret",
            "declared_by_kind": None,
            "declared_by_name": None,
        },
    ]
    assert [list(reference) for reference in project_references(entries)] == [
        ["source_kind", "source_name", "usage", "declared_by_kind", "declared_by_name"],
        ["source_kind", "source_name", "usage", "declared_by_kind", "declared_by_name"],
        ["source_kind", "source_name", "usage", "declared_by_kind", "declared_by_name"],
    ]


def test_instance_reference_projections_preserve_order_and_duplicates() -> None:
    references = (
        InstanceRef(instance_kind="vm", instance_name="alpha"),
        InstanceRef(instance_kind="session", instance_name="work"),
        InstanceRef(instance_kind="vm", instance_name="alpha"),
    )

    assert project_instance_references(references) == [
        {"kind": "vm", "name": "alpha"},
        {"kind": "session", "name": "work"},
        {"kind": "vm", "name": "alpha"},
    ]
    assert [list(reference) for reference in project_instance_references(references)] == [
        ["kind", "name"],
        ["kind", "name"],
        ["kind", "name"],
    ]
