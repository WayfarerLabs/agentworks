"""Versioned, bounded JSON encoding for artifact inputs in instance state."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from agentworks.artifacts.capture import content_from_members, normalize_text, validate_provenance
from agentworks.artifacts.model import (
    ArtifactComponent,
    ArtifactContent,
    ArtifactInput,
    ArtifactMember,
    ArtifactOrigin,
    ArtifactProvenance,
    ArtifactType,
)
from agentworks.package_sources import MAX_MEMBER_PATH_LENGTH, CaptureLimits, validate_member_set
from agentworks.sources import SourceRefError

_LIMITS = CaptureLimits()
_MAX_ENCODED_MEMBER = ((_LIMITS.member_bytes + 2) // 3) * 4
SmallString = Annotated[StrictStr, Field(max_length=4096)]
MetadataString = Annotated[StrictStr, Field(max_length=65536)]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Member(_Record):
    path: Annotated[StrictStr, Field(max_length=MAX_MEMBER_PATH_LENGTH)]
    data: Annotated[StrictStr, Field(max_length=_MAX_ENCODED_MEMBER)]
    executable: StrictBool
    text: StrictBool


class _Content(_Record):
    type: Literal["hint", "rule", "skill", "agent"]
    name: SmallString
    description: SmallString
    text: Annotated[StrictStr, Field(max_length=_LIMITS.member_bytes)]
    members: Annotated[list[_Member], Field(max_length=_LIMITS.members)]
    metadata_json: MetadataString
    native_options_json: MetadataString
    digest: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]


class _Origin(_Record):
    component: ArtifactComponent
    resource_kind: SmallString
    resource_name: SmallString
    producer: SmallString
    bundle: SmallString
    entry: SmallString


class _Provenance(_Record):
    source: SmallString
    requested_ref: SmallString
    selected_path: SmallString
    commit: Annotated[StrictStr, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})?$")]


class _Input(_Record):
    content: _Content
    origin: _Origin
    provenance: _Provenance
    identity: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]


class _Envelope(_Record):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    inputs: Annotated[list[_Input], Field(max_length=_LIMITS.members)]


def encode_inputs(inputs: tuple[ArtifactInput, ...]) -> dict[str, object]:
    """Validate the persisted write boundary against the same schema used on read.

    No native state or source access is involved. Acquisition also calls this before
    handing a buffered capture to lifecycle operations that can have native effects.
    """
    from dataclasses import asdict

    result: list[dict[str, object]] = []
    for item in inputs:
        content = item.content
        result.append(
            {
                "content": {
                    "type": content.type.value,
                    "name": content.name,
                    "description": content.description,
                    "text": content.text,
                    "metadata_json": content.metadata_json,
                    "native_options_json": content.native_options_json,
                    "digest": content.digest,
                    "members": [
                        {
                            "path": member.path,
                            "data": base64.b64encode(member.data).decode("ascii"),
                            "executable": member.executable,
                            "text": member.text,
                        }
                        for member in content.members
                    ],
                },
                "origin": asdict(item.origin),
                "provenance": asdict(item.provenance),
                "identity": item.identity,
            }
        )
    payload: dict[str, object] = {"version": 1, "inputs": result}
    decode_inputs(payload)
    return payload


def decode_inputs(payload: object) -> tuple[ArtifactInput, ...]:
    """Validate persisted JSON across executions, including corrupt or old records.

    Errors intentionally omit Pydantic details because rejected inputs may contain
    source credentials or artifact bodies. The caller supplies owning-state context.
    """
    try:
        _bound_payload(payload)
        envelope = _Envelope.model_validate(payload)
        result: list[ArtifactInput] = []
        total_bytes = 0
        total_members = 0
        for record in envelope.inputs:
            members: list[ArtifactMember] = []
            for row in record.content.members:
                if total_bytes + len(row.data) * 3 // 4 > _LIMITS.total_bytes + 2:
                    raise SourceRefError("persisted artifacts exceed their total content limit")
                data = base64.b64decode(row.data, validate=True)
                if len(data) > _LIMITS.member_bytes:
                    raise SourceRefError("persisted artifact exceeds its member size limit")
                if row.text and normalize_text(data).encode() != data:
                    raise SourceRefError("persisted artifact text is not normalized")
                members.append(ArtifactMember(row.path, data, row.executable, row.text))
                total_bytes += len(data)
                total_members += 1
            validate_member_set([member.path for member in members], depth=_LIMITS.depth)
            content_row = record.content
            if len(content_row.text.encode()) > _LIMITS.member_bytes:
                raise SourceRefError("persisted artifact instructions exceed their size limit")
            if normalize_text(content_row.text.encode()) != content_row.text:
                raise SourceRefError("persisted artifact text is not normalized")
            if not members:
                total_bytes += len(content_row.text.encode())
                total_members += 1
            if total_bytes > _LIMITS.total_bytes or total_members > _LIMITS.members:
                raise SourceRefError("persisted artifacts exceed their total content limit")
            for value in (content_row.metadata_json, content_row.native_options_json):
                _validate_metadata(value)
            content = ArtifactContent(
                ArtifactType(content_row.type),
                content_row.name,
                content_row.description,
                content_row.text,
                tuple(members),
                content_row.metadata_json,
                content_row.native_options_json,
            )
            origin = ArtifactOrigin(**record.origin.model_dump())
            provenance = ArtifactProvenance(**record.provenance.model_dump())
            validate_provenance(provenance)
            if content.members:
                expected = content_from_members(
                    content.type, origin.entry, content.members, provenance.selected_path or provenance.source
                )
            elif content.type in (ArtifactType.HINT, ArtifactType.RULE) and content.text.strip():
                expected = ArtifactContent(content.type, origin.entry, text=content.text)
            else:
                raise SourceRefError("persisted artifact is missing its entrypoint")
            if expected != content:
                raise SourceRefError("persisted artifact metadata does not match its entrypoint")
            if bool(content.members) != (provenance.source != "inline"):
                raise SourceRefError("persisted artifact source does not match its content")
            item = ArtifactInput(content, provenance, origin)
            if item.identity != record.identity or content.digest != content_row.digest:
                raise SourceRefError("persisted artifact identity does not match its content")
            if any(previous.identity == item.identity for previous in result):
                raise SourceRefError("persisted artifacts contain duplicate input identities")
            result.append(item)
        return tuple(result)
    except (ValidationError, ValueError, TypeError, binascii.Error, UnicodeError, RecursionError):
        raise SourceRefError("invalid or unsupported persisted artifact capture") from None


def _validate_metadata(text: str) -> None:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise SourceRefError("persisted artifact metadata must be an object")
    if json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) != text:
        raise SourceRefError("persisted artifact metadata is not canonical JSON")
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 32:
            raise SourceRefError("persisted artifact metadata exceeds its depth limit")
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def _bound_payload(payload: object) -> None:
    pending: list[tuple[object, int]] = [(payload, 0)]
    size = 0
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > _LIMITS.members * 64:
            raise SourceRefError("persisted artifact capture exceeds its structural limit")
        if isinstance(value, str):
            size += len(value)
        elif isinstance(value, dict):
            if len(value) > _LIMITS.members:
                raise SourceRefError("persisted artifact object exceeds its size limit")
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            if len(value) > _LIMITS.members:
                raise SourceRefError("persisted artifact list exceeds its size limit")
            pending.extend((child, depth + 1) for child in value)
        # JSON carries base64 members plus their extracted logical instruction text.
        if size > _LIMITS.total_bytes * 3:
            raise SourceRefError("persisted artifact capture exceeds its encoded size limit")
