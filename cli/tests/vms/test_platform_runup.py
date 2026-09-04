"""Provisioning-phase runup for vm-platform.

The proxmox platform authenticates its API token with a cheap read
(next available VMID) before ``create`` mutates anything: a 401/403 is
a definitive rejection (fatal, before any VM exists); a transient error
or unreachable host warns and continues unverified. lima/wsl2 have no
token to check, so their runup is the base no-op.

The azure-vm platform's runup is an authenticated, read-only
resource-group existence check (issue #198 follow-up): the site's
configured resource group either exists (pass silently) or does not (a
definitive ``NotFoundError`` raised before ``create`` provisions
anything). Most of these tests fake the cached resource client on the
class, so no credential is built at all; the service-principal tests at
the end (issue #199) deliberately do NOT, because on that path building
the credential IS what runup makes happen first.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import AuthorizationError, NotFoundError, TokenRejectedError, ValidationError
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.azure.network import AzureError
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.proxmox.api import (
    ProxmoxAPI,
    ProxmoxAPIError,
)
from agentworks.plugins.proxmox.platform import ProxmoxPlatform

_CONFIG = {
    "api_url": "https://pve:8006",
    "node": "n",
    "token_id": "t",
    "template_vmid": 1,
}


class _StubResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def register_name(self, name: str) -> str:
        return name

    def get(self, name: str) -> str:
        return self._values[name]


def _platform() -> ProxmoxPlatform:
    return ProxmoxPlatform("px", _CONFIG)


def _ctx(value: str = "tok") -> RunContext:
    """A runup context carrying the resolved API token, as the service
    layer assembles after the boundary resolve pass."""
    return RunContext(secrets=_StubResolver({"proxmox-token": value}))  # type: ignore[arg-type]


def test_proxmox_runup_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProxmoxAPI, "next_id", lambda self: 100)
    _platform().runup(_ctx())  # no error


@pytest.mark.parametrize("code", [401, 403])
def test_proxmox_runup_rejection_is_fatal(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    def _boom(self: object) -> int:
        err = ProxmoxAPIError(f"failed ({code})")
        err.code = code
        raise err

    monkeypatch.setattr(ProxmoxAPI, "next_id", _boom)
    with pytest.raises(TokenRejectedError, match="Proxmox rejected"):
        _platform().runup(_ctx())


def test_proxmox_runup_other_status_warns(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(self: object) -> int:
        err = ProxmoxAPIError("failed (500)")
        err.code = 500
        raise err

    monkeypatch.setattr(ProxmoxAPI, "next_id", _boom)
    _platform().runup(_ctx())  # no raise
    assert "could not verify" in capsys.readouterr().err


def test_proxmox_runup_network_warns(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(self: object) -> int:
        raise OSError("unreachable")

    monkeypatch.setattr(ProxmoxAPI, "next_id", _boom)
    _platform().runup(_ctx())  # no raise
    assert "could not reach Proxmox" in capsys.readouterr().err


def test_proxmox_runup_without_secrets_is_error() -> None:
    """A runup with no resolved secrets in the context (inspection) is a
    typed error, not a crash: runup runs post-resolve and must be handed
    the token via ``ctx.secret(name)``."""
    from agentworks.errors import ConfigError

    with pytest.raises(ConfigError, match="resolved secrets"):
        ProxmoxPlatform("px", _CONFIG).runup(RunContext())


@pytest.mark.parametrize("token", ["prefix\nsuffix", "prefix\rsuffix", "prefix\0suffix"])
def test_proxmox_rejects_line_unsafe_token_before_api_client_construction(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    def _no_client(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("line-unsafe token reached ProxmoxAPI construction")

    monkeypatch.setattr("agentworks.plugins.proxmox.platform.ProxmoxAPI", _no_client)

    with pytest.raises(ValidationError) as caught:
        _platform().runup(_ctx(token))

    assert token not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# -- Azure -----------------------------------------------------------------

_AZURE_CONFIG = {
    "subscription_id": "sub-123",
    "resource_group": "rg-dev",
    "region": "eastus",
    "auth": {"mode": "ambient"},
}


def _azure_platform() -> AzureVMPlatform:
    return AzureVMPlatform("az", _AZURE_CONFIG)


def _wire_rg(monkeypatch: pytest.MonkeyPatch, *, exists: bool) -> None:
    """Fake the cached resource client so ``check_existence`` returns
    ``exists`` without building a credential or touching Azure."""
    fake_resource = SimpleNamespace(resource_groups=SimpleNamespace(check_existence=lambda *a, **k: exists))
    monkeypatch.setattr(AzureVMPlatform, "_resource_client", lambda self, az, ctx: fake_resource)


def test_azure_runup_ok_when_group_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_rg(monkeypatch, exists=True)
    _azure_platform().runup(RunContext())  # no raise


def test_azure_runup_missing_group_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_rg(monkeypatch, exists=False)
    with pytest.raises(NotFoundError) as exc:
        _azure_platform().runup(RunContext())
    assert exc.value.entity_kind == "resource-group"
    assert exc.value.entity_name == "rg-dev"
    # The hint offers both recoveries: create the group or repoint the site.
    assert exc.value.hint is not None
    assert "az group create -n rg-dev -l eastus" in exc.value.hint
    assert "existing resource group" in exc.value.hint


def test_azure_runup_error_names_group_and_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_rg(monkeypatch, exists=False)
    with pytest.raises(NotFoundError) as exc:
        _azure_platform().runup(RunContext())
    msg = str(exc.value)
    assert "rg-dev" in msg
    assert "sub-123" in msg


def _wire_rg_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Fake the cached resource client so ``check_existence`` RAISES ``exc``
    (a credential or reachability failure from the SDK call) rather than
    returning a boolean existence verdict."""

    def _raise(*_a: object, **_k: object) -> bool:
        raise exc

    fake_resource = SimpleNamespace(resource_groups=SimpleNamespace(check_existence=_raise))
    monkeypatch.setattr(AzureVMPlatform, "_resource_client", lambda self, az, ctx: fake_resource)


def _auth_error() -> Exception:
    """A representative credential-rejection failure raised by the existence
    probe: an auth-flavored ``HttpResponseError`` (``ClientAuthenticationError``
    subclasses it), which exercises ``wrap_azure_error``'s HttpResponseError
    branch rather than its generic fallback. Imported function-locally so azure
    is not loaded at collection time, matching the suite's convention."""
    from azure.core.exceptions import ClientAuthenticationError

    return ClientAuthenticationError(message="Bearer token rejected")


@pytest.mark.parametrize(
    "make_exc",
    [
        pytest.param(lambda: Exception("boom"), id="generic-exception"),
        pytest.param(_auth_error, id="auth-flavored-http-error"),
    ],
)
def test_azure_runup_sdk_failure_wraps_not_masquerades_as_missing(
    monkeypatch: pytest.MonkeyPatch, make_exc: object
) -> None:
    """A failure of the existence probe itself (an EXCEPTION from
    ``check_existence``, not a ``False`` verdict) is the runup docstring's
    load-bearing guarantee: a bad or unreachable credential surfaces as the
    wrapped Azure error, never as a ``NotFoundError`` claiming the resource
    group is absent. runup routes such exceptions through
    ``wrap_azure_error`` (``AzureError``), so the ``False``-return branch that
    raises ``NotFoundError`` is never reached."""
    raised = make_exc()  # type: ignore[operator]
    _wire_rg_raises(monkeypatch, raised)
    with pytest.raises(AzureError) as exc:
        _azure_platform().runup(RunContext())
    # The forbidden masquerade: a probe FAILURE must not read as "group missing".
    assert not isinstance(exc.value, NotFoundError)
    # And it is genuinely the wrapped SDK failure, not a fresh error that happens
    # to share a type: runup chains it via ``raise wrap_azure_error(exc) from
    # exc``, so the raised object is the wrapped error's cause.
    assert exc.value.__cause__ is raised


# -- Azure with an explicit service principal (issue #199) -------------------

_AZURE_SP_CONFIG = {
    **_AZURE_CONFIG,
    "auth": {
        "mode": "service-principal",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "secret": "az-sp",
    },
}


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        return self._values[name]


def _sp_ctx() -> RunContext:
    return RunContext(secrets=_Secrets({"az-sp": "sp-value"}))  # type: ignore[arg-type]


def test_azure_runup_rejects_a_bad_service_principal_before_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of running this ahead of ``create``: a bad or expired
    client secret aborts here, with a typed error naming the site and the
    secret, while nothing has been provisioned and no DB row exists.

    No client is faked, deliberately. Building the resource client is what
    forces the credential, so the failure has to come from the credential
    probe, not from the existence check (which never runs)."""
    from azure.core.exceptions import ClientAuthenticationError

    from agentworks.plugins.azure.network import AzureError as _AzureError

    class _RejectingCred:
        def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None: ...

        def get_token(self, *_scopes: str, **_kw: object) -> object:
            raise ClientAuthenticationError("AADSTS7000215: Invalid client secret provided")

    monkeypatch.setattr("azure.identity.ClientSecretCredential", _RejectingCred)

    with pytest.raises(_AzureError) as exc:
        AzureVMPlatform("az", _AZURE_SP_CONFIG).runup(_sp_ctx())

    assert exc.value.entity_kind == "vm-site"
    assert "az-sp" in str(exc.value)
    # Not a "group missing" verdict: a credential failure must never
    # masquerade as an absent resource group.
    assert not isinstance(exc.value, NotFoundError)


def test_azure_runup_without_the_client_secret_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """runup runs post-resolve; a context with no resolved secrets is the
    accessor's typed ConfigError, exactly as for proxmox above."""
    from agentworks.errors import ConfigError

    with pytest.raises(ConfigError, match="resolved secrets"):
        AzureVMPlatform("az", _AZURE_SP_CONFIG).runup(RunContext())


# -- EC2 (aws) -------------------------------------------------------------
#
# The aws-ec2 platform requires a validated STS account identity because create
# persists it as the destructive-operation account binding. A configured but
# missing subnet is also fatal, like Azure's missing resource group.

_EC2_CONFIG = {"region": "us-east-1", "auth": {"mode": "ambient"}}
_EC2_CREDS_CONFIG = {
    "region": "us-east-1",
    "auth": {"mode": "access-key", "access_key_id": "AKIA", "access_key_secret": "aws-secret"},
}


def _ec2_ctx() -> RunContext:
    return RunContext(secrets=_Secrets({"aws-secret": "value"}))  # type: ignore[arg-type]


def test_aws_ec2_runup_ok_when_identity_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.aws.platform import EC2Platform
    from tests._aws_fakes import install_fakes

    install_fakes(monkeypatch)
    EC2Platform("aws", _EC2_CONFIG).runup(RunContext())  # no raise (ambient path)


def test_aws_ec2_runup_auth_rejection_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.aws.platform import EC2Platform
    from tests._aws_fakes import Controls, client_error, install_fakes

    install_fakes(
        monkeypatch, Controls(identity_error=client_error("InvalidClientTokenId", "bad key", "GetCallerIdentity"))
    )
    with pytest.raises(TokenRejectedError, match="AWS rejected"):
        EC2Platform("aws", _EC2_CONFIG).runup(RunContext())


@pytest.mark.parametrize("code", ["UnauthorizedOperation", "AccessDenied", "AccessDeniedException"])
def test_aws_ec2_runup_permission_denial_is_authorization(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    from agentworks.plugins.aws.platform import EC2Platform
    from tests._aws_fakes import Controls, client_error, install_fakes

    install_fakes(monkeypatch, Controls(identity_error=client_error(code, "denied", "GetCallerIdentity")))

    with pytest.raises(AuthorizationError) as exc:
        EC2Platform("aws", _EC2_CONFIG).runup(RunContext())

    assert exc.value.entity_kind == "vm-site"
    assert exc.value.entity_name == "aws"


def test_aws_ec2_runup_unreachable_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.aws.platform import EC2Platform
    from tests._aws_fakes import Controls, install_fakes, unreachable

    install_fakes(monkeypatch, Controls(identity_error=unreachable()))
    with pytest.raises(EC2Error):
        EC2Platform("aws", _EC2_CONFIG).runup(RunContext())


def test_aws_ec2_runup_non_auth_client_error_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.aws.platform import EC2Platform
    from tests._aws_fakes import Controls, client_error, install_fakes

    install_fakes(monkeypatch, Controls(identity_error=client_error("Throttling", "slow down", "GetCallerIdentity")))
    with pytest.raises(EC2Error):
        EC2Platform("aws", _EC2_CONFIG).runup(RunContext())


def test_aws_ec2_runup_missing_subnet_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.aws.platform import EC2Platform
    from tests._aws_fakes import Controls, client_error, install_fakes

    install_fakes(
        monkeypatch,
        Controls(subnet_error=client_error("InvalidSubnetID.NotFound", "no such subnet", "DescribeSubnets")),
    )
    with pytest.raises(NotFoundError) as exc:
        EC2Platform("aws", {**_EC2_CONFIG, "subnet_id": "subnet-xyz"}).runup(RunContext())
    assert exc.value.entity_kind == "subnet"
    assert exc.value.entity_name == "subnet-xyz"


def test_aws_ec2_runup_rejects_a_bad_explicit_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the explicit-credentials path the identity probe verifies the secret
    the context delivered; a server rejection aborts create with a typed,
    secret-naming error before anything is provisioned."""
    from agentworks.plugins.aws.platform import EC2Platform
    from tests._aws_fakes import Controls, client_error, install_fakes

    install_fakes(
        monkeypatch, Controls(identity_error=client_error("SignatureDoesNotMatch", "bad sig", "GetCallerIdentity"))
    )
    with pytest.raises(TokenRejectedError) as exc:
        EC2Platform("aws", _EC2_CREDS_CONFIG).runup(_ec2_ctx())
    assert exc.value.entity_kind == "vm-site"
    assert "aws-secret" in (exc.value.hint or "")


def test_aws_ec2_runup_without_the_secret_is_typed() -> None:
    from agentworks.errors import ConfigError
    from agentworks.plugins.aws.platform import EC2Platform

    with pytest.raises(ConfigError, match="resolved secrets"):
        EC2Platform("aws", _EC2_CREDS_CONFIG).runup(RunContext())
