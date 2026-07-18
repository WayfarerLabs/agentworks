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

from agentworks.errors import StateError
from agentworks.orchestration.secrets import (
    ScopedSecrets,
    predict_resolution,
    secret_declarations,
    secret_union,
)
from agentworks.secrets.base import SecretDecl

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
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


def test_interactive_backend_is_optimistic_without_probing() -> None:
    """``preview_resolution``'s exact semantics survive
    centralization: a prompt backend reports
    resolvable without probing, because probing would BE the prompt."""
    prompt = _FakeBackend("prompt", interactive=True)
    assert predict_resolution([_decl("a")], _chain(prompt)) == {"a": "prompt"}
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
