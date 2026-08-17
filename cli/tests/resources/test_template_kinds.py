"""Parametrized tests for the three template kinds Phase 2a.2 adds:
``agent-template``, ``workspace-template``, ``session-template``.

Each kind has the same shape as ``vm-template`` (covered separately in
``test_vm_template_kind.py``). This file pins the parallel behavior:
kind shape, ``synthesize`` empty + non-empty paths, framework miss-
policy on typo'd ``inherits``, cycle detection at build_registry, and
the resolver's internal cycle guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.resources import (
    ALWAYS_MATERIALIZE_SOURCE,
    KIND_REGISTRY,
    TemplateReference,
)
from agentworks.resources.graph import FinalizeContext
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.template import VMTemplate
from agentworks.workspaces.template import WorkspaceTemplate
from tests.conftest import ManifestDoc, write_cfg


@dataclass(frozen=True)
class _KindSpec:
    """One parametrization entry per template kind."""

    kind: str
    expected_type: type


#: Every kind whose rows inherit. All four behave identically here, which
#: is the point of the parametrization: what makes one an inheriting kind
#: is its ``auto-declare`` miss policy and its ``inherits`` field, not
#: anything per kind, so a fifth arrives by being added to this tuple.
SPECS: tuple[_KindSpec, ...] = (
    _KindSpec("vm-template", VMTemplate),
    _KindSpec("agent-template", AgentTemplate),
    _KindSpec("workspace-template", WorkspaceTemplate),
    _KindSpec("session-template", SessionTemplate),
)


def _write_cfg(path: Path, *manifests: ManifestDoc) -> Path:
    """``write_cfg`` under this file's path-taking spelling."""
    return write_cfg(path.parent, *manifests, filename=path.name)


# -- Kind shape -------------------------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_kind_attributes(spec: _KindSpec) -> None:
    kind = KIND_REGISTRY[spec.kind]
    assert kind.miss_policy == "auto-declare"
    assert kind.auto_declare_names == frozenset({"default"})


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_synthesize_empty_builds_default(spec: _KindSpec) -> None:
    """The always-materialize path: synthesize empty yields the kind's
    code-defined default template with the framework's reserved
    sentinel source. All inherit-shaped fields at their defaults; the
    per-template resolver layer applies concrete defaults via the
    Resolved* type.
    """
    kind = KIND_REGISTRY[spec.kind]
    raw = kind.synthesize(())
    # Runtime guard; ``spec.expected_type`` is a dynamic ``type`` so mypy
    # can't statically narrow, hence the explicit ``Any`` after.
    assert isinstance(raw, spec.expected_type)
    result: Any = raw
    assert result.name == "default"
    assert result.origin is not None
    assert result.origin.variant == "auto-declared"
    assert result.origin.source == ALWAYS_MATERIALIZE_SOURCE
    assert result.inherits == []


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_no_inherits_produces_no_template_requirements(spec: _KindSpec) -> None:
    tmpl = spec.expected_type(name="alone")
    template_reqs = [r for r in tmpl.dependencies(FinalizeContext()) if isinstance(r, TemplateReference)]
    assert template_reqs == []


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_synthesize_with_requirement_uses_first_source(spec: _KindSpec) -> None:
    """Non-empty synthesize records the first requirement's source as
    origin (the worklist-driven path -- defensive symmetry; the
    always-materialize pre-step short-circuits this in practice).
    """
    kind = KIND_REGISTRY[spec.kind]
    req = TemplateReference(
        name="default",
        kind=spec.kind,
        usage="a parent template",
        source=(spec.kind, "child"),
    )
    result = kind.synthesize([req])
    assert result.origin is not None
    assert result.origin.source == (spec.kind, "child")


# -- dependencies emission -------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_template_dependencies_emits_template_requirement(
    spec: _KindSpec,
) -> None:
    """Each ``XxxTemplate.dependencies(FinalizeContext())`` emits a TemplateReference
    per name in ``inherits`` with the right kind and source.
    """
    tmpl = spec.expected_type(name="child", inherits=["base", "extras"])
    reqs = tmpl.dependencies(FinalizeContext())
    template_reqs = [r for r in reqs if isinstance(r, TemplateReference)]
    assert len(template_reqs) == 2
    by_name = {r.name: r for r in template_reqs}
    assert by_name["base"].kind == spec.kind
    assert by_name["base"].source == (spec.kind, "child")
    assert by_name["base"].usage == "a parent template"


# -- Framework miss-policy / cycle detection -------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_inherits_typo_fires_framework_miss_policy(spec: _KindSpec, tmp_path: Path) -> None:
    """A typo'd ``inherits`` reference (not ``"default"``, not declared)
    surfaces as a clean framework miss-policy error at build_registry
    time.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        ManifestDoc(spec.kind, "child", {"inherits": ["defualt"]}),
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match=f"{spec.kind} kind only auto-declares"):
        build_registry(cfg)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_inherits_default_works_without_operator_declaration(spec: _KindSpec, tmp_path: Path) -> None:
    """``inherits = ["default"]`` works even when the operator omits
    ``[<section>.default]``; the always-materialize step seeds the
    default row and the framework's miss policy resolves the
    reference via it.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        ManifestDoc(spec.kind, "child", {"inherits": ["default"]}),
    )
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)

    default = registry.lookup(spec.kind, "default")
    child = registry.lookup(spec.kind, "child")
    assert default.origin.variant == "auto-declared"
    assert child.origin.variant == "operator-declared"


def test_inherits_cycle_caught_by_framework(tmp_path: Path) -> None:
    """Non-default cycles slip past any load-time eager resolve
    (workspace and session resolve lazily; agent's eager resolve only
    descends from default). The framework's cycle pass at
    build_registry time catches them.

    One kind, not all three: ``Registry._detect_cycles`` is a DFS over
    the built edge map and branches on nothing kind-shaped, and THAT each
    kind emits its inherits edges at all is what
    ``test_template_dependencies_emits_template_requirement`` pins, per
    kind. A cycle through ``default``, where materialization is also in
    play, is a different shape and is pinned on vm-template in
    ``test_vm_template_kind.py``.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        ManifestDoc("agent-template", "a", {"inherits": ["b"]}),
        ManifestDoc("agent-template", "b", {"inherits": ["a"]}),
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError):
        build_registry(cfg)
