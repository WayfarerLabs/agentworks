"""Deterministic asynchronous-interruption pins for batch consumers."""

from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from agentworks.secrets.base import SecretDecl
from agentworks.secrets.orchestration import resolve_for_command
from agentworks.secrets.outcomes import (
    ResolutionCategory,
    ResolutionDetail,
    ResolutionOutcome,
    ResolutionRemediation,
)
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.resolve import (
    _BATCH_TOKEN,
    ResolutionBatch,
    resolve_partial_for_reveal,
)
from agentworks.secrets.verification import verify_secrets

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.config import Config
    from agentworks.resources.registry import Registry


@contextmanager
def _interrupt_line(function: Any, source_line: str, interrupt: KeyboardInterrupt) -> Iterator[None]:
    lines, first_line = inspect.getsourcelines(function)
    matches = [first_line + index for index, line in enumerate(lines) if line.rstrip("\n") == source_line]
    assert matches, (function, source_line, matches)
    target_line = matches[0]
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


def _batch(name: str, value: str) -> ResolutionBatch:
    outcome = ResolutionOutcome(
        name=name,
        category=ResolutionCategory.RESOLVED,
        detail=ResolutionDetail.RESOLVED,
        remediation=ResolutionRemediation.NONE,
        source="fixture",
        identifier="fixture-id",
    )
    return ResolutionBatch((outcome,), {name: value}, _token=_BATCH_TOKEN)


def _install_batch(monkeypatch: pytest.MonkeyPatch, *, name: str, value: str) -> ResolutionBatch:
    batch = _batch(name, value)
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [])
    monkeypatch.setattr("agentworks.secrets.resolve.resolve_batch", lambda *args, **kwargs: batch)
    return batch


class _Registry:
    def __init__(self, declaration: SecretDecl) -> None:
        self.declaration = declaration

    def lookup(self, kind: str, name: str) -> SecretDecl:
        assert kind == "secret" and name == self.declaration.name
        return self.declaration

    def iter_kind_items(self, kind: str) -> Iterator[tuple[str, SecretDecl]]:
        assert kind == "secret"
        yield self.declaration.name, self.declaration


@pytest.mark.parametrize(
    "source_line",
    (
        "        projected = batch.complete_or_raise()",
        "        batch.scrub_values()",
        "        result.update(projected)",
        "        projected.clear()",
    ),
    ids=("core-entry", "first-post-core", "result-transfer", "final-pre-return"),
)
def test_standalone_transfer_interrupt_scrubs_every_mapping(
    monkeypatch: pytest.MonkeyPatch,
    source_line: str,
) -> None:
    sentinel = "sentinel-standalone-transfer"
    batch = _install_batch(monkeypatch, name="token", value=sentinel)
    declaration = SecretDecl(name="token", description="token")
    interrupt = KeyboardInterrupt("standalone-transfer")

    with (
        _interrupt_line(resolve_for_command, source_line, interrupt),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        resolve_for_command(
            [],
            cast("Config", SimpleNamespace()),
            cast("Registry", _Registry(declaration)),
            extra_decls=[declaration],
            interaction=InteractionPolicy.REFUSE,
        )

    assert caught.value is interrupt
    assert batch._values == {}
    assert sentinel not in _traceback_values(caught.value)


@pytest.mark.parametrize(
    "source_line",
    (
        "        result.outcomes = batch.outcomes",
        "        projected = _copy_partial_values(batch)",
        "        batch.scrub_values()",
        "        result.values = projected",
        "        projected = {}",
        "        projected.clear()",
    ),
    ids=("outcome-transfer", "projection", "first-post-core", "result-transfer", "pre-detach", "final-pre-return"),
)
def test_partial_reveal_transfer_interrupt_scrubs_batch_and_result(
    monkeypatch: pytest.MonkeyPatch,
    source_line: str,
) -> None:
    sentinel = "sentinel-partial-transfer"
    batch = _install_batch(monkeypatch, name="token", value=sentinel)
    interrupt = KeyboardInterrupt("partial-transfer")

    with (
        _interrupt_line(resolve_partial_for_reveal, source_line, interrupt),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        resolve_partial_for_reveal(
            [SecretDecl(name="token", description="token")],
            [],
            interaction=InteractionPolicy.REFUSE,
        )

    assert caught.value is interrupt
    assert batch._values == {}
    assert sentinel not in _traceback_values(caught.value)


@pytest.mark.parametrize(
    "source_line",
    (
        "        outcomes = batch.discard_values()",
        "        batch.scrub_values()",
    ),
    ids=("first-post-core", "final-pre-return"),
)
def test_verify_discard_interrupt_scrubs_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    source_line: str,
) -> None:
    sentinel = "sentinel-verify-discard"
    batch = _install_batch(monkeypatch, name="token", value=sentinel)
    declaration = SecretDecl(name="token", description="token")
    interrupt = KeyboardInterrupt("verify-discard")

    with _interrupt_line(verify_secrets, source_line, interrupt), pytest.raises(KeyboardInterrupt) as caught:
        verify_secrets(
            cast("Config", SimpleNamespace()),
            cast("Registry", _Registry(declaration)),
            ["token"],
            interaction=InteractionPolicy.REFUSE,
        )

    assert caught.value is interrupt
    assert batch._values == {}
    assert sentinel not in _traceback_values(caught.value)
