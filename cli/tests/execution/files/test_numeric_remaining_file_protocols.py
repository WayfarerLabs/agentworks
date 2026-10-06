"""Real packed file protocols after mocked numeric guest admission."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from agentworks.execution import _file_effect_gate
from agentworks.execution._file_gate_control import setup_file_effect_gate
from agentworks.execution._file_inventory_exchange import list_directory
from agentworks.execution._file_metadata_exchange import ensure_file_directory, set_file_metadata
from agentworks.execution._file_object_exchange import remove_file, stat_file
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_publication import Create, CreateMetadata, Match, Replace
from agentworks.execution._file_publication_exchange import publication_cleanup, publication_reconcile, publish
from agentworks.execution._file_read import read_file
from agentworks.execution._file_snapshot_read import read_snapshot
from agentworks.execution._file_stage_exchange import stage_begin, stage_chunk, stage_cleanup, stage_reconcile
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution._scratch import _cleanup_debt, cleanup_scratch
from agentworks.execution.carrier import Deadline, ExitStatus
from tests.execution.files._publication_test_support import open_parent, ready_scratch, record_stage
from tests.execution.files.test_numeric_guest_helper_adoption import _GUEST, _ROOT, _RUNTIME, _TOKEN, _plan
from tests.execution.files.test_numeric_remaining_file_helper_adoption import _DATA, _call, _PackedCarrier

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="private numeric helpers require Linux")


def _common(root: Path) -> dict:
    return dict(
        trusted_root_path=str(root),
        relative_path="payload",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=_RUNTIME,
        bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST),
    )


def test_stage_binary_transfer_reconciliation_and_cleanup_keep_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    carrier = _PackedCarrier("stage", monkeypatch)
    common = _common(tmp_path)
    begun = stage_begin(carrier, token=_TOKEN, expected_length=len(_DATA), **common)
    assert begun.observation is not None and begun.observation.reference is not None
    reference = begun.observation.reference
    written = stage_chunk(
        carrier,
        token=_TOKEN,
        reference=reference,
        offset=0,
        data=_DATA,
        chunk_digest=hashlib.sha256(_DATA).digest(),
        **common,
    )
    assert written.observation is not None and written.observation.state.value == "accepted"
    assert (tmp_path / _cleanup_debt(reference)._name / "data").read_bytes() == _DATA
    recovered = stage_reconcile(carrier, token=_TOKEN, **common)
    assert recovered.observation is not None and recovered.observation.cleanup_debt is not None
    cleaned = stage_cleanup(carrier, token=_TOKEN, cleanup_debt=recovered.observation.cleanup_debt, **common)
    assert cleaned.observation is not None and cleaned.observation.state.value == "cleaned"
    assert not list(tmp_path.iterdir())
    assert carrier.calls == 4 and carrier.events.count("drop") == 4 and carrier.events.count("body") == 4
    assert carrier.events[:3] == ["drop", "prefix", "init"]
    assert carrier.events.index("init") < carrier.events.index("remaining") < carrier.events.index("body")
    assert carrier.events.index("body") < carrier.events.index("request")


@pytest.mark.parametrize("condition_name", ["create", "replace", "match"])
def test_publication_conditions_preserve_binary_and_metadata_protocol(
    condition_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "payload"
    condition: Create | Replace | Match = Create()
    if condition_name != "create":
        target.write_bytes(b"old")
        target.chmod(0o640)
        if condition_name == "replace":
            condition = Replace()
        else:
            descriptor = open_parent(tmp_path)
            try:
                observed = read_snapshot(descriptor, "payload", 1024)
            finally:
                os.close(descriptor)
            assert observed is not None
            condition = Match(observed.revision)
    descriptor = open_parent(tmp_path)
    try:
        ready = ready_scratch(descriptor, _TOKEN, _DATA)
    finally:
        os.close(descriptor)
    carrier = _PackedCarrier("publication", monkeypatch)
    result = publish(
        carrier,
        token=_TOKEN,
        reference=ready._reference,
        digest=hashlib.sha256(_DATA).digest(),
        condition=condition,
        create_metadata=CreateMetadata(os.geteuid(), os.getegid(), 0o600),
        **_common(tmp_path),
    )
    assert result.observation is not None and result.observation.state.value == "published"
    assert (
        result.observation.revision is not None and result.observation.revision.digest == hashlib.sha256(_DATA).digest()
    )
    assert target.read_bytes() == _DATA
    assert stat.S_IMODE(target.stat().st_mode) == (0o600 if condition_name == "create" else 0o640)
    assert carrier.events[:3] == ["drop", "prefix", "init"]
    descriptor = open_parent(tmp_path)
    try:
        cleanup_scratch(descriptor, _cleanup_debt(ready))
    finally:
        os.close(descriptor)


def test_publication_reconcile_and_cleanup_forward_context_to_real_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = open_parent(tmp_path)
    try:
        ready = ready_scratch(descriptor, _TOKEN, _DATA)
        ownership, stage_descriptor = record_stage(descriptor, ready, descriptor)
        os.close(stage_descriptor)
    finally:
        os.close(descriptor)
    carrier = _PackedCarrier("publication", monkeypatch)
    common = dict(token=_TOKEN, reference=ready._reference, **_common(tmp_path))
    recovered = publication_reconcile(carrier, **common)
    assert recovered.observation is not None and recovered.observation.cleanup_debt is not None
    cleaned = publication_cleanup(carrier, cleanup_debt=recovered.observation.cleanup_debt, **common)
    assert cleaned.observation is not None and cleaned.observation.state.value == "cleaned"
    assert not (tmp_path / ownership._stage_name).exists()
    assert carrier.calls == 2 and carrier.events.count("drop") == 2 and carrier.events.count("body") == 2
    descriptor = open_parent(tmp_path)
    try:
        cleanup_scratch(descriptor, _cleanup_debt(ready))
    finally:
        os.close(descriptor)


def test_read_inventory_metadata_and_object_protocols_follow_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "payload"
    target.write_bytes(_DATA)
    target.chmod(0o600)
    common = _common(root)
    read_carrier = _PackedCarrier("read", monkeypatch)
    read = read_file(read_carrier, max_bytes=len(_DATA), **common)
    assert read.observation is not None and read.observation.snapshot is not None
    assert read.observation.snapshot.data == _DATA
    inventory_carrier = _PackedCarrier("inventory", monkeypatch)
    inventory = list_directory(
        inventory_carrier,
        **{**common, "trusted_root_path": str(tmp_path), "relative_path": "tree"},
        max_entries=4096,
        max_depth=8,
        max_encoded_bytes=4 * 1024 * 1024,
    )
    assert inventory.observation is not None and inventory.observation.entries is not None
    assert [entry.relative_path for entry in inventory.observation.entries] == ["payload"]
    metadata_carrier = _PackedCarrier("metadata", monkeypatch)
    metadata = set_file_metadata(metadata_carrier, uid=os.geteuid(), gid=os.getegid(), mode=0o640, **common)
    assert metadata.observation is not None and metadata.observation.state.value == "changed"
    ensured = ensure_file_directory(
        metadata_carrier, uid=os.geteuid(), gid=os.getegid(), mode=0o700, **{**common, "relative_path": "directory"}
    )
    assert ensured.observation is not None and ensured.observation.state.value == "changed"
    assert (root / "directory").is_dir() and stat.S_IMODE(target.stat().st_mode) == 0o640
    object_carrier = _PackedCarrier("object", monkeypatch)
    observed = stat_file(object_carrier, **common)
    assert observed.observation is not None and observed.observation.revision is not None
    removed = remove_file(
        object_carrier, expected_kind=FileKind.REGULAR, expected_revision=observed.observation.revision, **common
    )
    assert removed.observation is not None and removed.observation.state.value == "changed" and not target.exists()
    for carrier in (read_carrier, inventory_carrier, metadata_carrier, object_carrier):
        assert carrier.events[:3] == ["drop", "prefix", "init"]
        assert carrier.events.index("init") < carrier.events.index("remaining") < carrier.events.index("body")


def test_intended_root_read_target_is_admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "payload").write_bytes(_DATA)
    carrier = _PackedCarrier("read", monkeypatch, plan=_ROOT)
    result = _call(
        "read.read",
        carrier,
        plan=_ROOT,
        trusted_root_path=str(tmp_path),
        bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST),
    )
    assert result.observation is not None and result.observation.snapshot is not None
    assert result.observation.snapshot.data == _DATA and "body" in carrier.events


@pytest.mark.parametrize(
    "case",
    [
        "stage.begin",
        "stage.chunk",
        "stage.reconcile",
        "stage.cleanup",
        "publication.publish",
        "publication.reconcile",
        "publication.cleanup",
    ],
)
@pytest.mark.parametrize("changed", [False, True])
def test_gated_body_refreshes_the_bound_reader_before_effect(
    case: str,
    changed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = tmp_path / "run" / "agentworks" / "file-gates-v1"
    (namespace / str(os.geteuid())).mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(_file_effect_gate, "_GATE_NAMESPACE", str(namespace))
    monkeypatch.setattr(_file_effect_gate, "_ROOT_UID", os.geteuid())
    path = namespace / str(os.geteuid()) / ("a" * 64 + ".db")
    gate = setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "vm-a", lambda: _GUEST)
    root = tmp_path / "files"
    root.mkdir()
    carrier = _PackedCarrier(case.split(".")[0], monkeypatch, changed=changed, gate_namespace=namespace)
    result = _call(
        case, carrier, trusted_root_path=str(root), effect_gate=gate, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST)
    )
    assert result.carrier_completion == ExitStatus(code=0) and carrier.reads == 2
    assert "body" in carrier.events
    if changed:
        assert result.observation is not None and result.observation.failure is not None
        assert result.observation.failure.code.value == "effect_gate_refused"
        assert not list(root.iterdir())
    elif case == "stage.begin":
        assert result.observation is not None and result.observation.reference is not None
        cleaned = stage_cleanup(
            carrier,
            token=_TOKEN,
            cleanup_debt=_cleanup_debt(result.observation.reference),
            effect_gate=gate,
            **_common(root),
        )
        assert cleaned.observation is not None and cleaned.observation.state.value == "cleaned"
