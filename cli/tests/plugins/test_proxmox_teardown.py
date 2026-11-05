"""Only exact provider-confirmed absence permits successful backend deletion."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.plugins.proxmox.api import ProxmoxAPI, ProxmoxAPIError, ProxmoxVMNotFoundError
from tests.plugins.test_proxmox_create_interrupt import _NEWID, _platform_with_fake
from tests.plugins.test_proxmox_create_interrupt import TestDeleteOp as _DeleteOp


def _api() -> ProxmoxAPI:
    return ProxmoxAPI("https://pve.fixture.invalid", "fixture@pam!api", "fixture-secret")


def _http_error(code: int, payload: object, *, structured: bool = True) -> urllib.error.HTTPError:
    body = json.dumps(payload).encode() if structured else str(payload).encode()
    return urllib.error.HTTPError(
        "https://pve.fixture.invalid/api2/json/nodes/pve1/qemu/100/status/current",
        code,
        "provider failure",
        None,
        BytesIO(body),
    )


@pytest.mark.parametrize("suffix", ["", "\n"])
def test_exact_structured_missing_configuration_is_typed(monkeypatch, suffix):
    message = "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist" + suffix
    failure = _http_error(500, {"data": None, "message": message})
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(side_effect=failure))
    with pytest.raises(ProxmoxVMNotFoundError) as caught:
        _api().vm_status("pve1", 100)
    assert caught.value.code == 500
    assert isinstance(caught.value.__cause__, ProxmoxAPIError)
    assert caught.value.__cause__.__cause__ is failure


@pytest.mark.parametrize(
    "code,payload,structured",
    [
        (401, {"data": None, "message": "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist"}, True),
        (403, {"data": None, "message": "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist"}, True),
        (404, {"data": None, "message": "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist"}, True),
        (500, {"data": None, "message": "Configuration file 'nodes/pve2/qemu-server/100.conf' does not exist"}, True),
        (500, {"data": None, "message": "Configuration file 'nodes/pve1/qemu-server/101.conf' does not exist"}, True),
        (500, {"data": None, "message": "VM is locked (backup)"}, True),
        (
            500,
            {"data": None, "message": "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist extra"},
            True,
        ),
        (500, {"message": "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist"}, True),
        (500, {"data": False, "message": "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist"}, True),
        (
            500,
            {
                "data": None,
                "errors": [],
                "message": "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist",
            },
            True,
        ),
        (500, "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist", False),
        (500, [{"message": "Configuration file 'nodes/pve1/qemu-server/100.conf' does not exist"}], True),
    ],
)
def test_other_errors_do_not_establish_absence(monkeypatch, code, payload, structured):
    failure = _http_error(code, payload, structured=structured)
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(side_effect=failure))
    with pytest.raises(ProxmoxAPIError) as caught:
        _api().vm_status("pve1", 100)
    assert type(caught.value) is ProxmoxAPIError
    assert caught.value.code == code
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("value", [None, [], {}, {"status": "unknown"}, {"status": False}])
def test_invalid_success_response_is_not_an_absence_proof(monkeypatch, value):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({"data": value}).encode()
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=response))
    with pytest.raises(ProxmoxAPIError) as caught:
        _api().vm_status("pve1", 100)
    assert type(caught.value) is ProxmoxAPIError


@pytest.mark.parametrize("stage", ["status", "stop", "stop-task", "delete", "delete-task", "confirm"])
def test_direct_delete_propagates_failures_instead_of_claiming_success(monkeypatch, stage):
    platform, fake = _platform_with_fake(monkeypatch)
    fake.running = True
    failure = ProxmoxAPIError("fixture API failure")
    if stage == "status":
        fake.status_errors = [failure]
    elif stage == "stop":
        fake.stop_error = failure
    elif stage == "stop-task":
        fake.wait_errors["UPID:pve1:stop"] = [failure]
    elif stage == "delete":
        fake.delete_error = failure
    elif stage == "delete-task":
        fake.wait_errors["UPID:pve1:delete"] = [failure]
    else:
        fake.status_errors = [None, failure]
    with pytest.raises(ProxmoxAPIError) as caught:
        platform.delete(_DeleteOp._vm_row(), RunContext())
    assert caught.value is failure
    if stage not in ("delete-task", "confirm"):
        assert not fake.vm_deleted
    assert all(call[2] == _NEWID for call in fake.calls if call[0] in ("stop_vm", "delete_vm"))


def test_successful_task_without_absence_confirmation_still_fails(monkeypatch):
    platform, fake = _platform_with_fake(monkeypatch)

    def report_success_without_removal(node, vmid):
        fake.calls.append(("delete_vm", node, vmid))
        return "UPID:pve1:delete"

    monkeypatch.setattr(fake, "delete_vm", report_success_without_removal)
    with pytest.raises(ProxmoxAPIError):
        platform.delete(_DeleteOp._vm_row(), RunContext())
    assert not fake.vm_deleted
