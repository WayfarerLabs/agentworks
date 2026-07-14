"""Authenticated token verification at the capability ``runup()`` stage.

``runup()`` is the post-resolve readiness stage: the provider reads its
resolved PAT from the operation's resolver and probes it against the
host (github ``GET /user``, azdo connectionData). Policy: a definitive
rejection raises ``TokenRejectedError`` (safe -- runup runs before any
VM/user mutation); network indeterminacy warns and continues unverified.
The suite-wide conftest guard makes any unmocked probe look like a
network failure, so no test can reach the real network.

Doctor no longer runs an authenticated token check (it is preflight-only,
relying on the Secrets group for resolvability); on-demand authenticated
checking is the deferred ``doctor --verify`` (issue #176).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.base import RunContext
from agentworks.config import load_config
from agentworks.errors import TokenRejectedError
from agentworks.git_credentials.azdo import AzDOCredentialProvider
from agentworks.git_credentials.github import GitHubCredentialProvider
from agentworks.vms.manager import _collect_git_tokens

_EXPIRY_HEADER = "github-authentication-token-expiration"


def _probe(status: int, body: bytes = b"{}", headers: dict[str, str] | None = None):  # noqa: ANN202
    calls: list[tuple[str, dict[str, str]]] = []

    def fake(url: str, req_headers: dict[str, str], *, timeout: float = 5.0):  # noqa: ANN202, ARG001
        calls.append((url, req_headers))
        return (status, body, headers or {})

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


class _StubResolver:
    """Minimal resolver stand-in: a provider registers its token secret
    at construct and reads the value back in ``runup()``."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    def register_name(self, name: str) -> object:  # noqa: ANN401 (matches Resolver)
        return name

    def get(self, name: str) -> str:
        return self._tokens[name]


# -- github ---------------------------------------------------------------


def test_github_200_verifies_with_login_and_expiry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _probe(
        200,
        b'{"login": "wfscot"}',
        {_EXPIRY_HEADER: "2026-10-01 17:24:32 UTC"},
    )
    monkeypatch.setattr("agentworks.git_credentials.base._http_probe", fake)
    p = GitHubCredentialProvider("gh", {}, _StubResolver({"git-token-gh": "tok"}))
    p.runup(RunContext(secrets=p.resolver))
    out = capsys.readouterr().out
    assert "Verified git token for 'gh'" in out
    assert "login wfscot" in out
    assert "expires 2026-10-01" in out
    (url, headers), = fake.calls  # type: ignore[attr-defined]
    assert url == "https://api.github.com/user"
    assert headers["Authorization"] == "Bearer tok"


def test_github_401_is_definitive_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe", _probe(401)
    )
    p = GitHubCredentialProvider(
        "gh", {"token": "my-secret"}, _StubResolver({"my-secret": "bogus"})
    )
    with pytest.raises(TokenRejectedError, match="rejected the token") as exc:
        p.runup(RunContext(secrets=p.resolver))
    assert "'gh'" in str(exc.value)
    assert "'my-secret'" in str(exc.value)
    assert "verify_git_tokens = false" in (exc.value.hint or "")


def test_github_other_status_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe", _probe(503)
    )
    p = GitHubCredentialProvider("gh", {}, _StubResolver({"git-token-gh": "tok"}))
    p.runup(RunContext(secrets=p.resolver))
    assert "could not verify" in capsys.readouterr().err


def test_network_failure_warns_and_continues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No probe monkeypatch here: the conftest guard IS the network
    failure, proving both the guard and the indeterminacy path."""
    p = GitHubCredentialProvider("gh", {}, _StubResolver({"git-token-gh": "tok"}))
    p.runup(RunContext(secrets=p.resolver))
    assert "could not verify" in capsys.readouterr().err


def test_expiry_header_format_drift_tolerated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _probe(200, b'{"login": "x"}', {_EXPIRY_HEADER: "soonish"})
    monkeypatch.setattr("agentworks.git_credentials.base._http_probe", fake)
    p = GitHubCredentialProvider("gh", {}, _StubResolver({"git-token-gh": "t"}))
    p.runup(RunContext(secrets=p.resolver))
    out = capsys.readouterr().out
    assert "Verified git token for 'gh'" in out
    assert "login x" in out
    assert "expires" not in out  # drift -> no expiry shown


def test_github_runup_without_secrets_is_error() -> None:
    """A runup with no resolved secrets in the context (inspection) is
    a typed error, not a crash."""
    from agentworks.errors import ConfigError

    p = GitHubCredentialProvider("gh", {})
    with pytest.raises(ConfigError, match="resolved secrets"):
        p.runup(RunContext())


# -- azdo -------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 203])
def test_azdo_rejection_statuses(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe", _probe(status)
    )
    p = AzDOCredentialProvider(
        "ado", {"org": "my-org"}, _StubResolver({"git-token-ado": "bogus"})
    )
    with pytest.raises(TokenRejectedError, match="Azure DevOps rejected"):
        p.runup(RunContext(secrets=p.resolver))


def test_azdo_200_verifies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _probe(200)
    monkeypatch.setattr("agentworks.git_credentials.base._http_probe", fake)
    p = AzDOCredentialProvider(
        "ado", {"org": "my-org"}, _StubResolver({"git-token-ado": "tok"})
    )
    p.runup(RunContext(secrets=p.resolver))
    assert "Verified git token for 'ado'" in capsys.readouterr().out
    (url, headers), = fake.calls  # type: ignore[attr-defined]
    assert url == "https://dev.azure.com/my-org/_apis/connectionData"
    assert headers["Authorization"].startswith("Basic ")


# -- collector wiring (agent-create token pass) -----------------------------


def _config_with_github_cred(tmp_path: Path, *, extra: str = ""):  # noqa: ANN202
    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        {extra}
        [git_credentials.gh]
        provider = "github"
        """)
    )
    return load_config(cfg, warn_issues=False)


def test_collect_aborts_on_rejected_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry-point contract: a definitively rejected token aborts
    collection -- which runs before anything is created."""
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "bogus")
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe", _probe(401)
    )
    config = _config_with_github_cred(tmp_path)
    registry = build_registry(config)
    with pytest.raises(TokenRejectedError, match="'gh'"):
        _collect_git_tokens(config, registry, ["gh"])


def test_collect_verifies_and_announces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "goodtok")
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe",
        _probe(200, b'{"login": "wfscot"}', {_EXPIRY_HEADER: "2026-10-01 00:00:00 UTC"}),
    )
    config = _config_with_github_cred(tmp_path)
    registry = build_registry(config)
    tokens = _collect_git_tokens(config, registry, ["gh"])
    assert tokens == {"gh": "goodtok"}
    out = capsys.readouterr().out
    assert "Verified git token for 'gh'" in out
    assert "login wfscot" in out
    assert "expires 2026-10-01" in out


def test_collect_skips_verification_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "whatever")

    def _explode(*_a: object, **_k: object) -> object:
        raise AssertionError("probe must not be called with verification off")

    monkeypatch.setattr("agentworks.git_credentials.base._http_probe", _explode)
    config = _config_with_github_cred(
        tmp_path,
        extra="[defaults]\nverify_git_tokens = false\n",
    )
    assert config.defaults.verify_git_tokens is False
    registry = build_registry(config)
    tokens = _collect_git_tokens(config, registry, ["gh"])
    assert tokens == {"gh": "whatever"}
