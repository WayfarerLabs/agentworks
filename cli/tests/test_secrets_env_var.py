"""Tests for the env-var provider, exercised through the backend door
(``SecretBackendDecl`` methods) -- the only way runtime code reaches a
provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.secrets import SecretBackendDecl, SecretDecl, env_var_name_for

if TYPE_CHECKING:
    import pytest


def _backend(name: str = "env-var") -> SecretBackendDecl:
    """A backend exposing the env-var provider. The built-in's name
    coincides with the provider's; ``_named`` variants exercise the
    name/provider split."""
    return SecretBackendDecl(name=name, provider="env-var")


def test_default_convention_uppercases_and_dashes_to_underscores() -> None:
    assert env_var_name_for("github-token") == "AW_SECRET_GITHUB_TOKEN"
    assert env_var_name_for("a") == "AW_SECRET_A"
    assert env_var_name_for("azdo-ifc-pat") == "AW_SECRET_AZDO_IFC_PAT"


def test_default_convention_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_GITHUB_TOKEN", "ghp_xxx")
    decl = SecretDecl(name="github-token", description="GitHub PAT")
    assert _backend().resolve([decl]) == {"github-token": "ghp_xxx"}


def test_resolve_strips_trailing_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trailing newlines (the common ``op read`` / ``pbpaste`` artifact)
    are stripped so the value cleanly transports through SSH SetEnv."""
    monkeypatch.setenv("AW_SECRET_TOKEN", "ghp_xxx\n")
    decl = SecretDecl(name="token", description="t")
    assert _backend().resolve([decl]) == {"token": "ghp_xxx"}


def test_resolve_strips_trailing_crlf(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRLF (Windows clipboard) trailing also stripped."""
    monkeypatch.setenv("AW_SECRET_TOKEN", "ghp_xxx\r\n")
    decl = SecretDecl(name="token", description="t")
    assert _backend().resolve([decl]) == {"token": "ghp_xxx"}


def test_resolve_preserves_internal_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stripping is rstrip(newlines), not full strip(); internal spaces
    and leading whitespace are preserved (some token formats use them)."""
    monkeypatch.setenv("AW_SECRET_TOKEN", "  internal value  ")
    decl = SecretDecl(name="token", description="t")
    assert _backend().resolve([decl]) == {"token": "  internal value  "}


def test_override_uses_alternate_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AW_SECRET_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "from-existing-env")
    decl = SecretDecl(
        name="github-token",
        description="GitHub PAT",
        backend_mappings={"env-var": "GITHUB_TOKEN"},
    )
    assert _backend().resolve([decl]) == {"github-token": "from-existing-env"}


def test_mapping_keyed_by_backend_name_not_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A differently-named backend on the same provider reads ITS OWN
    mapping entry -- the name/provider split, end to end."""
    monkeypatch.setenv("AW_SECRET_TOKEN", "default-convention")
    monkeypatch.setenv("SIBLING_TOKEN", "sibling-mapped")
    decl = SecretDecl(
        name="token",
        description="t",
        backend_mappings={"sibling-env": "SIBLING_TOKEN"},
    )
    sibling = _backend(name="sibling-env")
    assert sibling.resolve([decl]) == {"token": "sibling-mapped"}
    # The built-in backend has no mapping entry -> default convention.
    assert _backend().resolve([decl]) == {"token": "default-convention"}


def test_opt_out_is_per_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """``backend_mappings.<name> = false`` opts out ONE backend; a
    sibling backend on the same provider still attempts."""
    monkeypatch.setenv("AW_SECRET_FORCED", "value")
    decl = SecretDecl(
        name="forced",
        description="Force prompt only",
        backend_mappings={"env-var": False},
    )
    assert _backend().would_attempt(decl) is False
    assert _backend(name="sibling-env").would_attempt(decl) is True


def test_resolve_omits_unset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset env var is a soft miss: absent from the result."""
    monkeypatch.setenv("AW_SECRET_FOO", "foo-val")
    monkeypatch.delenv("AW_SECRET_BAR", raising=False)
    out = _backend().resolve(
        [SecretDecl(name="foo", description="F"), SecretDecl(name="bar", description="B")]
    )
    assert out == {"foo": "foo-val"}


def test_would_attempt_true_when_default_convention_applies() -> None:
    assert _backend().would_attempt(SecretDecl(name="x", description="X")) is True


def test_would_attempt_is_config_only_not_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """would_attempt returns True even when the env var isn't actually set --
    it answers 'will I try?', not 'will I succeed?'."""
    monkeypatch.delenv("AW_SECRET_X", raising=False)
    assert _backend().would_attempt(SecretDecl(name="x", description="X")) is True


def test_describe_lookup_returns_default_convention() -> None:
    """Without an override, describe_lookup returns ``AW_SECRET_<UPPER>``."""
    decl = SecretDecl(name="github-token", description="...")
    assert _backend().describe_lookup(decl) == "AW_SECRET_GITHUB_TOKEN"


def test_describe_lookup_returns_override() -> None:
    """A ``backend_mappings.<name>`` string override wins over the default."""
    decl = SecretDecl(
        name="github-token",
        description="...",
        backend_mappings={"env-var": "GITHUB_TOKEN"},
    )
    assert _backend().describe_lookup(decl) == "GITHUB_TOKEN"


def test_describe_lookup_returns_none_when_opted_out() -> None:
    """``backend_mappings.<name> = False`` opts the backend out entirely,
    so there's no identifier to describe."""
    decl = SecretDecl(
        name="forced",
        description="...",
        backend_mappings={"env-var": False},
    )
    assert _backend().describe_lookup(decl) is None


def test_backend_is_not_interactive() -> None:
    assert _backend().interactive is False
