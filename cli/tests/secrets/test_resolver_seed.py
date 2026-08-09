"""``Resolver.seed``: the gate-to-boundary seam.

The activation gate resolves its narrow just-in-time secrets before
the boundary pass; seeding hands those values to the operation's
resolver so (a) the platform's power ops, which read the BOUND
resolver pre-boundary (proxmox's ``status``), see them immediately,
and (b) the boundary pass excludes them, so nothing resolves or
prompts twice in one command.
"""

from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest

from agentworks.errors import StateError
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.resolver import Resolver

if TYPE_CHECKING:
    from collections.abc import Iterator

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

    def scrub_values(self) -> None:
        self.values.clear()


@contextmanager
def _interrupt_line(
    function: Any,
    source_line: str,
    interrupt: KeyboardInterrupt,
    *,
    occurrence: int = 0,
) -> Iterator[None]:
    lines, first_line = inspect.getsourcelines(function)
    matches = [first_line + index for index, line in enumerate(lines) if line.rstrip("\n") == source_line]
    assert len(matches) > occurrence, (function, source_line, occurrence)
    target_line = matches[occurrence]
    target_code = function.__code__
    fired = False

    def trace(frame: Any, event: str, argument: object) -> Any:
        del argument
        nonlocal fired
        if not fired and frame.f_code is target_code and event == "line" and frame.f_lineno == target_line:
            fired = True
            sys.settrace(None)
            raise interrupt
        return trace

    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(None)
    assert fired


def _traceback_values(exc: BaseException) -> str:
    values: list[str] = []
    seen: set[int] = set()

    def collect(value: object) -> None:
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                collect(item)
        elif type(value).__module__.startswith(("agentworks.", "tests.")) or type(value).__module__ == __name__:
            for item in getattr(value, "__dict__", {}).values():
                collect(item)

    traceback = exc.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agentworks."):
            for name, value in traceback.tb_frame.f_locals.items():
                if name != "self":
                    collect(value)
        traceback = traceback.tb_next
    return "\n".join(values)


def _install_batches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    value: str,
) -> list[_FakeBatch]:
    batches: list[_FakeBatch] = []

    def resolve(secrets: list[SecretDecl], sources: object, **kwargs: object) -> _FakeBatch:
        batch = _FakeBatch({secret.name: value for secret in secrets})
        batches.append(batch)
        return batch

    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [])
    monkeypatch.setattr("agentworks.secrets.resolve.resolve_batch", resolve)
    return batches


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _ResolveSpy:
    from agentworks.secrets import resolve as secrets_resolve

    spy = _ResolveSpy()
    monkeypatch.setattr(secrets_resolve, "active_sources", lambda config, registry: [])
    monkeypatch.setattr(secrets_resolve, "resolve_batch", spy)
    return spy


def _resolver() -> Resolver:
    return Resolver(cast("Config", object()), cast("Registry", _EmptyRegistry()), interaction=InteractionPolicy.REFUSE)


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


@pytest.mark.parametrize(
    ("source_line", "occurrence"),
    [
        ("            projected = batch.complete_or_raise()", 0),
        ("            batch.scrub_values()", 0),
        ("            candidate = {**self._seeded, **projected}", 0),
        ("            published = True", 0),
        ("            self._values = candidate", 0),
        ("            candidate = {}", 0),
        ("            candidate.clear()", 0),
    ],
    ids=("core-entry", "first-post-core", "join", "arm", "publish", "pre-detach", "post-detach"),
)
def test_boundary_publication_interrupt_scrubs_restores_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    source_line: str,
    occurrence: int,
) -> None:
    sentinel = "sentinel-boundary-publication"
    batches = _install_batches(monkeypatch, value=sentinel)
    resolver = _resolver()
    resolver.register_name("token")
    interrupt = KeyboardInterrupt("boundary-publication")

    with (
        _interrupt_line(Resolver.resolve, source_line, interrupt, occurrence=occurrence),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        resolver.resolve()

    assert caught.value is interrupt
    assert batches[0].values == {}
    assert resolver.__dict__["_values"] is None
    assert resolver.__dict__["_seeded"] == {}
    assert sentinel not in _traceback_values(caught.value)

    resolver.resolve()
    assert resolver.get("token") == sentinel
    assert len(batches) == 2


@pytest.mark.parametrize(
    "source_line",
    (
        "                seeded_candidate = dict(self._seeded)",
        "                published = True",
        "                self._values = seeded_candidate",
        "                seeded_candidate = {}",
        "                seeded_candidate.clear()",
    ),
    ids=("copy", "arm", "publish", "pre-detach", "post-detach"),
)
def test_all_seeded_publication_interrupt_restores_prior_seed_and_retries(
    source_line: str,
) -> None:
    sentinel = "sentinel-existing-seed"
    resolver = _resolver()
    resolver.seed({"token": sentinel})
    interrupt = KeyboardInterrupt("seeded-publication")

    with _interrupt_line(Resolver.resolve, source_line, interrupt), pytest.raises(KeyboardInterrupt) as caught:
        resolver.resolve()

    assert caught.value is interrupt
    assert resolver.__dict__["_values"] is None
    assert resolver.__dict__["_seeded"] == {"token": sentinel}
    assert sentinel not in _traceback_values(caught.value)

    resolver.resolve()
    assert resolver.values == {"token": sentinel}


@pytest.mark.parametrize(
    "source_line",
    (
        "            projected = batch.complete_or_raise()",
        "            batch.scrub_values()",
        "            seeded = True",
        "            self._seeded[name] = projected.pop(name)",
        "            projected.clear()",
        "            return self._seeded[name]",
    ),
    ids=("core-entry", "first-post-core", "arm", "publish", "post-transfer", "return-transfer"),
)
def test_gate_publication_interrupt_removes_new_seed_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    source_line: str,
) -> None:
    sentinel = "sentinel-gate-publication"
    batches = _install_batches(monkeypatch, value=sentinel)
    resolver = _resolver()
    interrupt = KeyboardInterrupt("gate-publication")

    with _interrupt_line(Resolver.resolve_gate, source_line, interrupt), pytest.raises(KeyboardInterrupt) as caught:
        resolver.resolve_gate("token")

    assert caught.value is interrupt
    assert batches[0].values == {}
    assert resolver.__dict__["_seeded"] == {}
    assert resolver.__dict__["_values"] is None
    assert sentinel not in _traceback_values(caught.value)

    assert resolver.resolve_gate("token") == sentinel
    assert resolver.get("token") == sentinel
    assert len(batches) == 2


@pytest.mark.parametrize(
    "source_line",
    (
        "            projected = batch.complete_or_raise()",
        "            transfer[decl.name] = projected.pop(decl.name)",
        "            projected.clear()",
        "            return transfer.pop(decl.name)",
    ),
    ids=("first-post-core", "transfer", "projected-clear", "return"),
)
def test_late_repair_transfer_interrupt_preserves_cache_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    source_line: str,
) -> None:
    sentinel = "sentinel-late-repair"
    batches = _install_batches(monkeypatch, value=sentinel)
    resolver = _resolver()
    resolver.resolve()
    declaration = resolver.register_name("repair")
    interrupt = KeyboardInterrupt("late-repair")

    with (
        _interrupt_line(Resolver.resolve_late_repair, source_line, interrupt),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        resolver.resolve_late_repair(declaration)

    assert caught.value is interrupt
    assert batches[0].values == {}
    assert resolver.values == {}
    assert resolver.__dict__["_seeded"] == {}
    assert sentinel not in _traceback_values(caught.value)

    assert resolver.resolve_late_repair(declaration) == sentinel
    assert resolver.values == {}
    assert len(batches) == 2
