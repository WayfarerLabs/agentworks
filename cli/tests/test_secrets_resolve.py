"""Tests for the resolve loop (``agentworks.secrets.resolve``).

Resolution is a loop over the active backends -- no resolver object, no
cache. Fakes here are backend-shaped duck types (name / interactive /
would_attempt / describe_lookup / resolve): the loop only speaks the
door methods.
"""

from __future__ import annotations

from typing import cast

import pytest

from agentworks.errors import ConfigError, SecretMappingError, SecretUnavailableError
from agentworks.secrets import SecretDecl
from agentworks.secrets.base import SecretBackendDecl
from agentworks.secrets.resolve import preview_resolution, resolve_secrets


class _FakeBackend:
    """A backend-door stub controllable per-test."""

    def __init__(
        self,
        name: str,
        values: dict[str, str] | None = None,
        attempts: set[str] | None = None,
        interactive: bool = False,
    ) -> None:
        self.name = name
        self.interactive = interactive
        self._values = values or {}
        # If attempts is None, this backend attempts everything except
        # explicit opt-outs (keyed by BACKEND NAME). If attempts is a
        # set, only secrets in the set are attempted.
        self._attempts = attempts
        self.resolve_calls: list[list[str]] = []  # secret-names per call

    def would_attempt(self, secret: SecretDecl) -> bool:
        if secret.backend_mappings.get(self.name) is False:
            return False
        if self._attempts is not None:
            return secret.name in self._attempts
        return True

    def describe_lookup(self, secret: SecretDecl) -> str | None:  # noqa: ARG002 - stub
        return f"<{self.name}>"

    def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
        self.resolve_calls.append([s.name for s in secrets])
        return {
            s.name: self._values[s.name]
            for s in secrets
            if s.name in self._values
        }


def _decl(name: str, **kw: object) -> SecretDecl:
    return SecretDecl(name=name, description=f"{name} description", **kw)  # type: ignore[arg-type]


def _chain(*backends: object) -> list[SecretBackendDecl]:
    """The fakes duck-type the door methods; cast for the signatures."""
    return cast("list[SecretBackendDecl]", list(backends))


def test_first_backend_wins() -> None:
    b1 = _FakeBackend("first", values={"x": "from-first"})
    b2 = _FakeBackend("second", values={"x": "from-second"})
    assert resolve_secrets([_decl("x")], _chain(b1, b2)) == {"x": "from-first"}
    # Second backend never got called for x.
    assert b2.resolve_calls == []


def test_fallthrough_to_later_backend() -> None:
    b1 = _FakeBackend("first")  # no values
    b2 = _FakeBackend("second", values={"x": "from-second"})
    assert resolve_secrets([_decl("x")], _chain(b1, b2)) == {"x": "from-second"}


def test_hard_miss_halts_chain_via_secret_mapping_error() -> None:
    """A persistent-store provider raises SecretMappingError when an
    explicit mapping doesn't resolve. The loop lets the exception
    propagate so a misconfigured store doesn't fall through to a prompt
    that would mask the real config problem."""

    class _StrictMissBackend(_FakeBackend):
        def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
            raise SecretMappingError(
                f"strict backend has no item for {secrets[0].name!r}",
            )

    strict = _StrictMissBackend("strict")
    later = _FakeBackend("prompt", values={"x": "would-prompt"})

    with pytest.raises(SecretMappingError, match="strict backend has no item"):
        resolve_secrets([_decl("x")], _chain(strict, later))

    # Critical contract: the prompt backend NEVER ran. Hard miss halts
    # the chain.
    assert later.resolve_calls == []


def test_unsatisfied_raises_with_backends_tried() -> None:
    b1 = _FakeBackend("env-var")
    b2 = _FakeBackend("prompt")
    with pytest.raises(SecretUnavailableError) as exc:
        resolve_secrets([_decl("x")], _chain(b1, b2))
    assert "x" in str(exc.value)
    assert "env-var" in (exc.value.hint or "")
    assert "prompt" in (exc.value.hint or "")


@pytest.mark.parametrize(
    "value",
    ["line1\nline2", "line1\rline2", "valid\x00rest"],
    ids=["newline", "carriage-return", "nul"],
)
def test_control_characters_in_resolved_value_raise(value: str) -> None:
    """ADR 0014: a resolved secret value containing a newline / CR / NUL
    corrupts SSH SetEnv. The loop hard-fails so the operator sees a
    clear error instead of an opaque SSH-side rejection."""
    b1 = _FakeBackend("vault", values={"x": value})
    with pytest.raises(ConfigError, match="control character"):
        resolve_secrets([_decl("x")], _chain(b1))


def test_each_backend_called_once_with_still_missing_set() -> None:
    b1 = _FakeBackend("first", values={"a": "1"})
    b2 = _FakeBackend("second", values={"b": "2", "c": "3"})
    out = resolve_secrets([_decl("a"), _decl("b"), _decl("c")], _chain(b1, b2))
    assert out == {"a": "1", "b": "2", "c": "3"}
    # b1 was asked for [a, b, c] (it would_attempt all); returned only a.
    assert b1.resolve_calls == [["a", "b", "c"]]
    # b2 was asked for [b, c]; a was already resolved.
    assert b2.resolve_calls == [["b", "c"]]


def test_input_deduped_by_name() -> None:
    """Duplicate decls in the input resolve once (one backend call, one
    result entry) -- callers union decls from several targets."""
    b1 = _FakeBackend("first", values={"x": "v"})
    out = resolve_secrets([_decl("x"), _decl("x")], _chain(b1))
    assert out == {"x": "v"}
    assert b1.resolve_calls == [["x"]]


def test_opt_out_skips_backend_for_that_secret_only() -> None:
    b1 = _FakeBackend("env-var", values={"x": "from-env", "y": "from-env-y"})
    b2 = _FakeBackend("prompt", values={"x": "prompted"})
    x = _decl("x", backend_mappings={"env-var": False})
    y = _decl("y")
    out = resolve_secrets([x, y], _chain(b1, b2))
    # x skipped env-var (opt-out) and fell through to prompt.
    # y was resolved by env-var on the first try.
    assert out == {"x": "prompted", "y": "from-env-y"}


def test_empty_chain_with_no_secrets_resolves_empty() -> None:
    assert resolve_secrets([], _chain()) == {}


def test_unsatisfied_hint_omits_opted_out_backends() -> None:
    """The hint for a missing secret should not list backends whose
    would_attempt returned False (e.g. via backend_mappings.env-var =
    false). Only backends that actually tried appear."""
    b1 = _FakeBackend("env-var")
    b2 = _FakeBackend("prompt")
    decl = _decl("x", backend_mappings={"env-var": False})
    with pytest.raises(SecretUnavailableError) as exc:
        resolve_secrets([decl], _chain(b1, b2))
    hint = exc.value.hint or ""
    assert "x" in hint
    assert "prompt" in hint
    assert "env-var" not in hint


def test_unsatisfied_hint_per_secret_listing() -> None:
    """When multiple secrets fail, each gets its own per-secret hint line
    so operators can see which backends were tried for each one."""
    b_env = _FakeBackend("env-var")
    b_prompt = _FakeBackend("prompt")
    a = _decl("a", backend_mappings={"env-var": False})
    b = _decl("b")
    with pytest.raises(SecretUnavailableError) as exc:
        resolve_secrets([a, b], _chain(b_env, b_prompt))
    hint = exc.value.hint or ""
    # 'a' opted out of env-var, only prompt tried.
    assert "a: tried prompt" in hint
    # 'b' had no opt-out, both tried.
    assert "b: tried env-var, prompt" in hint


# -- collect mode (errors out-param) -----------------------------------------


def test_collect_mode_keeps_partial_values_and_records_failures() -> None:
    """With an ``errors`` dict, the loop returns what resolved and
    records per-secret failures instead of raising -- inspection
    surfaces get partial success from ONE pass (already-answered
    prompts are never discarded and re-asked)."""
    b1 = _FakeBackend("env-var", values={"good": "value"})
    good = _decl("good")
    bad = _decl("bad", backend_mappings={"env-var": False})
    errors: dict[str, str] = {}
    values = resolve_secrets([good, bad], _chain(b1), errors=errors)
    assert values == {"good": "value"}
    assert set(errors) == {"bad"}
    assert "no active backend could resolve" in errors["bad"]
    assert "bad: tried" in errors["bad"]


def test_collect_mode_records_control_character_values() -> None:
    """The SetEnv transport guard lands in ``errors`` (value withheld)
    instead of aborting the whole pass; clean values still return."""
    b1 = _FakeBackend("vault", values={"clean": "ok", "dirty": "a\nb"})
    errors: dict[str, str] = {}
    values = resolve_secrets([_decl("clean"), _decl("dirty")], _chain(b1), errors=errors)
    assert values == {"clean": "ok"}
    assert set(errors) == {"dirty"}
    assert "control character" in errors["dirty"]


def test_collect_mode_records_backend_exception_without_fallthrough() -> None:
    """A backend-level exception (hard miss / connectivity) is recorded
    against every secret that backend was attempting -- batch-level
    attribution -- and those secrets are NOT forwarded to later
    backends, preserving the don't-mask-a-store-misconfiguration
    semantics of the hard-miss halt."""

    class _StrictMissBackend(_FakeBackend):
        def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
            raise SecretMappingError("store has no item")

    strict = _StrictMissBackend("strict")
    later = _FakeBackend("prompt", values={"x": "would-prompt", "y": "would-prompt"})
    errors: dict[str, str] = {}
    values = resolve_secrets([_decl("x"), _decl("y")], _chain(strict, later), errors=errors)
    assert values == {}
    assert set(errors) == {"x", "y"}
    assert "store has no item" in errors["x"]
    # The prompt backend NEVER ran for the affected secrets.
    assert later.resolve_calls == []


def test_collect_mode_default_is_unchanged_raise_behavior() -> None:
    """Without ``errors``, the loop keeps its all-or-nothing contract --
    the out-param is additive, not a behavior change for commands."""
    b1 = _FakeBackend("env-var")
    with pytest.raises(SecretUnavailableError):
        resolve_secrets([_decl("x")], _chain(b1))


# -- preview_resolution ------------------------------------------------------


def test_preview_reports_first_backend_with_value() -> None:
    b1 = _FakeBackend("env-var", values={"x": "from-env"})
    b2 = _FakeBackend("prompt", interactive=True)
    assert preview_resolution(_decl("x"), _chain(b1, b2)) == "env-var"


def test_preview_falls_through_to_interactive() -> None:
    """env-var would_attempt is True but has no value; prompt is the
    next backend and is not opted out, so preview reports prompt."""
    b1 = _FakeBackend("env-var")  # no values
    b2 = _FakeBackend("prompt", interactive=True)
    assert preview_resolution(_decl("x"), _chain(b1, b2)) == "prompt"


def test_preview_never_probes_interactive_backends() -> None:
    """Preview must never call ``resolve`` on an interactive backend --
    doing so would actually prompt the operator. It is reported on the
    strength of ``would_attempt`` alone."""

    class _ExplodingPrompt(_FakeBackend):
        def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
            raise AssertionError("preview must not probe interactive backends")

    b1 = _FakeBackend("env-var")
    b2 = _ExplodingPrompt("prompt", interactive=True)
    assert preview_resolution(_decl("x"), _chain(b1, b2)) == "prompt"


def test_preview_skips_opted_out_backend() -> None:
    """A secret with ``backend_mappings.env-var = false`` makes env-var's
    would_attempt return False; preview skips it and continues."""
    b1 = _FakeBackend("env-var", values={"x": "from-env"})
    b2 = _FakeBackend("prompt", interactive=True)
    decl = _decl("x", backend_mappings={"env-var": False})
    assert preview_resolution(decl, _chain(b1, b2)) == "prompt"


def test_preview_honors_opt_out_for_interactive_backend() -> None:
    """Prompt opted out via ``backend_mappings.prompt = false`` returns
    None, matching what would actually happen at command time
    (SecretUnavailableError)."""
    b1 = _FakeBackend("env-var")  # no values; falls through
    b2 = _FakeBackend("prompt", interactive=True)
    decl = _decl("x", backend_mappings={"prompt": False})
    assert preview_resolution(decl, _chain(b1, b2)) is None


def test_preview_returns_none_when_no_backend_attempts() -> None:
    b1 = _FakeBackend("env-var", attempts=set())  # never attempts anything
    assert preview_resolution(_decl("x"), _chain(b1)) is None
