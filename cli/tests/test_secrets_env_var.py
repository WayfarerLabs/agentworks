"""Environment-variable source behavior through the typed core."""

from __future__ import annotations

import inspect
import os
import sys
from contextlib import contextmanager
from typing import Any

import pytest

from agentworks.capabilities.secret_backend.client import SecretLookupRequest
from agentworks.capabilities.secret_backend.env_var import EnvVarBackend, EnvVarSourceConfig
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionCategory, ResolutionOutcome
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.resolve import (
    ActiveSource,
    CompletionPolicy,
    ResolutionPolicy,
    resolve_batch,
)


@contextmanager
def _interrupt_return_line(function: Any) -> Any:
    lines, first_line = inspect.getsourcelines(function)
    matches = [
        first_line + index for index, line in enumerate(lines) if line.rstrip("\n") == "            return resolved"
    ]
    assert len(matches) == 1
    target_line = matches[0]
    target_code = function.__code__

    def trace(frame: Any, event: str, argument: object) -> Any:
        del argument
        if frame.f_code is target_code and event == "line" and frame.f_lineno == target_line:
            sys.settrace(None)
            raise KeyboardInterrupt
        return trace

    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(None)


def _source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="env-var", backend=CapabilityBlock.of("env-var")),
        backend_class=EnvVarBackend,
        config=EnvVarSourceConfig(name="env-var"),
        readiness=Readiness.ready(),
    )


def _resolve(decl: SecretDecl) -> tuple[dict[str, str], ResolutionOutcome]:
    batch = resolve_batch(
        [decl],
        [_source()],
        policy=ResolutionPolicy(
            interaction=InteractionPolicy.REFUSE,
            completion=CompletionPolicy.COMPLETE,
        ),
        interaction_broker=None,
    )
    return batch.complete_or_raise(), batch.outcomes[0]


def test_default_convention_reads_env_and_strips_trailing_crlf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_GITHUB_TOKEN", "ghp_xxx\r\n")
    values, outcome = _resolve(SecretDecl(name="github-token", description="GitHub PAT"))
    assert values == {"github-token": "ghp_xxx"}
    assert outcome.category is ResolutionCategory.RESOLVED
    assert outcome.identifier == "AW_SECRET_GITHUB_TOKEN"


def test_override_uses_alternate_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "from-existing-env")
    values, outcome = _resolve(
        SecretDecl(
            name="github-token",
            description="GitHub PAT",
            backend_mappings={"env-var": "GITHUB_TOKEN"},
        )
    )
    assert values == {"github-token": "from-existing-env"}
    assert outcome.identifier == "GITHUB_TOKEN"


def test_opt_out_is_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_FORCED", "value")
    decl = SecretDecl(name="forced", description="forced", backend_mappings={"env-var": False})
    source = _source()
    assert source.would_attempt(decl) is False
    batch = resolve_batch(
        [decl],
        [source],
        policy=ResolutionPolicy(InteractionPolicy.REFUSE, CompletionPolicy.COMPLETE),
        interaction_broker=None,
    )
    assert batch.outcomes[0].category is not ResolutionCategory.RESOLVED


def test_unset_env_is_soft_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AW_SECRET_MISSING", raising=False)
    batch = resolve_batch(
        [SecretDecl(name="missing", description="missing")],
        [_source()],
        policy=ResolutionPolicy(InteractionPolicy.REFUSE, CompletionPolicy.COMPLETE),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail.value == "soft-miss"


def test_internal_whitespace_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_TOKEN", "  internal value  ")
    values, _outcome = _resolve(SecretDecl(name="token", description="token"))
    assert values == {"token": "  internal value  "}


def test_keyboard_interrupt_clears_prior_env_value_from_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def get(name: str, default: str | None = None) -> str | None:
        del name, default
        nonlocal calls
        calls += 1
        if calls == 1:
            return "sentinel-prior-env-value"
        raise KeyboardInterrupt

    monkeypatch.setattr(os.environ, "get", get)
    context = EnvVarBackend.create_client(
        source_name="env-var",
        config=EnvVarSourceConfig(name="env-var"),
        interaction_broker=None,
        remaining_time=lambda: None,
    )
    client = context.__enter__()
    requests = (
        SecretLookupRequest(name="first", mapping=None),
        SecretLookupRequest(name="second", mapping=None),
    )
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            client.resolve(requests, remaining_time=lambda: None)
    finally:
        context.__exit__(None, None, None)

    local_text: list[str] = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agentworks."):
            local_text.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert "sentinel-prior-env-value" not in "\n".join(local_text)


def test_exact_success_return_interrupt_clears_env_value_from_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AW_SECRET_TOKEN", "sentinel-env-return-value")
    context = EnvVarBackend.create_client(
        source_name="env-var",
        config=EnvVarSourceConfig(name="env-var"),
        interaction_broker=None,
        remaining_time=lambda: None,
    )
    client = context.__enter__()
    try:
        with pytest.raises(KeyboardInterrupt) as caught, _interrupt_return_line(type(client).resolve):
            client.resolve((SecretLookupRequest(name="token", mapping=None),), remaining_time=lambda: None)
    finally:
        context.__exit__(None, None, None)

    local_text: list[str] = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agentworks."):
            local_text.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert "sentinel-env-return-value" not in "\n".join(local_text)
