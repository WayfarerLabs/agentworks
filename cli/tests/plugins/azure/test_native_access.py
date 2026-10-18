"""Concrete retained read slot, identity admission and terminal cleanup."""

from __future__ import annotations

import traceback
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.db import VMStatus
from agentworks.errors import LimitExceededError, NotFoundError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.azure import _passive_clients
from agentworks.plugins.azure._native_access import AzureOwnedReadAccess
from agentworks.plugins.azure.config import AzureAmbientAuth
from agentworks.plugins.azure.network import AzureError

RESOURCE = "/subscriptions/sub/resourceGroups/group:literal/providers/Microsoft.Compute/virtualMachines/vm"
UUID = "00112233-4455-6677-8899-aabbccddeeff"
NIC = "/subscriptions/sub/resourceGroups/group/providers/Microsoft.Network/networkInterfaces/nic"
PIP = "/subscriptions/sub/resourceGroups/group/providers/Microsoft.Network/publicIPAddresses/ip"


@pytest.fixture
def owned(monkeypatch):
    calls: list[Any] = []
    now = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    result = SimpleNamespace(
        calls=calls,
        now=now,
        compute_error=None,
        compute_close_error=None,
        network_close_error=None,
        nic_id=NIC,
        pip_ref=PIP,
        pip_id=PIP,
    )
    result.current = SimpleNamespace(
        id=RESOURCE,
        vm_id=UUID.upper(),
        instance_view=SimpleNamespace(statuses=[SimpleNamespace(code="PowerState/running")]),
        network_profile=SimpleNamespace(network_interfaces=[SimpleNamespace(id=NIC)]),
    )

    def probe(*scopes, **kwargs):
        calls.append("probe")
        assert result.access._auth._credential is result.credential
        return object()

    result.credential = SimpleNamespace(get_token=probe, close=lambda: calls.append("credential-close"))
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda **kwargs: result.credential)
    result.platform = SimpleNamespace(config=SimpleNamespace(auth=AzureAmbientAuth(mode="ambient")), site_name="site")
    result.ctx = SimpleNamespace(secret=lambda _: pytest.fail("ambient secret lookup"))
    result.vm = SimpleNamespace(name="vm", platform_metadata={"resource_id": RESOURCE, "vm_id": UUID})

    def new() -> AzureOwnedReadAccess:
        access = AzureOwnedReadAccess(result.vm, result.platform, result.ctx)
        result.access = access
        return access

    result.new = new
    new()

    def get(group, name, **options):
        assert result.access._read_client is result.compute
        assert (group, name) == ("group:literal", "vm")
        assert (
            options["retry_total"] == options["retry_connect"] == options["retry_read"] == options["retry_status"] == 0
        )
        assert 0 < options["connection_timeout"] == options["read_timeout"] <= 10
        calls.append("vm-get")
        if result.compute_error is not None:
            raise result.compute_error
        return result.current

    def close_compute():
        calls.append("compute-close")
        if result.compute_close_error is not None:
            raise result.compute_close_error

    result.compute = SimpleNamespace(virtual_machines=SimpleNamespace(get=get), close=close_compute)

    def linked_get(kind: str, group: str, name: str, **options: object) -> SimpleNamespace:
        assert result.access._read_client is result.network
        calls.append(kind + "-get")
        if kind == "nic":
            return SimpleNamespace(
                id=result.nic_id,
                ip_configurations=[SimpleNamespace(public_ip_address=SimpleNamespace(id=result.pip_ref))],
            )
        return SimpleNamespace(id=result.pip_id, properties=SimpleNamespace(ip_address="198.51.100.2"))

    def close_network():
        calls.append("network-close")
        if result.network_close_error is not None:
            raise result.network_close_error

    result.network = SimpleNamespace(
        network_interfaces=SimpleNamespace(get=lambda *a, **k: linked_get("nic", *a, **k)),
        public_ip_addresses=SimpleNamespace(get=lambda *a, **k: linked_get("pip", *a, **k)),
        close=close_network,
    )

    def construct_compute(credential, subscription):
        assert credential is result.credential and subscription == "sub"
        calls.append("compute")
        return result.compute

    def construct_network(credential, subscription):
        assert credential is result.credential and subscription == "sub"
        assert result.access._read_client is None
        calls.append("network")
        return result.network

    monkeypatch.setattr(_passive_clients, "compute_read_client", construct_compute)
    monkeypatch.setattr(_passive_clients, "network_read_client", construct_network)
    return result


def test_passive_metadata_copy_and_opaque_locator(owned):
    assert owned.calls == [] and not owned.access.cleanup_incomplete
    owned.vm.platform_metadata["vm_id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    locator = owned.access.observe_locator(Deadline.after(10))
    assert locator.token == f"azure-vm:v2:{UUID}:{RESOURCE}"
    assert owned.calls == ["probe", "compute", "vm-get", "compute-close"]
    assert owned.access.close(Deadline.after(10))
    assert owned.calls[-1] == "credential-close"


def test_credential_borrow_requires_prior_admission_and_positive_client_retirement(owned):
    with pytest.raises(StateError):
        owned.access.borrow_credential(Deadline.after(10))
    assert owned.calls == []
    assert owned.access.observe_vm(Deadline.after(10)) is owned.current
    before = list(owned.calls)
    assert owned.access.borrow_credential(Deadline.after(10)) is owned.credential
    assert owned.calls == before
    owned.compute_close_error = RuntimeError("offline close failure")
    owned.access.observe_vm(Deadline.after(10))
    with pytest.raises(StateError):
        owned.access.borrow_credential(Deadline.after(10))
    owned.compute_close_error = None
    assert owned.access.close(Deadline.after(10))
    with pytest.raises(StateError):
        owned.access.borrow_credential(Deadline.after(10))


@pytest.mark.parametrize("key", ["resource_id", "vm_id"])
def test_missing_historical_identity_refuses_before_work(owned, key):
    del owned.vm.platform_metadata[key]
    with pytest.raises(StateError):
        owned.new()
    assert owned.calls == [] and key not in owned.vm.platform_metadata


@pytest.mark.parametrize("observed", [RESOURCE.upper(), RESOURCE.lower(), RESOURCE.swapcase()])
def test_observed_vm_casing_keeps_frozen_locator_and_metadata(owned, observed):
    owned.current.id = observed
    assert owned.access.observe_locator(Deadline.after(10)).token == f"azure-vm:v2:{UUID}:{RESOURCE}"
    assert owned.vm.platform_metadata == {"resource_id": RESOURCE, "vm_id": UUID}
    assert owned.access.close(Deadline.after(10))


@pytest.mark.parametrize("left,right", [("Straße", "STRASSE"), ("é", "e\u0301"), ("É", "é")])
def test_unicode_names_do_not_invent_provider_equivalence(owned, monkeypatch, left, right):
    owned.vm.platform_metadata["resource_id"] = RESOURCE.removesuffix("vm") + left
    access = owned.new()
    owned.current.id = RESOURCE.removesuffix("vm") + right
    monkeypatch.setattr(owned.compute.virtual_machines, "get", lambda *a, **k: owned.current)
    with pytest.raises(StateError):
        access.observe_locator(Deadline.after(10))
    assert "network" not in owned.calls
    assert access.close(Deadline.after(10))


def test_linked_id_casing_accepts_without_changing_stored_identity(owned):
    owned.current.network_profile.network_interfaces[0].id = NIC.upper()
    owned.nic_id = NIC.lower()
    owned.pip_ref = PIP.swapcase()
    owned.pip_id = PIP.upper()
    assert owned.access.observe_public_endpoint(Deadline.after(10)) == "198.51.100.2"
    assert owned.calls.count("nic-get") == owned.calls.count("pip-get") == 1
    assert owned.vm.platform_metadata == {"resource_id": RESOURCE, "vm_id": UUID}
    assert owned.access.close(Deadline.after(10))


@pytest.mark.parametrize("field", ["nic_ref", "nic_id", "pip_ref", "pip_id"])
@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.replace("/sub/", "/foreign/"),
        lambda p: p + "/extra",
        lambda p: p + "\x00",
        lambda p: p + "\x1f",
        lambda p: p + "\x7f",
        lambda p: p + "\ud800",
        lambda p: p + "x" * 2048,
        lambda p: p.replace("/group/", "/../"),
        lambda p: p.replace("/group/", "/./"),
        lambda p: p.replace("/group/", "//"),
        lambda p: p.replace("Microsoft.Network", "Microsoft.Compute"),
    ],
)
def test_invalid_linked_identity_stops_before_subsequent_fetch(owned, field, mutation):
    original = NIC if field.startswith("nic") else PIP
    value = mutation(original)
    if field == "nic_ref":
        owned.current.network_profile.network_interfaces[0].id = value
    else:
        setattr(owned, field, value)
    with pytest.raises(StateError):
        owned.access.observe_public_endpoint(Deadline.after(10))
    assert owned.calls.count("nic-get") == int(field != "nic_ref")
    assert owned.calls.count("pip-get") == int(field == "pip_id")
    assert owned.access.close(Deadline.after(10))


@pytest.mark.parametrize("field", ["nic_id", "pip_id"])
@pytest.mark.parametrize("component", ["group", "name"])
def test_changed_linked_resource_refuses(owned, field, component):
    original = NIC if field == "nic_id" else PIP
    changed = original.replace("/group/", "/foreign/") if component == "group" else original + "-foreign"
    setattr(owned, field, changed)
    with pytest.raises(StateError):
        owned.access.observe_public_endpoint(Deadline.after(10))
    assert owned.calls.count("pip-get") == int(field == "pip_id")
    assert owned.access.close(Deadline.after(10))


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", RESOURCE + "-foreign"),
        ("id", RESOURCE.replace("/sub/", "/foreign/")),
        ("id", RESOURCE.replace("/group:literal/", "/foreign/")),
        ("id", RESOURCE.replace("Microsoft.Compute", "Microsoft.Network")),
        ("id", RESOURCE + "/extra"),
        ("id", RESOURCE + "\x00"),
        ("id", RESOURCE + "\x1f"),
        ("id", RESOURCE + "\x7f"),
        ("id", RESOURCE + "\ud800"),
        ("id", RESOURCE + "x" * 2048),
        ("id", RESOURCE.replace("/group:literal/", "/../")),
        ("id", RESOURCE.replace("/group:literal/", "/./")),
        ("id", RESOURCE.replace("/group:literal/", "//")),
        ("id", None),
        ("vm_id", None),
        ("vm_id", UUID[:-1] + "0"),
    ],
)
def test_path_and_uuid_must_both_match(owned, field, value):
    setattr(owned.current, field, value)
    with pytest.raises(StateError):
        owned.access.observe_public_endpoint(Deadline.after(10))
    assert "network" not in owned.calls and owned.calls[-1] == "compute-close"
    assert owned.vm.platform_metadata == {"resource_id": RESOURCE, "vm_id": UUID}
    assert owned.access.close(Deadline.after(10))


def test_linked_literal_url_syntax_is_not_decoded(owned):
    literal = PIP.removesuffix("ip") + "ip%2F:?#é"
    owned.pip_ref = owned.pip_id = literal
    assert owned.access.observe_public_endpoint(Deadline.after(10)) == "198.51.100.2"
    owned.pip_id = literal.replace("%2F", "/")
    with pytest.raises(StateError):
        owned.access.observe_public_endpoint(Deadline.after(10))
    assert owned.access.close(Deadline.after(10))


@pytest.mark.parametrize(
    "code,status", [("running", VMStatus.RUNNING), ("stopped", VMStatus.STOPPED), ("deallocated", VMStatus.DEALLOCATED)]
)
def test_power_requires_one_stable_code(owned, code, status):
    owned.current.instance_view.statuses = [SimpleNamespace(code="PowerState/" + code)]
    assert owned.access.observe_power(Deadline.after(10)) is status
    for statuses in [
        None,
        [],
        [SimpleNamespace(code=None)],
        [SimpleNamespace(code="PowerState/starting")],
        [SimpleNamespace(code="PowerState/running"), SimpleNamespace(code="PowerState/stopped")],
    ]:
        owned.current.instance_view.statuses = statuses
        assert owned.access.observe_power(Deadline.after(10)) is VMStatus.UNKNOWN
    assert owned.access.close(Deadline.after(10))


@pytest.mark.parametrize("stage", ["construct", "get", "close"])
def test_original_deadline_refuses_setup_dispatch_or_cleanup_late_success(owned, monkeypatch, stage):
    target, method = {
        "construct": (_passive_clients, "compute_read_client"),
        "get": (owned.compute.virtual_machines, "get"),
        "close": (owned.compute, "close"),
    }[stage]
    original = getattr(target, method)

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        owned.now[0] = 111.0
        return result

    monkeypatch.setattr(target, method, late)
    with pytest.raises(LimitExceededError):
        owned.access.observe_power(Deadline.after(10))
    assert owned.calls.count("vm-get") == int(stage != "construct")
    assert owned.calls[-1] == "compute-close" and owned.access._read_client is None
    assert owned.access.close(Deadline.after(10))


@pytest.mark.parametrize("method", ["observe_power", "observe_locator", "observe_public_endpoint"])
def test_either_uncertain_slot_blocks_every_next_observation(owned, method):
    owned.compute_close_error = RuntimeError("offline compute close failure")
    assert owned.access.observe_power(Deadline.after(10)) is VMStatus.RUNNING
    assert owned.access.cleanup_incomplete and owned.access._read_client is owned.compute
    before = list(owned.calls)
    with pytest.raises(StateError):
        getattr(owned.access, method)(Deadline.after(10))
    assert owned.calls == before
    owned.compute_close_error = None
    assert owned.access.close(Deadline.after(10))
    if method == "observe_public_endpoint":
        owned.new()
        owned.compute_close_error = RuntimeError("offline compute close failure")
        before_count = len(owned.calls)
        with pytest.raises(StateError):
            owned.access.observe_public_endpoint(Deadline.after(10))
        assert owned.calls[before_count:] == ["probe", "compute", "vm-get", "compute-close"]
        assert owned.access._read_client is owned.compute
        owned.compute_close_error = None
        assert owned.access.close(Deadline.after(10))
    owned.new()
    owned.network_close_error = RuntimeError("offline network close failure")
    assert owned.access.observe_public_endpoint(Deadline.after(10)) == "198.51.100.2"
    assert owned.access._read_client is owned.network and owned.access.cleanup_incomplete
    before = list(owned.calls)
    with pytest.raises(StateError):
        getattr(owned.access, method)(Deadline.after(10))
    assert owned.calls == before
    assert not owned.access.close(Deadline.after(10))
    assert "credential-close" not in owned.calls[len(before) :]
    owned.network_close_error = None
    assert owned.access.close(Deadline.after(10))
    assert owned.calls[-2:] == ["network-close", "credential-close"]


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_original_control_survives_cleanup_and_bookkeeping_controls(owned, monkeypatch, exception_type):
    primary = exception_type("offline dispatch control")
    secondary = SystemExit("offline cleanup control") if exception_type is KeyboardInterrupt else KeyboardInterrupt()
    owned.compute_error, owned.compute_close_error = primary, secondary
    with pytest.raises(exception_type) as caught:
        owned.access.observe_power(Deadline.after(10))
    assert caught.value is primary and owned.access._read_client is owned.compute
    owned.compute_error = owned.compute_close_error = None
    assert owned.access.close(Deadline.after(10))
    owned.new()
    original_close = owned.access._close_read_client

    def interrupted_bookkeeping():
        original_close()
        raise primary

    monkeypatch.setattr(owned.access, "_close_read_client", interrupted_bookkeeping)
    original_lock = owned.access._lock

    class ReleaseControl:
        def acquire(self, **kwargs):
            return original_lock.acquire(**kwargs)

        def release(self):
            original_lock.release()
            raise secondary

    monkeypatch.setattr(owned.access, "_lock", ReleaseControl())
    with pytest.raises(exception_type) as caught:
        owned.access.observe_power(Deadline.after(10))
    assert caught.value is primary
    monkeypatch.setattr(owned.access, "_close_read_client", original_close)
    monkeypatch.setattr(owned.access, "_lock", original_lock)
    assert owned.access.close(Deadline.after(10))


def test_finite_lock_and_terminal_closure(owned):
    owned.access._lock.acquire()
    try:
        with pytest.raises(StateError):
            owned.access.observe_locator(Deadline.after(0.01))
    finally:
        owned.access._lock.release()
    assert owned.calls == []
    owned.compute_close_error = RuntimeError("offline close failure")
    assert owned.access.observe_power(Deadline.after(10)) is VMStatus.RUNNING
    assert owned.access._read_client is owned.compute
    before_count = len(owned.calls)
    assert not owned.access.close(Deadline.after(10))
    assert owned.calls[before_count:] == ["compute-close"]
    assert owned.access._read_client is owned.compute
    owned.compute_close_error = None
    assert owned.access.close(Deadline.after(10))
    assert owned.calls[-2:] == ["compute-close", "credential-close"]
    before = list(owned.calls)
    with pytest.raises(StateError):
        owned.access.observe_power(Deadline.after(10))
    assert owned.calls == before
    assert owned.access.close(Deadline.after(10)) and owned.calls == before


@pytest.mark.parametrize("stage", ["compute", "nic", "pip"])
@pytest.mark.parametrize("exception_type", [ValidationError, NotFoundError, StateError, KeyboardInterrupt, SystemExit])
def test_read_domain_and_control_identity_survive_linked_translation_and_close_failure(
    owned, monkeypatch, stage, exception_type
):
    primary = exception_type("offline core or control failure")

    def fail(*args, **kwargs):
        raise primary

    if stage == "compute":
        monkeypatch.setattr(owned.compute.virtual_machines, "get", fail)
        owned.compute_close_error = RuntimeError("offline close failure")
    else:
        operations = owned.network.network_interfaces if stage == "nic" else owned.network.public_ip_addresses
        monkeypatch.setattr(operations, "get", fail)
        owned.network_close_error = RuntimeError("offline close failure")
    with pytest.raises(exception_type) as caught:
        if stage == "compute":
            owned.access.observe_power(Deadline.after(10))
        else:
            owned.access.observe_public_endpoint(Deadline.after(10))
    assert caught.value is primary
    assert primary.__cause__ is None and primary.__context__ is None
    assert owned.access.cleanup_incomplete and owned.access._auth._credential is owned.credential
    if stage == "compute":
        assert owned.access._read_client is owned.compute
    else:
        assert owned.access._read_client is owned.network
    before = list(owned.calls)
    with pytest.raises(StateError):
        owned.access.observe_power(Deadline.after(10))
    assert owned.calls == before
    owned.compute_close_error = owned.network_close_error = None
    assert owned.access.close(Deadline.after(10))
    assert owned.calls[-1] == "credential-close"


@pytest.mark.parametrize("stage", ["compute", "network"])
def test_read_constructor_failure_is_detached_without_an_unreturned_original(owned, monkeypatch, stage):
    canary = "offline-constructor-diagnostic-canary"

    def fail(*args, **kwargs):
        raise ValueError(canary)

    monkeypatch.setattr(_passive_clients, stage + "_read_client", fail)
    with pytest.raises(AzureError) as caught:
        if stage == "compute":
            owned.access.observe_power(Deadline.after(10))
        else:
            owned.access.observe_public_endpoint(Deadline.after(10))
    assert caught.value.detail == "ValueError"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert canary not in "".join(traceback.format_exception(caught.value))
    assert owned.access._read_client is None
    assert owned.access._auth._credential is owned.credential
    assert owned.access.close(Deadline.after(10)) and owned.calls[-1] == "credential-close"
