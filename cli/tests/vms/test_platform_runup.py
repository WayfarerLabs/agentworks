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
anything). The credential is ambient today, so the tests fake the cached
resource client on the class rather than building one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.azure_vm import AzureError, AzureVMPlatform
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.capabilities.vm_platform.proxmox_api import (
    ProxmoxAPI,
    ProxmoxAPIError,
)
from agentworks.errors import NotFoundError, TokenRejectedError

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


def _ctx() -> RunContext:
    """A runup context carrying the resolved API token, as the service
    layer assembles after the boundary resolve pass."""
    return RunContext(secrets=_StubResolver({"proxmox-token": "tok"}))  # type: ignore[arg-type]


def test_proxmox_runup_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProxmoxAPI, "next_id", lambda self: 100)
    _platform().runup(_ctx())  # no error


@pytest.mark.parametrize("code", [401, 403])
def test_proxmox_runup_rejection_is_fatal(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def _boom(self: object) -> int:
        err = ProxmoxAPIError(f"failed ({code})")
        err.code = code
        raise err

    monkeypatch.setattr(ProxmoxAPI, "next_id", _boom)
    with pytest.raises(TokenRejectedError, match="Proxmox rejected"):
        _platform().runup(_ctx())


def test_proxmox_runup_other_status_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(self: object) -> int:
        err = ProxmoxAPIError("failed (500)")
        err.code = 500
        raise err

    monkeypatch.setattr(ProxmoxAPI, "next_id", _boom)
    _platform().runup(_ctx())  # no raise
    assert "could not verify" in capsys.readouterr().err


def test_proxmox_runup_network_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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


# -- Azure -----------------------------------------------------------------

_AZURE_CONFIG = {
    "subscription_id": "sub-123",
    "resource_group": "rg-dev",
    "region": "eastus",
}


def _azure_platform() -> AzureVMPlatform:
    return AzureVMPlatform("az", _AZURE_CONFIG)


def _wire_rg(monkeypatch: pytest.MonkeyPatch, *, exists: bool) -> None:
    """Fake the cached resource client so ``check_existence`` returns
    ``exists`` without building a credential or touching Azure."""
    fake_resource = SimpleNamespace(
        resource_groups=SimpleNamespace(check_existence=lambda *a, **k: exists)
    )
    monkeypatch.setattr(
        AzureVMPlatform, "_resource_client", lambda self, az: fake_resource
    )


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

    fake_resource = SimpleNamespace(
        resource_groups=SimpleNamespace(check_existence=_raise)
    )
    monkeypatch.setattr(
        AzureVMPlatform, "_resource_client", lambda self, az: fake_resource
    )


def _auth_error() -> Exception:
    """A representative credential-rejection failure raised by the existence
    probe: an auth-flavored ``HttpResponseError`` (``ClientAuthenticationError``
    subclasses it), which exercises ``_wrap_azure_error``'s HttpResponseError
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
    ``_wrap_azure_error`` (``AzureError``), so the ``False``-return branch that
    raises ``NotFoundError`` is never reached."""
    raised = make_exc()  # type: ignore[operator]
    _wire_rg_raises(monkeypatch, raised)
    with pytest.raises(AzureError) as exc:
        _azure_platform().runup(RunContext())
    # The forbidden masquerade: a probe FAILURE must not read as "group missing".
    assert not isinstance(exc.value, NotFoundError)
    # And it is genuinely the wrapped SDK failure, not a fresh error that happens
    # to share a type: runup chains it via ``raise _wrap_azure_error(exc) from
    # exc``, so the raised object is the wrapped error's cause.
    assert exc.value.__cause__ is raised
