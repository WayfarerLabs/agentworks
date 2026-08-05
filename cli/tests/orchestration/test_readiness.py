"""The preflight sweep: every node, one context, first failure wins."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import ConfigError
from agentworks.orchestration.readiness import preflight_all
from agentworks.resources import Registry
from agentworks.resources.reference import ResourceReference


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
    preflight_all(nodes, ctx, registry=Registry.empty())
    assert [key for key, _ in log] == ["vm-site/px", "git-credential/gh", "vm/box"]
    assert all(seen is ctx for _, seen in log)


def test_sweep_propagates_the_first_failure() -> None:
    log: list[tuple[str, RunContext]] = []
    nodes = [
        _N("vm-site/px", log),
        _N("git-credential/gh", log, fail=True),
        _N("vm/box", log),
    ]
    with pytest.raises(ConfigError, match="git-credential/gh"):
        preflight_all(nodes, RunContext(), registry=Registry.empty())
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

    with pytest.raises(RuntimeError, match="not a rejection"):
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
        PLUGINS_ENABLED + f"[secret_config]\nbackends = [{chain}]\n",
        manifests=[proxmox_site()],
    )
    registry = build_registry(config)
    return config, registry, [vm_site_node(registry, "proxmox")]


def test_sweep_predicts_resolvability_with_owner_usage_framing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prediction moved OUT of node preflight and into the sweep, and it
    kept the error verbatim: the node's key as owner, the secret's name,
    its declared usage, and the describe hint."""
    config, registry, nodes = _site_graph(tmp_path, '"env-var"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)

    with pytest.raises(ConfigError) as exc:
        preflight_all(nodes, RunContext(config=config), registry=registry)  # type: ignore[arg-type]

    assert str(exc.value) == (
        "vm-site/proxmox: secret 'proxmox-token' (the Proxmox API token) is not resolvable by any active backend"
    )
    assert exc.value.hint is not None and "agw secret describe proxmox-token" in exc.value.hint


def test_node_preflight_alone_does_not_predict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the move, and the reason doctor comes out right
    for free: invoking the node's own preflight (what doctor does per
    row) asks nothing about resolvability, so the same unresolvable
    secret that fails the sweep above leaves the node itself healthy."""
    config, _registry, nodes = _site_graph(tmp_path, '"env-var"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)

    nodes[0].preflight(RunContext(config=config))  # type: ignore[attr-defined]


def test_sweep_fails_fast_non_interactively_on_a_prompt_only_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #202, re-pinned at the sweep. A secret only the prompt
    backend could supply, under ``--non-interactive``: the sweep refuses
    before any prompt or mutation rather than letting the command reach
    a resolve-end failure. Interactive, the same graph passes and the
    value check defers to resolve time.
    """
    from agentworks import output

    config, registry, nodes = _site_graph(tmp_path, '"env-var", "prompt"')
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)

    monkeypatch.setattr(output, "is_interactive", lambda: False)
    with pytest.raises(ConfigError, match="not resolvable by any active backend"):
        preflight_all(nodes, RunContext(config=config), registry=registry)  # type: ignore[arg-type]

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    preflight_all(nodes, RunContext(config=config), registry=registry)  # type: ignore[arg-type]
