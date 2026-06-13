"""Tests for PromptSource."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.secrets import PromptSource, SecretDecl

if TYPE_CHECKING:
    import pytest


def _set_interactive(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Set output.is_interactive() to a fixed value for the test."""
    from agentworks import output

    monkeypatch.setattr(output, "is_interactive", lambda: value)


def test_would_attempt_always_true() -> None:
    """Prompt applies to any secret; the runtime TTY check is in get()."""
    src = PromptSource()
    assert src.would_attempt(SecretDecl(name="x", description="X")) is True


def test_get_returns_none_when_not_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_interactive(monkeypatch, False)
    src = PromptSource()
    assert src.get(SecretDecl(name="x", description="X")) is None


def test_get_prompts_when_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_interactive(monkeypatch, True)
    from agentworks import output

    captured: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        output,
        "prompt_secret",
        lambda label, hint=None: captured.append((label, hint)) or "operator-entered",
    )

    src = PromptSource()
    decl = SecretDecl(name="github-token", description="GitHub PAT", hint="https://...")
    assert src.get(decl) == "operator-entered"
    assert captured == [("Secret 'github-token': GitHub PAT", "https://...")]


def test_batch_get_returns_empty_when_not_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_interactive(monkeypatch, False)
    src = PromptSource()
    assert src.batch_get([SecretDecl(name="x", description="X")]) == {}


def test_batch_get_prompts_each_secret_when_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_interactive(monkeypatch, True)
    from agentworks import output

    values = iter(["v1", "v2", "v3"])
    monkeypatch.setattr(output, "prompt_secret", lambda label, hint=None: next(values))

    src = PromptSource()
    secrets = [
        SecretDecl(name="a", description="A"),
        SecretDecl(name="b", description="B"),
        SecretDecl(name="c", description="C"),
    ]
    out = src.batch_get(secrets)
    assert out == {"a": "v1", "b": "v2", "c": "v3"}
