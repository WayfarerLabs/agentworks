"""Tests for ``VMTemplateKind`` (Phase 2a.1).

The framework now owns inherits-reference validation and cycle detection
for ``vm_template`` resources; the existing per-template field-merging
resolver in ``agentworks.vms.templates`` keeps doing the merge work.

Test coverage:

- ``VMTemplateKind`` declares the right kind / miss_policy / auto_declare_names.
- ``synthesize`` honors the empty-requirements contract (Phase 2a.0 work)
  and the worklist-driven path (non-empty requirements).
- ``VMTemplate.required_resources`` emits ``TemplateRequirement`` for each
  entry in ``inherits``.
- The framework's miss policy fires on typo'd ``inherits`` references
  (e.g. ``inherits = ["defualt"]``).
- The framework's cycle detection catches inheritance loops.
- Inheriting from ``"default"`` works even when the operator omits
  ``[vm_templates.default]`` (always-materialize + framework's miss policy).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import VMTemplate, load_config
from agentworks.errors import ConfigError
from agentworks.resources import (
    ALWAYS_MATERIALIZE_SOURCE,
    KIND_REGISTRY,
    TemplateRequirement,
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


def test_vm_template_kind_attributes() -> None:
    kind = KIND_REGISTRY["vm_template"]
    assert kind.kind == "vm_template"
    assert kind.miss_policy == "auto-declare"
    assert kind.auto_declare_names == frozenset({"default"})


def test_vm_template_kind_synthesize_empty_builds_default() -> None:
    """The always-materialize path: synthesize with no requirements
    yields ``VMTemplate(name="default")`` with the framework's reserved
    sentinel source.
    """
    kind = KIND_REGISTRY["vm_template"]
    result = kind.synthesize(())
    assert isinstance(result, VMTemplate)
    assert result.name == "default"
    assert result.origin is not None
    assert result.origin.variant == "auto-declared"
    assert result.origin.source == ALWAYS_MATERIALIZE_SOURCE
    # All inherit-shaped fields default to None / empty -- the resolver
    # layer applies concrete defaults via ResolvedVMTemplate.
    assert result.cpus is None
    assert result.inherits == []


def test_vm_template_kind_synthesize_with_requirement_uses_first_source() -> None:
    """The worklist-driven path: when a child template's
    ``inherits = ["default"]`` triggers auto-declare of
    ``vm_template:default``, the synthesized default's origin source is
    the requirement's source (the child template).
    """
    kind = KIND_REGISTRY["vm_template"]
    req = TemplateRequirement(
        name="default",
        kind="vm_template",
        usage="a parent template",
        source=("vm_template", "child"),
    )
    result = kind.synthesize([req])
    assert result.name == "default"
    assert result.origin is not None
    assert result.origin.source == ("vm_template", "child")


# -- VMTemplate.required_resources ------------------------------------------


def test_vm_template_required_resources_emits_template_requirement_for_inherits() -> None:
    """Each name in ``inherits`` produces a TemplateRequirement with
    kind=vm_template and the declaring template's source. Other
    requirements (env secrets, tailscale auth key) are unchanged.
    """
    tmpl = VMTemplate(name="child", inherits=["base", "extras"])
    reqs = tmpl.required_resources()
    template_reqs = [r for r in reqs if isinstance(r, TemplateRequirement)]
    assert len(template_reqs) == 2
    by_name = {r.name: r for r in template_reqs}
    assert by_name["base"].kind == "vm_template"
    assert by_name["base"].source == ("vm_template", "child")
    assert by_name["base"].usage == "a parent template"
    assert by_name["extras"].source == ("vm_template", "child")


def test_vm_template_no_inherits_produces_no_template_requirements() -> None:
    tmpl = VMTemplate(name="alone")
    reqs = tmpl.required_resources()
    template_reqs = [r for r in reqs if isinstance(r, TemplateRequirement)]
    assert template_reqs == []


# -- Framework validation via load_config + build_registry -----------------


def test_inherits_typo_fires_framework_miss_policy_error(tmp_path: Path) -> None:
    """A typo in ``inherits`` (a name that's neither operator-declared
    nor the reserved ``default``) surfaces as a framework miss-policy
    error with the requirement source attached.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        """
        [vm_templates.child]
        inherits = ["defualt"]  # typo
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="vm_template kind only auto-declares"):
        build_registry(cfg)


def test_inherits_default_works_without_operator_declaration(tmp_path: Path) -> None:
    """``inherits = ["default"]`` works even when the operator omits
    ``[vm_templates.default]``. The always-materialize step seeds
    ``vm_template:default``; the framework's miss policy resolves the
    reference via the seeded row.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        """
        [vm_templates.child]
        inherits = ["default"]
        cpus = 4
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)
    # Both rows exist; default is auto-declared (always-materialize),
    # child is operator-declared.
    default = registry.lookup("vm_template", "default")
    child = registry.lookup("vm_template", "child")
    assert default.origin.variant == "auto-declared"
    assert child.origin.variant == "operator-declared"


def test_non_default_inherits_cycle_caught_by_framework(tmp_path: Path) -> None:
    """Mutually-inheriting templates form a cycle; the resolver's
    internal visited-set guard catches it during ``load_config``'s
    eager resolve of the default template. The framework's
    ``Registry.finalize`` cycle pass is the canonical check (and
    runs independently at ``build_registry`` time); the resolver
    guard is the safety net for the load-time eager path.

    Note: this particular config (cycle between non-default
    templates) doesn't actually trip the eager resolve, since
    ``resolve_from_dict`` only descends from "default". The
    framework path is what catches it here.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        """
        [vm_templates.a]
        inherits = ["b"]

        [vm_templates.b]
        inherits = ["a"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="cycle detected"):
        build_registry(cfg)


def test_non_default_self_reference_caught_by_framework(tmp_path: Path) -> None:
    """``inherits = ["a"]`` where the template itself is ``a`` -- a
    self-loop is a one-node cycle. As with the mutual-inherits case,
    a non-default self-loop slips past the eager resolve (which
    descends from "default" only) and is caught by the framework's
    cycle pass at ``build_registry`` time.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        """
        [vm_templates.a]
        inherits = ["a"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="cycle detected"):
        build_registry(cfg)


def test_framework_cycle_detector_catches_registry_cycles(tmp_path: Path) -> None:
    """The framework's cycle detector is the canonical check (per
    Phase 2a.1's design). Bypass the defensive load-time check by
    publishing VMTemplates directly into a Registry, then finalize.
    The framework pass should detect the cycle and raise.
    """
    from agentworks.config import VMTemplate
    from agentworks.resources import Origin, Registry

    registry = Registry.empty()
    fake_origin = Origin.operator_declared(file=tmp_path / "c.toml", line=1)
    registry.add(
        "vm_template", "a",
        VMTemplate(name="a", inherits=["b"]),
        fake_origin,
    )
    registry.add(
        "vm_template", "b",
        VMTemplate(name="b", inherits=["a"]),
        fake_origin,
    )
    with pytest.raises(ConfigError, match="cycle detected"):
        registry.finalize()


def test_inherits_cycle_through_default_caught_at_load(tmp_path: Path) -> None:
    """A cycle whose path goes through ``default`` (the always-materialized
    reserved name) IS exercised by ``load_config``'s eager resolve --
    ``resolve_from_dict`` descends from ``default`` recursively. The
    resolver's internal visited-set guard catches it and raises
    ``ConfigError`` instead of crashing with ``RecursionError``.
    Regression test for Phase 2a.1's retirement of the load-time
    ``_detect_template_cycles`` pass: the resolver guards itself; the
    framework still owns the canonical check at build_registry time.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        """
        [vm_templates.default]
        inherits = ["a"]

        [vm_templates.a]
        inherits = ["default"]
        """,
    )
    with pytest.raises(ConfigError, match="cycle"):
        load_config(cfg_file, warn_issues=False)


def test_unreferenced_vm_template_default_lands_with_framework_source(
    tmp_path: Path,
) -> None:
    """Direct positive: a config that declares NO ``[vm_templates.*]``
    blocks and nothing referencing ``vm_template:default`` still lands
    the default row in the registry with the synthetic
    ``("framework", "always-materialize")`` source. Mirrors the
    admin-template test in ``test_always_materialize.py`` for the
    Phase 2a.1 kind.
    """
    cfg_file = _write_cfg(tmp_path / "config.toml", "")
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)

    default = registry.lookup("vm_template", "default")
    assert default.origin is not None
    assert default.origin.variant == "auto-declared"
    assert default.origin.source == ALWAYS_MATERIALIZE_SOURCE
