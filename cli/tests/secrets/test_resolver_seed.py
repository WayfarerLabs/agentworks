"""``Resolver.seed``: the gate-to-boundary seam.

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


class _ResolveSpy:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, secrets: list[SecretDecl], sources: object, **kwargs: object) -> dict[str, str]:
        self.calls.append([secret.name for secret in secrets])
        return {secret.name: "ghtok" for secret in secrets}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _ResolveSpy:
    from agentworks.secrets import resolve as secrets_resolve

    spy = _ResolveSpy()
    monkeypatch.setattr(secrets_resolve, "active_backends", lambda config, registry: [])
    monkeypatch.setattr(secrets_resolve, "resolve_secrets", spy)
    return spy


def _resolver() -> Resolver:
    return Resolver(cast("Config", object()), cast("Registry", _EmptyRegistry()))


def test_seeded_value_is_readable_before_the_boundary_pass(
    backend: _ResolveSpy,
) -> None:
    """The seam's whole point: the gate's power ops read the bound
    resolver before the boundary pass runs."""
    resolver = _resolver()
    resolver.seed({"proxmox-token": "pve"})
    assert not resolver.resolved
    assert resolver.get("proxmox-token") == "pve"


def test_unseeded_pre_pass_read_still_raises(backend: _ResolveSpy) -> None:
    resolver = _resolver()
    resolver.seed({"proxmox-token": "pve"})
    with pytest.raises(StateError, match="before the operation's resolve"):
        resolver.get("git-token-gh")


def test_boundary_pass_excludes_seeded_names(backend: _ResolveSpy) -> None:
    """No secret resolves twice: the backend loop covers only the
    un-seeded remainder, and the cache serves both."""
    resolver = _resolver()
    resolver.register_name("git-token-gh")
    resolver.seed({"proxmox-token": "gate-value"})
    resolver.resolve()
    assert backend.calls == [["git-token-gh"]]
    assert resolver.get("proxmox-token") == "gate-value"
    assert resolver.get("git-token-gh") == "ghtok"
    assert resolver.values == {
        "proxmox-token": "gate-value",
        "git-token-gh": "ghtok",
    }


def test_all_seeded_resolve_skips_the_backend_loop(backend: _ResolveSpy) -> None:
    resolver = _resolver()
    resolver.seed({"proxmox-token": "pve"})
    resolver.resolve()
    assert backend.calls == []
    assert resolver.values == {"proxmox-token": "pve"}


def test_resolve_stays_idempotent_with_seeded_names(backend: _ResolveSpy) -> None:
    resolver = _resolver()
    resolver.seed({"proxmox-token": "pve"})
    resolver.resolve()
    resolver.resolve()  # no raise: the seeded name is in the cache
    assert backend.calls == []


def test_seeding_after_the_pass_is_a_loud_error(backend: _ResolveSpy) -> None:
    """Same contract as post-pass registration: a value the pass never
    covered must not quietly widen the cache."""
    resolver = _resolver()
    resolver.resolve()
    with pytest.raises(StateError, match="seeded after"):
        resolver.seed({"proxmox-token": "pve"})
