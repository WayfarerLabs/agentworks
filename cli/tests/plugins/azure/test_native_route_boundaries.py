"""Malformed evidence, deadline/control edges and exact route admission fences."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.azure._native_route import AzureNativeRoute
from agentworks.plugins.azure._native_route_evidence import resource_url
from tests.plugins.azure.test_activation_sdk import deny
from tests.plugins.azure.test_native_route import (
    DELETE_OP,
    LOCATOR,
    NIC,
    NSG,
    OP,
    PREFIXES,
    RESOURCE,
    SUB,
    VM_ID,
    Control,
    mutations,
    row,
)
from tests.plugins.azure.test_native_route import owned as owned


@pytest.mark.parametrize(
    "prefixes",
    [(), ("0.0.0.0/0",) * 65, ("::1/128",), ("192.0.2.5/24",), ("192.0.2.1/32",) * 2, (1,), ["192.0.2.1/32"]],
)
def test_invalid_caller_prefixes_refused_passively(owned, prefixes):
    with pytest.raises(ValidationError):
        AzureNativeRoute(owned.owner, "vm", owned.credential, RESOURCE, VM_ID, LOCATOR, owned.vm, prefixes)
    assert owned.clients == []


@pytest.mark.parametrize("kind", ["uuid", "locator", "path", "scope", "no-nic", "two-nics", "foreign-nic", "child-nic"])
def test_wrong_selection_refused_before_client_construction(owned, kind):
    vm_id, locator, vm_name = VM_ID, LOCATOR, "vm"
    if kind == "uuid":
        owned.vm.vm_id = "31234567-89ab-0cde-0123-456789abcdef"
    elif kind == "locator":
        locator = ProviderLocator(f"azure-vm:{RESOURCE}")
    elif kind == "path":
        owned.vm.id = RESOURCE + "/child"
    elif kind == "scope":
        vm_name = "foreign"
    elif kind == "no-nic":
        owned.vm.network_profile.network_interfaces = []
    elif kind == "two-nics":
        owned.vm.network_profile.network_interfaces *= 2
    else:
        owned.vm.network_profile.network_interfaces[0].id = (
            NIC.replace(SUB, "foreign") if kind == "foreign-nic" else NIC + "/child"
        )
    with pytest.raises((StateError, ValidationError)):
        AzureNativeRoute(owned.owner, vm_name, owned.credential, RESOURCE, vm_id, locator, owned.vm, PREFIXES)
    assert owned.clients == []


def test_semantic_ascii_casing_preserves_frozen_locator_and_request_ids(owned):
    owned.vm.id = RESOURCE.upper()
    owned.vm.vm_id = VM_ID.upper()
    owned.vm.network_profile.network_interfaces[0].id = NIC.upper()
    owned.route = AzureNativeRoute(owned.owner, "vm", owned.credential, RESOURCE, VM_ID, LOCATOR, owned.vm, PREFIXES)
    owned.transport.nic["id"] = NIC.upper()
    owned.transport.nic["properties"]["virtualMachine"]["id"] = RESOURCE.upper()
    owned.transport.nic["properties"]["networkSecurityGroup"]["id"] = NSG.upper()
    owned.transport.nsg["id"] = NSG.upper()
    owned.transport.nsg["properties"]["networkInterfaces"][0]["id"] = NIC.upper()
    owned.route.open(Deadline.after(5))
    assert json.loads(row(owned).payload)["resource_id"] == RESOURCE
    assert owned.transport.calls[0][1] == resource_url(NIC.upper())
    assert owned.route.settle(Deadline.after(5))


def test_literal_network_url_characters_quoted_once(owned):
    nic, nsg = NIC + ":?#%", NSG + ":?#%"
    owned.vm.network_profile.network_interfaces[0].id = nic
    owned.route = AzureNativeRoute(owned.owner, "vm", owned.credential, RESOURCE, VM_ID, LOCATOR, owned.vm, PREFIXES)
    owned.transport.nic["id"] = nic
    owned.transport.nic["properties"]["networkSecurityGroup"]["id"] = nsg
    owned.transport.nsg["id"] = nsg
    owned.transport.nsg["properties"]["networkInterfaces"][0]["id"] = nic
    owned.route.open(Deadline.after(5))
    assert owned.transport.calls[0][1] == resource_url(nic)
    assert "%3A%3F%23%25" in mutations(owned)[0][1]
    assert owned.route.settle(Deadline.after(5))


@pytest.mark.parametrize("kind", ["nic", "uuid", "path"])
def test_check_validates_fresh_vm_before_any_network_request(owned, kind):
    owned.route.open(Deadline.after(5))
    if kind == "nic":
        owned.vm.network_profile.network_interfaces[0].id = NIC + "replacement"
    elif kind == "uuid":
        owned.vm.vm_id = "31234567-89ab-0cde-0123-456789abcdef"
    else:
        owned.vm.id = RESOURCE + "replacement"
    before = len(owned.transport.calls)
    with pytest.raises(StateError):
        owned.route.check(Deadline.after(5), observed_vm=owned.vm)
    assert len(owned.transport.calls) == before
    assert owned.route.settle(Deadline.after(5))


@pytest.mark.parametrize("field", ["id", "name"])
def test_changed_rule_identity_never_deleted(owned, field):
    owned.route.open(Deadline.after(5))
    owned.transport.rule[field] += "foreign"
    with pytest.raises(StateError):
        owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 1


def test_async_monitor_subscription_casing_is_semantic_but_request_url_exact(owned):
    monitor = OP.replace(SUB, SUB.upper())
    owned.transport.overrides.extend(
        [
            ("PUT", (201, {"Azure-AsyncOperation": monitor}, deny)),
            (monitor, (200, {}, {"status": "Succeeded"})),
        ]
    )
    owned.route.open(Deadline.after(5))
    assert any(call[:2] == ("GET", monitor) for call in owned.transport.calls)
    assert owned.route.settle(Deadline.after(5))


@pytest.mark.parametrize(
    "headers",
    [
        {"Azure-AsyncOperation": "http://management.azure.com/subscriptions/foreign/op"},
        {"Azure-AsyncOperation": OP.replace(SUB, "foreign")},
        {"Azure-AsyncOperation": OP.replace("management.azure.com", "management.azure.com:443")},
        {"Azure-AsyncOperation": OP + "&secret=1"},
        {"Azure-AsyncOperation": OP + "#fragment"},
        {"Azure-AsyncOperation": OP.replace("/operations/", "/../operations/")},
        {"Azure-AsyncOperation": OP.replace("/operations/", "/%2e%2e/operations/")},
        {"Azure-AsyncOperation": OP + "%"},
        {"Azure-AsyncOperation": OP, "azure-asyncoperation": OP},
        {"Location": OP + "," + OP},
        {"x-ms-request-id": "a" * 257},
        {"x-ms-request-id": "bad\nheader"},
        {"Location": 7},
    ],
)
@pytest.mark.parametrize("stage", ["PUT", "DELETE"])
def test_invalid_receipt_headers_keep_possible_effect(owned, headers, stage):
    if stage == "DELETE":
        owned.route.open(Deadline.after(5))
    owned.transport.overrides.append((stage, (200, headers, deny)))
    with pytest.raises(ValidationError):
        (owned.route.open if stage == "PUT" else owned.route.settle)(Deadline.after(5))
    assert not owned.route.settle(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert sum(call[0] == stage for call in owned.transport.calls) == 1


@pytest.mark.parametrize("kind", ["method", "url", "status"])
@pytest.mark.parametrize("stage", ["PUT", "DELETE", "monitor"])
def test_exact_response_request_and_status_required(owned, kind, stage):
    if stage == "DELETE":
        owned.route.open(Deadline.after(5))
    if stage == "monitor":
        owned.transport.overrides.append(("PUT", (200, {"Azure-AsyncOperation": OP}, deny)))

    def foreign(response):
        match = response.request.url == OP if stage == "monitor" else response.request.method == stage
        if match:
            if kind == "status":
                response._status = 202 if stage == "PUT" else 201
            else:
                response._request = SimpleNamespace(
                    method="FOREIGN" if kind == "method" else response.request.method,
                    url=response.request.url + "&foreign=1" if kind == "url" else response.request.url,
                )

    owned.transport.hook = foreign
    with pytest.raises(ValidationError):
        (owned.route.settle if stage == "DELETE" else owned.route.open)(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize(
    "body",
    [
        [b'{"status":"Succeeded","status":"Failed"}'],
        [b"[]"],
        [b"{}"],
        [b'{"status":1}'],
        [b'{"status":""}'],
        [b'{"status":"secret\\n"}'],
        [b"x" * 16385],
        ["foreign"],
    ],
)
def test_bounded_monitor_json_refusal_does_not_delete(owned, body):
    owned.transport.overrides.extend([("PUT", (201, {"Azure-AsyncOperation": OP}, deny)), (OP, (200, {}, body))])
    with pytest.raises(ValidationError):
        owned.route.open(Deadline.after(5))
    assert len(mutations(owned)) == 1 and row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("body", [None, {}, [], {"properties": {"provisioningState": "Updating"}}])
def test_no_header_put_requires_positive_completed_resource(owned, body):
    owned.transport.overrides.append(("PUT", (201, {}, body)))
    with pytest.raises((ValidationError, StateError)):
        owned.route.open(Deadline.after(5))
    with pytest.raises(StateError):
        owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 1


def test_pending_then_terminal_uses_same_retained_monitor(owned, monkeypatch):
    pauses = []
    monkeypatch.setattr(time, "sleep", lambda seconds: pauses.append(seconds))
    owned.transport.overrides.extend(
        [
            ("PUT", (200, {"Azure-AsyncOperation": OP}, deny)),
            (OP, (200, {}, {"status": "Running"})),
            (OP, (200, {}, {"status": "Succeeded"})),
        ]
    )
    owned.route.open(Deadline.after(5))
    assert pauses == [0.25]
    assert sum(call[:2] == ("GET", OP) for call in owned.transport.calls) == 2
    assert owned.route.settle(Deadline.after(5))


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit, Control])
@pytest.mark.parametrize("secondary_type", [OSError, KeyboardInterrupt, SystemExit, Control])
def test_primary_control_identity_precedes_cleanup_control(owned, primary_type, secondary_type):
    primary = primary_type("primary")
    owned.transport.overrides.extend([("PUT", (201, {"Azure-AsyncOperation": OP}, deny)), (OP, (200, {}, primary))])

    def fail_close(response):
        if response.request.url == OP:
            response.close_errors.append(secondary_type("secondary"))

    owned.transport.hook = fail_close
    with pytest.raises(primary_type) as caught:
        owned.route.open(Deadline.after(5))
    assert caught.value is primary and owned.route.cleanup_incomplete
    assert owned.route.close(Deadline.after(5))
    assert len(mutations(owned)) == 1


@pytest.mark.parametrize("stage", ["put", "monitor"])
def test_late_async_evidence_kept_and_no_new_body_reads(owned, monkeypatch, stage):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    owned.transport.overrides.append(("PUT", (201, {"Azure-AsyncOperation": OP}, deny)))

    def late(response):
        if (stage == "put" and response.request.method == "PUT") or (stage == "monitor" and response.request.url == OP):
            clock[0] = 102.0

    owned.transport.hook = late
    with pytest.raises(TimeoutError):
        owned.route.open(Deadline.after(1))
    assert owned.transport.responses[-1].iterations == 0
    assert owned.route.cleanup_incomplete
    owned.transport.hook = None
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 2


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, Control])
def test_delete_intent_is_durable_before_transmission_and_lost_reply(owned, monkeypatch, committed, error_type):
    owned.route.open(Deadline.after(5))
    original = owned.repository.publish_lifecycle_obligation_payload
    primary = error_type("lost cleanup intent reply")

    def lose_intent(*args, **kwargs):
        if json.loads(kwargs["payload"])["delete_intent"]:
            if committed:
                original(*args, **kwargs)
            raise primary
        return original(*args, **kwargs)

    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", lose_intent)
    with pytest.raises(error_type) as caught:
        owned.route.settle(Deadline.after(5))
    assert caught.value is primary
    assert len(mutations(owned)) == 1
    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", original)

    def check_intent(response):
        if response.request.method == "DELETE":
            assert json.loads(row(owned).payload)["delete_intent"] is True

    owned.transport.hook = check_intent
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 2


def test_unknown_delete_receipt_has_durable_intent_and_no_absence_shortcut(owned):
    owned.route.open(Deadline.after(5))
    owned.transport.overrides.append(("DELETE", OSError("lost receipt")))
    with pytest.raises(StateError):
        owned.route.settle(Deadline.after(5))
    assert json.loads(row(owned).payload)["delete_intent"] is True
    owned.transport.rule = None
    before = len(owned.transport.calls)
    assert not owned.route.settle(Deadline.after(5))
    assert len(owned.transport.calls) == before


@pytest.mark.parametrize("state", ["Failed", "Canceled"])
@pytest.mark.parametrize("absent", [False, True])
def test_terminal_failed_delete_retries_only_on_later_explicit_settle(owned, state, absent):
    owned.route.open(Deadline.after(5))
    owned.transport.overrides.extend(
        [
            ("DELETE", (200, {"Azure-AsyncOperation": DELETE_OP}, deny)),
            (DELETE_OP, (200, {}, {"status": state})),
        ]
    )

    def remove(response):
        if absent and response.request.url == DELETE_OP:
            owned.transport.rule = None

    owned.transport.hook = remove
    assert owned.route.settle(Deadline.after(5)) is absent
    assert len(mutations(owned)) == 2
    assert row(owned).state is (
        LifecycleObligationState.RESOLVED if absent else LifecycleObligationState.POSSIBLE_EFFECT
    )
    if not absent:
        assert owned.route.settle(Deadline.after(5))
        assert len(mutations(owned)) == 3


def failed_delete(owned, state: str) -> None:
    owned.transport.overrides.extend(
        [
            ("DELETE", (200, {"Azure-AsyncOperation": DELETE_OP}, deny)),
            (DELETE_OP, (200, {}, {"status": state})),
        ]
    )
    assert not owned.route.settle(Deadline.after(5))


@pytest.mark.parametrize("state", ["Failed", "Canceled"])
def test_each_explicit_settle_submits_at_most_one_cleanup_delete(owned, state):
    owned.route.open(Deadline.after(5))
    failed_delete(owned, state)
    first = row(owned)
    assert json.loads(first.payload)["delete_state"] == state
    assert len(mutations(owned)) == 2
    intents = []

    def inspect_new_intent(response):
        if response.request.method == "DELETE":
            selected = row(owned)
            payload = json.loads(selected.payload)
            assert selected.payload_revision > first.payload_revision
            assert payload["delete_intent"] is True
            assert payload["delete_ack"] is None and payload["delete_state"] is None
            intents.append(selected.payload_revision)

    owned.transport.hook = inspect_new_intent
    failed_delete(owned, state)
    assert len(mutations(owned)) == 3 and len(intents) == 1
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 4 and len(intents) == 2
    assert row(owned).state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize("state", ["Failed", "Canceled"])
@pytest.mark.parametrize("error_type", [OSError, Control])
def test_lost_retry_receipt_never_replayed_or_drained_by_absence(owned, state, error_type):
    owned.route.open(Deadline.after(5))
    failed_delete(owned, state)
    primary = error_type("lost retry receipt")
    owned.transport.overrides.append(("DELETE", primary))
    with pytest.raises(StateError if error_type is OSError else error_type) as caught:
        owned.route.settle(Deadline.after(5))
    if error_type is Control:
        assert caught.value is primary
    intent = json.loads(row(owned).payload)
    assert intent["delete_intent"] is True
    assert intent["delete_ack"] is None and intent["delete_state"] is None
    assert len(mutations(owned)) == 3
    owned.transport.rule = None
    before = len(owned.transport.calls)
    assert not owned.route.settle(Deadline.after(5))
    assert len(owned.transport.calls) == before
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("state", ["Failed", "Canceled"])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, Control])
def test_lost_retry_intent_reply_reconciles_only_known_unsent_attempt(owned, monkeypatch, state, committed, error_type):
    owned.route.open(Deadline.after(5))
    failed_delete(owned, state)
    previous = row(owned)
    original = owned.repository.publish_lifecycle_obligation_payload
    primary = error_type("lost next cleanup intent")

    def lose_intent(*args, **kwargs):
        candidate = json.loads(kwargs["payload"])
        assert candidate["delete_intent"] is True
        assert candidate["delete_ack"] is None and candidate["delete_state"] is None
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", lose_intent)
    with pytest.raises(error_type) as caught:
        owned.route.settle(Deadline.after(5))
    assert caught.value is primary and len(mutations(owned)) == 2
    current = row(owned)
    assert current.payload_revision == previous.payload_revision + int(committed)
    assert json.loads(current.payload)["delete_state"] == (None if committed else state)
    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", original)

    def inspect_intent(response):
        if response.request.method == "DELETE":
            intent = json.loads(row(owned).payload)
            assert intent["delete_ack"] is None and intent["delete_state"] is None

    owned.transport.hook = inspect_intent
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 3 and row(owned).state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize("state", ["Failed", "Canceled"])
@pytest.mark.parametrize("change", ["nic-guid", "nsg-guid", "association", "nonce", "content"])
def test_retry_revalidates_original_parents_and_exact_rule(owned, state, change):
    owned.route.open(Deadline.after(5))
    failed_delete(owned, state)
    if change in {"nic-guid", "nsg-guid"}:
        selected = owned.transport.nic if change == "nic-guid" else owned.transport.nsg
        selected["properties"]["resourceGuid"] = "31234567-89ab-0cde-0123-456789abcdef"
    elif change == "association":
        owned.transport.nic["properties"]["networkSecurityGroup"]["id"] = NSG + "foreign"
    elif change == "nonce":
        owned.transport.rule["properties"]["description"] += "foreign"
    else:
        owned.transport.rule["properties"]["destinationPortRange"] = "23"
    with pytest.raises(StateError):
        owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 2
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("state", ["Failed", "Canceled"])
def test_retry_requires_positive_original_retirement(owned, state):
    owned.route.open(Deadline.after(5))
    terminal_seen = False

    def retain_original(response):
        nonlocal terminal_seen
        if response.request.url == DELETE_OP:
            terminal_seen = True
        if terminal_seen and response.request.method == "GET" and "/securityRules/" in response.request.url:
            response.close_errors.extend([OSError("first close"), OSError("next close")])

    owned.transport.hook = retain_original
    failed_delete(owned, state)
    assert owned.route.cleanup_incomplete
    original = owned.transport.responses[-1]
    assert original.close_calls == 1
    before = len(owned.transport.calls)
    assert not owned.route.settle(Deadline.after(5))
    assert original.close_calls == 2 and len(owned.transport.calls) == before
    owned.transport.hook = None
    assert owned.route.settle(Deadline.after(5))
    assert original.close_calls == 3 and len(mutations(owned)) == 3


def test_successful_delete_with_present_rule_only_observes_delayed_absence(owned):
    owned.route.open(Deadline.after(5))
    owned.transport.overrides.append(("DELETE", (200, {}, deny)))
    assert not owned.route.settle(Deadline.after(5))
    assert not owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 2
    owned.transport.rule = None
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 2


@pytest.mark.parametrize("state", ["Failed", "Canceled"])
def test_newly_observed_terminal_failure_waits_for_another_settle_call(owned, state):
    owned.route.open(Deadline.after(5))
    primary = Control("interrupted monitor")
    owned.transport.overrides.extend(
        [
            ("DELETE", (200, {"Azure-AsyncOperation": DELETE_OP}, deny)),
            (DELETE_OP, (200, {}, primary)),
        ]
    )
    with pytest.raises(Control) as caught:
        owned.route.settle(Deadline.after(5))
    assert caught.value is primary and len(mutations(owned)) == 2
    owned.transport.overrides.append((DELETE_OP, (200, {}, {"status": state})))
    assert not owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 2
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 3


def test_passive_settle_bars_open_and_check_but_permits_further_cleanup(owned):
    assert owned.route.settle(Deadline.after(5))
    with pytest.raises(StateError):
        owned.route.open(Deadline.after(5))
    with pytest.raises(StateError):
        owned.route.check(Deadline.after(5), observed_vm=owned.vm)
    assert owned.route.close(Deadline.after(5))
    assert owned.route.settle(Deadline.after(5))
    assert owned.transport.calls == [] and row(owned) is None


def test_oversized_combined_receipt_is_not_truncated_or_followed(owned):
    huge = f"https://management.azure.com/subscriptions/{SUB}/operations/" + "x" * 6100
    owned.transport.overrides.append(("PUT", (201, {"Azure-AsyncOperation": huge, "Location": huge}, deny)))
    with pytest.raises(ValidationError):
        owned.route.open(Deadline.after(5))
    assert len(mutations(owned)) == 1
    with pytest.raises(StateError):
        owned.route.settle(Deadline.after(5))
    assert len(row(owned).payload) <= 8192


def test_takeover_fences_original_before_more_provider_work(owned):
    from agentworks.operations import OperationOwner

    owned.route.open(Deadline.after(5))
    successor = OperationOwner.recover(owned.repository, owned.owner.ownership, "b" * 32)
    before = len(owned.transport.calls)
    with pytest.raises(StateError):
        owned.route.settle(Deadline.after(5))
    assert len(owned.transport.calls) == before
    assert owned.route.close(Deadline.after(5))
    pending = successor.inspect_lifecycle_obligation(owned.route.obligation_id)
    assert pending is not None and pending.state is LifecycleObligationState.POSSIBLE_EFFECT


def test_rejected_check_does_not_retry_unretired_original(owned):
    owned.route.open(Deadline.after(5))

    def fail(response):
        if "/securityRules/" in response.request.url:
            response.close_errors.append(OSError("first close"))

    owned.transport.hook = fail
    owned.route.check(Deadline.after(5), observed_vm=owned.vm)
    original = owned.transport.responses[-1]
    assert original.close_calls == 1 and owned.route.cleanup_incomplete
    with pytest.raises(StateError):
        owned.route.check(Deadline.after(5), observed_vm=owned.vm)
    assert original.close_calls == 1 and owned.route.cleanup_incomplete
    owned.transport.hook = None
    assert owned.route.close(Deadline.after(5))
    assert original.close_calls == 2
    assert owned.route.settle(Deadline.after(5))
