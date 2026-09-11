"""Domain codec and partial replacement of native setup instance evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from agentworks.db import AppliedStateKey, AppliedStateSlice, VersionedPayload
from agentworks.errors import StateError
from agentworks.harness_setup.model import NativeSetupState, SetupComponent, SetupRecord

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.db.instance_state import InstanceKind, JsonObject

_VERSION = 1
_COMPONENTS: dict[str, frozenset[SetupComponent]] = {
    "vm": frozenset({"vm", "admin"}),
    "agent": frozenset({"agent"}),
    "workspace": frozenset({"workspace"}),
}


class UnsupportedNativeSetupVersionError(StateError):
    """This release cannot interpret the stored native setup domain payload."""


def encode_native_setup(state: NativeSetupState) -> VersionedPayload:
    """Encode typed metadata without acquiring any settings or secret values."""
    return VersionedPayload(_VERSION, cast("JsonObject", state.model_dump(mode="json")))


def decode_native_setup(record: AppliedStateSlice) -> NativeSetupState:
    """Validate persisted evidence without echoing malformed input contents."""
    if record.key is not AppliedStateKey.HARNESS_NATIVE_SETUP or record.instance_kind not in _COMPONENTS:
        raise TypeError("native setup requires its matching owner slice")
    if record.payload.payload_version != _VERSION:
        raise UnsupportedNativeSetupVersionError(
            "native setup evidence requires a different Agentworks version",
            hint="Use a release that understands this native setup record. The stored evidence was retained.",
        )
    try:
        state = NativeSetupState.model_validate(record.payload.value)
        if any(item.component not in _COMPONENTS[record.instance_kind] for item in state.records):
            raise ValueError("component does not belong to this owner")
    except (ValidationError, ValueError, RecursionError):
        raise StateError("stored native setup evidence is malformed") from None
    return state


def read_native_setup(db: Database, kind: InstanceKind, name: str) -> NativeSetupState:
    """Read one closed slice, preserving unknown versions by refusing mutation."""
    for record in db.instance_state.get_applied_slices(kind, name):
        if record.key is AppliedStateKey.HARNESS_NATIVE_SETUP:
            return decode_native_setup(record)
    return NativeSetupState()


def write_native_setup(
    db: Database,
    kind: InstanceKind,
    name: str,
    state: NativeSetupState,
    *,
    operation: str,
) -> None:
    """Replace only this domain slice; all other applied keys remain intact."""
    if any(record.component not in _COMPONENTS.get(kind, ()) for record in state.records):
        raise StateError("native setup component does not belong to its owner")
    db.instance_state.replace_applied_slices(
        kind, name, operation, {AppliedStateKey.HARNESS_NATIVE_SETUP: encode_native_setup(state)}
    )


def replace_setup_record(state: NativeSetupState, record: SetupRecord) -> NativeSetupState:
    """Replace one component/integration while retaining prior declaration order."""
    key = (record.component, record.integration)
    records = list(state.records)
    for index, previous in enumerate(records):
        if (previous.component, previous.integration) == key:
            records[index] = record
            break
    else:
        records.append(record)
    return NativeSetupState(records=tuple(records))


def canonicalize_native_setup(record: AppliedStateSlice) -> VersionedPayload:
    """Validate known exported payloads and preserve uninterpreted future versions."""
    try:
        return encode_native_setup(decode_native_setup(record))
    except UnsupportedNativeSetupVersionError:
        return record.payload
