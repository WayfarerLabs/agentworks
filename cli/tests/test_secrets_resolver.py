"""The per-operation ``Resolver``: registration, non-prompting
prediction, the single boundary resolve, strict cached ``get``, and the
late-registration guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.secrets.resolver import Resolver


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")

    def _make(extra: str = ""):
        path = tmp_path / "config.toml"
        path.write_text(
            f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
            '[secret_config]\nbackends = ["env-var"]\n'
            + extra
        )
        config = load_config(path, warn_issues=False, warn_deprecations=False)
        return config, build_registry(config)

    return _make


def test_register_name_synthesizes_when_registry_is_sparse(env) -> None:
    config, registry = env()
    resolver = Resolver(config, registry)
    decl = resolver.register_name("never-declared")
    assert decl.name == "never-declared"


def test_predict_reports_backend_or_none(env, monkeypatch: pytest.MonkeyPatch) -> None:
    config, registry = env()
    resolver = Resolver(config, registry)
    decl = resolver.register_name("some-token")

    monkeypatch.setenv("AW_SECRET_SOME_TOKEN", "v")
    assert resolver.predict(decl) == "env-var"

    monkeypatch.delenv("AW_SECRET_SOME_TOKEN")
    assert resolver.predict(decl) is None


def test_resolve_is_one_pass_and_idempotent(env, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.secrets import resolve as secrets_resolve

    config, registry = env()
    monkeypatch.setenv("AW_SECRET_SOME_TOKEN", "v1")

    calls: list[object] = []
    real = secrets_resolve.resolve_secrets

    def _counting(*args: object, **kwargs: object) -> dict[str, str]:
        calls.append(args)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_resolve, "resolve_secrets", _counting)

    resolver = Resolver(config, registry)
    resolver.register_name("some-token")
    resolver.resolve()
    resolver.resolve()  # idempotent while the set is unchanged
    assert len(calls) == 1
    assert resolver.get("some-token") == "v1"


def test_empty_set_resolves_without_touching_backends(env, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.secrets import resolve as secrets_resolve

    config, registry = env()
    monkeypatch.setattr(
        secrets_resolve,
        "resolve_secrets",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no backends for an empty set")),
    )
    resolver = Resolver(config, registry)
    resolver.resolve()
    assert resolver.resolved


def test_get_before_resolve_raises(env) -> None:
    config, registry = env()
    resolver = Resolver(config, registry)
    resolver.register_name("some-token")
    with pytest.raises(StateError, match="before the operation's resolve"):
        resolver.get("some-token")


def test_get_unregistered_name_raises(env, monkeypatch: pytest.MonkeyPatch) -> None:
    config, registry = env()
    resolver = Resolver(config, registry)
    resolver.resolve()
    with pytest.raises(StateError, match="not part of the operation's resolve"):
        resolver.get("never-registered")


def test_late_registration_then_resolve_raises(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Registering after the boundary pass and resolving again would be
    a second prompt session; the contract violation is loud."""
    config, registry = env()
    monkeypatch.setenv("AW_SECRET_EARLY", "v")
    resolver = Resolver(config, registry)
    resolver.register_name("early")
    resolver.resolve()
    resolver.register_name("late")
    with pytest.raises(StateError, match="registered after"):
        resolver.resolve()
