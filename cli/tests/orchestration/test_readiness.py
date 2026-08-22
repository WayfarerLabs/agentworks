"""The preflight sweep: every node, one context, first failure wins."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import ConfigError
from agentworks.orchestration.readiness import preflight_all
from agentworks.resources import Registry
from agentworks.resources.reference import ResourceReference
from agentworks.secrets.policy import TtyInteractionPolicy


@dataclass
class _N:
    key: str
    log: list[tuple[str, RunContext]]
    fail: bool = False
    _deps: tuple[_N, ...] = ()
    _secret_refs: tuple[str, ...] = field(default=())
    _config_secret_refs: tuple[ResourceReference, ...] = field(default=())

    def deps(self) -> tuple[_N, ...]:
        return self._deps

    def secret_refs(self) -> tuple[str, ...]:
        return self._secret_refs

    def config_secret_refs(self) -> tuple[ResourceReference, ...]:
        return self._config_secret_refs

    def preflight(self, ctx: RunContext) -> None:
        self.log.append((self.key, ctx))
        if self.fail:
            raise ConfigError(f"{self.key}: not ready")

    def runup(self, ctx: RunContext) -> None: ...


def test_sweep_hits_every_node_in_order_with_one_context() -> None:
    log: list[tuple[str, RunContext]] = []
    nodes = [_N("vm-site/px", log), _N("git-credential/gh", log), _N("vm/box", log)]
    ctx = RunContext()
    preflight_all(nodes, ctx, registry=Registry.empty(), interaction=TtyInteractionPolicy.REFUSE)
    assert [key for key, _ in log] == ["vm-site/px", "git-credential/gh", "vm/box"]
    assert all(seen is ctx for _, seen in log)


def test_sweep_propagates_the_first_failure() -> None:
    log: list[tuple[str, RunContext]] = []
    nodes = [
        _N("vm-site/px", log),
        _N("git-credential/gh", log, fail=True),
        _N("vm/box", log),
    ]
    with pytest.raises(ConfigError):
        preflight_all(nodes, RunContext(), registry=Registry.empty(), interaction=TtyInteractionPolicy.REFUSE)
    # Nothing after the failure ran (the command aborts pre-mutation).
    assert [key for key, _ in log] == ["vm-site/px", "git-credential/gh"]


# -- the skip-and-degrade runup policy ---------------------------------------


class _RunupItem:
    def __init__(self, name: str, *, reject: bool = False, boom: bool = False) -> None:
        self.name = name
        self._reject = reject
        self._boom = boom

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None:
        if self._boom:
            raise RuntimeError(f"{self.name}: not a rejection")
        if self._reject:
            from agentworks.errors import TokenRejectedError

            raise TokenRejectedError(f"{self.name}: token rejected")


def test_skip_and_degrade_keeps_passing_items_in_order() -> None:
    from agentworks.orchestration.readiness import runup_skip_and_degrade

    items = [_RunupItem("gh"), _RunupItem("ado")]
    passed = runup_skip_and_degrade(items, RunContext(), on_reject=lambda item, exc: None)
    assert [item.name for item in passed] == ["gh", "ado"]


def test_skip_and_degrade_skips_rejected_and_reports_them() -> None:
    """The partial-degradation shape runup_and_filter pins: a rejected
    item is dropped from the returned set and handed to the caller's
    messaging; the rest continue."""
    from agentworks.orchestration.readiness import runup_skip_and_degrade

    items = [_RunupItem("gh", reject=True), _RunupItem("ado")]
    rejected: list[tuple[str, str]] = []
    announced: list[str] = []
    passed = runup_skip_and_degrade(
        items,
        RunContext(),
        announce=lambda item: announced.append(item.name),
        on_reject=lambda item, exc: rejected.append((item.name, str(exc))),
    )
    assert [item.name for item in passed] == ["ado"]
    assert announced == ["gh", "ado"]
    assert rejected == [("gh", "gh: token rejected")]


def test_skip_and_degrade_lets_non_rejections_propagate() -> None:
    """Only the typed definitive rejection is policy; anything else is
    a bug or a fatal condition and propagates uncaught."""
    from agentworks.orchestration.readiness import runup_skip_and_degrade

    with pytest.raises(RuntimeError):
        runup_skip_and_degrade(
            [_RunupItem("gh", boom=True)],
            RunContext(),
            on_reject=lambda item, exc: None,
        )


# -- the sweep owns secret-resolvability prediction ---------------------------


def _site_graph(tmp_path: Path, chain: str) -> tuple[object, object, list[object]]:
    """A real proxmox site node plus the config and registry behind it:
    the smallest honest stand-in for a command's graph, since vm-site is
    one of the two node kinds that declares a config secret."""
    from agentworks.bootstrap import build_registry
    from agentworks.vms.nodes import vm_site_node
    from tests.orchestrated_fixtures import PLUGINS_ENABLED, proxmox_site, write_operator_config

    config = write_operator_config(
        tmp_path,
        PLUGINS_ENABLED + f"[secret_config]\nsources = [{chain}]\n",
        manifests=[proxmox_site()],
    )
    registry = build_registry(config)
    return config, registry, [vm_site_node(registry, "proxmox")]


def test_sweep_requires_a_definitive_value_when_zero_impact_can_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, registry, nodes = _site_graph(tmp_path, '"env-var"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)

    with pytest.raises(ConfigError):
        preflight_all(
            nodes,
            RunContext(config=config),
            registry=registry,
            interaction=TtyInteractionPolicy.REFUSE,
        )  # type: ignore[arg-type]

    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "present")
    preflight_all(
        nodes,
        RunContext(config=config),
        registry=registry,
        interaction=TtyInteractionPolicy.REFUSE,
    )  # type: ignore[arg-type]


def test_node_preflight_alone_does_not_predict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the move, and the reason doctor comes out right
    for free: invoking the node's own preflight (what doctor does per
    row) asks nothing about source attemptability, so an absent current
    env value leaves the node itself healthy."""
    config, _registry, nodes = _site_graph(tmp_path, '"env-var"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)

    nodes[0].preflight(RunContext(config=config))  # type: ignore[attr-defined]


def test_sweep_treats_global_tty_policy_separately_from_zero_impact_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, registry, nodes = _site_graph(tmp_path, '"prompt"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)

    with pytest.raises(ConfigError):
        preflight_all(nodes, RunContext(config=config), registry=registry, interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    preflight_all(nodes, RunContext(config=config), registry=registry, interaction=TtyInteractionPolicy.ALLOW)  # type: ignore[arg-type]


def test_sweep_memoizes_repeated_refs_once_per_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterable, Sequence

    from agentworks.capabilities.secret_backend import PreviewAvailable, TtyInteractionAccess
    from agentworks.config import Config
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.preview import ResolutionPreview, SourcePreviewAttempt
    from agentworks.secrets.resolve import ActiveSource

    events: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: events.append(("sources",)) or (),
    )

    def predict(
        decls: Iterable[SecretDecl],
        sources: Sequence[ActiveSource],
        *,
        tty_access: TtyInteractionAccess,
    ) -> dict[str, ResolutionPreview]:
        names = tuple(decl.name for decl in decls)
        events.append(("preview", *names))
        return {
            name: ResolutionPreview(
                name,
                PreviewAvailable(),
                "fixture",
                None,
                (SourcePreviewAttempt("fixture", None, PreviewAvailable()),),
            )
            for name in names
        }

    monkeypatch.setattr("agentworks.orchestration.secrets.predict_resolution", predict)
    log: list[tuple[str, RunContext]] = []
    ref = ResourceReference(name="token", kind="secret", usage="test", source=("fixture", "owner"))
    nodes = [
        _N("first", log, _config_secret_refs=(ref,)),
        _N("second", log, _config_secret_refs=(ref,)),
    ]
    preflight_all(
        nodes,
        RunContext(config=cast("Config", object())),
        registry=Registry.empty(),
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert events == [("sources",), ("preview", "token")]
    assert [name for name, _ctx in log] == ["first", "second"]


def test_sweep_does_no_source_or_provider_work_for_an_unreachable_later_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.config import Config

    events: list[str] = []
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: events.append("sources") or (),
    )
    monkeypatch.setattr(
        "agentworks.orchestration.secrets.predict_resolution",
        lambda *args, **kwargs: events.append("provider") or {},
    )
    log: list[tuple[str, RunContext]] = []
    ref = ResourceReference(name="later", kind="secret", usage="test", source=("fixture", "later"))
    nodes = [
        _N("first", log, fail=True),
        _N("later", log, _config_secret_refs=(ref,)),
    ]
    with pytest.raises(ConfigError):
        preflight_all(
            nodes,
            RunContext(config=cast("Config", object())),
            registry=Registry.empty(),
            interaction=TtyInteractionPolicy.REFUSE,
        )
    assert events == []
    assert [name for name, _ctx in log] == ["first"]
