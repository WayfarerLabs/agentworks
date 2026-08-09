"""Workspace template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in empty template fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, NotFoundError, unknown_template_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.env import EnvEntry
    from agentworks.resources.registry import Registry
    from agentworks.workspaces.template import WorkspaceTemplate


@dataclass
class ResolvedTemplate:
    """A fully resolved workspace template with all inheritance applied."""

    name: str
    repo: str | None = None
    tmuxinator: bool = True
    git_user_name: str | None = None
    git_user_email: str | None = None
    env: dict[str, EnvEntry] = field(default_factory=dict)


def resolve_from_dict(
    templates: dict[str, WorkspaceTemplate],
    template_name: str | None = None,
) -> ResolvedTemplate:
    """Resolve a workspace template from a templates dict.

    Selection order:
    1. Explicit template_name
    2. "default" template if it exists
    3. Built-in empty template (tmuxinator=True, no repo)
    """
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            raise unknown_template_error(
                kind="workspace-template",
                label="workspace template",
                name=template_name,
                available=templates,
            )
        return _resolve(templates, template_name)

    if "default" in templates:
        return _resolve(templates, "default")

    return ResolvedTemplate(name="default")


def resolve_template(registry: Registry, template_name: str | None = None) -> ResolvedTemplate:
    """Resolve a workspace template by name from the Registry."""
    from agentworks.resources.access import kind_dict

    return resolve_from_dict(kind_dict(registry, "workspace-template"), template_name)


def resolve_ws_template_env_or_empty(
    registry: Registry,
    template_name: str | None,
) -> dict[str, EnvEntry]:
    """The workspace template's env, or an empty dict when the template
    cannot be resolved.

    A copied workspace records the synthetic ``template="copied"`` marker,
    which is not a real template; a workspace's template may also have been
    removed from config after the fact. Either way there is no template to
    draw env from, so the workspace contributes an empty env scope rather
    than raising ``unknown_template_error``. Shared by the vm-level
    exec/shell env-scope resolver and ``env show`` so those two
    env-resolution sites cannot drift. Note ``repair`` deliberately does NOT
    use this: it resolves the same template for git identity, not ``.env``,
    and keeps its own local handling (the same exception set, quietly
    skipping git-identity convergence when the template is gone — see
    ``_converge_git_identity``).
    """
    try:
        return resolve_template(registry, template_name).env
    except (ValueError, ConfigError, NotFoundError):
        return {}


def effective_template(templates: Mapping[str, WorkspaceTemplate], name: str) -> ResolvedTemplate:
    """The effective (merged) declaration of ``name``, for the finalize
    passes. TOTAL; see ``vms/templates.effective_template`` for why the
    degradation on a cyclic chain is never observed."""
    from agentworks.errors import InheritanceCycleError

    try:
        return _resolve(templates, name)
    except InheritanceCycleError:
        return ResolvedTemplate(name=name)


def _resolve(
    templates: Mapping[str, WorkspaceTemplate],
    name: str,
) -> ResolvedTemplate:
    """Resolve ``name``'s chain, defaults applied: one accumulator folded
    over the chain's declarations, last one wins. See
    ``vms.templates._resolve_from_dict`` for why the fold reads the
    DECLARATIONS rather than each parent's resolved template.
    """
    # Imported here, not at module level: ``agentworks.resources``'s package
    # init loads every kind module, and every kind module reaches this one.
    from agentworks.resources.inheritance import resolution_layers

    result = ResolvedTemplate(name=name)
    for layer in resolution_layers(templates, name, "workspace-template"):
        _merge_template(result, layer)
    return result


def _merge_template(target: ResolvedTemplate, tmpl: WorkspaceTemplate) -> None:
    """Fold one declared WorkspaceTemplate into the accumulator. None =
    not set, skip. The only writer of a ``ResolvedTemplate``'s fields."""
    if tmpl.repo is not None:
        target.repo = tmpl.repo
    if tmpl.tmuxinator is not None:
        target.tmuxinator = tmpl.tmuxinator
    if tmpl.git_user_name is not None:
        target.git_user_name = tmpl.git_user_name
    if tmpl.git_user_email is not None:
        target.git_user_email = tmpl.git_user_email
    if tmpl.env:
        target.env = {**target.env, **tmpl.env}
