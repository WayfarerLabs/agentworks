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
        backend_mappings={"env_var": "X_TOKEN", "onepassword": False, "vault": {"path": "x"}},
    )
    assert d.backend_mappings["env_var"] == "X_TOKEN"
    assert d.backend_mappings["onepassword"] is False
    assert d.backend_mappings["vault"] == {"path": "x"}


def test_secret_backend_config_carries_kind() -> None:
    assert SecretBackendConfig(kind="env_var").kind == "env_var"


def test_secret_config_default_empty() -> None:
    assert SecretConfig().backends == ()


def test_secret_config_preserves_order() -> None:
    cfg = SecretConfig(backends=("env_var", "onepassword", "prompt"))
    assert cfg.backends == ("env_var", "onepassword", "prompt")


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
