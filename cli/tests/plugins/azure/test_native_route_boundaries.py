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
