"""Tests for EnvVarSource."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.secrets import EnvVarSource, SecretDecl, env_var_name_for

if TYPE_CHECKING:
    import pytest


def test_default_convention_uppercases_and_dashes_to_underscores() -> None:
    assert env_var_name_for("github-token") == "AW_SECRET_GITHUB_TOKEN"
    assert env_var_name_for("a") == "AW_SECRET_A"
    assert env_var_name_for("azdo-ifc-pat") == "AW_SECRET_AZDO_IFC_PAT"


def test_default_convention_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_GITHUB_TOKEN", "ghp_xxx")
    src = EnvVarSource()
    assert src.get(SecretDecl(name="github-token", description="GitHub PAT")) == "ghp_xxx"


def test_get_strips_trailing_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trailing newlines (the common ``op read`` / ``pbpaste`` artifact)
    are stripped so the value cleanly transports through SSH SetEnv."""
    monkeypatch.setenv("AW_SECRET_TOKEN", "ghp_xxx\n")
    src = EnvVarSource()
    assert src.get(SecretDecl(name="token", description="t")) == "ghp_xxx"


def test_get_strips_trailing_crlf(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRLF (Windows clipboard) trailing also stripped."""
    monkeypatch.setenv("AW_SECRET_TOKEN", "ghp_xxx\r\n")
    src = EnvVarSource()
    assert src.get(SecretDecl(name="token", description="t")) == "ghp_xxx"


def test_get_preserves_internal_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stripping is rstrip(newlines), not full strip(); internal spaces
    and leading whitespace are preserved (some token formats use them)."""
    monkeypatch.setenv("AW_SECRET_TOKEN", "  internal value  ")
    src = EnvVarSource()
    assert src.get(SecretDecl(name="token", description="t")) == "  internal value  "


def test_override_uses_alternate_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AW_SECRET_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "from-existing-env")
    src = EnvVarSource()
    decl = SecretDecl(
        name="github-token",
        description="GitHub PAT",
        backend_mappings={"env_var": "GITHUB_TOKEN"},
    )
    assert src.get(decl) == "from-existing-env"


def test_opt_out_returns_none_even_when_default_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """`backend_mappings.env_var = False` means skip even if AW_SECRET_<NAME> is set."""
    monkeypatch.setenv("AW_SECRET_FORCED", "value")
    src = EnvVarSource()
    decl = SecretDecl(
        name="forced",
        description="Force prompt only",
        backend_mappings={"env_var": False},
    )
    assert src.get(decl) is None


def test_returns_none_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AW_SECRET_UNSET", raising=False)
    src = EnvVarSource()
    assert src.get(SecretDecl(name="unset", description="...")) is None


def test_would_attempt_true_when_default_convention_applies() -> None:
    src = EnvVarSource()
    assert src.would_attempt(SecretDecl(name="x", description="X")) is True


def test_would_attempt_true_when_override_string() -> None:
    src = EnvVarSource()
    decl = SecretDecl(
        name="x",
        description="X",
        backend_mappings={"env_var": "EXISTING_X"},
    )
    assert src.would_attempt(decl) is True


def test_would_attempt_false_when_opted_out() -> None:
    src = EnvVarSource()
    decl = SecretDecl(name="x", description="X", backend_mappings={"env_var": False})
    assert src.would_attempt(decl) is False


def test_would_attempt_is_config_only_not_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """would_attempt returns True even when the env var isn't actually set --
    it answers 'will I try?', not 'will I succeed?'."""
    monkeypatch.delenv("AW_SECRET_X", raising=False)
    src = EnvVarSource()
    assert src.would_attempt(SecretDecl(name="x", description="X")) is True


def test_batch_get_returns_only_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_FOO", "foo-val")
    monkeypatch.delenv("AW_SECRET_BAR", raising=False)
    src = EnvVarSource()
    out = src.batch_get(
        [SecretDecl(name="foo", description="F"), SecretDecl(name="bar", description="B")]
    )
    assert out == {"foo": "foo-val"}
