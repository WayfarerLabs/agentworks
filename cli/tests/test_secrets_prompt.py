"""Tests for the prompt backend, exercised through the runtime
``ActiveBackend`` wrapper -- how the resolution loop reaches a
capability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.secrets import ActiveBackend, SecretDecl
from agentworks.secrets.prompt import PromptBackend

if TYPE_CHECKING:
    import pytest


def _backend() -> ActiveBackend:
    return ActiveBackend(capability=PromptBackend())


def _set_interactive(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Set output.is_interactive() to a fixed value for the test."""
    from agentworks import output

    monkeypatch.setattr(output, "is_interactive", lambda: value)


def test_would_attempt_true_by_default() -> None:
    """Prompt applies to any secret unless opted out; the runtime TTY check
    is in resolve()."""
    assert _backend().would_attempt(SecretDecl(name="x", description="X")) is True


def test_would_attempt_false_when_opted_out() -> None:
    """``backend_mappings.prompt = false`` disables the prompt backend for
    this secret -- the way operators force a secret to error rather than
    silently fall through to interactive input when testing the env-var
    path in an interactive shell. The opt-out is generic loop-side
    behavior; the provider never sees it."""
    decl = SecretDecl(
        name="x", description="X", backend_mappings={"prompt": False},
    )
    assert _backend().would_attempt(decl) is False


def test_backend_is_interactive() -> None:
    """The interactive flag is what keeps inspection previews from
    probing prompt (probing would BE the operator interaction)."""
    assert _backend().interactive is True


def test_describe_lookup_is_none() -> None:
    """No static identifier: the "lookup" is the operator typing."""
    assert _backend().describe_lookup(SecretDecl(name="x", description="X")) is None


def test_resolve_returns_empty_when_not_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_interactive(monkeypatch, False)
    assert _backend().resolve([SecretDecl(name="x", description="X")]) == {}


def test_resolve_prompts_when_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_interactive(monkeypatch, True)
    from agentworks import output

    captured: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        output,
        "prompt_secret",
        lambda label, hint=None: captured.append((label, hint)) or "operator-entered",
    )

    decl = SecretDecl(name="github-token", description="GitHub PAT", hint="https://...")
    assert _backend().resolve([decl]) == {"github-token": "operator-entered"}
    assert captured == [("Secret 'github-token': GitHub PAT", "https://...")]


def test_resolve_prompts_each_secret_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All prompts in one operator interaction: the "prompt once at the
    start" UX, preserved even though prompt is just another backend."""
    _set_interactive(monkeypatch, True)
    from agentworks import output

    values = iter(["v1", "v2", "v3"])
    monkeypatch.setattr(output, "prompt_secret", lambda label, hint=None: next(values))

    secrets = [
        SecretDecl(name="a", description="A"),
        SecretDecl(name="b", description="B"),
        SecretDecl(name="c", description="C"),
    ]
    assert _backend().resolve(secrets) == {"a": "v1", "b": "v2", "c": "v3"}
