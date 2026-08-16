"""An inheriting template's edges come from its EFFECTIVE declaration.

FR17's producing half: a resource's runtime dependencies derive from the
merged blob, so the child of a template both INHERITS the parent's needs
as edges of its own and drops the ones it overrode. The consuming half
(that no runtime-need traversal crosses the inheritance edge) is pinned
by ``tests/resources/test_inheritance_traversal.py``.

Both halves have to be right together, which is why the override cases
below assert an absence as well as a presence: an edge set that only
grows would pass a presence-only test while still double-counting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.env.entry import EnvEntry
from agentworks.errors import ConfigError, InheritanceCycleError
from agentworks.resources.graph import FinalizeContext
from agentworks.schema import CapabilityBlock
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import effective_template
from agentworks.workspaces.template import WorkspaceTemplate
from tests.conftest import ManifestDoc
from tests.resources.test_graph import _write_cfg

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.declared_resource import DeclaredResource


def _context(kind: str, *rows: DeclaredResource) -> FinalizeContext:
    """A build context holding ``rows`` under ``kind``, as finalize's own
    context does."""
    return FinalizeContext(rows={kind: {row.name: row for row in rows}})


def _targets(row: DeclaredResource, context: FinalizeContext) -> set[tuple[str, str]]:
    return {(ref.kind, ref.name) for ref in row.dependencies(context)}


def test_vm_template_child_inherits_parent_needs_and_drops_what_it_overrides() -> None:
    parent = VMTemplate(
        name="base",
        env={"BASE": EnvEntry({"secret": "base-secret"})},
        apt_packages=["build-tools"],
    )
    child = VMTemplate(name="kid", inherits=["base"], tailscale_auth_key="kid-ts-key")
    context = _context("vm-template", parent, child)

    assert _targets(child, context) == {
        ("secret", "base-secret"),  # inherited, and now an edge of the child's own
        ("apt-package", "build-tools"),  # likewise
        ("vm-template", "base"),  # the inheritance edge itself
        ("secret", "kid-ts-key"),  # the override
    }
    # The parent keeps its own default-secret edge; the child does not take it on.
    assert ("secret", "tailscale-auth-key") in _targets(parent, context)


def test_a_later_silent_parent_does_not_move_the_childs_auth_key_edge() -> None:
    """The key travelling DOWN the chain, in the form that was wrong: the
    second parent declares no key, so it used to arrive carrying the
    built-in default and overwrite the first parent's real one. Nothing
    about the child changes, and the secret it depends on does.

    A SILENT wrong answer rather than a loud one, which is what earns it a
    case of its own: a child that loses its parent's key still resolves
    and still publishes a secret edge, just to the built-in default name,
    so the graph gates one secret while the VM provisions with another.
    The single-parent form is the prefix of this one and needs no separate
    case.

    ``tests/resources/test_inheritance_merge.py`` asserts the invariant
    behind this over every field of every inheriting kind; this pins what
    losing it COSTS, at the seam that reaches production infrastructure.
    """
    declares = VMTemplate(name="declares", tailscale_auth_key="prod-ts-key")
    silent = VMTemplate(name="silent")
    child = VMTemplate(name="kid", inherits=["declares", "silent"])
    targets = _targets(child, _context("vm-template", declares, silent, child))

    assert ("secret", "prod-ts-key") in targets
    assert ("secret", "tailscale-auth-key") not in targets


def test_workspace_template_child_inherits_parent_env_secrets() -> None:
    parent = WorkspaceTemplate(name="base", env={"K": EnvEntry({"secret": "base-secret"})})
    child = WorkspaceTemplate(name="kid", inherits=["base"])
    assert _targets(child, _context("workspace-template", parent, child)) == {
        ("secret", "base-secret"),
        ("workspace-template", "base"),
    }


def test_agent_template_child_inherits_credentials_and_install_commands() -> None:
    parent = AgentTemplate(name="base", git_credentials=["github"], user_install_commands=["mise"])
    child = AgentTemplate(name="kid", inherits=["base"])
    assert _targets(child, _context("agent-template", parent, child)) == {
        ("git-credential", "github"),
        ("user-install-command", "mise"),
        ("agent-template", "base"),
    }


def test_session_template_child_inherits_the_harness_selector_it_never_declared() -> None:
    """The pair comes off the lineage, so a silent child still points at
    the integration its parent selected. Collapsing the undeclared case to
    ``shell`` earlier would instead have every session template in the
    registry pointing at the shell row."""
    parent = SessionTemplate(name="base", harness_integration=CapabilityBlock.of("shell", **{"command": "top"}))
    child = SessionTemplate(name="kid", inherits=["base"])
    silent = SessionTemplate(name="lonely")
    context = _context("session-template", parent, child, silent)

    assert ("harness-integration", "shell") in _targets(child, context)
    assert _targets(silent, context) == set()


def test_a_kind_nobody_registered_is_refused_rather_than_answered_empty() -> None:
    """The one degradation the context will NOT perform silently: an
    emitter that misspells its own kind would otherwise stop merging its
    chain and go on publishing plausible edges off its declaration."""
    from agentworks.errors import StateError

    with pytest.raises(StateError, match="no resource kind 'vm-tempalte' is registered"):
        FinalizeContext().rows_of("vm-tempalte")


def test_a_capability_kind_nobody_registered_is_refused_rather_than_answered_empty() -> None:
    """The projection accessor takes the same posture as ``rows_of`` on the
    same mistake, and for the same reason: a real kind with nothing
    projected is a legitimate ``None``, so a misspelled kind answered the
    same way would read as "no such implementation" rather than as the
    framework bug it is.
    """
    from agentworks.errors import StateError

    context = FinalizeContext()
    assert context.capability_class("secret-backend", "onepassword") is None
    with pytest.raises(StateError):
        context.capability_class("not-a-capability-kind", "onepassword")


def test_a_bare_context_degrades_to_the_declaration_itself_not_to_nothing() -> None:
    """``FinalizeContext()`` carries no rows, so there are no ancestors to
    merge; the row's OWN declaration still has to come through, or a
    context-less caller would silently see an empty edge set."""
    template = VMTemplate(name="kid", inherits=["base"], env={"K": EnvEntry({"secret": "own-secret"})})
    assert ("secret", "own-secret") in _targets(template, FinalizeContext())


def test_the_effective_resolve_is_total_over_a_cyclic_chain() -> None:
    """``dependencies`` is total by contract, and a cyclic chain has no
    effective declaration; the resolve-time entry point still raises, so
    the totality is scoped to the finalize view rather than lost."""
    rows = {
        "a": VMTemplate(name="a", inherits=["b"]),
        "b": VMTemplate(name="b", inherits=["a"]),
    }
    assert effective_template(rows, "a").name == "a"
    from agentworks.vms.templates import _resolve_from_dict

    with pytest.raises(InheritanceCycleError, match="vm-template inheritance cycle detected: a -> b -> a"):
        _resolve_from_dict(rows, "a")


def test_a_cyclic_chain_still_reports_the_cycle_rather_than_a_degraded_graph(tmp_path: Path) -> None:
    """What makes the degradation above safe: the rows whose edges came
    out degraded are never read, because the cycle pass raises first.

    What degradation costs is the merged view: every inherited env secret,
    and any override of a defaulted field (the auth-key edge survives, at
    the kind's default name rather than at the declared one). Wrong
    answers, all of them, which is why the claim they are unobservable is
    worth a test rather than a comment."""
    cfg = _write_cfg(
        tmp_path,
        "",
        ManifestDoc("vm-template", "a", {"inherits": ["b"]}),
        ManifestDoc("vm-template", "b", {"inherits": ["a"]}),
    )
    with pytest.raises(ConfigError, match="resource reference cycle detected"):
        build_registry(load_config(cfg, warn_issues=False))
