"""Ambient interaction boundaries driven through the real Azure credential chain."""

from __future__ import annotations

import logging
from typing import Any

import azure.identity
import azure.identity._credentials.default as default_credentials
import azure.keyvault.secrets
import pytest
import requests
from azure.core.credentials import AccessToken, AccessTokenInfo
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import CredentialUnavailableError

from agentworks.capabilities.secret_backend import (
    BackendFailed,
    BackendResolved,
    FailureReason,
    IndeterminateReason,
    OperatorImpact,
    PreviewAvailable,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewIntent,
    ResolutionIntent,
    SecretLookupRequest,
    TtyInteractionAccess,
)
from agentworks.errors import UserAbort
from agentworks.plugins.azure.keyvault import AzureKeyVaultBackend
from agentworks.plugins.azure.keyvault_config import AzureKeyVaultMapping, AzureKeyVaultSourceConfig
from tests._azure_scripted_transport import _ScriptedAdapter

_CHALLENGE = {
    "WWW-Authenticate": 'Bearer authorization="https://login.microsoftonline.com/tenant", resource="https://vault.azure.net"'
}


class _AuthenticatedAdapter(_ScriptedAdapter):
    def __init__(self) -> None:
        super().__init__([(200, {}, {"value": "resolved"})])
        self.closed = False

    def _answer(self, request: requests.PreparedRequest) -> requests.Response:
        if not request.headers.get("Authorization"):
            return _ScriptedAdapter([(401, _CHALLENGE, {})])._answer(request)
        return super()._answer(request)

    def close(self) -> None:
        self.closed = True


class _UnavailableCredential:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_token(self, *scopes: Any, **kwargs: Any) -> AccessToken:
        raise CredentialUnavailableError("synthetic unavailable credential")

    def get_token_info(self, *scopes: Any, **kwargs: Any) -> AccessTokenInfo:
        raise CredentialUnavailableError("synthetic unavailable credential")

    def close(self) -> None:
        pass

    def __exit__(self, *args: object) -> None:
        self.close()


@pytest.fixture
def real_chain(monkeypatch: pytest.MonkeyPatch) -> _AuthenticatedAdapter:
    """Retain DAC and SecretClient while isolating every external credential location."""
    monkeypatch.delenv("AZURE_TOKEN_CREDENTIALS", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_CERTIFICATE_PATH", raising=False)
    monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
    for name in (
        "WorkloadIdentityCredential",
        "ManagedIdentityCredential",
        "VisualStudioCodeCredential",
        "AzureCliCredential",
        "AzurePowerShellCredential",
        "AzureDeveloperCliCredential",
        "BrokerCredential",
    ):
        monkeypatch.setattr(default_credentials, name, _UnavailableCredential)
    monkeypatch.setattr(azure.identity.SharedTokenCacheCredential, "supported", lambda: False)
    adapter = _AuthenticatedAdapter()
    real_client = azure.keyvault.secrets.SecretClient

    def client(*args: Any, **kwargs: Any) -> azure.keyvault.secrets.SecretClient:
        session = requests.Session()
        session.mount("https://", adapter)
        kwargs["transport"] = RequestsTransport(session=session, session_owner=True)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(azure.keyvault.secrets, "SecretClient", client)
    return adapter


@pytest.mark.parametrize("operation", ["none", "allow", "resolve"])
def test_real_deployed_credential_rejection_reaches_only_authorized_browser_fallback(
    monkeypatch: pytest.MonkeyPatch,
    real_chain: _AuthenticatedAdapter,
    operation: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000002")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "synthetic-bootstrap")
    calls: list[str] = []
    closed: list[str] = []
    close_dac = azure.identity.DefaultAzureCredential.close
    close_browser = azure.identity.InteractiveBrowserCredential.close

    def close_default(self: azure.identity.DefaultAzureCredential) -> None:
        closed.append("default")
        close_dac(self)

    def close_interactive(self: azure.identity.InteractiveBrowserCredential) -> None:
        closed.append("browser")
        close_browser(self)

    def reject(self: object, *args: Any, **kwargs: Any) -> Any:
        calls.append("environment")
        raise ClientAuthenticationError("synthetic-authentication-detail")

    def browser_token(self: object, *args: Any, **kwargs: Any) -> AccessToken:
        calls.append("browser")
        logging.getLogger("azure.identity").error("synthetic-browser-detail")
        return AccessToken("synthetic-browser-token", 2**31)

    def browser_info(self: object, *args: Any, **kwargs: Any) -> AccessTokenInfo:
        calls.append("browser")
        logging.getLogger("azure.identity").error("synthetic-browser-detail")
        return AccessTokenInfo("synthetic-browser-token", 2**31)

    monkeypatch.setattr(azure.identity.EnvironmentCredential, "get_token", reject)
    monkeypatch.setattr(azure.identity.EnvironmentCredential, "get_token_info", reject)
    monkeypatch.setattr(azure.identity.InteractiveBrowserCredential, "get_token", browser_token)
    monkeypatch.setattr(azure.identity.InteractiveBrowserCredential, "get_token_info", browser_info)
    monkeypatch.setattr(azure.identity.DefaultAzureCredential, "close", close_default)
    monkeypatch.setattr(azure.identity.InteractiveBrowserCredential, "close", close_interactive)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://real-chain-test.vault.azure.net")
    intent = ResolutionIntent() if operation == "resolve" else PreviewIntent(OperatorImpact(operation))
    request = SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("npm-token"))
    with AzureKeyVaultBackend.create_client(
        config=config, intent=intent, tty_access=TtyInteractionAccess.DISABLED, interaction_broker=None
    ) as client:
        result = client.resolve((request,)) if operation == "resolve" else client.preview((request,))
    if operation == "none":
        assert result == {"token": PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)}
        assert calls == ["environment"]
        assert closed == ["default"]
    else:
        expected = BackendResolved("resolved") if operation == "resolve" else PreviewAvailable()
        assert result == {"token": expected}
        assert calls == ["environment", "browser"]
        assert closed == ["browser", "default"]
    assert "synthetic-browser-detail" not in caplog.text
    assert real_chain.closed


@pytest.mark.parametrize("impact", [OperatorImpact.NONE, OperatorImpact.ALLOW])
def test_real_chain_excludes_shared_cache_only_at_zero_impact(
    monkeypatch: pytest.MonkeyPatch, real_chain: _AuthenticatedAdapter, impact: OperatorImpact
) -> None:
    monkeypatch.setattr(default_credentials, "EnvironmentCredential", _UnavailableCredential)
    calls: list[str] = []

    class SharedCache(_UnavailableCredential):
        @staticmethod
        def supported() -> bool:
            return True

        def get_token(self, *scopes: Any, **kwargs: Any) -> AccessToken:
            calls.append("cache")
            return AccessToken("synthetic-cache-token", 2**31)

        def get_token_info(self, *scopes: Any, **kwargs: Any) -> AccessTokenInfo:
            calls.append("cache")
            return AccessTokenInfo("synthetic-cache-token", 2**31)

    monkeypatch.setattr(default_credentials, "SharedTokenCacheCredential", SharedCache)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://cache-chain-test.vault.azure.net")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=PreviewIntent(impact),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.preview((SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("npm-token")),))
    if impact is OperatorImpact.NONE:
        assert calls == []
        assert result == {"token": PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)}
    else:
        assert calls == ["cache"]
        assert result == {"token": PreviewAvailable()}
    assert real_chain.closed


@pytest.mark.parametrize(
    "credential_name", ["AzureCliCredential", "AzurePowerShellCredential", "AzureDeveloperCliCredential"]
)
@pytest.mark.parametrize("impact", [OperatorImpact.NONE, OperatorImpact.ALLOW])
def test_real_chain_excludes_cache_backed_developer_credentials_at_zero_impact(
    monkeypatch: pytest.MonkeyPatch,
    real_chain: _AuthenticatedAdapter,
    credential_name: str,
    impact: OperatorImpact,
) -> None:
    monkeypatch.setattr(default_credentials, "EnvironmentCredential", _UnavailableCredential)
    calls: list[str] = []

    class DeveloperCache(_UnavailableCredential):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append("constructed")

        def get_token(self, *scopes: Any, **kwargs: Any) -> AccessToken:
            calls.append("token")
            return AccessToken("synthetic-developer-token", 2**31)

        def get_token_info(self, *scopes: Any, **kwargs: Any) -> AccessTokenInfo:
            calls.append("token")
            return AccessTokenInfo("synthetic-developer-token", 2**31)

    monkeypatch.setattr(default_credentials, credential_name, DeveloperCache)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://developer-chain-test.vault.azure.net")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=PreviewIntent(impact),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.preview((SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("npm-token")),))
    if impact is OperatorImpact.NONE:
        assert calls == []
        assert result == {"token": PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)}
    else:
        assert calls == ["constructed", "token"]
        assert result == {"token": PreviewAvailable()}
    assert real_chain.closed


@pytest.mark.parametrize(
    "credential_name", ["EnvironmentCredential", "WorkloadIdentityCredential", "ManagedIdentityCredential"]
)
def test_zero_impact_real_chain_still_uses_each_unattended_credential(
    monkeypatch: pytest.MonkeyPatch, real_chain: _AuthenticatedAdapter, credential_name: str
) -> None:
    monkeypatch.setattr(default_credentials, "EnvironmentCredential", _UnavailableCredential)
    calls: list[str] = []

    class UnattendedCredential(_UnavailableCredential):
        def get_token(self, *scopes: Any, **kwargs: Any) -> AccessToken:
            calls.append("token")
            return AccessToken("synthetic-unattended-token", 2**31)

        def get_token_info(self, *scopes: Any, **kwargs: Any) -> AccessTokenInfo:
            calls.append("token")
            return AccessTokenInfo("synthetic-unattended-token", 2**31)

    monkeypatch.setattr(default_credentials, credential_name, UnattendedCredential)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://unattended-chain-test.vault.azure.net")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=PreviewIntent(OperatorImpact.NONE),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.preview((SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("npm-token")),))
    assert calls == ["token"]
    assert result == {"token": PreviewAvailable()}
    assert real_chain.closed


def test_browser_abort_does_not_retain_original_authentication_failure(
    monkeypatch: pytest.MonkeyPatch, real_chain: _AuthenticatedAdapter
) -> None:
    monkeypatch.setattr(default_credentials, "EnvironmentCredential", _UnavailableCredential)
    browser_calls: list[str] = []
    real_browser = azure.identity.InteractiveBrowserCredential

    def browser(**kwargs: Any) -> azure.identity.InteractiveBrowserCredential:
        browser_calls.append("constructed")
        return real_browser(**kwargs)

    def abort(self: object, *args: Any, **kwargs: Any) -> Any:
        raise UserAbort("synthetic abort")

    monkeypatch.setattr(azure.identity, "InteractiveBrowserCredential", browser)
    monkeypatch.setattr(real_browser, "get_token", abort)
    monkeypatch.setattr(real_browser, "get_token_info", abort)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://abort-chain-test.vault.azure.net")
    with (
        pytest.raises(UserAbort) as raised,
        AzureKeyVaultBackend.create_client(
            config=config,
            intent=ResolutionIntent(),
            tty_access=TtyInteractionAccess.DISABLED,
            interaction_broker=None,
        ) as client,
    ):
        client.resolve((SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("npm-token")),))
    assert raised.value.__context__ is None
    assert browser_calls == ["constructed"]
    assert real_chain.closed


def test_failed_browser_fallback_is_not_reconstructed_for_later_requests(
    monkeypatch: pytest.MonkeyPatch, real_chain: _AuthenticatedAdapter
) -> None:
    monkeypatch.setattr(default_credentials, "EnvironmentCredential", _UnavailableCredential)
    browser_calls: list[str] = []

    class RejectedBrowser(_UnavailableCredential):
        def __init__(self, **kwargs: Any) -> None:
            browser_calls.append("constructed")

        def get_token(self, *scopes: Any, **kwargs: Any) -> AccessToken:
            raise ClientAuthenticationError("synthetic-browser-rejection")

        def get_token_info(self, *scopes: Any, **kwargs: Any) -> AccessTokenInfo:
            raise ClientAuthenticationError("synthetic-browser-rejection")

    monkeypatch.setattr(azure.identity, "InteractiveBrowserCredential", RejectedBrowser)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://rejected-chain-test.vault.azure.net")
    mapping = AzureKeyVaultMapping.model_validate("npm-token")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=ResolutionIntent(),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.resolve((SecretLookupRequest("first", mapping), SecretLookupRequest("second", mapping)))
    assert result == {
        "first": BackendFailed(FailureReason.AUTHENTICATION),
        "second": BackendFailed(FailureReason.AUTHENTICATION),
    }
    assert browser_calls == ["constructed"]
    assert real_chain.closed


@pytest.mark.parametrize(
    "selector",
    [
        "dev",
        "AzureCliCredential",
        "AzurePowerShellCredential",
        "AzureDeveloperCliCredential",
        "VisualStudioCodeCredential",
        " InteractiveBrowserCredential ",
    ],
)
def test_ui_only_ambient_selectors_are_indeterminate_at_zero_impact(
    monkeypatch: pytest.MonkeyPatch, real_chain: _AuthenticatedAdapter, selector: str
) -> None:
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", selector)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://selector-chain-test.vault.azure.net")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=PreviewIntent(OperatorImpact.NONE),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.preview((SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("npm-token")),))
    assert result == {"token": PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)}
    assert real_chain.methods == []


@pytest.mark.parametrize("selector", ["invalid-selector", "SharedTokenCacheCredential", "BrokerCredential"])
def test_invalid_sdk_selectors_are_definitive_failures(
    monkeypatch: pytest.MonkeyPatch, real_chain: _AuthenticatedAdapter, selector: str
) -> None:
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", selector)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://invalid-selector-test.vault.azure.net")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=PreviewIntent(OperatorImpact.NONE),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.preview((SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("npm-token")),))
    assert result == {"token": PreviewFailed(FailureReason.EXTERNAL)}
    assert real_chain.methods == []


def test_allowed_sdk_selector_does_not_reenable_unselected_credentials(
    monkeypatch: pytest.MonkeyPatch, real_chain: _AuthenticatedAdapter
) -> None:
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "AzureCliCredential")
    calls: list[str] = []

    class SelectedCli(_UnavailableCredential):
        def get_token(self, *scopes: Any, **kwargs: Any) -> AccessToken:
            calls.append("cli")
            return AccessToken("synthetic-selected-token", 2**31)

        def get_token_info(self, *scopes: Any, **kwargs: Any) -> AccessTokenInfo:
            calls.append("cli")
            return AccessTokenInfo("synthetic-selected-token", 2**31)

    class UnselectedCredential(_UnavailableCredential):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pytest.fail("an unselected credential was constructed")

    monkeypatch.setattr(default_credentials, "AzureCliCredential", SelectedCli)
    for name in (
        "AzurePowerShellCredential",
        "AzureDeveloperCliCredential",
        "VisualStudioCodeCredential",
        "BrokerCredential",
    ):
        monkeypatch.setattr(default_credentials, name, UnselectedCredential)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://selected-cli-test.vault.azure.net")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=PreviewIntent(OperatorImpact.ALLOW),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.preview((SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("npm-token")),))
    assert result == {"token": PreviewAvailable()}
    assert calls == ["cli"]
    assert real_chain.closed


def test_selected_browser_is_used_directly_once_per_source_turn(
    monkeypatch: pytest.MonkeyPatch, real_chain: _AuthenticatedAdapter
) -> None:
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "InteractiveBrowserCredential")
    calls: list[str] = []
    real_browser = azure.identity.InteractiveBrowserCredential

    def browser(**kwargs: Any) -> azure.identity.InteractiveBrowserCredential:
        calls.append("constructed")
        return real_browser(**kwargs)

    def token(self: object, *args: Any, **kwargs: Any) -> AccessToken:
        return AccessToken("synthetic-selected-browser-token", 2**31)

    def token_info(self: object, *args: Any, **kwargs: Any) -> AccessTokenInfo:
        return AccessTokenInfo("synthetic-selected-browser-token", 2**31)

    monkeypatch.setattr(azure.identity, "InteractiveBrowserCredential", browser)
    monkeypatch.setattr(real_browser, "get_token", token)
    monkeypatch.setattr(real_browser, "get_token_info", token_info)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://selected-browser-test.vault.azure.net")
    mapping = AzureKeyVaultMapping.model_validate("npm-token")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=ResolutionIntent(),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.resolve((SecretLookupRequest("first", mapping), SecretLookupRequest("second", mapping)))
    assert result == {"first": BackendResolved("resolved"), "second": BackendResolved("resolved")}
    assert calls == ["constructed"]
    assert real_chain.closed
