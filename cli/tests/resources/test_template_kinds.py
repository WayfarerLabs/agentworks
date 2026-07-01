"""Parametrized tests for the three template kinds Phase 2a.2 adds:
``agent_template``, ``workspace_template``, ``session_template``.

Each kind has the same shape as ``vm_template`` (covered separately in
``test_vm_template_kind.py``). This file pins the parallel behavior:
kind shape, ``synthesize`` empty + non-empty paths, framework miss-
policy on typo'd ``inherits``, cycle detection at build_registry, and
the resolver's internal cycle guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import AgentTemplate, SessionTemplate, WorkspaceTemplate, load_config
from agentworks.errors import ConfigError
from agentworks.resources import (
    ALWAYS_MATERIALIZE_SOURCE,
    KIND_REGISTRY,
    TemplateReference,
)


@dataclass(frozen=True)
class _KindSpec:
    """One parametrization entry per template kind."""

    kind: str
    section: str  # TOML section name (e.g. "agent_templates")
    expected_type: type


SPECS: tuple[_KindSpec, ...] = (
    _KindSpec("agent_template", "agent_templates", AgentTemplate),
    _KindSpec("workspace_template", "workspace_templates", WorkspaceTemplate),
    _KindSpec("session_template", "session_templates", SessionTemplate),
)


def _write_cfg(path: Path, body: str) -> Path:
    pub = path.parent / "id.pub"
    priv = path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body),
    )
    return path


# -- Kind shape -------------------------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_kind_attributes(spec: _KindSpec) -> None:
    kind = KIND_REGISTRY[spec.kind]
    assert kind.kind == spec.kind
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
    template_reqs = [
        r for r in tmpl.referenced_resources() if isinstance(r, TemplateReference)
    ]
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


# -- required_resources emission -------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_template_required_resources_emits_template_requirement(
    spec: _KindSpec,
) -> None:
    """Each ``XxxTemplate.referenced_resources()`` emits a TemplateReference
    per name in ``inherits`` with the right kind and source.
    """
    tmpl = spec.expected_type(name="child", inherits=["base", "extras"])
    reqs = tmpl.referenced_resources()
    template_reqs = [r for r in reqs if isinstance(r, TemplateReference)]
    assert len(template_reqs) == 2
    by_name = {r.name: r for r in template_reqs}
    assert by_name["base"].kind == spec.kind
    assert by_name["base"].source == (spec.kind, "child")
    assert by_name["base"].usage == "a parent template"


# -- Framework miss-policy / cycle detection -------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_inherits_typo_fires_framework_miss_policy(
    spec: _KindSpec, tmp_path: Path
) -> None:
    """A typo'd ``inherits`` reference (not ``"default"``, not declared)
    surfaces as a clean framework miss-policy error at build_registry
    time.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        f"""
        [{spec.section}.child]
        inherits = ["defualt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match=f"{spec.kind} kind only auto-declares"):
        build_registry(cfg)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_inherits_default_works_without_operator_declaration(
    spec: _KindSpec, tmp_path: Path
) -> None:
    """``inherits = ["default"]`` works even when the operator omits
    ``[<section>.default]``; the always-materialize step seeds the
    default row and the framework's miss policy resolves the
    reference via it.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        f"""
        [{spec.section}.child]
        inherits = ["default"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)

    default = registry.lookup(spec.kind, "default")
    child = registry.lookup(spec.kind, "child")
    assert default.origin.variant == "auto-declared"
    assert child.origin.variant == "operator-declared"


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_inherits_cycle_caught_by_framework(spec: _KindSpec, tmp_path: Path) -> None:
    """Non-default cycles slip past any load-time eager resolve
    (workspace and session resolve lazily; agent's eager resolve only
    descends from default). The framework's cycle pass at
    build_registry time catches them.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        f"""
        [{spec.section}.a]
        inherits = ["b"]

        [{spec.section}.b]
        inherits = ["a"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="cycle detected"):
        build_registry(cfg)


def test_agent_template_default_cycle_caught_at_load(tmp_path: Path) -> None:
    """For agent_templates specifically, ``load_config`` eagerly resolves
    the default via the per-template field-merging resolver. A cycle
    through ``default`` therefore hits the resolver's internal
    visited-set guard at load time, not the framework's pass at
    build_registry time.

    Non-parametrized because only ``agent_template`` (and
    ``vm_template``, tested in ``test_vm_template_kind.py``) is
    eagerly resolved at load time. ``workspace_template`` and
    ``session_template`` resolve lazily; cycles in them slip past
    load_config and are caught by the framework instead, which the
    parametrized ``test_inherits_cycle_caught_by_framework`` above
    covers for all three.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        """
        [agent_templates.default]
        inherits = ["a"]

        [agent_templates.a]
        inherits = ["default"]
        """,
    )
    with pytest.raises(ConfigError, match="cycle"):
        load_config(cfg_file, warn_issues=False)
