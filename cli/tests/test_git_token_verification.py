"""Provider-owned static runup and scoped materialization behavior."""

from __future__ import annotations

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
from agentworks.capabilities.git_credential.github import _GH_AUTH_CHECK, GitHubCredentialProvider
from agentworks.command_checks import run_user_shell_command
from agentworks.errors import StateError, TokenRejectedError, ValidationError
from agentworks.execution.results import (
    Completed,
    NotDelivered,
    NotDeliveredReason,
    Result,
    Unknown,
    UnknownReason,
)
from agentworks.git_credentials import CredentialRequest, materialize_credential_state
from agentworks.orchestration.secrets import ScopedSecrets
from agentworks.plugins.azure.azdo import _AZ_AUTH_CHECK, AzDOCredentialProvider
from agentworks.schema import AgwModel, NonEmptyStr, SecretRef
from tests.conftest import requires_posix_shell
from tests.vm_access_support import Admin, Agent, RecordingVMAccess, account_shell, command_status, local_vm_access

if TYPE_CHECKING:
    from agentworks.execution.access import VMAccess
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
        admin_access=None,  # noqa: ANN001
        agent_access=None,  # noqa: ANN001
    ) -> RunContext:
        assert names == allowed
        return RunContext(
            admin_access=admin_access,
            agent_access=agent_access,
            secrets=ScopedSecrets(values, names),
        )

    node.secret_refs.return_value = allowed
    return CredentialRequest(
        cast("GitCredentialNode", node),
        provider.credential_scopes(),
        scoped_context,
    )


def _cli_account(monkeypatch: pytest.MonkeyPatch, bin_dir: Path, args_path: Path, status: int) -> VMAccess:
    """A local account whose login ``PATH`` is ``bin_dir`` alone, where a stub CLI records its arguments."""
    home = bin_dir.parent / "home"
    home.mkdir()
    (home / ".profile").write_text(f'export PATH="{bin_dir}" AGW_TEST_ARGS="{args_path}" AGW_TEST_STATUS="{status}"\n')
    return local_vm_access(monkeypatch, home=home, shell=account_shell(bin_dir.parent))


def _stub_cli(bin_dir: Path, name: str) -> None:
    executable = bin_dir / name
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$AGW_TEST_ARGS"\n'
        '[ "$1 $2 $3" = "auth status --help" ] && { printf -- \'--active\\n\'; exit 0; }\n'
        "printf 'untrusted-stdout'\n"
        "printf 'untrusted-stderr' >&2\n"
        'exit "$AGW_TEST_STATUS"\n'
    )
    executable.chmod(0o755)


def _stub_unsupported_gh(bin_dir: Path) -> None:
    executable = bin_dir / "gh"
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$AGW_TEST_ARGS"\n'
        '[ "$1 $2 $3" = "auth status --help" ] && exit 1\n'
        "exit 90\n"
    )
    executable.chmod(0o755)


@pytest.mark.parametrize(
    ("provider", "command_name", "expected_args"),
    [
        (
            GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}),
            "gh",
            ("auth", "status", "--active", "--hostname", "github.com"),
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
@requires_posix_shell
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
    access = _cli_account(monkeypatch, bin_dir, args_path, status)
    details: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.detail", details.append)
    monkeypatch.setattr("agentworks.output.warn", warnings.append)

    provider.runup(RunContext(agent_access=access, secrets=ScopedSecrets({}, ())))

    assert bool(details) is (role == "detail")
    assert bool(warnings) is (role == "warning")
    assert all("untrusted" not in message for message in [*details, *warnings])
    if installed:
        assert tuple(args_path.read_text().splitlines()) == expected_args
    else:
        assert not args_path.exists()


@pytest.mark.parametrize(
    "provider",
    [
        GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}),
        AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "az-cli"}}),
    ],
)
@pytest.mark.parametrize(
    "outcomes",
    [
        (Result(Unknown(UnknownReason.CONNECTION), b"", b"", "untrusted transport detail"),),
        (Result(NotDelivered(NotDeliveredReason.PYTHON_MISSING), b"untrusted stdout", b"untrusted stderr"),),
        (command_status(0), Result(Completed(0), b"untrusted stdout", b"untrusted stderr")),
    ],
)
def test_cli_runup_warns_without_quoting_an_indeterminate_check(
    monkeypatch: pytest.MonkeyPatch,
    provider: GitCredentialProvider,
    outcomes: tuple[Result, ...],
) -> None:
    access = RecordingVMAccess([], Agent("dev", "agent-dev"))
    access.run_outcomes = list(outcomes)
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    provider.runup(RunContext(agent_access=access, secrets=ScopedSecrets({}, ())))
    assert len(warnings) == 1
    assert "untrusted" not in warnings[0]


@pytest.mark.parametrize(
    "provider",
    [
        GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}),
        AzDOCredentialProvider("ado", {"org": "my-org", "source": {"mode": "az-cli"}}),
    ],
)
def test_cli_runup_checks_are_bounded_by_a_ten_second_deadline(provider: GitCredentialProvider) -> None:
    access = RecordingVMAccess([], Agent("dev", "agent-dev"))
    access.default_outcome = command_status(0)
    provider.runup(RunContext(agent_access=access, secrets=ScopedSecrets({}, ())))
    assert access.requests()
    assert all(request.deadline == 10 for request in access.requests())


@requires_posix_shell
def test_github_cli_checks_active_status_support_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_unsupported_gh(bin_dir)
    details: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.detail", details.append)
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}})

    args_path = tmp_path / "args"
    provider.runup(
        RunContext(agent_access=_cli_account(monkeypatch, bin_dir, args_path, 0), secrets=ScopedSecrets({}, ()))
    )

    assert not details
    assert len(warnings) == 1
    assert tuple(args_path.read_text().splitlines()) == ("auth", "status", "--help")


@pytest.mark.parametrize(
    ("auth_check", "command_name"),
    [(_GH_AUTH_CHECK, "gh"), (_AZ_AUTH_CHECK, "az")],
)
@requires_posix_shell
def test_cli_auth_probe_does_not_treat_native_status_21_as_auth_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_check: str,
    command_name: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_cli(bin_dir, command_name)
    access = _cli_account(monkeypatch, bin_dir, tmp_path / "args", 21)

    assert run_user_shell_command(auth_check, access, deadline=10) == 22


@requires_posix_shell
def test_cli_runup_warning_keeps_managed_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_cli(bin_dir, "gh")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}})
    request = _request(provider, {}, ())
    state = materialize_credential_state(
        (request,),
        _cli_account(monkeypatch, bin_dir, tmp_path / "args", 1),
        "agent",
        _config(),
    )
    assert len(warnings) == 1
    assert state.has_credentials


def test_disabled_runup_skips_cli_readiness_but_keeps_managed_helper() -> None:
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}})
    access = RecordingVMAccess([], Admin())
    state = materialize_credential_state(
        (_request(provider, {}, ()),),
        access,
        "admin",
        _config(runup=False),
    )
    assert state.has_credentials
    assert access.calls == []


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
    contract_version: ClassVar[int] = 4
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
    state = materialize_credential_state((request,), RecordingVMAccess([], Admin()), "admin", _config(), logger)
    assert not state.has_credentials
    logger.warning.assert_called_once()


def test_disabled_runup_still_materializes_final_stored_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled runup must not probe")

    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", explode)
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "secret"}})
    request = _request(provider, {"git-token-gh": "value"}, ("git-token-gh",))
    state = materialize_credential_state((request,), RecordingVMAccess([], Admin()), "admin", _config(runup=False))
    assert state.has_credentials
