"""Native setup metadata survives partial replacement and export boundaries."""

from dataclasses import replace

import pytest

from agentworks.db import AppliedStateKey, AppliedStateSlice, Database, VersionedPayload
from agentworks.errors import StateError
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import (
    UnsupportedNativeSetupVersionError,
    canonicalize_native_setup,
    decode_native_setup,
    encode_native_setup,
    read_native_setup,
    replace_setup_record,
    write_native_setup,
)


def _state() -> NativeSetupState:
    return NativeSetupState(
        records=(
            SetupRecord(
                component="agent",
                integration="codex",
                destination_id="a" * 64,
                declaration={"config": {"plugins": ["p@m"]}, "env": {"TOKEN": {"secret": "ref"}}},
                claims=(NativeClaim(role="plugin", identifier="p@m", destination="/home/u/.codex"),),
            ),
        )
    )


def _slice(payload: VersionedPayload) -> AppliedStateSlice:
    return AppliedStateSlice(
        instance_kind="agent",
        instance_name="a",
        key=AppliedStateKey.HARNESS_NATIVE_SETUP,
        payload=payload,
        operation="agent-create",
        recorded_at="2026-09-07T12:00:00Z",
    )


def test_codec_round_trip_and_owner_validation():
    record = _slice(encode_native_setup(_state()))
    assert decode_native_setup(record) == _state()
    with pytest.raises(StateError):
        decode_native_setup(replace(record, instance_kind="workspace"))


def test_unknown_version_is_retained_but_cannot_bless_setup():
    record = _slice(VersionedPayload(2, {"future": True}))
    with pytest.raises(UnsupportedNativeSetupVersionError):
        decode_native_setup(record)
    assert canonicalize_native_setup(record) == record.payload


def test_malformed_known_payload_has_safe_error():
    with pytest.raises(StateError) as caught:
        decode_native_setup(_slice(VersionedPayload(1, {"records": "private-input"})))
    assert "private-input" not in str(caught.value)


def test_replacement_retains_sibling_records_and_incomplete_prefix(tmp_path):
    db = Database(tmp_path / "state.db")
    try:
        first = _state()
        second = first.records[0].model_copy(update={"integration": "claude-code", "complete": True})
        state = replace_setup_record(first, second)
        write_native_setup(db, "agent", "a", state, operation="agent-create")
        assert read_native_setup(db, "agent", "a") == state
        failed = first.records[0].model_copy(update={"pending_cleanup": True})
        state = replace_setup_record(state, failed)
        write_native_setup(db, "agent", "a", state, operation="agent-reinit")
        restored = read_native_setup(db, "agent", "a")
        assert restored.records == (failed, second)
    finally:
        db.close()
