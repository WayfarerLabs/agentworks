"""Provider-owned static runup and scoped materialization behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.git_credential.base import (
    CredentialMaterial,
    GitCredentialProvider,
    HttpsCredentialScope,
    ManagedHelper,
    StoredCredential,
)
from agentworks.capabilities.git_credential.github import GitHubCredentialProvider
from agentworks.errors import StateError, TokenRejectedError, ValidationError
from agentworks.git_credentials import CredentialRequest, materialize_credential_state
from agentworks.orchestration.secrets import ScopedSecrets
from agentworks.plugins.azure.azdo import AzDOCredentialProvider
from agentworks.schema import AgwModel, NonEmptyStr, SecretRef

if TYPE_CHECKING:
    from agentworks.git_credentials.nodes import GitCredentialNode


def _probe(status: int, body: bytes = b"{}", headers: dict[str, str] | None = None):  # noqa: ANN202
    calls: list[tuple[str, dict[str, str]]] = []

    def fake(url: str, request_headers: dict[str, str], *, timeout: float = 5.0):  # noqa: ANN202, ARG001
        calls.append((url, request_headers))
        return (status, body, headers or {})

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def _ctx(values: dict[str, str], allowed: tuple[str, ...]) -> RunContext:
    return RunContext(secrets=ScopedSecrets(values, allowed))


def _config(*, runup: bool = True):  # noqa: ANN202
    config = MagicMock()
    config.defaults.runup_git_credentials = runup
    return config


def _request(provider: GitCredentialProvider, context: RunContext) -> CredentialRequest:
    node = MagicMock()
    node.name = "gh"
    node.provider = provider
    node.runup.side_effect = provider.runup
    return CredentialRequest(cast("GitCredentialNode", node), context)


def test_github_secret_runup_uses_declared_input_and_authenticated_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _probe(200, b'{"login": "operator"}')
    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", fake)
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret", "secret": "github-input"}})
    provider.runup(_ctx({"github-input": "credential-value"}, ("github-input",)))
    ((url, headers),) = fake.calls  # type: ignore[attr-defined]
    assert url == "https://api.github.com/user"
    assert headers["Authorization"] == "Bearer credential-value"


@pytest.mark.parametrize("status", [401])
def test_github_secret_runup_definitively_rejects_bad_input(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", _probe(status))
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret"}})
    with pytest.raises(TokenRejectedError):
        provider.runup(_ctx({"git-token-gh": "rejected"}, ("git-token-gh",)))


@pytest.mark.parametrize("status", [401, 203])
def test_azdo_secret_runup_preserves_existing_org_probe(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    fake = _probe(status)
    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", fake)
    provider = AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "secret"}})
    with pytest.raises(TokenRejectedError):
        provider.runup(_ctx({"git-token-ado": "rejected"}, ("git-token-ado",)))
    ((url, headers),) = fake.calls  # type: ignore[attr-defined]
    assert url == "https://dev.azure.com/my-org/_apis/connectionData"
    assert headers["Authorization"].startswith("Basic ")


def test_cli_arms_do_no_provisioning_time_command_or_network_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("CLI-backed runup must not probe")

    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", explode)
    GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}).runup(_ctx({}, ()))
    AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "az-cli"}}).runup(_ctx({}, ()))


@pytest.mark.parametrize("value", ["line\nfeed", "carriage\rreturn", "nul\0byte"])
def test_secret_input_is_line_safe_before_probe_or_materialization(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unsafe value reached authenticated probe")

    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", explode)
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret"}})
    context = _ctx({"git-token-gh": value}, ("git-token-gh",))
    with pytest.raises(ValidationError):
        provider.runup(context)
    with pytest.raises(ValidationError):
        provider.credential_material(context)


def test_scoped_delivery_refuses_undeclared_provider_reads() -> None:
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret", "secret": "declared"}})
    with pytest.raises(StateError):
        provider.credential_material(_ctx({"declared": "value"}, ()))


class _ExchangeConfig(AgwModel):
    name: Literal["exchange"]
    first: Annotated[NonEmptyStr, SecretRef(usage="the first exchange input")]
    second: Annotated[NonEmptyStr, SecretRef(usage="the second exchange input")]
    output: Literal["stored", "helper"]


class _ExchangeProvider(GitCredentialProvider):
    contract_version: ClassVar[int] = 3
    name: ClassVar[str] = "exchange"
    description: ClassVar[str] = "test exchange provider"
    config_model: ClassVar[type[_ExchangeConfig]] = _ExchangeConfig

    @property
    def config(self) -> _ExchangeConfig:
        return self._config_as(_ExchangeConfig)

    def credential_material(self, ctx: RunContext) -> CredentialMaterial:
        joined = f"{ctx.secret(self.config.first)}:{ctx.secret(self.config.second)}"
        if self.config.output == "stored":
            payload: StoredCredential | ManagedHelper = StoredCredential("derived", joined)
        else:
            payload = ManagedHelper(b"#!/bin/sh\nexit 1\n", "fixed failure")
        return CredentialMaterial((HttpsCredentialScope("example.com"),), payload)


@pytest.mark.parametrize("output", ["stored", "helper"])
def test_multiple_declared_inputs_are_orthogonal_to_provider_output(output: str) -> None:
    provider = _ExchangeProvider(
        "exchange",
        {"first": "first", "second": "second", "output": output},
    )
    context = _ctx({"first": "one", "second": "two"}, ("first", "second"))
    material = provider.credential_material(context)
    if output == "stored":
        assert material.payload == StoredCredential("derived", "one:two")
    else:
        assert isinstance(material.payload, ManagedHelper)


def test_materialization_skip_policy_reconciles_zero_survivors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", _probe(401))
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret"}})
    request = _request(provider, _ctx({"git-token-gh": "rejected"}, ("git-token-gh",)))
    logger = MagicMock()
    state, count = materialize_credential_state((request,), _config(), logger)
    assert count == 0
    assert not state.has_credentials
    logger.warning.assert_called_once()


def test_disabled_runup_still_materializes_final_stored_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled runup must not probe")

    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", explode)
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret"}})
    request = _request(provider, _ctx({"git-token-gh": "value"}, ("git-token-gh",)))
    state, count = materialize_credential_state((request,), _config(runup=False))
    assert count == 1
    assert state.has_credentials
