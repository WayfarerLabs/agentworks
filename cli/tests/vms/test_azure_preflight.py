"""AzurePlatform.preflight: the non-interactive credential read.

The ``az account show`` equivalent: preflight probes for a management
token via the module's ``_noninteractive_credential_ok`` seam (a read).
One asymmetry, per the prompt-only-secret rule: in an interactive run a
missing credential defers to the op's browser-login fallback (failing
preflight would make that path unreachable); non-interactively it is
fatal before any mutation.

Tests stub the probe seam rather than the azure SDK: importing
``azure.identity`` pulls the native ``cryptography`` wheel, which is
not loadable on every test host.
"""

from __future__ import annotations

import pytest

from agentworks import output
from agentworks.capabilities.vm_platform import azure as azure_mod
from agentworks.capabilities.vm_platform.azure import AzurePlatform
from agentworks.errors import AuthorizationError

_CONFIG = {
    "subscription_id": "sub",
    "resource_group": "rg",
    "region": "eastus",
}


def test_preflight_passes_with_noninteractive_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        azure_mod, "_noninteractive_credential_ok", lambda: (True, "")
    )
    AzurePlatform("azure-dev", _CONFIG).preflight()


def test_preflight_fails_noninteractive_without_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        azure_mod,
        "_noninteractive_credential_ok",
        lambda: (False, "no credential source succeeded"),
    )
    monkeypatch.setattr(output, "is_interactive", lambda: False)
    with pytest.raises(
        AuthorizationError, match="non-interactive Azure credential"
    ) as exc:
        AzurePlatform("azure-dev", _CONFIG).preflight()
    assert "az login" in (exc.value.hint or "")


def test_preflight_defers_to_browser_login_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential + interactive run: preflight passes so the op's
    InteractiveBrowserCredential fallback stays reachable."""
    monkeypatch.setattr(
        azure_mod,
        "_noninteractive_credential_ok",
        lambda: (False, "no credential source succeeded"),
    )
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    AzurePlatform("azure-dev", _CONFIG).preflight()
