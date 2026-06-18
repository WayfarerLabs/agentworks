"""Tests for the SecretSource protocol, the SecretSourceBase ABC, and the
dataclass shapes in agentworks.secrets.base.
"""

from __future__ import annotations

import pytest

from agentworks.secrets import (
    SecretBackendConfig,
    SecretConfig,
    SecretDecl,
    SecretSourceBase,
)


def test_secret_decl_defaults() -> None:
    d = SecretDecl(name="x", description="X")
    assert d.hint is None
    assert d.backend_mappings == {}


def test_secret_decl_carries_mappings() -> None:
    d = SecretDecl(
        name="x",
        description="X",
        backend_mappings={"env-var": "X_TOKEN", "onepassword": False, "vault": {"path": "x"}},
    )
    assert d.backend_mappings["env-var"] == "X_TOKEN"
    assert d.backend_mappings["onepassword"] is False
    assert d.backend_mappings["vault"] == {"path": "x"}


def test_secret_backend_config_carries_kind() -> None:
    assert SecretBackendConfig(kind="env-var").kind == "env-var"


def test_secret_config_default_chain() -> None:
    """SecretConfig defaults to the standard env-var + prompt chain when no
    [secret_config].backends is provided. Operators who don't use secrets
    pay nothing; operators who do get sensible zero-config resolution."""
    from agentworks.secrets.base import DEFAULT_BACKEND_CHAIN

    assert SecretConfig().backends == DEFAULT_BACKEND_CHAIN
    assert DEFAULT_BACKEND_CHAIN == ("env-var", "prompt")


def test_secret_config_explicit_empty_disables_chain() -> None:
    """An explicit empty list opts out of resolution entirely (different
    from absence-of-config, which gets the default chain)."""
    assert SecretConfig(backends=()).backends == ()


def test_secret_config_preserves_order() -> None:
    cfg = SecretConfig(backends=("env-var", "onepassword", "prompt"))
    assert cfg.backends == ("env-var", "onepassword", "prompt")


def test_base_class_default_batch_get_loops_get() -> None:
    """SecretSourceBase.batch_get loops .get and skips None values."""

    class _LoopSource(SecretSourceBase):
        kind = "loop"

        def __init__(self, values: dict[str, str]) -> None:
            self._values = values

        def would_attempt(self, secret: SecretDecl) -> bool:
            return True

        def get(self, secret: SecretDecl) -> str | None:
            return self._values.get(secret.name)

    src = _LoopSource(values={"a": "1", "c": "3"})
    out = src.batch_get(
        [
            SecretDecl(name="a", description="A"),
            SecretDecl(name="b", description="B"),
            SecretDecl(name="c", description="C"),
        ]
    )
    assert out == {"a": "1", "c": "3"}


def test_base_class_refuses_missing_abstract_methods() -> None:
    """A subclass missing an abstract method cannot be instantiated."""

    class _MissingGet(SecretSourceBase):
        kind = "missing-get"

        def would_attempt(self, secret: SecretDecl) -> bool:
            return True

    with pytest.raises(TypeError):
        _MissingGet()  # type: ignore[abstract]


def test_base_class_subclass_can_override_batch_get() -> None:
    """Overriding batch_get works; the default is not pinned."""

    class _CustomBatch(SecretSourceBase):
        kind = "custom"

        def would_attempt(self, secret: SecretDecl) -> bool:
            return True

        def get(self, secret: SecretDecl) -> str | None:
            return None  # should not be called

        def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
            return {s.name: f"batched-{s.name}" for s in secrets}

    out = _CustomBatch().batch_get([SecretDecl(name="x", description="X")])
    assert out == {"x": "batched-x"}
