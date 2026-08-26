"""Strict instance-overlay input, typed codecs, and value-safe disposition output."""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from agentworks.db.instance_state import InstanceKind, JsonObject, VersionedPayload
from agentworks.errors import StateError, ValidationError
from agentworks.instance_overlay_codec import OVERLAY_EXCLUDED_FIELDS, UnsupportedOverlayFieldsError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pydantic import BaseModel

    from agentworks.agents.template import AgentTemplate
    from agentworks.db import Database, DesiredOverlayRecord
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.sessions.template import SessionTemplate
    from agentworks.vms.template import VMTemplate
    from agentworks.workspaces.template import WorkspaceTemplate

_PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class InstanceOverlay[T: BaseModel]:
    """One domain-validated partial declaration and its canonical payload."""

    instance_kind: InstanceKind
    declaration: T
    payload: VersionedPayload

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(sorted(self.payload.value))


class OverlayDisposition(StrEnum):
    SET = "set"
    RETAINED = "retained"
    REPLACED = "replaced"
    CLEARED = "cleared"
    EXPLICITLY_ABSENT = "explicitly absent"


@dataclass(frozen=True)
class OverlayOutcome:
    """The final retained declaration state to report after lifecycle handling."""

    disposition: OverlayDisposition
    fields: tuple[str, ...] = ()


class UnsupportedStoredOverlayError(StateError):
    """Stored desired state was written for a declaration schema this release cannot read."""


def parse_instance_spec(instance_kind: InstanceKind, value: str) -> InstanceOverlay[BaseModel]:
    """Parse strict inline JSON and validate it through the kind's spec model."""
    raw = _parse_json_object(value)
    forbidden = sorted(OVERLAY_EXCLUDED_FIELDS.intersection(raw))
    if forbidden:
        names = ", ".join(forbidden)
        raise ValidationError(
            f"{instance_kind} instance spec cannot declare framework fields: {names}",
            entity_kind=instance_kind,
        )
    codec = _codec(instance_kind)
    declaration = codec.decode(raw)
    canonical = codec.encode(declaration)
    return InstanceOverlay(instance_kind, declaration, VersionedPayload(_PAYLOAD_VERSION, canonical))


def decode_stored_overlay(record: DesiredOverlayRecord) -> InstanceOverlay[BaseModel]:
    """Decode a persisted overlay at the state-database trust boundary."""
    if record.payload.payload_version != _PAYLOAD_VERSION:
        raise UnsupportedStoredOverlayError(
            f"stored {record.instance_kind} {record.instance_name!r} instance spec uses "
            f"unsupported payload version {record.payload.payload_version}",
            entity_kind=record.instance_kind,
            entity_name=record.instance_name,
            hint="Upgrade Agentworks to a compatible or newer release before applying this instance spec.",
        )
    try:
        encoded = json.dumps(
            record.payload.value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return parse_instance_spec(record.instance_kind, encoded)
    except UnsupportedOverlayFieldsError as error:
        raise UnsupportedStoredOverlayError(
            f"stored {record.instance_kind} {record.instance_name!r} instance spec uses unsupported fields: {error}",
            entity_kind=record.instance_kind,
            entity_name=record.instance_name,
            hint="Upgrade Agentworks to a compatible or newer release before applying this instance spec.",
        ) from error
    except ValidationError as error:
        raise StateError(
            f"stored {record.instance_kind} {record.instance_name!r} instance spec is invalid: {error}",
            entity_kind=record.instance_kind,
            entity_name=record.instance_name,
            hint="Back up the state database before repairing it, or restore a known-good backup.",
        ) from error


def get_instance_overlay(
    db: Database,
    instance_kind: InstanceKind,
    instance_name: str,
) -> InstanceOverlay[BaseModel] | None:
    record = db.instance_state.get_desired_overlay(instance_kind, instance_name)
    return None if record is None else decode_stored_overlay(record)


def persist_creation_overlay(
    db: Database,
    instance_kind: InstanceKind,
    instance_name: str,
    overlay: InstanceOverlay[BaseModel] | None,
) -> OverlayOutcome | None:
    """Persist a nonempty creation layer inside the caller's owner transaction."""
    if overlay is None or not overlay.payload.value:
        return None
    db.instance_state.put_desired_overlay(instance_kind, instance_name, overlay.payload)
    return OverlayOutcome(OverlayDisposition.SET, overlay.fields)


def refuse_orphan_creation_state(
    db: Database,
    instance_kind: InstanceKind,
    instance_name: str,
) -> None:
    """Refuse silently adopting desired state whose owner no longer exists."""
    if _instance_owner(db, instance_kind, instance_name) is not None:
        return
    if db.instance_state.has_instance_records(instance_kind, instance_name):
        raise StateError(
            f"cannot create {instance_kind} {instance_name!r}: orphan instance-state records already exist",
            entity_kind=instance_kind,
            entity_name=instance_name,
            hint="Back up the state database before repairing or removing the orphan record.",
        )


def replace_agent_overlay(
    db: Database,
    instance_name: str,
    overlay: InstanceOverlay[BaseModel] | None,
    *,
    supplied: bool,
) -> OverlayOutcome | None:
    """Apply agent-reinit retain, replace, and clear semantics in an outer transaction."""
    if supplied and overlay is not None and not overlay.payload.value:
        prior_record = db.instance_state.get_desired_overlay("agent", instance_name)
        if prior_record is None:
            return OverlayOutcome(OverlayDisposition.EXPLICITLY_ABSENT)
        try:
            prior_fields = decode_stored_overlay(prior_record).fields
        except UnsupportedStoredOverlayError:
            prior_fields = ()
        db.instance_state.clear_desired_overlay("agent", instance_name)
        return OverlayOutcome(OverlayDisposition.CLEARED, prior_fields)

    prior = get_instance_overlay(db, "agent", instance_name)
    if not supplied:
        if prior is None:
            return None
        return OverlayOutcome(OverlayDisposition.RETAINED, prior.fields)
    if overlay is None:
        raise TypeError("a supplied instance spec must be parsed")
    db.instance_state.put_desired_overlay("agent", instance_name, overlay.payload)
    disposition = OverlayDisposition.SET if prior is None else OverlayDisposition.REPLACED
    return OverlayOutcome(disposition, overlay.fields)


def render_overlay_outcome(outcome: OverlayOutcome | None) -> None:
    """Render field names only, never values that may contain plaintext environment data."""
    if outcome is None:
        return
    from agentworks import output

    fields = f" (fields: {', '.join(outcome.fields)})" if outcome.fields else ""
    output.info(f"Instance spec: {outcome.disposition.value}{fields}")


def render_retained_creation_overlay(
    db: Database,
    instance_kind: InstanceKind,
    instance_name: str,
) -> None:
    """Report a created layer only when its owner and desired row remain."""
    if _instance_owner(db, instance_kind, instance_name) is None:
        return
    overlay = get_instance_overlay(db, instance_kind, instance_name)
    if overlay is not None:
        render_overlay_outcome(OverlayOutcome(OverlayDisposition.SET, overlay.fields))


def _instance_owner(db: Database, instance_kind: InstanceKind, instance_name: str) -> object | None:
    return {
        "vm": db.get_vm,
        "workspace": db.get_workspace,
        "agent": db.get_agent,
        "session": db.get_session,
    }[instance_kind](instance_name)


@contextmanager
def report_overlay_outcome(outcome: OverlayOutcome | None) -> Iterator[None]:
    """Render retained desired state after the guarded lifecycle settles."""
    try:
        yield
    finally:
        render_overlay_outcome(outcome)


def validate_effective_instance_references(
    registry: Registry,
    references: tuple[ResourceReference, ...],
) -> None:
    """Validate and use-gate references added by an effective instance layer."""
    from agentworks.resources.access import ensure_recipe_enabled
    from agentworks.resources.kind import KIND_REGISTRY
    from agentworks.resources.reference import validate_reference_targets

    validate_reference_targets(registry, references)
    for reference in references:
        handler = KIND_REGISTRY[reference.kind]
        if handler.category == "declarable":
            ensure_recipe_enabled(registry, reference.kind, reference.name)


def _parse_json_object(value: str) -> JsonObject:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("instance spec must be a nonempty inline JSON object")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValidationError(f"instance spec contains duplicate object member {key!r}")
            result[key] = item
        return result

    def reject_constant(constant: str) -> None:
        raise ValidationError(f"instance spec contains non-finite number {constant}")

    decoder = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=reject_constant)
    buffer = value.strip()
    try:
        decoded, end = decoder.raw_decode(buffer)
    except ValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as error:
        raise ValidationError(f"instance spec is not valid JSON: {error}") from None
    except ValueError:
        raise ValidationError("instance spec contains an unsupported JSON number") from None
    if buffer[end:].strip():
        raise ValidationError("instance spec contains trailing data")
    if not isinstance(decoded, dict):
        raise ValidationError("instance spec must be a JSON object")
    _validate_json_tree(decoded)
    return cast("JsonObject", decoded)


def _validate_json_tree(value: object) -> None:
    pending: list[tuple[object, tuple[str, ...]]] = [(value, ())]
    while pending:
        item, path = pending.pop()
        if item is None:
            location = ".".join(path) or "<root>"
            raise ValidationError(f"instance spec cannot contain null at {location}")
        if isinstance(item, float) and not math.isfinite(item):
            location = ".".join(path) or "<root>"
            raise ValidationError(f"instance spec contains a non-finite number at {location}")
        if isinstance(item, list):
            pending.extend((child, (*path, str(index))) for index, child in reversed(list(enumerate(item))))
        elif isinstance(item, dict):
            pending.extend((child, (*path, key)) for key, child in reversed(list(item.items())))


@dataclass(frozen=True)
class _OverlayCodec:
    decode: Callable[[JsonObject], BaseModel]
    encode: Callable[[BaseModel], JsonObject]


def _codec(instance_kind: InstanceKind) -> _OverlayCodec:
    if instance_kind == "vm":
        from agentworks.vms import instance_overlay as vm_codec

        return _OverlayCodec(
            vm_codec.decode_overlay,
            lambda model: vm_codec.encode_overlay(cast("VMTemplate", model)),
        )
    if instance_kind == "workspace":
        from agentworks.workspaces import instance_overlay as workspace_codec

        return _OverlayCodec(
            workspace_codec.decode_overlay,
            lambda model: workspace_codec.encode_overlay(cast("WorkspaceTemplate", model)),
        )
    if instance_kind == "agent":
        from agentworks.agents import instance_overlay as agent_codec

        return _OverlayCodec(
            agent_codec.decode_overlay,
            lambda model: agent_codec.encode_overlay(cast("AgentTemplate", model)),
        )
    from agentworks.sessions import instance_overlay as session_codec

    return _OverlayCodec(
        session_codec.decode_overlay,
        lambda model: session_codec.encode_overlay(cast("SessionTemplate", model)),
    )
