"""``Resolver.seed``: the gate-to-boundary seam (orchestration-layer
SDD, Phase 1).

The activation gate resolves its narrow just-in-time secrets before
the boundary pass; seeding hands those values to the operation's
resolver so (a) the platform's power ops, which read the BOUND
resolver pre-boundary (proxmox's ``status``), see them immediately,
and (b) the boundary pass excludes them, so nothing resolves or
prompts twice in one command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from agentworks.errors import StateError
from agentworks.secrets.resolver import Resolver

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl


class _EmptyRegistry:
    """No declared secrets: every name falls back to a bare decl."""

    def lookup(self, kind: str, name: str) -> object:
        raise KeyError(name)


class _FakeBackend:
    def __init__(self, name: str, values: dict[str, str]) -> None:
        self.name = name
        self.interactive = False
        self._values = values
        self.resolve_calls: list[list[str]] = []

    def would_attempt(self, secret: SecretDecl) -> bool:
        return True

    def describe_lookup(self, secret: SecretDecl) -> str | None:
        return None

    def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
        self.resolve_calls.append([s.name for s in secrets])
        return {s.name: self._values[s.name] for s in secrets if s.name in self._values}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    from agentworks.secrets import resolve as secrets_resolve

    fake = _FakeBackend("fake", {"git-token-gh": "ghtok", "proxmox-token": "pve"})
    monkeypatch.setattr(
        secrets_resolve, "active_backends", lambda config, registry: [fake]
    )
    return fake


def _resolver() -> Resolver:
    return Resolver(cast("Config", object()), cast("Registry", _EmptyRegistry()))


def test_seeded_value_is_readable_before_the_boundary_pass(
    backend: _FakeBackend,
) -> None:
    """The seam's whole point: the gate's power ops read the bound
    resolver before the boundary pass runs."""
    resolver = _resolver()
    resolver.seed({"proxmox-token": "pve"})
    assert not resolver.resolved
    assert resolver.get("proxmox-token") == "pve"


def test_unseeded_pre_pass_read_still_raises(backend: _FakeBackend) -> None:
    resolver = _resolver()
    resolver.seed({"proxmox-token": "pve"})
    with pytest.raises(StateError, match="before the operation's resolve"):
        resolver.get("git-token-gh")


def test_boundary_pass_excludes_seeded_names(backend: _FakeBackend) -> None:
    """No secret resolves twice: the backend loop covers only the
    un-seeded remainder, and the cache serves both."""
    resolver = _resolver()
    resolver.register_name("git-token-gh")
    resolver.seed({"proxmox-token": "gate-value"})
    resolver.resolve()
    assert backend.resolve_calls == [["git-token-gh"]]
    assert resolver.get("proxmox-token") == "gate-value"
    assert resolver.get("git-token-gh") == "ghtok"
    assert resolver.values == {
        "proxmox-token": "gate-value",
        "git-token-gh": "ghtok",
    }


def test_all_seeded_resolve_skips_the_backend_loop(backend: _FakeBackend) -> None:
    resolver = _resolver()
    resolver.seed({"proxmox-token": "pve"})
    resolver.resolve()
    assert backend.resolve_calls == []
    assert resolver.values == {"proxmox-token": "pve"}


def test_resolve_stays_idempotent_with_seeded_names(backend: _FakeBackend) -> None:
    resolver = _resolver()
    resolver.seed({"proxmox-token": "pve"})
    resolver.resolve()
    resolver.resolve()  # no raise: the seeded name is in the cache
    assert backend.resolve_calls == []


def test_seeding_after_the_pass_is_a_loud_error(backend: _FakeBackend) -> None:
    """Same contract as post-pass registration: a value the pass never
    covered must not quietly widen the cache."""
    resolver = _resolver()
    resolver.resolve()
    with pytest.raises(StateError, match="seeded after"):
        resolver.seed({"proxmox-token": "pve"})
