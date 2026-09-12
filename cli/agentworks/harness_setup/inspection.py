"""Read-only native completion summaries on existing instance inspection surfaces."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.db import AppliedStateKey
from agentworks.errors import StateError
from agentworks.harness_setup.state import UnsupportedNativeSetupVersionError, decode_native_setup
from agentworks.instance_description import (
    InstanceStateDescription,
    InstanceStateIssue,
    InstanceStateIssueCode,
    LifecycleEvidence,
    UnconsumedRecord,
)
from agentworks.resources.render import sanitize_fact_line

if TYPE_CHECKING:
    from agentworks.db import InstanceStateInspection
    from agentworks.machine_output import JsonValue


def include_native_setup(
    state: InstanceStateDescription, inspection: InstanceStateInspection
) -> InstanceStateDescription:
    """Expose completion metadata without config, contents, env, or native I/O."""
    selected = next(
        (item.record for item in inspection.applied_slices if item.record.key is AppliedStateKey.HARNESS_NATIVE_SETUP),
        None,
    )
    if selected is None:
        return state
    key = selected.key.value
    try:
        native = decode_native_setup(selected)
    except UnsupportedNativeSetupVersionError:
        return replace(
            state,
            lifecycle_evidence=(*state.lifecycle_evidence, LifecycleEvidence(key, "unavailable")),
            unconsumed_records=(
                *state.unconsumed_records,
                UnconsumedRecord("applied-state", key, selected.payload.payload_version, selected.recorded_at),
            ),
            issues=(
                *state.issues,
                InstanceStateIssue(InstanceStateIssueCode.APPLIED_RECORD_UNSUPPORTED, record_key=key),
            ),
        )
    except StateError:
        return replace(
            state,
            lifecycle_evidence=(*state.lifecycle_evidence, LifecycleEvidence(key, "unavailable")),
            issues=(*state.issues, InstanceStateIssue(InstanceStateIssueCode.APPLIED_RECORD_MALFORMED, record_key=key)),
        )
    summaries: list[JsonValue] = [
        {
            "integration": sanitize_fact_line(record.integration)[:128],
            "component": record.component,
            "complete": record.complete,
            "pending_cleanup": record.pending_cleanup,
            "claim_count": len(record.claims),
        }
        for record in native.records
    ]
    return replace(
        state,
        lifecycle_evidence=(
            *state.lifecycle_evidence,
            LifecycleEvidence(key, "recorded", selected.recorded_at, selected.operation, {"integrations": summaries}),
        ),
    )
