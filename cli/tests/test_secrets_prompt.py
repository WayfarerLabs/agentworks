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


def test_would_attempt_true_by_default() -> None:
    """Prompt applies to any secret unless opted out; the runtime TTY check
    is in get()."""
    src = PromptSource()
    assert src.would_attempt(SecretDecl(name="x", description="X")) is True


def test_would_attempt_false_when_opted_out() -> None:
    """``backend_mappings.prompt = false`` disables prompt for this secret
    -- the way operators force a secret to error rather than silently
    fall through to interactive input (useful for testing and for
    non-interactive pipelines)."""
    src = PromptSource()
    decl = SecretDecl(
        name="x", description="X", backend_mappings={"prompt": False},
    )
    assert src.would_attempt(decl) is False


def test_get_returns_none_when_opted_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even in an interactive shell, an opted-out secret never prompts."""
    _set_interactive(monkeypatch, True)
    src = PromptSource()
    decl = SecretDecl(
        name="x", description="X", backend_mappings={"prompt": False},
    )
    assert src.get(decl) is None


def test_batch_get_skips_opted_out_in_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``batch_get`` collects only secrets prompt would actually attempt;
    opted-out secrets are omitted from the prompt batch and the returned
    map. They'll surface as SecretUnavailableError upstream if no other
    backend produces them."""
    _set_interactive(monkeypatch, True)

    prompted: list[str] = []

    def fake_prompt(label: str, hint: str | None = None) -> str:  # noqa: ARG001
        prompted.append(label)
        return "typed-value"

    from agentworks import output as _output
    monkeypatch.setattr(_output, "prompt_secret", fake_prompt)

    src = PromptSource()
    opted_out = SecretDecl(
        name="forced", description="X", backend_mappings={"prompt": False},
    )
    normal = SecretDecl(name="normal", description="Y")
    out = src.batch_get([opted_out, normal])
    assert "forced" not in out
    assert out["normal"] == "typed-value"
    # And only the normal secret prompted the operator.
    assert len(prompted) == 1


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
