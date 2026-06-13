"""Tests for SecretResolver."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import SecretUnavailableError
from agentworks.secrets import SecretDecl, SecretResolver

if TYPE_CHECKING:
    pass


class _FakeSource:
    """A SecretSource stub controllable per-test."""

    def __init__(
        self,
        kind: str,
        values: dict[str, str] | None = None,
        attempts: set[str] | None = None,
    ) -> None:
        self.kind = kind
        self._values = values or {}
        # If attempts is None, this source attempts everything except explicit opt-outs.
        # If attempts is a set, only secrets in the set are attempted.
        self._attempts = attempts
        self.batch_get_calls: list[list[str]] = []  # secret-names per call

    def would_attempt(self, secret: SecretDecl) -> bool:
        if secret.backend_mappings.get(self.kind) is False:
            return False
        if self._attempts is not None:
            return secret.name in self._attempts
        return True

    def get(self, secret: SecretDecl) -> str | None:
        if not self.would_attempt(secret):
            return None
        return self._values.get(secret.name)

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        self.batch_get_calls.append([s.name for s in secrets])
        out: dict[str, str] = {}
        for s in secrets:
            v = self.get(s)
            if v is not None:
                out[s.name] = v
        return out


def _decl(name: str, **kw: object) -> SecretDecl:
    return SecretDecl(name=name, description=f"{name} description", **kw)  # type: ignore[arg-type]


def _decls(*names: str) -> dict[str, SecretDecl]:
    return {n: _decl(n) for n in names}


def test_first_source_wins() -> None:
    s1 = _FakeSource("first", values={"x": "from-first"})
    s2 = _FakeSource("second", values={"x": "from-second"})
    r = SecretResolver([s1, s2], _decls("x"))
    assert r.resolve_all([_decl("x")]) == {"x": "from-first"}
    # Second source never got called for x.
    assert s2.batch_get_calls == []


def test_fallthrough_to_later_source() -> None:
    s1 = _FakeSource("first")  # no values
    s2 = _FakeSource("second", values={"x": "from-second"})
    r = SecretResolver([s1, s2], _decls("x"))
    assert r.resolve_all([_decl("x")]) == {"x": "from-second"}


def test_unsatisfied_raises_with_backends_tried() -> None:
    s1 = _FakeSource("env_var")
    s2 = _FakeSource("prompt")
    r = SecretResolver([s1, s2], _decls("x"))
    with pytest.raises(SecretUnavailableError) as exc:
        r.resolve_all([_decl("x")])
    assert "x" in str(exc.value)
    assert "env_var" in (exc.value.hint or "")
    assert "prompt" in (exc.value.hint or "")


def test_cache_hits_skip_sources() -> None:
    s1 = _FakeSource("first", values={"x": "v1"})
    r = SecretResolver([s1], _decls("x"))
    r.resolve_all([_decl("x")])
    s1.batch_get_calls.clear()
    # Second call should hit cache; no batch_get invoked.
    r.resolve_all([_decl("x")])
    assert s1.batch_get_calls == []


def test_batch_get_called_once_per_source_per_resolve() -> None:
    s1 = _FakeSource("first", values={"a": "1"})
    s2 = _FakeSource("second", values={"b": "2", "c": "3"})
    r = SecretResolver([s1, s2], _decls("a", "b", "c"))
    out = r.resolve_all([_decl("a"), _decl("b"), _decl("c")])
    assert out == {"a": "1", "b": "2", "c": "3"}
    # s1 was asked for [a, b, c] (it would_attempt all); returned only a.
    assert s1.batch_get_calls == [["a", "b", "c"]]
    # s2 was asked for [b, c]; a was already resolved.
    assert s2.batch_get_calls == [["b", "c"]]


def test_opt_out_skips_source_for_that_secret_only() -> None:
    s1 = _FakeSource("env_var", values={"x": "from-env", "y": "from-env-y"})
    s2 = _FakeSource("prompt", values={"x": "prompted"})
    decls = {
        "x": _decl("x", backend_mappings={"env_var": False}),
        "y": _decl("y"),
    }
    r = SecretResolver([s1, s2], decls)
    out = r.resolve_all([decls["x"], decls["y"]])
    # x skipped env_var (opt-out) and fell through to prompt.
    # y was resolved by env_var on the first try.
    assert out == {"x": "prompted", "y": "from-env-y"}


def test_unreachable_secrets_at_load_time() -> None:
    """If a secret has every active source returning False from would_attempt,
    the loader can surface it via unreachable_secrets()."""
    decls = {"reachable": _decl("reachable"), "stranded": _decl("stranded")}
    src = _FakeSource("only", attempts={"reachable"})  # doesn't attempt "stranded"
    r = SecretResolver([src], decls)
    unreachable = r.unreachable_secrets()
    assert [d.name for d in unreachable] == ["stranded"]


def test_skipping_sources_reports_per_secret() -> None:
    s1 = _FakeSource("env_var")
    s2 = _FakeSource("onepassword", attempts={"x"})
    decls = {"x": _decl("x"), "y": _decl("y")}
    r = SecretResolver([s1, s2], decls)
    # For y, onepassword skips (it has no mapping); env_var still attempts.
    skipping = r.skipping_sources(decls["y"])
    assert [s.kind for s in skipping] == ["onepassword"]


def test_first_attempting_source() -> None:
    s1 = _FakeSource("env_var")  # always attempts
    s2 = _FakeSource("prompt")
    r = SecretResolver([s1, s2], _decls("x"))
    first = r.first_attempting_source(_decl("x"))
    assert first is s1


def test_first_attempting_source_skips_opted_out() -> None:
    s1 = _FakeSource("env_var")
    s2 = _FakeSource("prompt")
    decl = _decl("x", backend_mappings={"env_var": False})
    r = SecretResolver([s1, s2], {"x": decl})
    first = r.first_attempting_source(decl)
    assert first is s2


def test_render_resolves_secret_refs_and_passes_through_plaintext() -> None:
    s1 = _FakeSource("env_var", values={"sec": "resolved-value"})
    r = SecretResolver([s1], _decls("sec"))

    @dataclass(frozen=True)
    class _Entry:
        value: str | None = None
        secret: str | None = None

    env = {
        "PLAIN": _Entry(value="plain-val"),
        "SECRET": _Entry(secret="sec"),
    }
    out = r.render(env)
    assert out == {"PLAIN": "plain-val", "SECRET": "resolved-value"}


def test_render_handles_bare_string_entries() -> None:
    """If an env dict has bare-string values (no .value attr), render
    passes them through."""
    r = SecretResolver([], _decls())
    out = r.render({"PLAIN": "raw"})
    assert out == {"PLAIN": "raw"}


def test_required_for_dedupes_and_returns_decls() -> None:
    s1 = _FakeSource("env_var", values={"shared": "v"})
    decls = {"shared": _decl("shared")}
    r = SecretResolver([s1], decls)

    @dataclass(frozen=True)
    class _Entry:
        secret: str | None = None
        value: str | None = None

    env = {
        "A": _Entry(secret="shared"),
        "B": _Entry(secret="shared"),  # same secret twice
        "C": _Entry(value="plain"),
    }
    needed = r.required_for(env)
    assert [d.name for d in needed] == ["shared"]


def test_empty_chain_with_no_secrets_resolves_empty() -> None:
    r = SecretResolver([], {})
    assert r.resolve_all([]) == {}
