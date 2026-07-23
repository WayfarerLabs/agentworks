"""``_resolve_session_plan``: the pre-build phase of ``create_session``.

Sections S1-S8: flag-shape validation, CLI-flag canonicalization, early
VM-anchor narrowing, the workspace prompt, pure (no-SSH, no-mutation)
validation, the existing-workspace lookup, the mode prompt, and VM
resolution. Every prompt and cross-check fires here; the result is a
settled :class:`SessionPlan` the build consumes.

The body is the original ``create_session`` prologue moved verbatim: no
side effect, DB call, or prompt was reordered in the extraction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.config import validate_name
from agentworks.errors import (
    AlreadyExistsError,
    NotFoundError,
    ValidationError,
)

from ._create_types import SessionPlan

if TYPE_CHECKING:
    from agentworks.db import AgentRow, Database, VMRow, WorkspaceRow


def _resolve_session_plan(
    db: Database,
    *,
    name: str,
    workspace: str | None,
    new_workspace: bool,
    workspace_name: str | None,
    workspace_template: str | None,
    agent: str | None,
    new_agent: bool,
    agent_name: str | None,
    agent_template: str | None,
    admin: bool,
    vm_name: str | None,
) -> SessionPlan:
    """Validate flags, run the prompts, and resolve every anchor into a
    settled :class:`SessionPlan` (sections S1-S8)."""
    # ===== Flag-shape validation (mutexes + ephemeral-arg gating) ===========

    if workspace and new_workspace:
        raise ValidationError(
            "specify --workspace or --new-workspace, not both",
            entity_kind="session",
            entity_name=name,
        )
    if not new_workspace and (workspace_name or workspace_template):
        raise ValidationError(
            "--workspace-name and --workspace-template require --new-workspace",
            entity_kind="session",
            entity_name=name,
        )
    agent_modes = sum(1 for x in (bool(agent), new_agent, admin) if x)
    if agent_modes > 1:
        raise ValidationError(
            "specify at most one of --agent, --new-agent, or --admin",
            entity_kind="session",
            entity_name=name,
        )
    if not new_agent and (agent_name or agent_template):
        raise ValidationError(
            "--agent-name and --agent-template require --new-agent",
            entity_kind="session",
            entity_name=name,
        )

    # ===== Canonicalize CLI-flag shape into internal form ===================
    #
    # After this block:
    #   workspace_name : str | None   -- the workspace's name (None until
    #                                    DB lookup / default-to-session-name)
    #   new_workspace  : bool         -- True iff we're creating it
    #   workspace_template : str | None
    #   agent_name : str | None       -- the agent's name (None == admin mode)
    #   new_agent  : bool
    #   agent_template : str | None
    #
    # ``workspace`` / ``agent`` / ``admin`` are consumed here and unused below.

    if workspace:
        workspace_name = workspace
    if agent:
        agent_name = agent
    if admin:
        agent_name = None
        new_agent = False

    # ===== Early VM-anchor narrowing for the workspace prompt ===============
    #
    # If ``--vm`` and/or ``--agent`` were specified, they already pin a VM.
    # Load the agent row now (rather than in the later VM-anchor block) so
    # we can:
    #   1. Cross-check ``--vm`` against the agent's VM before any prompt
    #      fires (no point prompting for a workspace when we know the
    #      command is inconsistent).
    #   2. Filter the workspace chooser to workspaces on the known VM,
    #      so the operator doesn't have to mentally exclude irrelevant
    #      entries (and so picking one on the wrong VM isn't reachable).
    existing_agent: AgentRow | None = None
    known_vm: str | None = vm_name
    if not new_agent and agent_name is not None:
        existing_agent = db.get_agent(agent_name)
        if existing_agent is None:
            raise NotFoundError(
                f"agent '{agent_name}' not found",
                entity_kind="agent",
                entity_name=agent_name,
            )
        if known_vm is not None and known_vm != existing_agent.vm_name:
            raise ValidationError(
                f"VM mismatch: --vm={known_vm}, agent '{agent_name}'={existing_agent.vm_name}",
                entity_kind="session",
                entity_name=name,
            )
        known_vm = existing_agent.vm_name

    # ===== Workspace prompt (force explicit choice even with one option) ===
    #
    # No auto-select: workspace is part of the session's identity, and a
    # single-workspace "shortcut" today silently changes behavior the day
    # the operator adds a second one. Always prompt. Include a
    # ``[Create new]`` option so the interactive UX is fully equivalent
    # to passing ``--new-workspace`` on the CLI. Filter to ``known_vm``
    # when other anchors pin one. Non-interactive: raise.

    if not workspace_name and not new_workspace:
        chosen_existing, new_workspace = _mgr._prompt_workspace_choice(db, known_vm)
        if chosen_existing is not None:
            workspace_name = chosen_existing

    # ===== Pure validation (no SSH, no mutations) ===========================

    # Default ephemeral resource names to the session name when omitted.
    if new_workspace and workspace_name is None:
        workspace_name = name
    if new_agent and agent_name is None:
        agent_name = name
    assert workspace_name is not None  # invariant after canonicalize + prompt

    validate_name(name)
    if new_workspace:
        validate_name(workspace_name)
    if new_agent:
        assert agent_name is not None
        validate_name(agent_name)

    # DB existence checks. Session must not exist. Ephemeral workspace /
    # agent must not exist; existing workspace / agent must exist.
    if db.get_session(name) is not None:
        raise AlreadyExistsError(
            f"session '{name}' already exists",
            entity_kind="session",
            entity_name=name,
        )
    if new_workspace and db.get_workspace(workspace_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{workspace_name}' already exists",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if new_agent:
        assert agent_name is not None  # defaulted to ``name`` above
        if db.get_agent(agent_name) is not None:
            raise AlreadyExistsError(
                f"agent '{agent_name}' already exists",
                entity_kind="agent",
                entity_name=agent_name,
            )

    # ===== Existing-workspace lookup + VM-anchor accretion =================
    #
    # If the operator named an existing workspace, load it now -- both
    # to validate it exists and to contribute its VM to ``known_vm``
    # before the mode prompt fires. This lets the mode prompt filter
    # agents by the workspace's VM, and lets a downstream VM mismatch
    # surface before the mode prompt rather than after.
    existing_ws: WorkspaceRow | None = None
    if not new_workspace:
        existing_ws = db.get_workspace(workspace_name)
        if existing_ws is None:
            raise NotFoundError(
                f"workspace '{workspace_name}' not found",
                entity_kind="workspace",
                entity_name=workspace_name,
            )
        if known_vm is not None and known_vm != existing_ws.vm_name:
            anchor_label = "--vm" if vm_name is not None else f"agent '{agent_name}'"
            raise ValidationError(
                f"VM mismatch: {anchor_label}={known_vm}, workspace '{workspace_name}'={existing_ws.vm_name}",
                entity_kind="session",
                entity_name=name,
            )
        known_vm = existing_ws.vm_name

    # ===== Mode prompt (force explicit choice; no silent default) ==========
    #
    # Fires before VM resolution so the operator's pick of an existing
    # agent can pin the VM (one less prompt for the common case). When
    # ``known_vm`` is set, the chooser filters to that VM's agents; when
    # not, it shows agents across all VMs (each labeled with its VM) and
    # picking one sets the VM. ``admin`` and ``[Create new agent]`` don't
    # pin a VM -- those paths fall through to the VM-prompt at the end.

    if agent_name is None and not new_agent and not admin:
        vm_for_mode_prompt: VMRow | None = None
        if known_vm is not None:
            vm_for_mode_prompt = db.get_vm(known_vm)
            assert vm_for_mode_prompt is not None  # known_vm was sourced from a real row

        chosen_agent, new_agent, admin = _mgr._prompt_mode_choice(db, vm_for_mode_prompt)
        if chosen_agent is not None:
            # Existing-agent pick: the prompt already filtered by
            # ``known_vm`` (if set) OR the picked agent's VM becomes
            # the new known_vm. No vm-anchor cross-check needed -- the
            # filter / pick path enforces agreement by construction.
            agent_name = chosen_agent
            existing_agent = db.get_agent(agent_name)
            assert existing_agent is not None  # came from list_agents
            known_vm = existing_agent.vm_name

        # Re-run the agent-specific default / validation / existence
        # checks that the upfront block did for the flag path. The
        # workspace equivalents ran already because the workspace
        # prompt sits BEFORE that block; the mode prompt sits AFTER
        # because it may need to pin the VM via an existing-agent
        # pick. Without this, a ``[Create new agent]`` pick lands at
        # the eager-resolve SecretTarget with ``is_admin_mode=True``
        # (wrong scope) and asserts ``agent_name is not None``
        # inside the ephemeral-create block.
        if new_agent:
            if agent_name is None:
                agent_name = name
            validate_name(agent_name)
            if db.get_agent(agent_name) is not None:
                raise AlreadyExistsError(
                    f"agent '{agent_name}' already exists",
                    entity_kind="agent",
                    entity_name=agent_name,
                )

    # ===== VM resolution (final step; prompts only if nothing pinned it) ===
    #
    # By this point every anchor (vm_name, existing workspace, existing
    # agent -- whether passed as a flag or picked from a prompt) has
    # contributed to ``known_vm`` and the cross-checks fired as each
    # anchor was loaded. If ``known_vm`` is still ``None`` we genuinely
    # have no anchor (e.g. ``--new-workspace --admin`` with no ``--vm``),
    # so prompt for VM. The cross-check below is defense-in-depth: a
    # future refactor that adds a new anchor without piping it through
    # ``known_vm`` would trip it.
    if known_vm is None:
        vm = _mgr._prompt_vm(db)
    else:
        loaded_vm = db.get_vm(known_vm)
        if loaded_vm is None:
            raise NotFoundError(
                f"VM '{known_vm}' not found",
                entity_kind="vm",
                entity_name=known_vm,
            )
        vm = loaded_vm
    target_vm_name = vm.name

    vm_anchors: list[tuple[str, str]] = []
    if vm_name is not None:
        vm_anchors.append(("--vm", vm_name))
    if existing_ws is not None:
        vm_anchors.append((f"workspace '{workspace_name}'", existing_ws.vm_name))
    if existing_agent is not None:
        vm_anchors.append((f"agent '{agent_name}'", existing_agent.vm_name))
    if any(candidate != target_vm_name for _, candidate in vm_anchors):
        detail = ", ".join(f"{src}={v}" for src, v in vm_anchors)
        raise ValidationError(
            f"VM mismatch: {detail}",
            entity_kind="session",
            entity_name=name,
        )

    return SessionPlan(
        name=name,
        workspace_name=workspace_name,
        new_workspace=new_workspace,
        workspace_template=workspace_template,
        agent_name=agent_name,
        new_agent=new_agent,
        agent_template=agent_template,
        existing_ws=existing_ws,
        existing_agent=existing_agent,
        vm=vm,
        target_vm_name=target_vm_name,
    )
