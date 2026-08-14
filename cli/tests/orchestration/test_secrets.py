"""The orchestrator's secret helpers: union, central prediction, and
scoped delivery.

Prediction fakes are source-shaped duck types, same as
``tests/test_secrets_resolve.py``: the helpers only speak the
``ActiveSource`` surface.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import pytest

from agentworks.errors import ConfigError, StateError
from agentworks.orchestration.secrets import (
    ScopedSecrets,
    predict_resolution,
    require_predicted_refs,
    secret_declarations,
    secret_union,
)
from agentworks.resources.graph import Readiness
from agentworks.resources.reference import ResourceReference, SecretReference
from agentworks.schema import AgwModel, AgwRootModel, CapabilityBlock
from agentworks.secrets.base import SecretDecl
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.preview import PreviewCategory, SkippedSource
from agentworks.secrets.sources import SecretSourceDecl

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.resources.registry import Registry
    from agentworks.secrets.resolve import ActiveSource


@dataclass
class _N:
    key: str
    _secret_refs: tuple[str, ...] = ()

    def deps(self) -> tuple[_N, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        return self._secret_refs

    def config_secret_refs(self) -> tuple[ResourceReference, ...]:
        return ()

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
    """State captured by a final ActiveSource-shaped prediction fixture."""

    def __init__(
        self,
        name: str,
        values: dict[str, str] | None = None,
        interactive: bool = False,
        not_ready_reason: str | None = None,
    ) -> None:
        self.name = name
        self.interactive = interactive
        self._values = values or {}
        self.resolve_calls: list[list[str]] = []
        # ActiveSource carries its stored readiness verdict; preview_resolution
        # skips a not-ready source (R9.6). Ready by default.
        self.readiness = Readiness.ready() if not_ready_reason is None else Readiness.blocked(not_ready_reason)


class _PredictionConfig(AgwModel):
    name: Literal["prediction"]


class _PredictionMapping(AgwRootModel[str]):
    pass


class _PredictionClient:
    def __init__(self, state: _FakeBackend) -> None:
        self._state = state

    def prepare(self, requests: tuple[object, ...], *, remaining_time: object) -> None:
        return None

    def resolve(self, requests: tuple[object, ...], *, remaining_time: object) -> dict[str, str]:
        names = [cast("Any", request).name for request in requests]
        self._state.resolve_calls.append(names)
        return {name: self._state._values[name] for name in names if name in self._state._values}


class _PredictionContext(AbstractContextManager[Any]):
    def __init__(self, state: _FakeBackend) -> None:
        self._state = state

    def __enter__(self) -> _PredictionClient:
        return _PredictionClient(self._state)

    def __exit__(self, *args: object) -> None:
        return None


def _decl(name: str, **kw: object) -> SecretDecl:
    return SecretDecl(name=name, description="", **kw)  # type: ignore[arg-type]


def _chain(*backends: _FakeBackend) -> list[ActiveSource]:
    from agentworks.capabilities.secret_backend import SecretBackend
    from agentworks.capabilities.secret_backend.client import InteractionBroker, RemainingTime, SecretSourceClient
    from agentworks.secrets.resolve import ActiveSource

    out: list[ActiveSource] = []
    for state in backends:

        class _PredictionBackend(SecretBackend):
            _state: ClassVar[_FakeBackend] = state
            contract_version: ClassVar[int] = 2
            config_model: ClassVar[type[AgwModel]] = _PredictionConfig
            mapping_model: ClassVar[type[AgwRootModel[Any]]] = _PredictionMapping
            name: ClassVar[str] = "prediction"
            description: ClassVar[str] = "prediction fixture"
            prose = None
            interactive: ClassVar[bool] = state.interactive

            @classmethod
            def backend_readiness(cls) -> Readiness:
                return Readiness.ready()

            @classmethod
            def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
                return True

            @classmethod
            def describe_lookup(cls, secret_name: str, mapping: object) -> str:
                return f"<{cls._state.name}>"

            @classmethod
            def create_client(
                cls,
                *,
                source_name: str,
                config: AgwModel,
                interaction_broker: InteractionBroker | None,
                remaining_time: RemainingTime,
            ) -> AbstractContextManager[SecretSourceClient]:
                return cast("AbstractContextManager[SecretSourceClient]", _PredictionContext(cls._state))

        out.append(
            ActiveSource(
                source=SecretSourceDecl(name=state.name, backend=CapabilityBlock.of("prediction")),
                backend_class=_PredictionBackend,
                config=_PredictionConfig(name="prediction"),
                readiness=state.readiness,
            )
        )
    return out


def test_prediction_reports_the_first_attemptable_source() -> None:
    chain = _chain(
        _FakeBackend("env-var"),  # attempts but produces nothing
        _FakeBackend("op", values={"a": "1"}),
    )
    preview = predict_resolution([_decl("a")], chain, interaction=InteractionPolicy.REFUSE)["a"]
    assert preview.category is PreviewCategory.ATTEMPTABLE
    assert preview.source == "env-var"
    assert chain[0].source.name == "env-var"


def test_prediction_skips_a_not_ready_backend() -> None:
    """R9.6/R9.7 lockstep: a not-ready backend is skipped by the predictor even
    though it WOULD produce a value, so it never names a backend resolution will
    skip; the chain falls through to the next ready backend."""
    chain = _chain(
        _FakeBackend("op", values={"a": "1"}, not_ready_reason="op CLI not installed"),
        _FakeBackend("env-var", values={"a": "2"}),
    )
    preview = predict_resolution([_decl("a")], chain, interaction=InteractionPolicy.REFUSE)["a"]
    assert preview.category is PreviewCategory.ATTEMPTABLE
    assert preview.source == "env-var"
    assert preview.skipped_not_ready == (SkippedSource(source="op", reason="op CLI not installed"),)


def test_prediction_none_when_nothing_would_resolve() -> None:
    preview = predict_resolution([], _chain(_FakeBackend("env-var")), interaction=InteractionPolicy.REFUSE)
    assert preview == {}


def test_interactive_backend_predicted_resolvable_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt backend reports resolvable without probing (probing would
    BE the prompt) WHEN interactive input is available this run."""
    from agentworks import output

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    prompt = _FakeBackend("prompt", interactive=True)
    preview = predict_resolution([_decl("a")], _chain(prompt), interaction=InteractionPolicy.ALLOW)["a"]
    assert preview.category is PreviewCategory.ATTEMPTABLE
    assert preview.source == "prompt"
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
    preview = predict_resolution([_decl("a")], _chain(prompt), interaction=InteractionPolicy.REFUSE)["a"]
    assert preview.category is PreviewCategory.REFUSED_INTERACTION
    assert preview.source == "prompt"
    assert prompt.resolve_calls == []


def test_prediction_respects_backend_opt_out() -> None:
    prompt = _FakeBackend("prompt", interactive=True)
    decl = _decl("a", backend_mappings={"prompt": False})
    preview = predict_resolution([decl], _chain(prompt), interaction=InteractionPolicy.REFUSE)["a"]
    assert preview.category is PreviewCategory.UNAVAILABLE


def test_prediction_covers_every_declaration() -> None:
    chain = _chain(_FakeBackend("env-var", values={"a": "1"}))
    predictions = predict_resolution([_decl("a"), _decl("b")], chain, interaction=InteractionPolicy.REFUSE)
    assert tuple(predictions) == ("a", "b")
    assert all(preview.category is PreviewCategory.ATTEMPTABLE for preview in predictions.values())


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

    config = write_operator_config(tmp_path, '[secret_config]\nsources = ["env-var"]\n')
    return config, build_registry(config)


def _env_and_prompt_setup(tmp_path: Path) -> tuple[Config, Registry]:
    """A real config whose chain is env-var THEN prompt, so an unset env
    var falls through to the interactive backend."""
    from agentworks.bootstrap import build_registry
    from tests.orchestrated_fixtures import write_operator_config

    config = write_operator_config(tmp_path, '[secret_config]\nsources = ["env-var", "prompt"]\n')
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
    require_predicted_refs("vm-site/px", (_px_ref(),), config, registry, interaction=InteractionPolicy.ALLOW)


def test_require_predicted_refs_prompt_only_fails_fast_when_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same setup under --non-interactive: prompt cannot resolve, so
    preflight prediction fails fast (issue #202) instead of deferring to a
    harmless resolve-end failure."""
    from agentworks import output

    config, registry = _px_site_setup(tmp_path, '"prompt"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)
    monkeypatch.setattr(output, "is_interactive", lambda: False)
    with pytest.raises(ConfigError, match="not attemptable by any active source"):
        require_predicted_refs("vm-site/px", (_px_ref(),), config, registry, interaction=InteractionPolicy.REFUSE)


def test_require_predicted_refs_passes_when_resolvable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, registry = _env_only_setup(tmp_path)
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "tok")
    require_predicted_refs("vm-site/px", (_px_ref(),), config, registry, interaction=InteractionPolicy.REFUSE)


def test_require_predicted_refs_refuses_with_owner_usage_framing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-instance error shape, preserved VERBATIM through
    centralization (the retired base-preflight prediction's framing):
    owner display, secret name, declared usage, and the describe
    hint."""
    config, registry = _px_site_setup(tmp_path, '"prompt"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)
    with pytest.raises(ConfigError) as exc:
        require_predicted_refs("vm-site/px", (_px_ref(),), config, registry, interaction=InteractionPolicy.REFUSE)
    assert str(exc.value) == (
        "vm-site/px: secret 'proxmox-token' (the Proxmox API token) is not attemptable by any active source"
    )
    assert exc.value.hint == (
        "preview category: refused-interaction. Run `agw secret describe proxmox-token` for details."
    )


def test_require_predicted_refs_empty_refs_is_a_no_op() -> None:
    """The early return: with nothing declared, neither the config nor
    the registry is touched (the cast object would explode on any
    lookup), so a secret-free node's preflight costs nothing here."""
    require_predicted_refs("vm/box", (), None, cast("Registry", object()), interaction=InteractionPolicy.REFUSE)


def test_require_predicted_refs_without_config_is_loud(
    tmp_path: Path,
) -> None:
    """A config-less context reaching a secret-declaring node's
    prediction is an inspection-shaped caller bug, refused with a typed
    error rather than a crash (the old cannot-preflight-without-a-
    resolver guard's successor)."""
    _config, registry = _env_only_setup(tmp_path)
    with pytest.raises(ConfigError, match="without config on the context") as exc:
        require_predicted_refs("vm-site/px", (_px_ref(),), None, registry, interaction=InteractionPolicy.REFUSE)
    assert str(exc.value).startswith("vm-site/px: ")


# -- ScopedSecrets -----------------------------------------------------------


def test_scoped_reader_serves_declared_names() -> None:
    reader = ScopedSecrets({"git-token-gh": "tok"}, ("git-token-gh",))
    assert reader.get("git-token-gh") == "tok"


def test_scoped_reader_refuses_undeclared_names() -> None:
    """A node reads ONLY the secrets it declared: the declare/receive
    contract, enforced at delivery."""
    reader = ScopedSecrets({"git-token-gh": "tok", "proxmox-token": "other"}, ("git-token-gh",))
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


def _px_site_setup(tmp_path: Path, chain: str = '"env-var"') -> tuple[Config, Registry]:
    """A real config DECLARING the proxmox site, so its ``proxmox-token``
    reference is auto-declared into the registry at finalize. The
    prediction helpers above do not need this (they are handed a
    reference directly); intactness does, because it asks the registry."""
    from agentworks.bootstrap import build_registry
    from tests.orchestrated_fixtures import PLUGINS_ENABLED, proxmox_site, write_operator_config

    config = write_operator_config(
        tmp_path,
        PLUGINS_ENABLED + f"[secret_config]\nsources = [{chain}]\n",
        manifests=[proxmox_site()],
    )
    return config, build_registry(config)


# -- require_declared_refs (reference intactness) ----------------------------


def test_require_declared_refs_passes_for_a_declared_secret(tmp_path: Path) -> None:
    """The normal case: a referenced secret is auto-declared at finalize,
    so its row exists and intactness holds."""
    from agentworks.orchestration.secrets import require_declared_refs

    _config, registry = _px_site_setup(tmp_path)
    require_declared_refs("vm-site/proxmox", (_px_ref(),), registry)


def test_require_declared_refs_refuses_a_dangling_reference(tmp_path: Path) -> None:
    """A reference naming no registry row is a typed error, not a
    ``KeyError`` and not a silently synthesized bare declaration.

    This is the half of the old node preflight that STAYS the node's
    concern: whether the node's own declarations and the registry agree
    is registry consistency. It reaches no backend and asks nothing
    about how the secret would get a value."""
    from agentworks.orchestration.secrets import require_declared_refs

    _config, registry = _px_site_setup(tmp_path)
    dangling = SecretReference(
        name="never-declared",
        kind="secret",
        usage="the Proxmox API token",
        source=("vm-site", "px"),
    )
    with pytest.raises(ConfigError) as exc:
        require_declared_refs("vm-site/px", (dangling,), registry)
    assert "vm-site/px" in str(exc.value)
    assert "never-declared" in str(exc.value)
    assert "the Proxmox API token" in str(exc.value)


def test_require_declared_refs_says_nothing_about_resolvability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing separation: a declared secret that NOTHING can
    resolve still passes intactness. Resolvability is the operation's
    question, asked by the preflight sweep; a node asking it would make
    every resource that names a secret carry a verdict about the
    operator's backend chain."""
    from agentworks import output
    from agentworks.orchestration.secrets import require_declared_refs

    config, registry = _px_site_setup(tmp_path, '"prompt"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)
    monkeypatch.setattr(output, "is_interactive", lambda: False)

    require_declared_refs("vm-site/px", (_px_ref(),), registry)  # no raise
    # ... while the prediction the SWEEP runs over the same reference does refuse.
    with pytest.raises(ConfigError, match="not attemptable"):
        require_predicted_refs("vm-site/px", (_px_ref(),), config, registry, interaction=InteractionPolicy.REFUSE)
