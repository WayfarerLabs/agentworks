"""Malformed wire input, finite custody, fence loss and no replay boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import pytest

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp._activation_operation import ActivationState
from agentworks.plugins.gcp._native_route_evidence import decode_payload
from tests.plugins.gcp.test_native_route import (
    OBSERVATION,
    baseline,
    mutations,
    open_route,
    receipt,
)
from tests.plugins.gcp.test_native_route import (
    isolated_config as isolated_config,
)
from tests.plugins.gcp.test_native_route import (
    sdk as sdk,
)


@pytest.mark.parametrize("status", [201, 204, 301, 302, 401, 403, 404, 429, 500])
def test_service_status_refused_by_public_hook_before_body(sdk, status):
    sdk.override = lambda request, value: (status, {}, value) if request.method == "POST" else None
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert sdk.responses[-1].raw.reads == []
    assert sdk.route.payload.create_operation is None
    assert not sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "POST")) == 1 and not mutations(sdk, "DELETE")


@pytest.mark.parametrize("encoding", ["gzip", "identity, gzip", "", "br"])
def test_encoded_body_refused_before_raw_read(sdk, encoding):
    sdk.override = lambda request, value: (
        (200, {"Content-Encoding": encoding}, value) if request.method == "POST" else None
    )
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert not sdk.responses[-1].raw.reads
    assert not sdk.route.settle(Deadline.after(5))


@pytest.mark.parametrize(
    "body",
    [
        b" " * 65537,
        b'{"kind":1,"kind":2}',
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b"\xff",
        b"[]",
        b'{"incomplete":',
    ],
)
def test_raw_ingestion_bound_and_strict_json(sdk, body):
    sdk.override = lambda request, value: (200, {}, body) if request.method == "POST" else None
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert sum(sdk.responses[-1].raw.reads) <= 65537 + 8192
    assert not sdk.route.settle(Deadline.after(5))
    assert not mutations(sdk, "DELETE")


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "foreign"),
        ("name", "Operation/foreign"),
        ("name", "x" * 513),
        ("clientOperationId", "00000000-0000-0000-0000-000000000000"),
        ("operationType", "delete"),
        ("targetId", "0789"),
        ("targetId", 789),
        ("targetId", "18446744073709551616"),
        ("targetId", "0"),
        ("targetLink", "https://foreign.test/compute/v1/projects/project/global/firewalls/rule"),
    ],
)
def test_foreign_insert_receipt_not_retained(sdk, field, value):
    def override(request, body):
        if request.method == "POST":
            body[field] = value

    sdk.override = override
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert sdk.route.payload.create_operation is None
    assert not sdk.route.settle(Deadline.after(5))


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "UNKNOWN"},
        {"status": None},
        {"error": {}},
        {"error": {"errors": []}},
        {"error": {"errors": [{"code": ""}]}},
        {"httpErrorStatusCode": 500},
        {"httpErrorMessage": "oops"},
        {"status": "PENDING", "error": {"errors": [{"code": "FAILED"}]}},
        {"region": "projects/project/regions/us-central1"},
        {"selfLink": "https://foreign.test/operation"},
    ],
)
def test_matching_identity_survives_malformed_completion_and_can_observe(sdk, extra):
    def override(request, body):
        if request.method == "POST":
            body.update(extra)

    sdk.override = override
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert sdk.route.payload.create_operation == "Insert_1" and sdk.route.payload.rule_id == "789"
    assert sdk.route.payload.create_state is ActivationState.PENDING
    row = sdk.owner.inspect_lifecycle_obligation(sdk.route.obligation_id)
    assert decode_payload(row.payload).create_operation == "Insert_1"
    sdk.override = None
    assert sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "POST")) == len(mutations(sdk, "DELETE")) == 1


@pytest.mark.parametrize("field,value", [("name", "Other_1"), ("targetId", "999"), ("operationType", "delete")])
def test_global_observation_requires_original_receipt(sdk, field, value):
    sdk.create_status = "PENDING"

    def override(request, body):
        if "/operations/" in request.url:
            body[field] = value

    sdk.override = override
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert sdk.route.payload.create_state is ActivationState.PENDING
    sdk.override = None
    assert sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "POST")) == 1


@pytest.mark.parametrize("original_kind", ["response", "session"])
def test_actual_unretired_original_bars_reads_and_close_retries_only_that_original(sdk, original_kind):
    held: list[Any] = []

    def fail(original):
        selected = sdk.route._response if original_kind == "response" else sdk.route._service_session
        if sdk.route._create_entered and original is selected:
            held.append(original)
            raise OSError("public close failed")

    sdk.before_close = fail
    with pytest.raises(StateError):
        open_route(sdk)
    assert sdk.route.cleanup_incomplete and held
    assert sdk.route.payload.create_operation == "Insert_1"
    before = len(sdk.calls)
    assert not sdk.route.close(Deadline.after(5))
    assert len(sdk.calls) == before
    assert (sdk.route._response if original_kind == "response" else sdk.route._service_session) is held[0]
    sdk.before_close = None
    assert sdk.route.close(Deadline.after(5))
    assert len(sdk.calls) == before and not sdk.auth.closed
    assert sdk.route.settle(Deadline.after(5))


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_primary_control_survives_service_cleanup_auth_end_and_lock_release(sdk, monkeypatch, control_type):
    primary = control_type("primary dispatch control")
    secondary = SystemExit("secondary cleanup") if control_type is KeyboardInterrupt else KeyboardInterrupt("secondary")
    original_end = sdk.auth.end
    lock = sdk.route._lock

    def override(request, body):
        if request.method == "POST":
            raise primary

    def fail_close(original):
        if sdk.route._create_entered:
            raise secondary

    def end():
        original_end()
        if sdk.route._create_entered:
            raise secondary

    class Lock:
        def acquire(self, **kwargs):
            return lock.acquire(**kwargs)

        def release(self):
            lock.release()
            raise secondary

        def locked(self):
            return lock.locked()

    sdk.override, sdk.before_close = override, fail_close
    monkeypatch.setattr(sdk.auth, "end", end)
    sdk.route._lock = Lock()
    with pytest.raises(control_type) as caught:
        open_route(sdk)
    assert caught.value is primary and not lock.locked()
    assert not sdk.auth._operation.locked()
    assert len(mutations(sdk, "POST")) == 1 and sdk.route.cleanup_incomplete


@pytest.mark.parametrize("stage", ["insert", "ack-publication", "delete-intent", "delete-dispatch"])
def test_owner_fence_takeover_blocks_further_mutations(sdk, monkeypatch, stage):
    if stage in ("delete-intent", "delete-dispatch"):
        open_route(sdk)
    recovered: list[OperationOwner] = []

    def take_over() -> None:
        if not recovered:
            recovered.append(OperationOwner.recover(sdk.database.operations, sdk.owner.ownership, "f" * 32))

    if stage == "insert":
        original = LifecycleObligation.mark_possible_effect

        def mark(*args, **kwargs):
            take_over()
            return original(*args, **kwargs)

        monkeypatch.setattr(LifecycleObligation, "mark_possible_effect", mark)
    elif stage in ("ack-publication", "delete-intent"):
        original_inspect = OperationOwner.inspect_lifecycle_obligation

        def inspect(*args, **kwargs):
            if sdk.route.payload.create_operation is not None and (
                stage == "ack-publication" or sdk.route.payload.delete_request_id is not None
            ):
                take_over()
            return original_inspect(*args, **kwargs)

        monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", inspect)
    else:
        original_headers = sdk.auth.apply_headers

        def headers(method, url):
            result = original_headers(method, url)
            if method == "DELETE":
                take_over()
            return result

        monkeypatch.setattr(sdk.auth, "apply_headers", headers)
    with pytest.raises(StateError):
        if stage.startswith("delete"):
            sdk.route.settle(Deadline.after(5))
        else:
            open_route(sdk)
    assert len(mutations(sdk, "POST")) == int(stage != "insert")
    assert not mutations(sdk, "DELETE") and recovered


@pytest.mark.parametrize(
    "field,value",
    [
        ("obligation_kind", "foreign"),
        ("payload_version", 2),
        ("payload_revision", 42),
        ("payload", b"{}"),
        ("payload", b"not JSON"),
    ],
)
def test_exact_ledger_row_is_required_for_later_checks(sdk, monkeypatch, field, value):
    open_route(sdk)
    original = sdk.owner.inspect_lifecycle_obligation

    def inspect(obligation_id):
        return replace(original(obligation_id), **{field: value})

    monkeypatch.setattr(sdk.owner, "inspect_lifecycle_obligation", inspect)
    before = len(sdk.calls)
    with pytest.raises(StateError):
        sdk.route.check(Deadline.after(5), observed_instance=OBSERVATION)
    assert len(sdk.calls) == before


@pytest.mark.parametrize("committed", [False, True])
def test_lost_resolution_reply_reconciles_exact_row(sdk, monkeypatch, committed):
    open_route(sdk)
    original = LifecycleObligation.resolve

    def resolve(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise KeyboardInterrupt("resolution reply lost")

    monkeypatch.setattr(LifecycleObligation, "resolve", resolve)
    with pytest.raises(KeyboardInterrupt):
        sdk.route.settle(Deadline.after(5))
    monkeypatch.setattr(LifecycleObligation, "resolve", original)
    before = len(sdk.calls)
    assert sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "DELETE")) == 1
    if committed:
        assert len(sdk.calls) == before
    else:
        assert len(sdk.calls) == before + 1
        assert sdk.calls[-1][0] == "GET" and sdk.calls[-1][1].endswith(sdk.route.payload.name)
    assert sdk.owner.inspect_lifecycle_obligation(sdk.route.obligation_id).state is LifecycleObligationState.RESOLVED


def test_close_bars_admission_even_when_cleanup_budget_already_expired(sdk):
    with pytest.raises(LimitExceededError):
        sdk.route.close(Deadline.after(0))
    with pytest.raises(StateError):
        open_route(sdk)
    assert not sdk.calls and sdk.route.settle(Deadline.after(5))


def test_delete_pending_ack_observes_without_replay_on_later_settlement(sdk):
    open_route(sdk)
    sdk.delete_status = "PENDING"

    def override(request, value):
        if "/operations/Delete_1" in request.url:
            raise OSError("monitor reply lost")

    sdk.override = override
    with pytest.raises(OSError):
        sdk.route.settle(Deadline.after(5))
    assert (
        sdk.route.payload.delete_operation == "Delete_1" and sdk.route.payload.delete_state is ActivationState.PENDING
    )
    sdk.override = None
    assert sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "DELETE")) == 1


def test_terminal_failed_create_and_fresh_absence_settle_without_delete(sdk):
    def override(request, value):
        if request.method == "POST":
            sdk.exists = False
            return 200, {}, receipt(sdk.route, failed=True)

    sdk.override = override
    with pytest.raises(StateError):
        open_route(sdk)
    assert sdk.route.payload.create_state is ActivationState.FAILED
    assert sdk.route.settle(Deadline.after(5)) and not mutations(sdk, "DELETE")


def test_complete_inventory_page_limit(sdk):
    pages: list[Any] = []

    def page(query):
        pages.append(query)
        return {"items": [], "nextPageToken": f"next-{len(pages)}"}

    sdk.pages = page
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert len(pages) == 64 and not mutations(sdk, "POST")


def test_complete_inventory_rule_limit(sdk):
    pages: list[Any] = []

    def page(query):
        index = len(pages)
        pages.append(query)
        rules = [
            dict(baseline(), name=f"other-{index}-{j}", id=str(1000 + index * 100 + j), targetTags=["other"])
            for j in range(100)
        ]
        return {"items": rules, "nextPageToken": f"next-{index}"}

    sdk.pages = page
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert len(pages) == 41 and not mutations(sdk, "POST")


def test_full_rule_inventory_rejects_duplicate_identity(sdk):
    sdk.pages = lambda query: {"items": [baseline(), baseline()]}
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert not mutations(sdk, "POST")


def test_rule_name_collision_never_adopted_without_receipt(sdk):
    sdk.exists = True
    with pytest.raises(StateError):
        open_route(sdk)
    assert not mutations(sdk, "POST") and not mutations(sdk, "DELETE")
    assert sdk.route.settle(Deadline.after(5))


def test_bad_delete_target_id_retains_uncertainty_and_never_replays(sdk):
    open_route(sdk)

    def override(request, value):
        if request.method == "DELETE":
            value["targetId"] = "999"

    sdk.override = override
    with pytest.raises(ValidationError):
        sdk.route.settle(Deadline.after(5))
    assert sdk.route.payload.delete_operation is None
    sdk.override = None
    assert not sdk.route.settle(Deadline.after(5)) and len(mutations(sdk, "DELETE")) == 1


def test_delete_intent_contains_only_selected_facts_before_dispatch(sdk):
    open_route(sdk)
    seen = []

    def override(request, value):
        if request.method == "DELETE":
            row = sdk.owner.inspect_lifecycle_obligation(sdk.route.obligation_id)
            payload = decode_payload(row.payload)
            assert payload.delete_request_id and payload.delete_operation is None
            assert payload.delete_state is ActivationState.UNKNOWN
            seen.append(json.loads(row.payload))
        assert urlsplit(request.url).hostname == "compute.googleapis.com"

    sdk.override = override
    assert sdk.route.settle(Deadline.after(5)) and len(seen) == 1
