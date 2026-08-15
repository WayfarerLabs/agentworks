"""The per-operation ``Resolver``: registration, the single boundary
resolve, strict cached ``get``, and the late-registration guard.
(Resolvability prediction is central, not this object's: see
``tests/orchestration/test_secrets.py``.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.env import EnvEntry
from agentworks.errors import StateError, ValidationError
from agentworks.secrets import SecretTarget
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.resolve import ResolutionBatch
from agentworks.secrets.resolver import Resolver
from tests.conftest import ManifestDoc, write_manifests


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")

    def _make(extra: str = "", *, manifests: list[ManifestDoc] | None = None):
        path = tmp_path / "config.toml"
        path.write_text(
            f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
            '[secret_config]\nsources = ["env-var"]\n' + extra
        )
        if manifests:
            write_manifests(tmp_path, *manifests)
        config = load_config(path, warn_issues=False, warn_deprecations=False)
        return config, build_registry(config)

    return _make


def test_construction_rejects_a_non_enum_policy(env) -> None:
    """The check every ``Resolver(...)`` site inherits, including both shared VM
    boundaries. ``InteractionPolicy`` is a ``StrEnum`` and every consumer branches
    on it by identity, so a plain ``"refuse"`` is equal to the enum, fails the
    identity test, and resolves through an interactive source in a run that meant
    to refuse."""
    config, registry = env()
    with pytest.raises(StateError):
        Resolver(config, registry, interaction="refuse")  # type: ignore[arg-type]


def test_register_name_synthesizes_when_registry_is_sparse(env) -> None:
    config, registry = env()
    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    decl = resolver.register_name("never-declared")
    assert decl.name == "never-declared"


def test_resolve_is_one_pass_and_idempotent(env, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.secrets import resolve as secrets_resolve

    config, registry = env()
    monkeypatch.setenv("AW_SECRET_SOME_TOKEN", "v1")

    calls: list[object] = []
    real = secrets_resolve.resolve_batch

    def _counting(*args: Any, **kwargs: Any) -> ResolutionBatch:
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(secrets_resolve, "resolve_batch", _counting)

    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    resolver.register_name("some-token")
    resolver.resolve()
    resolver.resolve()  # idempotent while the set is unchanged
    assert len(calls) == 1
    assert resolver.get("some-token") == "v1"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("pretty\nvalue\n", id="lf-with-terminal-newline"),
        pytest.param("pretty\r\nvalue\r\n", id="crlf-with-terminal-newline"),
    ],
)
def test_operation_resolver_preserves_env_var_multiline_value(
    env,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    config, registry = env()
    monkeypatch.setenv("AW_SECRET_STRUCTURED", value)
    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    resolver.register_name("structured")

    resolver.resolve()

    assert resolver.get("structured") == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("environment-sentinel\nsecond\n", id="lf-with-terminal-newline"),
        pytest.param("environment-sentinel\r\nsecond\r\n", id="crlf-with-terminal-newline"),
    ],
)
def test_operation_resolver_rejects_multiline_environment_target_after_delivery(
    env,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    config, registry = env(manifests=[ManifestDoc("secret", "structured", description="environment input")])
    monkeypatch.setenv("AW_SECRET_STRUCTURED", value)
    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    resolver.register_targets([SecretTarget(vm={"STRUCTURED": EnvEntry({"secret": "structured"})})])

    with pytest.raises(ValidationError) as caught:
        resolver.resolve()

    assert not resolver.resolved
    assert "cannot be used for environment injection" in str(caught.value)
    assert "environment-sentinel" not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_empty_set_resolves_without_touching_backends(env, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.secrets import resolve as secrets_resolve

    config, registry = env()
    monkeypatch.setattr(
        secrets_resolve,
        "resolve_batch",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no backends for an empty set")),
    )
    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    resolver.resolve()
    assert resolver.resolved


def test_get_before_resolve_raises(env) -> None:
    config, registry = env()
    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    resolver.register_name("some-token")
    with pytest.raises(StateError, match="before the operation's resolve"):
        resolver.get("some-token")


def test_get_unregistered_name_raises(env, monkeypatch: pytest.MonkeyPatch) -> None:
    config, registry = env()
    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    resolver.resolve()
    with pytest.raises(StateError, match="not part of the operation's resolve"):
        resolver.get("never-registered")


def test_late_registration_then_resolve_raises(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Registering after the boundary pass and resolving again would be
    a second prompt session; the contract violation is loud."""
    config, registry = env()
    monkeypatch.setenv("AW_SECRET_EARLY", "v")
    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    resolver.register_name("early")
    resolver.resolve()
    resolver.register_name("late")
    with pytest.raises(StateError, match="registered after"):
        resolver.resolve()
