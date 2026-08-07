"""Which Azure credential a site gets, and the caching around it.

Two concerns, one subject:

- SELECTION (issue #199): a site declaring a ``service_principal`` gets
  exactly that credential, built from the client secret the RunContext
  delivers, and NEVER falls back to the ambient chain or a browser
  prompt when it fails. A site declaring none gets today's ambient
  ``DefaultAzureCredential`` with its browser fallback, unchanged.
- CACHING: one credential build (one live ``get_token`` probe) per
  platform instance on either path, reused across ops, with the
  browser-fallback decision preserved and paid once (perf fix for the
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
from agentworks.db import VMStatus
from agentworks.errors import ConfigError
from agentworks.plugins.azure.network import AzureError
from agentworks.plugins.azure.platform import AzureVMPlatform

#: The well-known default the model's SecretRef template names.
DEFAULT_CLIENT_SECRET = "azure-client-secret"

if TYPE_CHECKING:
    from agentworks.db import VMRow

_RESOURCE_ID = "/subscriptions/sub-A/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"
_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus"}
_SP = {"tenant_id": "tenant-A", "client_id": "client-A", "secret": "az-sp"}


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
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_fails: bool = False,
    sp_auth_fails: bool = False,
) -> dict[str, int]:
    """Patch the Azure SDK symbols azure_vm imports, returning a counter
    dict the tests assert on. ``auth_fails`` drives the DefaultAzureCredential
    probe down the ClientAuthenticationError browser-fallback path;
    ``sp_auth_fails`` makes the ClientSecretCredential probe reject (a bad
    or expired client secret)."""
    from azure.core.exceptions import ClientAuthenticationError

    counters = {
        "cred_build": 0,
        "get_token": 0,
        "browser_build": 0,
        "sp_build": 0,
        "sp_get_token": 0,
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

    class _FakeClientSecretCred:
        def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
            counters["sp_build"] += 1
            # Appended to the module-level ``_sp_args`` (cleared per test
            # by the autouse fixture below) so a test can pin exactly
            # what the platform passed the SDK: the config's identifiers
            # and the DELIVERED secret value, never a name.
            _sp_args.append((tenant_id, client_id, client_secret))
            # The real ClientSecretCredential validates eagerly and raises
            # a bare ValueError (not ClientAuthenticationError) on an empty
            # secret or tenant, BEFORE any token request. Mirrored here so
            # the reachable "a backend resolved the secret to an empty
            # string" case is exercised against the same exception type the
            # SDK actually raises.
            if not client_secret:
                raise ValueError("secret should be a Microsoft Entra application's client secret")
            if not tenant_id:
                raise ValueError("tenant_id should be a Microsoft Entra tenant's id")

        def get_token(self, *_scopes: str, **_kw: object) -> object:
            counters["sp_get_token"] += 1
            if sp_auth_fails:
                raise ClientAuthenticationError("AADSTS7000215: Invalid client secret provided")
            return SimpleNamespace(token="sp-tok", expires_on=0)

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
    monkeypatch.setattr("azure.identity.ClientSecretCredential", _FakeClientSecretCred)
    monkeypatch.setattr("azure.mgmt.compute.ComputeManagementClient", _FakeCompute)
    monkeypatch.setattr("azure.mgmt.network.NetworkManagementClient", _FakeNetwork)
    monkeypatch.setattr("azure.mgmt.resource.resources.ResourceManagementClient", _FakeResource)
    return counters


# Every (tenant_id, client_id, client_secret) triple handed to the faked
# ClientSecretCredential, in order. Module-level so _install_fakes's inner
# class can append to it; cleared by the fixture below.
_sp_args: list[tuple[str, str, str]] = []


@pytest.fixture(autouse=True)
def _clear_sp_args() -> None:
    _sp_args.clear()


def _platform() -> AzureVMPlatform:
    return AzureVMPlatform("az-site", dict(_CONFIG))


def _sp_platform(secret_key: str | None = "secret") -> AzureVMPlatform:
    """A platform bound to a site that declares a service principal.
    ``secret_key=None`` omits the ``secret`` field so the default name
    applies."""
    sp = dict(_SP) if secret_key else {k: v for k, v in _SP.items() if k != "secret"}
    return AzureVMPlatform("az-site", {**_CONFIG, "service_principal": sp})


class _Secrets:
    """A SecretReader over a fixed mapping, as the boundary delivers."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        return self._values[name]


def _sp_ctx(name: str = "az-sp", value: str = "sp-secret-value") -> RunContext:
    return RunContext(secrets=_Secrets({name: value}))  # type: ignore[arg-type]


class TestCredentialCaching:
    def test_one_build_across_ops_and_per_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple ops on one instance (status + start + the public-IP
        heal, which together touch both SDK clients and the credential)
        build the credential and each client exactly once; a second
        instance builds its own rather than reusing the first's."""
        counters = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        platform = _platform()
        assert platform.status(vm, RunContext()) is VMStatus.RUNNING
        platform.start(vm, RunContext())
        platform._ensure_public_ip(vm, RunContext())

        # One credential build (one live probe), reused across all three ops;
        # one build of each client (compute is shared by status/start and the
        # heal's location lookup, network is built by the heal).
        assert counters["cred_build"] == 1
        assert counters["get_token"] == 1
        assert counters["compute_build"] == 1
        assert counters["network_build"] == 1

        # A second instance is a fresh cache: it builds its own credential.
        second = _platform()
        assert second.status(vm, RunContext()) is VMStatus.RUNNING
        assert counters["cred_build"] == 2
        assert counters["compute_build"] == 2

    def test_probe_runs_once_per_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The eager get_token probe fires at most once per instance: it
        is the validation step of the first credential build, not a
        per-op cost, and the accessor hands back the same cached object."""
        counters = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]
        platform = _platform()

        first = platform._get_credential(RunContext())
        again = platform._get_credential(RunContext())
        platform.status(vm, RunContext())
        platform.start(vm, RunContext())

        assert first is again
        assert counters["get_token"] == 1
        assert counters["cred_build"] == 1

    def test_second_subscription_builds_own_clients_not_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        platform._ensure_public_ip(vm_a, RunContext())
        platform._ensure_public_ip(vm_b, RunContext())

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

    def test_resource_client_caches_per_subscription(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        first = platform._resource_client(az_a, RunContext())
        assert platform._resource_client(az_a, RunContext()) is first
        assert counters["resource_build"] == 1

        # A second subscription builds its own, keyed by subscription.
        second = platform._resource_client(az_b, RunContext())
        assert second is not first
        assert counters["resource_build"] == 2
        assert set(platform._resource_cached) == {"sub-A", "sub-B"}

        # The subscription-independent credential (and its probe) built once
        # across both resource clients.
        assert counters["cred_build"] == 1
        assert counters["get_token"] == 1

    def test_browser_fallback_preserved_and_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the DefaultAzureCredential probe raises
        ClientAuthenticationError, the decision lands on the interactive
        browser credential, and it is that credential which is cached: the
        fallback is decided once (one probe, one browser build) and reused,
        never re-decided per op."""
        counters = _install_fakes(monkeypatch, auth_fails=True)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]
        platform = _platform()

        cred = platform._get_credential(RunContext())
        # The browser credential is the decision, and it is what got cached.
        assert counters["browser_build"] == 1
        assert platform._get_credential(RunContext()) is cred

        # Ops reuse the cached browser credential: no re-probe, no re-decide.
        platform.status(vm, RunContext())
        assert counters["cred_build"] == 1
        assert counters["get_token"] == 1
        assert counters["browser_build"] == 1


class TestCredentialSelection:
    """Issue #199: the site's config decides WHICH credential, and a
    configured service principal never degrades into another identity."""

    def test_no_service_principal_uses_the_ambient_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The unchanged default: a site that declares no service
        principal authenticates exactly as before, and the
        ClientSecretCredential is never constructed. The zero-SP-build
        assertion is the regression tripwire for existing operators."""
        counters = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        assert _platform().status(vm, RunContext()) is VMStatus.RUNNING

        assert counters["cred_build"] == 1
        assert counters["sp_build"] == 0
        assert _sp_args == []

    def test_service_principal_builds_from_the_delivered_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A declared service principal is built from the site's
        identifiers plus the client secret ``ctx.secret`` DELIVERS (the
        declare/receive contract: the config carries the secret's name,
        the context carries its value), and the ambient chain is never
        touched."""
        counters = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        assert _sp_platform().status(vm, _sp_ctx()) is VMStatus.RUNNING

        assert _sp_args == [("tenant-A", "client-A", "sp-secret-value")]
        assert counters["sp_build"] == 1
        # The whole ambient path stayed cold: no DefaultAzureCredential,
        # no browser prompt.
        assert counters["cred_build"] == 0
        assert counters["browser_build"] == 0

    def test_service_principal_secret_name_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting ``service_principal.secret`` reads the well-known
        default name, so the common case needs two config lines."""
        _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        platform = _sp_platform(secret_key=None)
        platform.status(vm, _sp_ctx(name=DEFAULT_CLIENT_SECRET, value="default-named"))

        assert _sp_args == [("tenant-A", "client-A", "default-named")]

    def test_service_principal_rejection_is_fatal_and_never_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The load-bearing one: when the configured service principal
        cannot authenticate, the op FAILS with a typed error naming the
        site and the secret. It must never quietly fall through to the
        ambient chain or a browser prompt, which would run the operator's
        command as a different identity than they configured."""
        counters = _install_fakes(monkeypatch, sp_auth_fails=True)
        platform = _sp_platform()

        with pytest.raises(AzureError) as exc:
            platform._get_credential(_sp_ctx())

        message = str(exc.value)
        assert "az-site" in message
        assert "az-sp" in message
        assert exc.value.entity_kind == "vm-site"
        assert exc.value.entity_name == "az-site"
        assert exc.value.hint is not None and "expired" in exc.value.hint
        # No fallback, of any kind.
        assert counters["cred_build"] == 0
        assert counters["browser_build"] == 0
        # And nothing bad got cached: a retry re-probes rather than
        # handing back a credential that never authenticated.
        assert platform._credential_cached is None

    def test_empty_resolved_client_secret_is_typed_not_a_bare_valueerror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A backend that resolves the client secret to an empty string
        makes the SDK constructor raise a bare ValueError before any
        token request. That must still come out as the platform's typed
        error with the site, the secret, and the hint.

        Config validation cannot catch this one: the config names a
        secret and the NAME is well-formed; only the resolved VALUE is
        empty. And an untyped escape here would be worse than noisy,
        because it would blow past the AgentworksError degrade paths
        `agw vm describe` and the gate's status probe rely on."""
        counters = _install_fakes(monkeypatch)

        with pytest.raises(AzureError) as exc:
            _sp_platform()._get_credential(_sp_ctx(value=""))

        assert "az-site" in str(exc.value)
        assert exc.value.hint is not None and "az-sp" in exc.value.hint
        # Still no fallback: an unusable credential is not a licence to
        # authenticate as somebody else.
        assert counters["cred_build"] == 0
        assert counters["browser_build"] == 0

    def test_service_principal_rejection_surfaces_through_the_ops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The rejection reaches the operator through a real op, not only
        through the private accessor: the client build is where the
        credential is first needed."""
        _install_fakes(monkeypatch, sp_auth_fails=True)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(AzureError, match="service principal"):
            _sp_platform().start(vm, _sp_ctx())

    def test_service_principal_without_delivered_secrets_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A context assembled before the resolve boundary (or for
        inspection) is the accessor's typed ConfigError, the same refusal
        every capability gets, rather than a crash or a silent ambient
        fallback."""
        counters = _install_fakes(monkeypatch)

        with pytest.raises(ConfigError, match="resolved secrets"):
            _sp_platform()._get_credential(RunContext())

        assert counters["sp_build"] == 0
        assert counters["cred_build"] == 0

    def test_service_principal_credential_caches_per_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SP path caches like the ambient one: the identity is fixed
        by the bound config, so one build and one probe serve every op on
        the instance."""
        counters = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]
        platform = _sp_platform()
        ctx = _sp_ctx()

        first = platform._get_credential(ctx)
        assert platform._get_credential(ctx) is first
        platform.status(vm, ctx)
        platform.start(vm, ctx)
        platform._ensure_public_ip(vm, ctx)

        assert counters["sp_build"] == 1
        assert counters["sp_get_token"] == 1
