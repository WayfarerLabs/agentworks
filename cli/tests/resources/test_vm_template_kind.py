"""Tests for ``VMTemplateKind`` (Phase 2a.1).

The framework now owns inherits-reference validation and cycle detection
for ``vm-template`` resources; the existing per-template field-merging
resolver in ``agentworks.vms.templates`` keeps doing the merge work.

Test coverage:

- ``VMTemplateKind`` declares the right kind / miss_policy / auto_declare_names.
- ``synthesize`` honors the empty-requirements contract (Phase 2a.0 work)
  and the worklist-driven path (non-empty requirements).
- ``VMTemplate.dependencies`` emits ``TemplateReference`` for each
  entry in ``inherits``.
- The framework's miss policy fires on typo'd ``inherits`` references
  (e.g. ``inherits = ["defualt"]``).
- The framework's cycle detection catches inheritance loops.
- Inheriting from ``"default"`` works even when the operator omits
  ``[vm_templates.default]`` (always-materialize + framework's miss policy).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.resources import (
    ALWAYS_MATERIALIZE_SOURCE,
)
from tests.conftest import ManifestDoc, write_cfg


def _write_cfg(path: Path, *manifests: ManifestDoc) -> Path:
    """``write_cfg`` under this file's path-taking spelling."""
    return write_cfg(path.parent, *manifests, filename=path.name)


# -- Kind shape -------------------------------------------------------------


# The kind's shape, its ``synthesize`` on both paths, and the
# ``TemplateReference`` its rows emit are pinned for all four inheriting
# template kinds at once in ``test_template_kinds.py``, where vm-template
# is the first ``_KindSpec``. What is left here is what only vm-template
# has: an always-materialized default, and the cycle shapes that reach
# through it.


# -- Framework validation via load_config + build_registry -----------------


def test_non_default_self_reference_caught_by_framework(tmp_path: Path) -> None:
    """``inherits = ["a"]`` where the template itself is ``a``: a
    self-loop is a one-node cycle. A non-default self-loop slips past the
    eager resolve (which descends from "default" only) and is caught by
    the framework's cycle pass at ``build_registry`` time.

    The one-node shape is what this case is here for. The two-node one is
    pinned once, in ``test_template_kinds.py``, since the detector
    branches on nothing kind-shaped; publishing the same two-node cycle
    straight into a ``Registry`` rather than through manifests reached
    that same pass by a shorter road and proved nothing further.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        ManifestDoc("vm-template", "a", {"inherits": ["a"]}),
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="cycle detected"):
        build_registry(cfg)


def test_inherits_cycle_through_default_caught_at_build_registry(tmp_path: Path) -> None:
    """A cycle whose path goes through ``default`` loads cleanly
    (Phase 1 of the resource-manifests SDD removed load_config's eager
    default resolve) and is caught by the framework's canonical cycle
    pass at build_registry time. The resolver's internal visited-set
    guard remains as a safety net for direct resolver callers.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        ManifestDoc("vm-template", "default", {"inherits": ["a"]}),
        ManifestDoc("vm-template", "a", {"inherits": ["default"]}),
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="cycle"):
        build_registry(cfg)


def test_unreferenced_vm_template_default_lands_with_framework_source(
    tmp_path: Path,
) -> None:
    """Direct positive: a config that declares NO ``[vm_templates.*]``
    blocks and nothing referencing ``vm-template:default`` still lands
    the default row in the registry with the synthetic
    ``("framework", "always-materialize")`` source. Mirrors the
    admin-template test in ``test_always_materialize.py`` for the
    Phase 2a.1 kind.
    """
    cfg_file = _write_cfg(tmp_path / "config.toml")
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)

    default = registry.lookup("vm-template", "default")
    assert default.origin is not None
    assert default.origin.variant == "auto-declared"
    assert default.origin.source == ALWAYS_MATERIALIZE_SOURCE
