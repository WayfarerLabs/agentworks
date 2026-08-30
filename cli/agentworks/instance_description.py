"""Shared live-instance description facts and presentation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

from agentworks import output
from agentworks.resources.render import sanitize_fact_line

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from agentworks.config import Config
    from agentworks.db import Database, DesiredOverlayRecord, InstanceKind, InstanceStateInspection
    from agentworks.db.instance_state import JsonObject, JsonValue
    from agentworks.resources.access import ResourceIdentity
    from agentworks.resources.inheritance import LayeredResolution
    from agentworks.resources.registry import Registry
    from agentworks.resources.resolved_spec import SpecResolution


type InstanceSpecStatus = Literal["absent", "present", "unavailable"]
type LifecycleEvidenceStatus = Literal["recorded", "not-recorded", "unavailable"]
type InstanceComparisonState = Literal["not-recorded", "unverifiable", "match", "drift"]


@dataclass(frozen=True, slots=True)
class InstanceSpec:
    """One persisted final declaration layer, without framework fields."""

    status: InstanceSpecStatus
    recorded_at: str | None = None
    spec: JsonObject | None = None
    reason: Literal["malformed", "unsupported-version"] | None = None

    def __post_init__(self) -> None:
        if self.status == "absent":
            if self.recorded_at is not None or self.spec is not None or self.reason is not None:
                raise ValueError("an absent instance spec carries no additional facts")
            return
        if self.status == "present":
            if self.recorded_at is None or self.spec is None or self.reason is not None:
                raise ValueError("a present instance spec requires a timestamp and spec")
            return
        if self.recorded_at is not None or self.spec is not None or self.reason is None:
            raise ValueError("an unavailable instance spec carries only a reason")


@dataclass(frozen=True, slots=True)
class DeclarationSlot:
    """Selected base, stored final layer, and current effective declaration."""

    name: str
    selection: ResourceIdentity
    instance_spec: InstanceSpec
    current: SpecResolution


@dataclass(frozen=True, slots=True)
class LifecycleEvidence:
    """One configuration fact evidenced by a successful lifecycle operation."""

    key: str
    status: LifecycleEvidenceStatus
    recorded_at: str | None = None
    operation: str | None = None
    value: JsonObject | None = None

    def __post_init__(self) -> None:
        if self.status == "recorded":
            if self.recorded_at is None or self.operation is None or self.value is None:
                raise ValueError("recorded lifecycle evidence requires operation, timestamp, and value")
            return
        if self.recorded_at is not None or self.operation is not None or self.value is not None:
            raise ValueError("unrecorded or unavailable lifecycle evidence carries no value")


@dataclass(frozen=True, slots=True)
class ComparisonDifference:
    """One scalar field whose current and recorded values differ."""

    field: str
    recorded: JsonValue
    current: JsonValue


@dataclass(frozen=True, slots=True)
class InstanceComparison:
    """A comparison whose outcome is supported by available evidence."""

    key: str
    state: InstanceComparisonState
    differences: tuple[ComparisonDifference, ...] = ()


@dataclass(frozen=True, slots=True)
class UnconsumedRecord:
    """Safe metadata for one future record this release did not consume."""

    record_type: str | None
    record_key: str | None
    payload_version: int | None
    recorded_at: str | None


class InstanceStateIssueCode(StrEnum):
    """Closed issue vocabulary for live instance-state inspection."""

    INSTANCE_SPEC_MALFORMED = "instance-spec-malformed"
    INSTANCE_SPEC_UNSUPPORTED = "instance-spec-unsupported"
    CURRENT_DECLARATION_UNRESOLVED = "current-declaration-unresolved"
    REGISTRY_UNAVAILABLE = "registry-unavailable"
    APPLIED_RECORD_MALFORMED = "applied-record-malformed"
    APPLIED_RECORD_UNSUPPORTED = "applied-record-unsupported"
    LIFECYCLE_EVIDENCE_UNAVAILABLE = "lifecycle-evidence-unavailable"
    CURRENT_IDENTITY_UNAVAILABLE = "current-identity-unavailable"


@dataclass(frozen=True, slots=True)
class InstanceStateIssue:
    """A bounded structural issue that prevented one inspection fact."""

    code: InstanceStateIssueCode
    slot: str | None = None
    record_key: str | None = None


@dataclass(frozen=True, slots=True)
class InstanceStateDescription:
    """Current declarations and independently recorded lifecycle evidence."""

    declarations: tuple[DeclarationSlot, ...]
    lifecycle_evidence: tuple[LifecycleEvidence, ...] = ()
    comparisons: tuple[InstanceComparison, ...] = ()
    unconsumed_records: tuple[UnconsumedRecord, ...] = ()
    issues: tuple[InstanceStateIssue, ...] = ()


def load_instance_description_registry(
    db: Database,
    config: Config,
    instance_kind: InstanceKind,
    instance_name: str,
) -> Registry:
    """Build the live registry, retaining inspection of an unreadable owner spec."""
    from agentworks.bootstrap import load_request_registry
    from agentworks.errors import StateError

    try:
        return load_request_registry(config, live_database=db)
    except StateError as error:
        if error.entity_kind != instance_kind or error.entity_name != instance_name:
            raise
        # This owner's current resolution will be structurally unavailable.
        # A config-only registry still lets its stored and applied siblings be
        # inspected without weakening failures from any other publisher.
        return load_request_registry(config, include_live_resources=False)


def load_tolerant_instance_description_registry(
    db: Database,
    config: Config,
    instance_kind: Literal["workspace", "agent"],
    instance_name: str,
) -> Registry | None:
    """Load workspace or agent resolution input without hiding stored facts."""
    from agentworks.errors import ConfigError, ValidationError

    try:
        return load_instance_description_registry(db, config, instance_kind, instance_name)
    except (ConfigError, ValidationError):
        return None


def inspected_desired_record(inspection: InstanceStateInspection) -> DesiredOverlayRecord | None:
    """Return the one recognized desired record from a singular inspection."""
    if len(inspection.desired_overlays) > 1:
        raise AssertionError("one instance owner cannot have multiple desired records")
    return None if not inspection.desired_overlays else inspection.desired_overlays[0].record


def inspection_metadata_facts(
    inspection: InstanceStateInspection,
) -> tuple[tuple[UnconsumedRecord, ...], tuple[InstanceStateIssue, ...]]:
    """Project value-free future and malformed record metadata."""
    unconsumed: list[UnconsumedRecord] = []
    issues: list[InstanceStateIssue] = []
    for item in inspection.unconsumed_records:
        metadata = item.metadata
        unconsumed.append(
            UnconsumedRecord(
                record_type=metadata.record_type,
                record_key=metadata.record_key,
                payload_version=metadata.payload_version,
                recorded_at=metadata.recorded_at,
            )
        )

    for malformed in inspection.malformed_records:
        metadata = malformed.metadata
        record_key = metadata.record_key
        if metadata.record_type == "desired-overlay":
            code = InstanceStateIssueCode.INSTANCE_SPEC_MALFORMED
        else:
            code = InstanceStateIssueCode.APPLIED_RECORD_MALFORMED
        issues.append(InstanceStateIssue(code, record_key=record_key))
    return tuple(unconsumed), tuple(issues)


def malformed_desired_record_present(inspection: InstanceStateInspection) -> bool:
    """Whether the desired record exists but failed common-envelope decoding."""
    return not inspection.desired_overlays and any(
        item.metadata.record_type == "desired-overlay" for item in inspection.malformed_records
    )


def single_declaration_instance_state[T: BaseModel, R](
    *,
    instance_kind: Literal["workspace", "agent", "session"],
    selection: ResourceIdentity,
    inspection: InstanceStateInspection,
    resolve: Callable[[T | None], LayeredResolution[R]] | None,
) -> InstanceStateDescription:
    """Collect one non-VM declaration slot from a singular DB inspection."""
    from agentworks.errors import NotFoundError, StateError
    from agentworks.instance_specs import (
        InstanceOverlay,
        UnsupportedStoredOverlayError,
        decode_stored_overlay,
    )
    from agentworks.resources.resolved_spec import UnresolvedSpec, project_resolved_spec

    unconsumed, metadata_issues = inspection_metadata_facts(inspection)
    issues = list(metadata_issues)
    record = inspected_desired_record(inspection)
    declaration: T | None = None
    unavailable = malformed_desired_record_present(inspection)
    if unavailable:
        instance_spec = InstanceSpec("unavailable", reason="malformed")
    elif record is None:
        instance_spec = InstanceSpec("absent")
    else:
        try:
            decoded = decode_stored_overlay(record)
            if not isinstance(decoded, InstanceOverlay) or decoded.instance_kind != instance_kind:
                raise AssertionError("a non-VM desired record must decode to one matching overlay")
            declaration = cast("T", decoded.declaration)
            instance_spec = InstanceSpec(
                "present",
                recorded_at=record.recorded_at,
                spec=decoded.payload.value,
            )
        except UnsupportedStoredOverlayError:
            unavailable = True
            instance_spec = InstanceSpec("unavailable", reason="unsupported-version")
            issues.append(
                InstanceStateIssue(
                    InstanceStateIssueCode.INSTANCE_SPEC_UNSUPPORTED,
                    slot=instance_kind,
                )
            )
        except StateError:
            unavailable = True
            instance_spec = InstanceSpec("unavailable", reason="malformed")
            issues.append(
                InstanceStateIssue(
                    InstanceStateIssueCode.INSTANCE_SPEC_MALFORMED,
                    slot=instance_kind,
                )
            )

    current: SpecResolution
    if unavailable:
        current = UnresolvedSpec(selection, "instance-spec-unavailable")
    elif resolve is None:
        current = UnresolvedSpec(selection, "registry-unavailable")
        issues.append(
            InstanceStateIssue(
                InstanceStateIssueCode.REGISTRY_UNAVAILABLE,
                slot=instance_kind,
            )
        )
    else:
        try:
            current = project_resolved_spec(resolve(declaration), selection)
        except NotFoundError:
            current = UnresolvedSpec(selection, "missing-selection")
            issues.append(
                InstanceStateIssue(
                    InstanceStateIssueCode.CURRENT_DECLARATION_UNRESOLVED,
                    slot=instance_kind,
                )
            )

    return InstanceStateDescription(
        declarations=(
            DeclarationSlot(
                instance_kind,
                selection,
                instance_spec,
                current,
            ),
        ),
        unconsumed_records=unconsumed,
        issues=tuple(issues),
    )


def instance_state_data(state: InstanceStateDescription) -> JsonObject:
    """Project live instance-state facts into the closed JSON v1 addition."""
    from agentworks.resources.resolved_spec import resolved_spec_data

    declarations: JsonObject = {}
    for declaration in state.declarations:
        instance_spec: JsonObject = {"status": declaration.instance_spec.status}
        if declaration.instance_spec.status == "present":
            assert declaration.instance_spec.recorded_at is not None
            assert declaration.instance_spec.spec is not None
            instance_spec["recorded_at"] = declaration.instance_spec.recorded_at
            instance_spec["spec"] = declaration.instance_spec.spec
        elif declaration.instance_spec.status == "unavailable":
            assert declaration.instance_spec.reason is not None
            instance_spec["reason"] = declaration.instance_spec.reason
        declarations[declaration.name] = {
            "selection": {
                "kind": declaration.selection.kind,
                "name": declaration.selection.name,
            },
            "instance_spec": instance_spec,
            "current": resolved_spec_data(declaration.current),
        }

    evidence: list[JsonValue] = []
    for fact in state.lifecycle_evidence:
        fact_data: JsonObject = {"key": fact.key, "status": fact.status}
        if fact.status == "recorded":
            assert fact.recorded_at is not None
            assert fact.operation is not None
            assert fact.value is not None
            fact_data.update(
                {
                    "recorded_at": fact.recorded_at,
                    "operation": fact.operation,
                    "value": fact.value,
                }
            )
        evidence.append(fact_data)

    comparisons: list[JsonValue] = []
    for comparison in state.comparisons:
        comparison_data: JsonObject = {"key": comparison.key, "state": comparison.state}
        if comparison.differences:
            comparison_data["differences"] = [
                {
                    "field": difference.field,
                    "recorded": difference.recorded,
                    "current": difference.current,
                }
                for difference in comparison.differences
            ]
        comparisons.append(comparison_data)

    return {
        "declarations": declarations,
        "lifecycle_evidence": evidence,
        "comparisons": comparisons,
        "unconsumed_records": [
            {
                "record_type": record.record_type,
                "record_key": record.record_key,
                "payload_version": record.payload_version,
                "recorded_at": record.recorded_at,
            }
            for record in state.unconsumed_records
        ],
        "issues": [
            {
                "code": issue.code.value,
                **({"slot": issue.slot} if issue.slot is not None else {}),
                **({"record_key": issue.record_key} if issue.record_key is not None else {}),
            }
            for issue in state.issues
        ],
    }


def render_instance_state(state: InstanceStateDescription) -> None:
    """Render the shared compact human instance-state sections."""
    from agentworks.resources.resolved_spec import ResolvedSpec

    output.info("\nCurrent declarations:")
    for declaration in state.declarations:
        output.detail(
            sanitize_fact_line(f"{declaration.name}: {declaration.selection.kind}/{declaration.selection.name}")
        )
        with output.section():
            spec = declaration.instance_spec
            if spec.status == "present":
                output.detail(sanitize_fact_line(f"Instance spec: recorded {spec.recorded_at}"))
                assert spec.spec is not None
                _render_json_object(spec.spec)
            elif spec.status == "unavailable":
                output.detail(sanitize_fact_line(f"Instance spec: unavailable ({spec.reason})"))
            else:
                output.detail("Instance spec: absent")

            current = declaration.current
            if not isinstance(current, ResolvedSpec):
                output.detail(sanitize_fact_line(f"Current spec: unresolved ({current.reason})"))
            else:
                output.detail("Current spec:")
                _render_json_object(current.spec)

    output.info("\nLifecycle evidence:")
    if not state.lifecycle_evidence:
        output.detail("not recorded")
    else:
        for fact in state.lifecycle_evidence:
            suffix = ""
            if fact.status == "recorded":
                suffix = f" by {fact.operation} at {fact.recorded_at}"
            output.detail(sanitize_fact_line(f"{fact.key}: {fact.status}{suffix}"))
            if fact.value:
                with output.section():
                    _render_json_object(fact.value)

    output.info("\nComparisons:")
    if not state.comparisons:
        output.detail("(none)")
    else:
        for comparison in state.comparisons:
            output.detail(sanitize_fact_line(f"{comparison.key}: {comparison.state}"))
            if comparison.differences:
                with output.section():
                    for difference in comparison.differences:
                        recorded = json.dumps(difference.recorded, ensure_ascii=True, separators=(",", ":"))
                        current_value = json.dumps(difference.current, ensure_ascii=True, separators=(",", ":"))
                        output.detail(
                            sanitize_fact_line(f"{difference.field}: recorded={recorded}, current={current_value}")
                        )

    if state.unconsumed_records:
        output.info("\nUnconsumed instance records:")
        for record in state.unconsumed_records:
            record_type = record.record_type or "unknown"
            record_key = record.record_key or "unknown"
            version = "?" if record.payload_version is None else str(record.payload_version)
            recorded_at = record.recorded_at or "unknown time"
            output.detail(sanitize_fact_line(f"{record_type}/{record_key} v{version} ({recorded_at})"))

    if state.issues:
        output.info("\nInstance-state issues:")
        for issue in state.issues:
            context = issue.slot or issue.record_key
            output.detail(sanitize_fact_line(f"{issue.code.value}{f' ({context})' if context else ''}"))


def _render_json_object(value: JsonObject) -> None:
    with output.section():
        for line in json.dumps(value, ensure_ascii=True, indent=2).splitlines():
            output.detail(line)
