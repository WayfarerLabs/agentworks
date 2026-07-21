"""The orchestrator's secret helpers: union, central prediction, and
scoped delivery.

Prediction fakes are backend-shaped duck types, same as
``tests/test_secrets_resolve.py``: the helpers only speak the
``ActiveBackend`` surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.errors import ConfigError, StateError
from agentworks.orchestration.secrets import (
    ScopedSecrets,
    predict_resolution,
    require_predicted_refs,
    secret_declarations,
    secret_union,
)
from agentworks.resources.reference import SecretReference
from agentworks.secrets.base import SecretDecl

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.resources.registry import Registry
    from agentworks.secrets.resolve import ActiveBackend


@dataclass
class _N:
    key: str
    _secret_refs: tuple[str, ...] = ()

    def deps(self) -> tuple[_N, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        return self._secret_refs

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...


# -- secret_union ------------------------------------------------------------


def test_union_dedups_in_first_encounter_order() -> None:
    nodes = [
        _N("vm-template/default", ("tailscale-auth-key",)),
        _N("vm-site/px", ("proxmox-token",)),
        _N("git-credential/gh", ("git-token-gh", "proxmox-token")),
        _N("vm/box"),
    ]
    assert secret_union(nodes) == (
        "tailscale-auth-key",
        "proxmox-token",
        "git-token-gh",
    )


def test_union_of_secretless_nodes_is_empty() -> None:
    assert secret_union([_N("vm/box")]) == ()


# -- secret_declarations -----------------------------------------------------


class _FakeRegistry:
    """Duck-typed ``Registry.lookup`` over a fixed decl set."""

    def __init__(self, decls: dict[str, SecretDecl]) -> None:
        self._decls = decls
        self.lookups: list[tuple[str, str]] = []

    def lookup(self, kind: str, name: str) -> SecretDecl:
        self.lookups.append((kind, name))
        return self._decls[name]


def test_declarations_come_from_the_registry() -> None:
    declared = SecretDecl(
        name="proxmox-token",
        description="the API token",
        backend_mappings={"env-var": "PVE_TOKEN"},
    )
    registry = cast("Registry", _FakeRegistry({"proxmox-token": declared}))
    (out,) = secret_declarations(["proxmox-token"], registry)
    assert out is declared


def test_unknown_name_falls_back_to_a_bare_declaration() -> None:
    """Parity with ``Resolver.register_name``: an empty registry must
    keep the backend chain callable for well-known names."""
    registry = cast("Registry", _FakeRegistry({}))
    (out,) = secret_declarations(["tailscale-auth-key"], registry)
    assert out == SecretDecl(name="tailscale-auth-key", description="")


# -- predict_resolution ------------------------------------------------------


class _FakeBackend:
    """An ActiveBackend-shaped stub controllable per-test."""

    def __init__(
        self,
        name: str,
        values: dict[str, str] | None = None,
        interactive: bool = False,
    ) -> None:
        self.name = name
        self.interactive = interactive
        self._values = values or {}
        self.resolve_calls: list[list[str]] = []

    def would_attempt(self, secret: SecretDecl) -> bool:
        return secret.backend_mappings.get(self.name) is not False

    def describe_lookup(self, secret: SecretDecl) -> str | None:
        return f"<{self.name}>"

    def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
        self.resolve_calls.append([s.name for s in secrets])
        return {
            s.name: self._values[s.name]
            for s in secrets
            if s.name in self._values
        }


def _decl(name: str, **kw: object) -> SecretDecl:
    return SecretDecl(name=name, description="", **kw)  # type: ignore[arg-type]


def _chain(*backends: _FakeBackend) -> list[ActiveBackend]:
    return cast("list[ActiveBackend]", list(backends))


def test_prediction_reports_the_first_producing_backend() -> None:
    chain = _chain(
        _FakeBackend("env-var"),  # attempts but produces nothing
        _FakeBackend("op", values={"a": "1"}),
    )
    assert predict_resolution([_decl("a")], chain) == {"a": "op"}


def test_prediction_none_when_nothing_would_resolve() -> None:
    assert predict_resolution([_decl("a")], _chain(_FakeBackend("env-var"))) == {
        "a": None
    }


def test_interactive_backend_predicted_resolvable_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt backend reports resolvable without probing (probing would
    BE the prompt) WHEN interactive input is available this run."""
    from agentworks import output

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    prompt = _FakeBackend("prompt", interactive=True)
    assert predict_resolution([_decl("a")], _chain(prompt)) == {"a": "prompt"}
    assert prompt.resolve_calls == []


def test_interactive_backend_predicted_unresolvable_when_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under --non-interactive / no TTY the prompt backend no-ops at
    resolve time, so preflight prediction must call a prompt-only secret
    unresolvable and fail fast (issue #202), still without probing."""
    from agentworks import output

    monkeypatch.setattr(output, "is_interactive", lambda: False)
    prompt = _FakeBackend("prompt", interactive=True)
    assert predict_resolution([_decl("a")], _chain(prompt)) == {"a": None}
    assert prompt.resolve_calls == []


def test_prediction_respects_backend_opt_out() -> None:
    prompt = _FakeBackend("prompt", interactive=True)
    decl = _decl("a", backend_mappings={"prompt": False})
    assert predict_resolution([decl], _chain(prompt)) == {"a": None}


def test_prediction_covers_every_declaration() -> None:
    chain = _chain(_FakeBackend("env-var", values={"a": "1"}))
    assert predict_resolution([_decl("a"), _decl("b")], chain) == {
        "a": "env-var",
        "b": None,
    }


# -- require_predicted_refs --------------------------------------------------


def _px_ref() -> SecretReference:
    return SecretReference(
        name="proxmox-token",
        kind="secret",
        usage="the Proxmox API token",
        source=("vm-site", "px"),
    )


def _env_only_setup(tmp_path: Path) -> tuple[Config, Registry]:
    """A real config and registry with the env-var backend alone, so
    predictions are driven by the environment (the node suites'
    not-resolvable shape)."""
    from agentworks.bootstrap import build_registry
    from tests.orchestrated_fixtures import write_operator_config

    config = write_operator_config(
        tmp_path, '[secret_config]\nbackends = ["env-var"]\n'
    )
    return config, build_registry(config)


def _env_and_prompt_setup(tmp_path: Path) -> tuple[Config, Registry]:
    """A real config whose chain is env-var THEN prompt, so an unset env
    var falls through to the interactive backend."""
    from agentworks.bootstrap import build_registry
    from tests.orchestrated_fixtures import write_operator_config

    config = write_operator_config(
        tmp_path, '[secret_config]\nbackends = ["env-var", "prompt"]\n'
    )
    return config, build_registry(config)


def test_require_predicted_refs_prompt_only_passes_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env-var unset, prompt in the chain, interactive input available:
    the ref is predicted resolvable (via prompt), so preflight passes and
    the value check defers to resolve time."""
    from agentworks import output

    config, registry = _env_and_prompt_setup(tmp_path)
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    require_predicted_refs("vm-site/px", (_px_ref(),), config, registry)


def test_require_predicted_refs_prompt_only_fails_fast_when_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same setup under --non-interactive: prompt cannot resolve, so
    preflight prediction fails fast (issue #202) instead of deferring to a
    harmless resolve-end failure."""
    from agentworks import output

    config, registry = _env_and_prompt_setup(tmp_path)
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)
    monkeypatch.setattr(output, "is_interactive", lambda: False)
    with pytest.raises(ConfigError, match="not resolvable by any active backend"):
        require_predicted_refs("vm-site/px", (_px_ref(),), config, registry)


def test_require_predicted_refs_passes_when_resolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, registry = _env_only_setup(tmp_path)
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "tok")
    require_predicted_refs("vm-site/px", (_px_ref(),), config, registry)


def test_require_predicted_refs_refuses_with_owner_usage_framing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-instance error shape, preserved VERBATIM through
    centralization (the retired base-preflight prediction's framing):
    owner display, secret name, declared usage, and the describe
    hint."""
    config, registry = _env_only_setup(tmp_path)
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)
    with pytest.raises(ConfigError) as exc:
        require_predicted_refs("vm-site/px", (_px_ref(),), config, registry)
    assert str(exc.value) == (
        "vm-site/px: secret 'proxmox-token' (the Proxmox API token) is "
        "not resolvable by any active backend"
    )
    assert exc.value.hint == (
        "`agw secret describe proxmox-token` shows how each backend "
        "looks the secret up; add a backend mapping or extend "
        "[secret_config].backends."
    )


def test_require_predicted_refs_empty_refs_is_a_no_op() -> None:
    """The early return: with nothing declared, neither the config nor
    the registry is touched (the cast object would explode on any
    lookup), so a secret-free node's preflight costs nothing here."""
    require_predicted_refs("vm/box", (), None, cast("Registry", object()))


def test_require_predicted_refs_without_config_is_loud(
    tmp_path: Path,
) -> None:
    """A config-less context reaching a secret-declaring node's
    prediction is an inspection-shaped caller bug, refused with a typed
    error rather than a crash (the old cannot-preflight-without-a-
    resolver guard's successor)."""
    _config, registry = _env_only_setup(tmp_path)
    with pytest.raises(
        ConfigError, match="without config on the context"
    ) as exc:
        require_predicted_refs("vm-site/px", (_px_ref(),), None, registry)
    assert str(exc.value).startswith("vm-site/px: ")


# -- ScopedSecrets -----------------------------------------------------------


def test_scoped_reader_serves_declared_names() -> None:
    reader = ScopedSecrets({"git-token-gh": "tok"}, ("git-token-gh",))
    assert reader.get("git-token-gh") == "tok"


def test_scoped_reader_refuses_undeclared_names() -> None:
    """A node reads ONLY the secrets it declared: the declare/receive
    contract, enforced at delivery."""
    reader = ScopedSecrets(
        {"git-token-gh": "tok", "proxmox-token": "other"}, ("git-token-gh",)
    )
    with pytest.raises(StateError, match="not declared"):
        reader.get("proxmox-token")


def test_scoped_reader_is_loud_on_unresolved_declared_names() -> None:
    reader = ScopedSecrets({}, ("git-token-gh",))
    with pytest.raises(StateError, match="not resolved"):
        reader.get("git-token-gh")


def test_scoped_reader_satisfies_the_secret_reader_protocol() -> None:
    """It drops into ``RunContext(secrets=...)``, so ``ctx.secret``
    delivery is scoped without any context change."""
    from agentworks.capabilities.base import RunContext as Ctx

    ctx = Ctx(secrets=ScopedSecrets({"a": "1"}, ("a",)))
    assert ctx.secret("a") == "1"
    with pytest.raises(StateError, match="not declared"):
        ctx.secret("b")
