"""Concrete read model and credential dependency seams for full Azure access."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agentworks.errors import StateError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.azure._native_access import AzureOwnedReadAccess
from tests.plugins.azure.test_native_access_sdk import (
    Credential,
    ScriptedTransport,
    bind_clients,
    deny,
    platform,
    vm_payload,
    vm_row,
)
from tests.plugins.azure.test_native_access_sdk import offline_native_guard as offline_native_guard


def test_vm_model_requests_fresh_instance_view_without_reprobing_borrow(monkeypatch):
    credential = Credential()
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda **kwargs: credential)
    transport = ScriptedTransport([(200, vm_payload(), {}), (200, vm_payload(), {})])
    bind_clients(monkeypatch, transport)
    reader = AzureOwnedReadAccess(vm_row(), platform(), SimpleNamespace(secret=deny))
    first = reader.observe_vm(Deadline.after(10))
    second = reader.observe_vm(Deadline.after(10))
    assert first is not second and first.instance_view.statuses[0].code == "PowerState/running"
    for method, url, _headers, _options in transport.calls:
        assert method == "GET" and parse_qs(urlsplit(url).query)["$expand"] == ["instanceView"]
    calls = list(credential.calls)
    assert reader.borrow_credential(Deadline.after(10)) is credential
    assert credential.calls == calls
    assert reader.close(Deadline.after(10)) and credential.closed == 1


@pytest.mark.parametrize("control", [False, True])
def test_read_original_can_retire_before_borrowed_credential(monkeypatch, control):
    credential = Credential()
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda **kwargs: credential)
    transport = ScriptedTransport([(200, vm_payload(), {})])
    error = KeyboardInterrupt("synthetic read close") if control else RuntimeError("synthetic read close")
    transport.close_error = error
    bind_clients(monkeypatch, transport)
    reader = AzureOwnedReadAccess(vm_row(), platform(), SimpleNamespace(secret=deny))
    if control:
        with pytest.raises(KeyboardInterrupt) as caught:
            reader.observe_vm(Deadline.after(10))
        assert caught.value is error
        with pytest.raises(KeyboardInterrupt) as caught:
            reader.close_read_client(Deadline.after(10))
        assert caught.value is error
    else:
        reader.observe_vm(Deadline.after(10))
        assert not reader.close_read_client(Deadline.after(10))
    assert reader.cleanup_incomplete and credential.closed == 0
    with pytest.raises(StateError):
        reader.borrow_credential(Deadline.after(10))
    transport.close_error = None
    assert reader.close_read_client(Deadline.after(10))
    assert credential.closed == 0
    with pytest.raises(StateError):
        reader.borrow_credential(Deadline.after(10))
    with pytest.raises(StateError):
        reader.observe_vm(Deadline.after(10))
    assert len(transport.calls) == 1
    assert reader.close(Deadline.after(10)) and credential.closed == 1
