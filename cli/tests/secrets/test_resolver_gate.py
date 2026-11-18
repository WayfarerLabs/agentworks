"""``Resolver.resolve_gate``: the gate-to-boundary seam.

The activation gate resolves its narrow just-in-time secrets before
the boundary pass, one name at a time, on the operation's resolver, so
(a) the platform's power ops, which read the BOUND resolver
pre-boundary (proxmox's ``status``), see them immediately, and (b) the
boundary pass excludes them, so nothing resolves or prompts twice in
one command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from agentworks.errors import SecretUnavailableError, StateError, UserAbort
from agentworks.secrets.policy import TtyInteractionPolicy
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

    def __call__(self, secrets: list[SecretDecl], sources: object, **kwargs: object) -> _FakeBatch:
        self.calls.append([secret.name for secret in secrets])
        return _FakeBatch({secret.name: "ghtok" for secret in secrets})


class _FakeBatch:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def complete_or_raise(self) -> dict[str, str]:
        return dict(self.values)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _ResolveSpy:
    from agentworks.secrets import resolve as secrets_resolve

    spy = _ResolveSpy()
    monkeypatch.setattr(secrets_resolve, "active_sources", lambda config, registry: [])
    monkeypatch.setattr(secrets_resolve, "resolve_batch", spy)
    return spy


def _resolver() -> Resolver:
    return Resolver(
        cast("Config", object()), cast("Registry", _EmptyRegistry()), interaction=TtyInteractionPolicy.REFUSE
    )


def test_gate_value_is_readable_before_the_boundary_pass(backend: _ResolveSpy) -> None:
    """The seam's whole point: the gate's power ops read the bound
    resolver before the boundary pass runs."""
    resolver = _resolver()
    resolver.resolve_gate("proxmox-token")
    assert not resolver.resolved
    assert resolver.get("proxmox-token") == "ghtok"


def test_a_name_no_gate_resolved_still_raises_before_the_pass(backend: _ResolveSpy) -> None:
    resolver = _resolver()
    resolver.resolve_gate("proxmox-token")
    with pytest.raises(StateError, match="before the operation's resolve"):
        resolver.get("git-token-gh")


def test_boundary_pass_excludes_gate_resolved_names(backend: _ResolveSpy) -> None:
    """No secret resolves twice: the backend loop covers only the
    remainder the gate did not resolve, and the cache serves both."""
    resolver = _resolver()
    resolver.register_name("git-token-gh")
    resolver.resolve_gate("proxmox-token")
    resolver.resolve()
    assert backend.calls == [["proxmox-token"], ["git-token-gh"]]
    assert resolver.values == {"proxmox-token": "ghtok", "git-token-gh": "ghtok"}


def test_boundary_pass_with_only_gate_names_skips_the_backend_loop(backend: _ResolveSpy) -> None:
    resolver = _resolver()
    resolver.resolve_gate("proxmox-token")
    resolver.resolve()
    assert backend.calls == [["proxmox-token"]]
    assert resolver.values == {"proxmox-token": "ghtok"}


def test_resolve_stays_idempotent_with_gate_names(backend: _ResolveSpy) -> None:
    resolver = _resolver()
    resolver.resolve_gate("proxmox-token")
    resolver.resolve()
    resolver.resolve()  # no raise: the gate name is in the cache
    assert backend.calls == [["proxmox-token"]]


def test_shared_gate_success_is_reused_by_gates_and_workload_boundary(backend: _ResolveSpy) -> None:
    resolver = _resolver()

    assert resolver.resolve_gate("shared") == "ghtok"
    assert resolver.resolve_gate("shared") == "ghtok"
    resolver.register_name("shared")
    resolver.register_name("workload")
    resolver.resolve()

    assert backend.calls == [["shared"], ["workload"]]
    assert resolver.values == {"shared": "ghtok", "workload": "ghtok"}
    with pytest.raises(StateError):
        resolver.resolve_gate("shared")
    assert backend.calls == [["shared"], ["workload"]]


def _failing_gate_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: _ResolveSpy,
) -> None:
    from agentworks.secrets import resolve as secrets_resolve

    def resolve(secrets: list[SecretDecl], sources: object, **kwargs: object) -> _FakeBatch:
        if any(secret.name == "failed" for secret in secrets):
            backend.calls.append([secret.name for secret in secrets])
            raise SecretUnavailableError("unsafe-source-detail-that-must-not-be-memoized")
        return backend(secrets, sources, **kwargs)

    monkeypatch.setattr(secrets_resolve, "resolve_batch", resolve)


def test_failed_gate_is_not_retried_or_registered_as_an_unrelated_workload(
    monkeypatch: pytest.MonkeyPatch,
    backend: _ResolveSpy,
) -> None:
    _failing_gate_backend(monkeypatch, backend)
    resolver = _resolver()
    with pytest.raises(SecretUnavailableError):
        resolver.resolve_gate("failed")
    with pytest.raises(SecretUnavailableError) as cached:
        resolver.resolve_gate("failed")

    assert "unsafe-source-detail-that-must-not-be-memoized" not in str(cached.value)
    assert cached.value.__cause__ is None
    assert backend.calls == [["failed"]]
    resolver.register_name("independent")
    resolver.resolve()
    assert backend.calls == [["failed"], ["independent"]]
    assert resolver.values == {"independent": "ghtok"}


@pytest.mark.parametrize("registered_before_gate", [False, True])
def test_workload_requiring_failed_gate_stops_before_any_new_source_call(
    monkeypatch: pytest.MonkeyPatch,
    backend: _ResolveSpy,
    registered_before_gate: bool,
) -> None:
    _failing_gate_backend(monkeypatch, backend)
    resolver = _resolver()
    if registered_before_gate:
        resolver.register_name("failed")
    with pytest.raises(SecretUnavailableError):
        resolver.resolve_gate("failed")
    resolver.register_name("new-workload")
    resolver.register_name("failed")

    with pytest.raises(SecretUnavailableError):
        resolver.resolve()

    assert not resolver.resolved
    assert backend.calls == [["failed"]]
    with pytest.raises(StateError):
        resolver.get("new-workload")


def test_gate_cancellation_is_not_memoized_as_credential_failure(
    monkeypatch: pytest.MonkeyPatch,
    backend: _ResolveSpy,
) -> None:
    from agentworks.secrets import resolve as secrets_resolve

    cancelled = False

    def resolve(secrets: list[SecretDecl], sources: object, **kwargs: object) -> _FakeBatch:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            backend.calls.append([secret.name for secret in secrets])
            raise UserAbort("cancelled")
        return backend(secrets, sources, **kwargs)

    monkeypatch.setattr(secrets_resolve, "resolve_batch", resolve)
    resolver = _resolver()
    with pytest.raises(UserAbort):
        resolver.resolve_gate("shared")

    assert resolver.resolve_gate("shared") == "ghtok"
    resolver.resolve()
    assert backend.calls == [["shared"], ["shared"]]
    assert resolver.values == {"shared": "ghtok"}


@pytest.mark.parametrize("phase", ["initial", "gate", "boundary"])
def test_disposal_prevents_source_access_and_cached_value_reads(
    backend: _ResolveSpy,
    phase: str,
) -> None:
    resolver = _resolver()
    if phase != "initial":
        resolver.resolve_gate("shared")
    if phase == "boundary":
        resolver.resolve()
    completed_calls = list(backend.calls)

    resolver.close()
    resolver.close()

    assert not resolver.resolved
    with pytest.raises(StateError):
        resolver.get("shared")
    with pytest.raises(StateError):
        _ = resolver.values
    with pytest.raises(StateError):
        resolver.resolve_gate("new")
    with pytest.raises(StateError):
        resolver.resolve()
    with pytest.raises(StateError):
        resolver.register_name("new")
    assert backend.calls == completed_calls
