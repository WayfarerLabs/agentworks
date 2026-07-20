"""Azure credential and SDK-client caching: one credential build (one
live ``get_token`` probe) per platform instance, reused across ops, with
the browser-fallback decision preserved and paid once (perf fix for the
fresh-credential-per-method-call cost).

Azure is a real dependency in the test env, so the fakes are installed
by patching the SDK symbols the azure_vm module imports function-locally
(``monkeypatch.setattr`` on the real modules), matching how the rest of
the suite stubs Azure without a live subscription."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.azure_vm import AzureVMPlatform
from agentworks.db import VMStatus

if TYPE_CHECKING:
    from agentworks.db import VMRow

_RESOURCE_ID = (
    "/subscriptions/sub-A/resourceGroups/rg1/providers/"
    "Microsoft.Compute/virtualMachines/vm1"
)
_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus"}


def _fake_vm(resource_id: str = _RESOURCE_ID) -> Any:
    """A stand-in for a VMRow carrying just what the power ops read."""
    return SimpleNamespace(
        name="vm1",
        admin_username="agentworks",
        platform_metadata={"resource_id": resource_id},
    )


class _Poller:
    """A begin_* long-running-operation stub: ``.result()`` yields a value."""

    def __init__(self, value: object) -> None:
        self._value = value

    def result(self) -> object:
        return self._value


class _FakeVMs:
    def instance_view(self, rg: str, name: str) -> object:
        return SimpleNamespace(statuses=[SimpleNamespace(code="PowerState/running")])

    def begin_start(self, rg: str, name: str) -> _Poller:
        return _Poller(None)

    def get(self, rg: str, name: str, **_kw: object) -> object:
        return SimpleNamespace(location="eastus")


class _FakePublicIps:
    def begin_create_or_update(self, rg: str, name: str, params: object) -> _Poller:
        return _Poller(SimpleNamespace(ip_address="203.0.113.5", id="/pip/id"))


class _FakeNics:
    def get(self, rg: str, name: str) -> object:
        return SimpleNamespace(ip_configurations=[SimpleNamespace(public_ip_address=None)])

    def begin_create_or_update(self, rg: str, name: str, nic: object) -> _Poller:
        return _Poller(None)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch, *, auth_fails: bool = False
) -> dict[str, int]:
    """Patch the Azure SDK symbols azure_vm imports, returning a counter
    dict the tests assert on. ``auth_fails`` drives the DefaultAzureCredential
    probe down the ClientAuthenticationError browser-fallback path."""
    from azure.core.exceptions import ClientAuthenticationError

    counters = {
        "cred_build": 0,
        "get_token": 0,
        "browser_build": 0,
        "compute_build": 0,
        "network_build": 0,
        "resource_build": 0,
    }

    class _FakeDefaultCred:
        def __init__(self) -> None:
            counters["cred_build"] += 1

        def get_token(self, *_scopes: str, **_kw: object) -> object:
            counters["get_token"] += 1
            if auth_fails:
                raise ClientAuthenticationError("no credentials in the chain")
            return SimpleNamespace(token="tok", expires_on=0)

    class _FakeBrowserCred:
        def __init__(self) -> None:
            counters["browser_build"] += 1

    class _FakeCompute:
        def __init__(self, credential: object, subscription_id: str) -> None:
            counters["compute_build"] += 1
            self.subscription_id = subscription_id
            self.virtual_machines = _FakeVMs()

    class _FakeNetwork:
        def __init__(self, credential: object, subscription_id: str) -> None:
            counters["network_build"] += 1
            self.subscription_id = subscription_id
            self.public_ip_addresses = _FakePublicIps()
            self.network_interfaces = _FakeNics()

    class _FakeResource:
        def __init__(self, credential: object, subscription_id: str) -> None:
            counters["resource_build"] += 1
            self.subscription_id = subscription_id

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _FakeDefaultCred)
    monkeypatch.setattr("azure.identity.InteractiveBrowserCredential", _FakeBrowserCred)
    monkeypatch.setattr("azure.mgmt.compute.ComputeManagementClient", _FakeCompute)
    monkeypatch.setattr("azure.mgmt.network.NetworkManagementClient", _FakeNetwork)
    monkeypatch.setattr(
        "azure.mgmt.resource.resources.ResourceManagementClient", _FakeResource
    )
    return counters


def _platform() -> AzureVMPlatform:
    return AzureVMPlatform("az-site", dict(_CONFIG))


class TestCredentialCaching:
    def test_one_build_across_ops_and_per_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple ops on one instance (status + start + attach, which
        together touch both SDK clients and the credential) build the
        credential and each client exactly once; a second instance builds
        its own rather than reusing the first's."""
        counters = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        platform = _platform()
        assert platform.status(vm, RunContext()) is VMStatus.RUNNING
        platform.start(vm, RunContext())
        assert platform.attach_public_ip(vm) == "203.0.113.5"

        # One credential build (one live probe), reused across all three ops;
        # one build of each client (compute is shared by status/start and the
        # attach location lookup, network is built by attach).
        assert counters["cred_build"] == 1
        assert counters["get_token"] == 1
        assert counters["compute_build"] == 1
        assert counters["network_build"] == 1

        # A second instance is a fresh cache: it builds its own credential.
        second = _platform()
        assert second.status(vm, RunContext()) is VMStatus.RUNNING
        assert counters["cred_build"] == 2
        assert counters["compute_build"] == 2

    def test_probe_runs_once_per_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The eager get_token probe fires at most once per instance: it
        is the validation step of the first credential build, not a
        per-op cost, and the accessor hands back the same cached object."""
        counters = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]
        platform = _platform()

        first = platform._get_credential()
        again = platform._get_credential()
        platform.status(vm, RunContext())
        platform.start(vm, RunContext())

        assert first is again
        assert counters["get_token"] == 1
        assert counters["cred_build"] == 1

    def test_second_subscription_builds_own_clients_not_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One instance serving VMs whose stored resource IDs name
        different subscriptions (a site whose subscription changed after
        older VMs were created) builds a client per subscription, never
        reusing the first subscription's client against the second, while
        the subscription-independent credential still builds exactly once."""
        counters = _install_fakes(monkeypatch)
        vm_a: VMRow = _fake_vm()  # type: ignore[assignment]
        vm_b: VMRow = _fake_vm(  # type: ignore[assignment]
            _RESOURCE_ID.replace("sub-A", "sub-B")
        )
        platform = _platform()

        assert platform.status(vm_a, RunContext()) is VMStatus.RUNNING
        assert platform.status(vm_b, RunContext()) is VMStatus.RUNNING
        platform.attach_public_ip(vm_a)
        platform.attach_public_ip(vm_b)

        # One compute and one network client per subscription, keyed by
        # subscription (the accessor passes the key to the constructor,
        # so the keys also pin what each client was bound to).
        assert counters["compute_build"] == 2
        assert counters["network_build"] == 2
        assert set(platform._compute_cached) == {"sub-A", "sub-B"}
        assert set(platform._network_cached) == {"sub-A", "sub-B"}

        # Repeats hit the per-subscription cache; the credential (and its
        # probe) built exactly once for the whole instance.
        assert platform.status(vm_a, RunContext()) is VMStatus.RUNNING
        assert platform.status(vm_b, RunContext()) is VMStatus.RUNNING
        assert counters["compute_build"] == 2
        assert counters["cred_build"] == 1
        assert counters["get_token"] == 1

    def test_resource_client_caches_per_subscription(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resource-management client added by #193 (runup's read-only
        resource-group existence check) caches exactly like compute/network:
        built once on first need for a subscription and reused on repeat, a
        second subscription builds its own client rather than reusing the
        first's, while the subscription-independent credential still builds
        exactly once."""
        counters = _install_fakes(monkeypatch)
        platform = _platform()
        az_a = SimpleNamespace(subscription_id="sub-A")
        az_b = SimpleNamespace(subscription_id="sub-B")

        # First need for sub-A builds one client; the repeat reuses the cache.
        first = platform._resource_client(az_a)
        assert platform._resource_client(az_a) is first
        assert counters["resource_build"] == 1

        # A second subscription builds its own, keyed by subscription.
        second = platform._resource_client(az_b)
        assert second is not first
        assert counters["resource_build"] == 2
        assert set(platform._resource_cached) == {"sub-A", "sub-B"}

        # The subscription-independent credential (and its probe) built once
        # across both resource clients.
        assert counters["cred_build"] == 1
        assert counters["get_token"] == 1

    def test_browser_fallback_preserved_and_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the DefaultAzureCredential probe raises
        ClientAuthenticationError, the decision lands on the interactive
        browser credential, and it is that credential which is cached: the
        fallback is decided once (one probe, one browser build) and reused,
        never re-decided per op."""
        counters = _install_fakes(monkeypatch, auth_fails=True)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]
        platform = _platform()

        cred = platform._get_credential()
        # The browser credential is the decision, and it is what got cached.
        assert counters["browser_build"] == 1
        assert platform._get_credential() is cred

        # Ops reuse the cached browser credential: no re-probe, no re-decide.
        platform.status(vm, RunContext())
        assert counters["cred_build"] == 1
        assert counters["get_token"] == 1
        assert counters["browser_build"] == 1
