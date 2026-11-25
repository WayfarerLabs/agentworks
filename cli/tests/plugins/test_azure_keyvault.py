"""Key Vault source behavior through the real Azure SDK and resolver."""

from __future__ import annotations

import concurrent.futures
import io
import logging
from contextlib import AbstractContextManager
from typing import Any

import azure.identity
import azure.keyvault.secrets
import pytest
import requests
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import RequestsTransport
from pydantic import ValidationError

from agentworks.capabilities.secret_backend import (
    BackendFailed,
    BackendResolved,
    EnvVarBackend,
    EnvVarSourceConfig,
    FailureReason,
    IndeterminateReason,
    OperatorImpact,
    PreviewAvailable,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewIntent,
    ResolutionIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.errors import UserAbort
from agentworks.plugins.azure.keyvault import AzureKeyVaultBackend
from agentworks.plugins.azure.keyvault_config import (
    AzureKeyVaultMapping,
    AzureKeyVaultSourceConfig,
    vault_authority,
)
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionStatus
from agentworks.secrets.resolve import ActiveSource, resolve_batch
from tests._azure_scripted_transport import _Answer, _ScriptedAdapter

_VAULT = "https://agw-test-kv.vault.azure.net"
_VERSION = "0123456789abcdef0123456789abcdef"
_CHALLENGE = {
    "WWW-Authenticate": 'Bearer authorization="https://login.microsoftonline.com/tenant", resource="https://vault.azure.net"'
}


class _Adapter(_ScriptedAdapter):
    def __init__(self, answers: list[_Answer]) -> None:
        super().__init__(answers)
        self.urls: list[str] = []
        self.closed = False

    def send(self, request: requests.PreparedRequest, *args: object, **kwargs: object) -> requests.Response:
        self.urls.append(str(request.url))
        return super().send(request, *args, **kwargs)

    def close(self) -> None:
        self.closed = True


class _Credential:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = False

    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        if self.error is not None:
            raise self.error
        return AccessToken("test-token", 2**31)

    def close(self) -> None:
        self.closed = True


class _SDK:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answers: list[_Answer]) -> None:
        self.adapter = _Adapter(answers)
        self.credential_error: BaseException | None = None
        self.credentials: list[_Credential] = []
        self.credential_options: list[dict[str, Any]] = []
        self.client_options: list[dict[str, Any]] = []
        self.client_error: BaseException | None = None
        real_client = azure.keyvault.secrets.SecretClient

        def credential(**kwargs: Any) -> _Credential:
            self.credential_options.append(kwargs)
            result = _Credential(self.credential_error)
            self.credentials.append(result)
            return result

        def client(*args: Any, **kwargs: Any) -> azure.keyvault.secrets.SecretClient:
            self.client_options.append(kwargs.copy())
            if self.client_error is not None:
                raise self.client_error
            session = requests.Session()
            session.mount("https://", self.adapter)
            kwargs["transport"] = RequestsTransport(session=session, session_owner=True)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(azure.identity, "DefaultAzureCredential", credential)
        monkeypatch.setattr(azure.keyvault.secrets, "SecretClient", client)


def _request(mapping: object = "npm-token", *, name: str = "token") -> SecretLookupRequest:
    return SecretLookupRequest(name, AzureKeyVaultMapping.model_validate(mapping))


def _context(
    intent: PreviewIntent | ResolutionIntent,
    *,
    config: AzureKeyVaultSourceConfig | None = None,
) -> AbstractContextManager[SecretSourceClient]:
    return AzureKeyVaultBackend.create_client(
        config=config or AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url=_VAULT),
        intent=intent,
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )


def _source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="work-kv", backend=CapabilityBlock.of("azure-keyvault", vault_url=_VAULT)),
        backend_class=AzureKeyVaultBackend,
        config=AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url=_VAULT),
        readiness=Readiness.ready(),
    )


@pytest.mark.parametrize("value", ["", "  text \r\n", "line1\nline2\n", "café 🔑"])
def test_exact_values_and_value_free_preview(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    sdk = _SDK(monkeypatch, [(200, {}, {"value": value})])
    with _context(ResolutionIntent()) as client:
        result = client.resolve((_request(),))
    assert result == {"token": BackendResolved(value)}
    assert sdk.credentials[0].closed
    assert sdk.adapter.closed
    with _context(PreviewIntent(OperatorImpact.NONE)) as client:
        preview = client.preview((_request(),))
    assert preview == {"token": PreviewAvailable()}
    assert sdk.credentials[-1].closed
    assert sdk.adapter.methods == ["GET", "GET"]


def test_version_selection_and_one_client_per_source_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _SDK(monkeypatch, [(200, {}, {"value": "resolved"})])
    with _context(ResolutionIntent()) as client:
        client.resolve((_request(), _request({"name": "pinned", "version": _VERSION}, name="pinned")))
    assert len(sdk.credentials) == 1
    assert len(sdk.client_options) == 1
    assert "/secrets/npm-token/?" in sdk.adapter.urls[0]
    assert f"/secrets/pinned/{_VERSION}?" in sdk.adapter.urls[1]
    assert sdk.client_options[0]["verify_challenge_resource"] is True


@pytest.mark.parametrize(
    ("status", "code", "falls_through"),
    [(404, "SecretNotFound", True), (404, "VaultNotFound", False), (404, "Unknown", False), (403, "Forbidden", False)],
)
def test_only_proven_secret_absence_falls_through(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str, falls_through: bool
) -> None:
    _SDK(monkeypatch, [(status, {}, {"error": {"code": code, "message": "sensitive-provider-text"}})])
    monkeypatch.setenv("AGW_KEYVAULT_FALLBACK", "fallback-value")
    fallback = ActiveSource(
        source=SecretSourceDecl(name="env-var", backend=CapabilityBlock.of("env-var")),
        backend_class=EnvVarBackend,
        config=EnvVarSourceConfig(name="env-var"),
        readiness=Readiness.ready(),
    )
    batch = resolve_batch(
        [
            SecretDecl(
                name="token",
                description="test",
                backend_mappings={"work-kv": "npm-token", "env-var": "AGW_KEYVAULT_FALLBACK"},
            )
        ],
        [_source(), fallback],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    if falls_through:
        assert batch.complete_or_raise() == {"token": "fallback-value"}
        assert batch.outcomes[0].source == "env-var"
    else:
        assert batch.outcomes[0].status is ResolutionStatus.FAILED
        assert batch.outcomes[0].reason is FailureReason.LOOKUP_REJECTED
        assert batch.outcomes[0].source == "work-kv"
    assert "sensitive-provider-text" not in repr(batch)


@pytest.mark.parametrize("value", [None, "bad\0value"])
def test_malformed_values_fail_preview_and_resolution(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    _SDK(monkeypatch, [(200, {}, {"value": value})])
    with _context(ResolutionIntent()) as client:
        result = client.resolve((_request(),))
    assert result == {"token": BackendFailed(FailureReason.MALFORMED_VALUE)}
    with _context(PreviewIntent(OperatorImpact.ALLOW)) as client:
        preview = client.preview((_request(),))
    assert preview == {"token": PreviewFailed(FailureReason.MALFORMED_VALUE)}


@pytest.mark.parametrize("impact", [OperatorImpact.NONE, OperatorImpact.ALLOW])
def test_preview_authentication_obeys_impact_ceiling(monkeypatch: pytest.MonkeyPatch, impact: OperatorImpact) -> None:
    sdk = _SDK(monkeypatch, [(401, _CHALLENGE, {})])
    sdk.credential_error = ClientAuthenticationError("sensitive-auth-detail")
    with _context(PreviewIntent(impact)) as client:
        result = client.preview((_request(),))
    if impact is OperatorImpact.NONE:
        assert result == {"token": PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)}
    else:
        assert result == {"token": PreviewFailed(FailureReason.AUTHENTICATION)}
    options = sdk.credential_options[0]
    assert options["exclude_interactive_browser_credential"] is (impact is OperatorImpact.NONE)
    assert options["exclude_visual_studio_code_credential"] is (impact is OperatorImpact.NONE)
    assert options["exclude_broker_credential"] is (impact is OperatorImpact.NONE)
    assert sdk.credentials[0].closed


def test_construction_readiness_and_description_do_not_start_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("provider constructed before selected operation")

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", forbidden)
    monkeypatch.setattr(azure.keyvault.secrets, "SecretClient", forbidden)
    with _context(ResolutionIntent()):
        pass
    assert AzureKeyVaultBackend.backend_readiness().is_ready
    assert AzureKeyVaultBackend.describe_lookup("token", None).identifier is None
    described = AzureKeyVaultBackend.describe_lookup("token", _request({"name": "pinned", "version": _VERSION}).mapping)
    assert described.identifier == f"pinned/{_VERSION}"


def test_provider_logs_are_contained_and_logging_is_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _SDK(monkeypatch, [(200, {}, {"value": "secret-value"})])
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("azure.identity.keyvault-test")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    monkeypatch.setenv("AGW_DEBUG", "1")

    def noisy_credential(**kwargs: object) -> _Credential:
        logger.exception("sensitive-provider-message")
        logging.getLogger("msal.new.keyvault-test").warning("secret-value")
        credential = _Credential()
        sdk.credentials.append(credential)
        return credential

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", noisy_credential)
    with _context(ResolutionIntent()) as client:
        assert client.resolve((_request(),)) == {"token": BackendResolved("secret-value")}
    assert output.getvalue() == ""
    assert logger.handlers == [handler]
    logger.warning("logging-restored")
    assert "logging-restored" in output.getvalue()


@pytest.mark.parametrize(
    "error", [requests.ConnectionError("sensitive-network-detail"), requests.ReadTimeout("secret")]
)
def test_transport_failures_remain_value_free_failures(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    sdk = _SDK(monkeypatch, [error])
    with _context(ResolutionIntent()) as client:
        result = client.resolve((_request(),))
    assert result == {"token": BackendFailed(FailureReason.CONNECTIVITY)}
    assert sdk.credentials[0].closed


def test_partial_provider_construction_is_closed_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _SDK(monkeypatch, [(200, {}, {"value": "unused"})])
    sdk.client_error = ValueError("sensitive-construction-detail")
    with _context(ResolutionIntent()) as client:
        result = client.resolve((_request(),))
    assert result == {"token": BackendFailed(FailureReason.EXTERNAL)}
    assert sdk.credentials[0].closed


def test_cleanup_failure_does_not_change_primary_resolution_or_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk = _SDK(monkeypatch, [(200, {}, {"value": "resolved"})])

    def broken_close(self: _Credential) -> None:
        self.closed = True
        logging.getLogger("azure.identity").error("sensitive-cleanup-detail")
        raise ValueError("sensitive-cleanup-detail")

    monkeypatch.setattr(_Credential, "close", broken_close)
    batch = resolve_batch(
        [SecretDecl(name="token", description="test", backend_mappings={"work-kv": "npm-token"})],
        [_source()],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"token": "resolved"}
    assert sdk.credentials[0].closed
    assert sdk.adapter.closed
    captured = capsys.readouterr()
    assert "sensitive-cleanup-detail" not in captured.out + captured.err


@pytest.mark.parametrize("error", [KeyboardInterrupt(), UserAbort("cancelled"), concurrent.futures.CancelledError()])
def test_cancellation_closes_credentials_and_preserves_exit(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    sdk = _SDK(monkeypatch, [(200, {}, {"value": "unused"})])
    sdk.client_error = error
    with pytest.raises(type(error)), _context(ResolutionIntent()) as client:
        client.resolve((_request(),))
    assert sdk.credentials[0].closed


@pytest.mark.parametrize(
    "url",
    [
        "http://safe-vault.vault.azure.net",
        "https://safe-vault.vault.azure.net.evil.test",
        "https://user@safe-vault.vault.azure.net",
        "https://safe-vault.vault.azure.net:8443",
        "https://safe-vault.vault.azure.net/secrets/token",
        "https://127.0.0.1",
        "https://safe--vault.vault.azure.net",
        "https://safe-vault.vault.azure.net?token=value",
    ],
)
def test_invalid_vault_endpoints_are_rejected_before_provider_work(url: str) -> None:
    with pytest.raises(ValidationError):
        AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url=url)


@pytest.mark.parametrize(
    ("suffix", "authority"),
    [
        ("vault.azure.net", "login.microsoftonline.com"),
        ("vault.usgovcloudapi.net", "login.microsoftonline.us"),
        ("vault.azure.cn", "login.chinacloudapi.cn"),
    ],
)
def test_supported_vaults_select_their_authentication_authority(suffix: str, authority: str) -> None:
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url=f"https://safe-vault.{suffix}/")
    assert vault_authority(config.vault_url) == authority


@pytest.mark.parametrize(
    "mapping", ["", "bad/name", {"name": "token", "version": "latest"}, {"name": "token", "unexpected": "value"}]
)
def test_invalid_mapping_input_is_rejected(mapping: object) -> None:
    with pytest.raises(ValidationError):
        AzureKeyVaultMapping.model_validate(mapping)
