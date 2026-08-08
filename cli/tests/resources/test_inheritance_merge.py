"""A layer that declares nothing changes nothing, for every inheriting kind.

The four resolvers merge different fields under different rules, but they
all rest on one invariant: an ``inherits`` entry contributes exactly what
its chain DECLARED, so naming an extra parent that declares nothing (or
naming one that does not exist) cannot move a value some earlier parent
really did declare.

Getting that wrong is silent and it reaches production infrastructure: a
``vm-template`` whose ``tailscale_auth_key`` was reset to the built-in
default still resolves, still builds a graph, and gates a DIFFERENT secret
than the one the VM provisions with. So this asserts the invariant over a
parent declaring every field the resolver merges, rather than checking a
hand-picked answer per kind, and :func:`test_the_declaring_parent_moves_every_
resolved_field` keeps the fixture honest: a field added to a resolver that
the fixture does not exercise fails here rather than quietly losing cover.

The shape that made this wrong was a resolver that merged each parent's
RESOLVED template into the accumulator. A resolved template has its
defaults already applied and cannot say which of its values it was given,
so a silent parent arrived indistinguishable from one that had genuinely
declared every default. The resolvers fold the DECLARATIONS instead, where
``None`` still means "not declared here".
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import resolve_from_dict as resolve_agent
from agentworks.env.entry import EnvEntry
from agentworks.schema import CapabilityBlock
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict as resolve_session
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import resolve_from_dict as resolve_vm
from agentworks.workspaces.template import WorkspaceTemplate
from agentworks.workspaces.templates import resolve_from_dict as resolve_workspace

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class _Kind:
    """One inheriting kind, as this module needs to drive it."""

    row: type[Any]
    """The declared-row model, constructed as ``row(name=..., inherits=[...])``."""

    resolve: Callable[[Any, str | None], Any]
    """The kind's ``resolve_from_dict``: rows plus a name, to a resolved template."""

    declared: dict[str, Any]
    """A value for every field the kind's resolver merges, each DIFFERENT from
    the resolved default so that losing it is visible."""

    undeclarable: frozenset[str] = frozenset()
    """Resolved fields :attr:`declared` cannot move off their default, with the
    reason. Stated rather than tolerated: an entry here that turns out to be
    movable fails just as loudly as a field nobody covered."""


_ENV = {"API_KEY": EnvEntry(secret="api-secret")}

KINDS = {
    "vm-template": _Kind(
        row=VMTemplate,
        resolve=resolve_vm,
        declared={
            "cpus": 32,
            "memory": 64,
            "disk": 500,
            "swap": 16,
            "apt": ["ripgrep"],
            "apt_packages": ["build-tools"],
            "snap": ["go"],
            "system_install_commands": ["docker"],
            "env": _ENV,
            "tailscale_auth_key": "prod-ts-key",
        },
    ),
    "agent-template": _Kind(
        row=AgentTemplate,
        resolve=resolve_agent,
        declared={
            "shell": "zsh",
            "git_credentials": ["github"],
            "user_install_commands": ["mise"],
            "dotfiles_source": "git@github.com:octocat/dotfiles.git",
            "dotfiles_destination": "~/config/dotfiles",
            "dotfiles_install_cmd": "./bootstrap.sh",
            "mise_activate": False,
            "mise_packages": ["node@22"],
            "mise_lockfile": "mise.lock",
            "mise_allow_unlocked": True,
            "mise_install_before": "30d",
            "mise_prune_on_reinit": False,
            "claude_marketplaces": ["octocat/marketplace"],
            "claude_plugins": ["reviewer"],
            "env": _ENV,
        },
    ),
    "workspace-template": _Kind(
        row=WorkspaceTemplate,
        resolve=resolve_workspace,
        declared={
            "repo": "git@github.com:octocat/repo.git",
            "tmuxinator": False,
            "git_user_name": "Ada Lovelace",
            "git_user_email": "ada@example.com",
            "env": _ENV,
        },
    ),
    "session-template": _Kind(
        row=SessionTemplate,
        resolve=resolve_session,
        declared={
            "description": "Prod debugging",
            "env": _ENV,
            "harness_integration": CapabilityBlock.of("shell", **{"command": "htop"}),
        },
        # ``shell`` is the only registered harness integration, so no fixture
        # can name a different one; the pair's coverage rides on
        # ``harness_integration_config``, which the same block moves.
        undeclarable=frozenset({"harness_integration"}),
    ),
}

_KIND = pytest.mark.parametrize("kind", KINDS.values(), ids=list(KINDS))


def _merged(kind: _Kind, rows: dict[str, Any], name: str | None) -> dict[str, Any]:
    """``name``'s resolved template as a field map, without ``name`` itself
    (which the resolver echoes back rather than merges)."""
    resolved = kind.resolve(rows, name)
    return {field.name: getattr(resolved, field.name) for field in fields(resolved) if field.name != "name"}


def _inheriting(kind: _Kind, *parents: str) -> dict[str, Any]:
    """The resolved template of a child that declares nothing of its own and
    inherits ``parents``, over rows holding a declaring parent and a silent one."""
    rows = {
        "declares": kind.row(name="declares", **kind.declared),
        "silent": kind.row(name="silent"),
        "kid": kind.row(name="kid", inherits=list(parents)),
    }
    return _merged(kind, rows, "kid")


@_KIND
def test_the_declaring_parent_moves_every_resolved_field(kind: _Kind) -> None:
    """The fixture's own guard, and the reason the tests below mean
    anything: every field the resolver produces has to actually differ
    from its built-in default, or the equality those tests assert would
    hold for a field nobody exercised. A field added to a resolver
    without a value here lands in ``unmoved`` and fails.
    """
    defaults = _merged(kind, {}, None)
    unmoved = {field for field, value in _inheriting(kind, "declares").items() if value == defaults[field]}
    assert unmoved == kind.undeclarable, (
        f"{sorted(unmoved - kind.undeclarable)} keep their default value even though the parent "
        f"declares one, so nothing below covers them; give each a distinct value in `declared`. "
        f"{sorted(kind.undeclarable - unmoved)} are excused in `undeclarable` but did move, so the "
        f"excuse is stale."
    )


@_KIND
def test_a_parent_that_declares_nothing_changes_nothing(kind: _Kind) -> None:
    """B2: ``inherits: [declares, silent]`` used to resolve to the built-in
    defaults, because the silent parent arrived fully defaulted and every
    default overwrote the value the first parent really did declare. Order
    is asserted both ways: the silent parent is inert wherever it sits.
    """
    alone = _inheriting(kind, "declares")
    assert _inheriting(kind, "declares", "silent") == alone
    assert _inheriting(kind, "silent", "declares") == alone


@_KIND
def test_a_parent_that_does_not_exist_changes_nothing(kind: _Kind) -> None:
    """The same defaults-in-disguise bug reached through a typo'd parent
    name, which is how an operator would actually meet it. Standing in a
    built-in default template for the missing row would silently answer a
    question the miss policy exists to raise; an absent parent contributes
    no layer at all, matching ``resources.inheritance.merge_layers``.
    """
    assert _inheriting(kind, "declares", "no-such-template") == _inheriting(kind, "declares")
