"""Provider-owned static runup and scoped materialization behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.git_credential.base import (
    CredentialPayload,
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
from agentworks.ssh import SSHError, SSHResult

if TYPE_CHECKING:
    from agentworks.git_credentials.nodes import GitCredentialNode
    from agentworks.transports import Transport


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


def _request(
    provider: GitCredentialProvider,
    values: dict[str, str],
    allowed: tuple[str, ...],
) -> CredentialRequest:
    node = MagicMock()
    node.name = "gh"
    node.provider = provider
    node.runup.side_effect = provider.runup

    def scoped_context(
        names: tuple[str, ...],
        *,
        admin_target=None,  # noqa: ANN001
        agent_target=None,  # noqa: ANN001
    ) -> RunContext:
        assert names == allowed
        return RunContext(
            admin_target=admin_target,
            agent_target=agent_target,
            secrets=ScopedSecrets(values, names),
        )

    node.secret_refs.return_value = allowed
    return CredentialRequest(
        cast("GitCredentialNode", node),
        provider.credential_scopes(),
        scoped_context,
    )


class _ShellTarget:
    def __init__(self, bin_dir: Path, args_path: Path, status: int) -> None:
        self.bin_dir = bin_dir
        self.args_path = args_path
        self.status = status

    def run(self, command: str, **kwargs: object) -> SSHResult:
        assert kwargs["check"] is False
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": str(self.bin_dir),
                "AGW_TEST_ARGS": str(self.args_path),
                "AGW_TEST_STATUS": str(self.status),
            },
        )
        return SSHResult(result.returncode, result.stdout, result.stderr)


def _stub_cli(bin_dir: Path, name: str) -> None:
    executable = bin_dir / name
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$AGW_TEST_ARGS"\n'
        "printf 'untrusted-stdout'\n"
        "printf 'untrusted-stderr' >&2\n"
        'exit "$AGW_TEST_STATUS"\n'
    )
    executable.chmod(0o755)


@pytest.mark.parametrize(
    ("provider", "command_name", "expected_args"),
    [
        (
            GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}),
            "gh",
            ("auth", "status", "--hostname", "github.com"),
        ),
        (
            AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "az-cli"}}),
            "az",
            ("account", "show", "--output", "none"),
        ),
    ],
)
@pytest.mark.parametrize(
    ("installed", "status", "role"),
    [(False, 0, "warning"), (True, 1, "warning"), (True, 0, "detail")],
)
def test_cli_runup_checks_current_target_without_forwarding_process_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: GitCredentialProvider,
    command_name: str,
    expected_args: tuple[str, ...],
    installed: bool,
    status: int,
    role: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_path = tmp_path / "args"
    if installed:
        _stub_cli(bin_dir, command_name)
    target = _ShellTarget(bin_dir, args_path, status)
    details: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.detail", details.append)
    monkeypatch.setattr("agentworks.output.warn", warnings.append)

    provider.runup(RunContext(agent_target=cast("Transport", target), secrets=ScopedSecrets({}, ())))

    assert bool(details) is (role == "detail")
    assert bool(warnings) is (role == "warning")
    assert all("untrusted" not in message for message in [*details, *warnings])
    if installed:
        assert tuple(args_path.read_text().splitlines()) == expected_args
    else:
        assert not args_path.exists()


@pytest.mark.parametrize(
    ("provider", "command_name"),
    [
        (GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}), "gh"),
        (AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "az-cli"}}), "az"),
    ],
)
def test_cli_runup_distinguishes_missing_command_from_unhealthy_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: GitCredentialProvider,
    command_name: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_path = tmp_path / "args"
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    missing_target = _ShellTarget(bin_dir, args_path, 1)
    provider.runup(RunContext(admin_target=cast("Transport", missing_target), secrets=ScopedSecrets({}, ())))
    _stub_cli(bin_dir, command_name)
    unhealthy_target = _ShellTarget(bin_dir, args_path, 1)
    provider.runup(RunContext(admin_target=cast("Transport", unhealthy_target), secrets=ScopedSecrets({}, ())))

    assert len(warnings) == 2
    assert warnings[0] != warnings[1]


@pytest.mark.parametrize(
    "provider",
    [
        GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}),
        AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "az-cli"}}),
    ],
)
def test_cli_runup_suppresses_transport_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    provider: GitCredentialProvider,
) -> None:
    target = MagicMock()
    target.run.side_effect = SSHError("untrusted transport detail")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    provider.runup(RunContext(agent_target=cast("Transport", target), secrets=ScopedSecrets({}, ())))
    assert len(warnings) == 1
    assert "untrusted transport detail" not in warnings[0]


@pytest.mark.parametrize(
    ("provider", "command_name"),
    [
        (GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}), "gh"),
        (AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "az-cli"}}), "az"),
    ],
)
@pytest.mark.parametrize("installed", [False, True])
def test_cli_runup_warning_keeps_managed_helper(
    tmp_path: Path,
    provider: GitCredentialProvider,
    command_name: str,
    installed: bool,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if installed:
        _stub_cli(bin_dir, command_name)
    request = _request(provider, {}, ())
    state = materialize_credential_state(
        (request,),
        cast("Transport", _ShellTarget(bin_dir, tmp_path / "args", 1)),
        "agent",
        _config(),
    )
    assert state.has_credentials


@pytest.mark.parametrize(
    "provider",
    [
        GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}),
        AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "az-cli"}}),
    ],
)
def test_disabled_runup_skips_cli_readiness_but_keeps_managed_helper(
    provider: GitCredentialProvider,
) -> None:
    target = MagicMock()
    state = materialize_credential_state(
        (_request(provider, {}, ()),),
        cast("Transport", target),
        "admin",
        _config(runup=False),
    )
    assert state.has_credentials
    target.run.assert_not_called()


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

    def credential_scopes(self) -> tuple[HttpsCredentialScope, ...]:
        return (HttpsCredentialScope("example.com"),)

    def credential_material(self, ctx: RunContext) -> CredentialPayload:
        joined = f"{ctx.secret(self.config.first)}:{ctx.secret(self.config.second)}"
        if self.config.output == "stored":
            payload: StoredCredential | ManagedHelper = StoredCredential("derived", joined)
        else:
            payload = ManagedHelper(b"#!/bin/sh\nexit 1\n", "fixed failure")
        return payload


@pytest.mark.parametrize("output", ["stored", "helper"])
def test_multiple_declared_inputs_are_orthogonal_to_provider_output(output: str) -> None:
    provider = _ExchangeProvider(
        "exchange",
        {"first": "first", "second": "second", "output": output},
    )
    context = _ctx({"first": "one", "second": "two"}, ("first", "second"))
    material = provider.credential_material(context)
    if output == "stored":
        assert material == StoredCredential("derived", "one:two")
    else:
        assert isinstance(material, ManagedHelper)


def test_materialization_skip_policy_reconciles_zero_survivors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", _probe(401))
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret"}})
    request = _request(provider, {"git-token-gh": "rejected"}, ("git-token-gh",))
    logger = MagicMock()
    state = materialize_credential_state((request,), MagicMock(), "admin", _config(), logger)
    assert not state.has_credentials
    logger.warning.assert_called_once()


def test_disabled_runup_still_materializes_final_stored_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled runup must not probe")

    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", explode)
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret"}})
    request = _request(provider, {"git-token-gh": "value"}, ("git-token-gh",))
    state = materialize_credential_state((request,), MagicMock(), "admin", _config(runup=False))
    assert state.has_credentials
