"""Tests for SecretResolver."""

from __future__ import annotations

import pytest

from agentworks.env import EnvEntry
from agentworks.errors import ConfigError, SecretMappingError, SecretUnavailableError
from agentworks.secrets import SecretDecl, SecretResolver


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

    def describe_lookup(self, secret: SecretDecl) -> str | None:  # noqa: ARG002 - test stub
        # Tests don't assert on identifiers; return a fixed sentinel so
        # resolver's resolution-detail line has something to print.
        return f"<{self.kind}>"


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


def test_hard_miss_halts_chain_via_secret_mapping_error() -> None:
    """Persistent-store backends (1Password, Vault) raise SecretMappingError
    when an explicit mapping doesn't resolve. The resolver lets the exception
    propagate so a misconfigured store doesn't fall through to a prompt that
    would mask the real config problem."""

    class _StrictMissSource:
        kind = "strict"

        def __init__(self) -> None:
            self.batch_get_calls: list[list[str]] = []

        def would_attempt(self, secret: SecretDecl) -> bool:  # noqa: ARG002
            return True

        def get(self, secret: SecretDecl) -> str | None:
            raise SecretMappingError(
                f"strict backend has no item for {secret.name!r}",
            )

        def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
            self.batch_get_calls.append([s.name for s in secrets])
            # Default behavior loops .get(), so propagation flows naturally.
            for s in secrets:
                self.get(s)
            return {}  # unreachable; .get raises

    strict = _StrictMissSource()
    later = _FakeSource("prompt", values={"x": "would-prompt"})
    # _StrictMissSource ducktypes SecretSource enough for this test (it
    # raises before the missing ``describe_lookup`` is ever queried).
    r = SecretResolver([strict, later], _decls("x"))  # type: ignore[list-item]

    with pytest.raises(SecretMappingError, match="strict backend has no item"):
        r.resolve_all([_decl("x")])

    # Critical contract: the prompt source NEVER ran. Hard miss halts the chain.
    assert later.batch_get_calls == []


def test_unsatisfied_raises_with_backends_tried() -> None:
    s1 = _FakeSource("env-var")
    s2 = _FakeSource("prompt")
    r = SecretResolver([s1, s2], _decls("x"))
    with pytest.raises(SecretUnavailableError) as exc:
        r.resolve_all([_decl("x")])
    assert "x" in str(exc.value)
    assert "env-var" in (exc.value.hint or "")
    assert "prompt" in (exc.value.hint or "")


def test_embedded_newline_in_resolved_value_raises() -> None:
    """ADR 0014: a resolved secret value containing a newline corrupts
    SSH SetEnv. The resolver hard-fails at resolve_all time so the
    operator sees a clear error instead of an opaque SSH-side rejection."""
    s1 = _FakeSource("vault", values={"x": "line1\nline2"})
    r = SecretResolver([s1], _decls("x"))
    with pytest.raises(ConfigError, match="control character"):
        r.resolve_all([_decl("x")])


def test_embedded_carriage_return_in_resolved_value_raises() -> None:
    """Same guard for bare CR (some legacy formats)."""
    s1 = _FakeSource("vault", values={"x": "line1\rline2"})
    r = SecretResolver([s1], _decls("x"))
    with pytest.raises(ConfigError, match="control character"):
        r.resolve_all([_decl("x")])


def test_embedded_nul_in_resolved_value_raises() -> None:
    """NUL bytes truncate the OpenSSH SetEnv arg at the C-string boundary;
    reject up front for the same reason newlines are rejected."""
    s1 = _FakeSource("vault", values={"x": "valid\x00rest"})
    r = SecretResolver([s1], _decls("x"))
    with pytest.raises(ConfigError, match="control character"):
        r.resolve_all([_decl("x")])


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
    s1 = _FakeSource("env-var", values={"x": "from-env", "y": "from-env-y"})
    s2 = _FakeSource("prompt", values={"x": "prompted"})
    decls = {
        "x": _decl("x", backend_mappings={"env-var": False}),
        "y": _decl("y"),
    }
    r = SecretResolver([s1, s2], decls)
    out = r.resolve_all([decls["x"], decls["y"]])
    # x skipped env-var (opt-out) and fell through to prompt.
    # y was resolved by env-var on the first try.
    assert out == {"x": "prompted", "y": "from-env-y"}


def test_unreachable_secrets_at_load_time() -> None:
    """If a secret has every active source returning False from would_attempt,
    the loader can surface it via unreachable_secrets()."""
    decls = {"reachable": _decl("reachable"), "stranded": _decl("stranded")}
    src = _FakeSource("only", attempts={"reachable"})  # doesn't attempt "stranded"
    r = SecretResolver([src], decls)
    unreachable = r.unreachable_secrets()
    assert [d.name for d in unreachable] == ["stranded"]


def test_first_attempting_source() -> None:
    s1 = _FakeSource("env-var")  # always attempts
    s2 = _FakeSource("prompt")
    r = SecretResolver([s1, s2], _decls("x"))
    first = r.first_attempting_source(_decl("x"))
    assert first is s1


def test_first_attempting_source_skips_opted_out() -> None:
    s1 = _FakeSource("env-var")
    s2 = _FakeSource("prompt")
    decl = _decl("x", backend_mappings={"env-var": False})
    r = SecretResolver([s1, s2], {"x": decl})
    first = r.first_attempting_source(decl)
    assert first is s2


def test_preview_resolution_reports_first_source_with_value() -> None:
    s1 = _FakeSource("env-var", values={"x": "from-env"})
    s2 = _FakeSource("prompt")
    r = SecretResolver([s1, s2], _decls("x"))
    assert r.preview_resolution(_decl("x")) == "env-var"


def test_preview_resolution_falls_through_to_prompt() -> None:
    """env-var would_attempt is True but get returns None; prompt is the
    next source and is not opted out, so preview reports prompt."""
    s1 = _FakeSource("env-var")  # no values
    s2 = _FakeSource("prompt")
    r = SecretResolver([s1, s2], _decls("x"))
    assert r.preview_resolution(_decl("x")) == "prompt"


def test_preview_resolution_never_calls_get_on_prompt_source() -> None:
    """Preview must never call ``get`` on a prompt source -- doing so would
    actually prompt the operator. Prompt is reported on the strength of
    ``would_attempt`` alone."""

    class _ExplodingPrompt(_FakeSource):
        def get(self, secret: SecretDecl) -> str | None:
            raise AssertionError("preview_resolution must not call prompt.get()")

    s1 = _FakeSource("env-var")
    s2 = _ExplodingPrompt("prompt")
    r = SecretResolver([s1, s2], _decls("x"))
    assert r.preview_resolution(_decl("x")) == "prompt"


def test_preview_resolution_skips_opted_out_env_var() -> None:
    """A secret with ``backend_mappings.env-var = false`` makes env-var's
    would_attempt return False; preview skips it and continues to prompt."""
    s1 = _FakeSource("env-var", values={"x": "from-env"})
    s2 = _FakeSource("prompt")
    decl = _decl("x", backend_mappings={"env-var": False})
    r = SecretResolver([s1, s2], {"x": decl})
    assert r.preview_resolution(decl) == "prompt"


def test_preview_resolution_honors_opt_out_for_prompt_source() -> None:
    """Regression: prompt was previously reported even when opted out via
    ``backend_mappings.prompt = false``. Now it correctly returns None,
    matching what would actually happen at command time
    (SecretUnavailableError)."""
    s1 = _FakeSource("env-var")  # no values; falls through
    s2 = _FakeSource("prompt")
    decl = _decl("x", backend_mappings={"prompt": False})
    r = SecretResolver([s1, s2], {"x": decl})
    assert r.preview_resolution(decl) is None


def test_preview_resolution_returns_none_when_no_source_attempts() -> None:
    """If nothing in the chain would attempt the secret, preview is None.
    Defensive case: the loader would normally raise at config-load time."""
    s1 = _FakeSource("env-var", attempts=set())  # never attempts anything
    r = SecretResolver([s1], _decls("x"))
    assert r.preview_resolution(_decl("x")) is None


def test_render_resolves_secret_refs_and_passes_through_plaintext() -> None:
    s1 = _FakeSource("env-var", values={"sec": "resolved-value"})
    r = SecretResolver([s1], _decls("sec"))

    env = {
        "PLAIN": EnvEntry(key="PLAIN", value="plain-val"),
        "SECRET": EnvEntry(key="SECRET", secret="sec"),
    }
    out = r.render(env)
    assert out == {"PLAIN": "plain-val", "SECRET": "resolved-value"}


def test_required_for_dedupes_and_returns_decls() -> None:
    s1 = _FakeSource("env-var", values={"shared": "v"})
    decls = {"shared": _decl("shared")}
    r = SecretResolver([s1], decls)

    env = {
        "A": EnvEntry(key="A", secret="shared"),
        "B": EnvEntry(key="B", secret="shared"),  # same secret twice
        "C": EnvEntry(key="C", value="plain"),
    }
    needed = r.required_for(env)
    assert [d.name for d in needed] == ["shared"]


def test_empty_chain_with_no_secrets_resolves_empty() -> None:
    r = SecretResolver([], {})
    assert r.resolve_all([]) == {}


def test_unsatisfied_hint_omits_opted_out_sources() -> None:
    """The hint for a missing secret should not list sources whose would_attempt
    returned False (e.g. via backend_mappings.env-var = false). Only sources
    that actually tried appear in the per-secret hint."""
    s1 = _FakeSource("env-var")
    s2 = _FakeSource("prompt")
    decl = _decl("x", backend_mappings={"env-var": False})
    r = SecretResolver([s1, s2], {"x": decl})
    with pytest.raises(SecretUnavailableError) as exc:
        r.resolve_all([decl])
    hint = exc.value.hint or ""
    assert "x" in hint
    assert "prompt" in hint
    assert "env-var" not in hint


def test_unsatisfied_hint_per_secret_listing() -> None:
    """When multiple secrets fail, each gets its own per-secret hint line so
    operators can see which backends were tried for each one."""
    s_env = _FakeSource("env-var")
    s_prompt = _FakeSource("prompt")
    decls = {
        "a": _decl("a", backend_mappings={"env-var": False}),
        "b": _decl("b"),
    }
    r = SecretResolver([s_env, s_prompt], decls)
    with pytest.raises(SecretUnavailableError) as exc:
        r.resolve_all([decls["a"], decls["b"]])
    hint = exc.value.hint or ""
    # 'a' opted out of env-var, only prompt tried.
    assert "a: tried prompt" in hint
    # 'b' had no opt-out, both tried.
    assert "b: tried env-var, prompt" in hint


def test_render_mixed_plaintext_and_secret_entries() -> None:
    """render() handles plaintext and secret EnvEntry instances together."""
    s1 = _FakeSource("env-var", values={"sec": "resolved"})
    r = SecretResolver([s1], _decls("sec"))

    env = {
        "PLAIN": EnvEntry(key="PLAIN", value="plain-val"),
        "SECRET": EnvEntry(key="SECRET", secret="sec"),
    }
    out = r.render(env)
    assert out == {"PLAIN": "plain-val", "SECRET": "resolved"}


def test_render_falls_through_to_backend_for_unknown_secret_reference() -> None:
    """Phase 1b: an env entry referencing a secret name not in
    ``self._decls`` is no longer a ``ConfigError`` -- it's treated as
    auto-declared (synthesized ``SecretDecl`` with empty
    ``backend_mappings``) and resolved through the backend chain. If no
    backend resolves it, the existing ``SecretUnavailableError`` path
    fires (covered by other tests in this module).

    This test exercises the success branch: a backend that recognizes
    the auto-declared name yields a value, render returns it normally.
    """
    s = _FakeSource("env-var", values={"unknown-secret": "from-backend"})
    r = SecretResolver([s], _decls("known"))
    env = {"BAD": EnvEntry(key="BAD", secret="unknown-secret")}
    out = r.render(env)
    assert out["BAD"] == "from-backend"


def test_render_unresolved_unknown_secret_surfaces_unavailable_error() -> None:
    """Same shape: unknown-secret reference, but no backend yields a
    value. The framework reports it as ``SecretUnavailableError`` (the
    "no active backend resolved" path) -- a clearer runtime error than
    the old "must declare" ConfigError at config load.
    """
    from agentworks.errors import SecretUnavailableError

    r = SecretResolver([_FakeSource("env-var")], _decls("known"))
    env = {"BAD": EnvEntry(key="BAD", secret="unknown-secret")}
    with pytest.raises(SecretUnavailableError, match="unknown-secret"):
        r.render(env)
