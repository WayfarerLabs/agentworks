"""Token verification at provisioning entry and in doctor (Part B of
the #166/#167 token-UX design).

``acquire_token(resolved_secret) -> TokenInfo`` is the transformation
seam: today the providers verify the mapped secret's value against
their API and return it enriched; tomorrow a minting provider can
exchange a bootstrap secret for a fresh token without framework
changes. Policy: definitive rejection raises ``TokenRejectedError``
(safe -- callers sit at manager entry, before any mutation); network
indeterminacy warns and continues unverified. The suite-wide conftest
guard makes any unmocked probe look like a network failure, so no test
can reach the real network.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import TokenRejectedError
from agentworks.git_credentials.azdo import AzDOCredentialProvider
from agentworks.git_credentials.base import (
    GitCredentialProvider,
    HelperEntry,
    TokenInfo,
)
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


# -- github ---------------------------------------------------------------


def test_github_200_verifies_with_login_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _probe(
        200,
        b'{"login": "wfscot"}',
        {_EXPIRY_HEADER: "2026-10-01 17:24:32 UTC"},
    )
    monkeypatch.setattr("agentworks.git_credentials.base._http_probe", fake)
    p = GitHubCredentialProvider(config_name="gh")
    info = p.acquire_token({p.secret_name: "tok"})
    assert info == TokenInfo(
        token="tok", login="wfscot", expires_at=date(2026, 10, 1), verified=True
    )
    (url, headers), = fake.calls  # type: ignore[attr-defined]
    assert url == "https://api.github.com/user"
    assert headers["Authorization"] == "Bearer tok"


def test_github_401_is_definitive_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe", _probe(401)
    )
    p = GitHubCredentialProvider(config_name="gh", secret_name="my-secret")
    with pytest.raises(TokenRejectedError, match="rejected the token") as exc:
        p.acquire_token({p.secret_name: "bogus"})
    assert "'gh'" in str(exc.value)
    assert "'my-secret'" in str(exc.value)
    assert "verify_git_tokens = false" in (exc.value.hint or "")


def test_github_other_status_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe", _probe(503)
    )
    p = GitHubCredentialProvider(config_name="gh")
    info = p.acquire_token({p.secret_name: "tok"})
    assert info.token == "tok"
    assert not info.verified
    assert "could not verify" in capsys.readouterr().err


def test_network_failure_warns_and_continues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No probe monkeypatch here: the conftest guard IS the network
    failure, proving both the guard and the indeterminacy path."""
    p = GitHubCredentialProvider(config_name="gh")
    info = p.acquire_token({p.secret_name: "tok"})
    assert info.token == "tok"
    assert not info.verified
    assert "could not verify" in capsys.readouterr().err


def test_expiry_header_format_drift_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _probe(200, b'{"login": "x"}', {_EXPIRY_HEADER: "soonish"})
    monkeypatch.setattr("agentworks.git_credentials.base._http_probe", fake)
    info = GitHubCredentialProvider(config_name="gh").acquire_token({"git-token-gh": "t"})
    assert info.verified
    assert info.expires_at is None


# -- azdo -------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 203])
def test_azdo_rejection_statuses(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe", _probe(status)
    )
    p = AzDOCredentialProvider(config_name="ado", org="my-org")
    with pytest.raises(TokenRejectedError, match="Azure DevOps rejected"):
        p.acquire_token({p.secret_name: "bogus"})


def test_azdo_200_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _probe(200)
    monkeypatch.setattr("agentworks.git_credentials.base._http_probe", fake)
    p = AzDOCredentialProvider(config_name="ado", org="my-org")
    assert p.acquire_token({p.secret_name: "tok"}).verified
    (url, headers), = fake.calls  # type: ignore[attr-defined]
    assert url == "https://dev.azure.com/my-org/_apis/connectionData"
    assert headers["Authorization"].startswith("Basic ")


def test_base_acquire_is_unverified_identity() -> None:
    class _Bare(GitCredentialProvider):
        provider_name = "bare"

        def credential_lines(self, token: str) -> list[str]:
            return []

        def helper_entry(self) -> HelperEntry:
            return HelperEntry(host="example.com", username=self.store_username)

    info = _Bare("b").acquire_token({"git-token-b": "tok"})
    assert info == TokenInfo(token="tok")


# -- collector wiring (provisioning entry) ----------------------------------


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
    collection -- which runs at manager entry, before anything is
    created."""
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


# -- doctor rows --------------------------------------------------------------


def _doctor_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, extra: str = ""):  # noqa: ANN202
    from agentworks.doctor import _check_config

    config = _config_with_github_cred(tmp_path, extra=extra)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config.source_path)
    g, _config, _registry = _check_config()
    return g


def test_doctor_verified_token_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "goodtok")
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe",
        _probe(200, b'{"login": "wfscot"}'),
    )
    g = _doctor_group(tmp_path, monkeypatch)
    rows = [c for c in g.checks if c.name == "Git token 'gh'"]
    assert rows and rows[0].message is not None
    assert "login wfscot" in rows[0].message


def test_doctor_rejected_token_is_fail_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.doctor import Status

    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "bogus")
    monkeypatch.setattr(
        "agentworks.git_credentials.base._http_probe", _probe(401)
    )
    g = _doctor_group(tmp_path, monkeypatch)
    rows = [c for c in g.checks if c.name == "Git token 'gh'"]
    assert rows and rows[0].status == Status.FAIL


def test_doctor_skips_when_not_resolvable_non_interactively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AW_SECRET_GIT_TOKEN_GH", raising=False)
    g = _doctor_group(tmp_path, monkeypatch)
    rows = [c for c in g.checks if c.name == "Git token 'gh'"]
    assert rows and "skipped" in (rows[0].message or "")
    assert "AW_SECRET_GIT_TOKEN_GH" in (rows[0].message or "")
