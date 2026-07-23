"""``_resolve_session_plan``: the pre-build phase of ``create_session``.

Sections S1-S8: flag-shape validation, CLI-flag canonicalization, early
VM-anchor narrowing, the workspace prompt, pure (no-SSH, no-mutation)
validation, the existing-workspace lookup, the mode prompt, and VM
resolution. Every prompt and cross-check fires here; the result is a
settled :class:`SessionPlan` the build consumes.

The eight sections accrete a shared working state (the canonicalized flag
shape plus the VM anchor and its loaded rows), so they are threaded
through one mutable :class:`_PlanDraft` carrier rather than a long
parameter list. Splitting the sections into named helpers keeps each
individually testable and under the complexity ceiling; the draft is
frozen into the immutable :class:`SessionPlan` at the end. No side
effect, DB call, or prompt was reordered relative to the original
``create_session`` prologue.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class _PlanDraft:
    """The mutable working state accreted across sections S1-S8.

    Holds the canonicalized flag shape (mutated by the canonicalize and
    mode-prompt sections) and the accreting VM anchor: ``known_vm`` plus
    the loaded ``existing_agent`` / ``existing_ws`` rows that pinned it,
    and finally the resolved ``vm`` / ``target_vm_name``. The raw
    ``workspace`` / ``agent`` flags are consumed by canonicalization and
    unused afterward.
    """

    workspace: str | None
    agent: str | None
    workspace_name: str | None
    new_workspace: bool
    workspace_template: str | None
    agent_name: str | None
    new_agent: bool
    agent_template: str | None
    admin: bool
    known_vm: str | None
    existing_agent: AgentRow | None = None
    existing_ws: WorkspaceRow | None = None
    vm: VMRow | None = None
    target_vm_name: str | None = None


def _validate_and_canonicalize_flags(draft: _PlanDraft, name: str) -> None:
    """S1-S2: reject invalid flag combinations, then canonicalize the
    CLI-flag shape into internal form.

    After this: ``workspace_name`` / ``agent_name`` carry the chosen
    names (``agent_name is None`` means admin mode), ``new_workspace`` /
    ``new_agent`` say whether to create them, and the raw ``workspace`` /
    ``agent`` / ``admin`` flags are consumed.
    """
    # ===== Flag-shape validation (mutexes + ephemeral-arg gating) ===========

    if draft.workspace and draft.new_workspace:
        raise ValidationError(
            "specify --workspace or --new-workspace, not both",
            entity_kind="session",
            entity_name=name,
        )
    if not draft.new_workspace and (draft.workspace_name or draft.workspace_template):
        raise ValidationError(
            "--workspace-name and --workspace-template require --new-workspace",
            entity_kind="session",
            entity_name=name,
        )
    agent_modes = sum(1 for x in (bool(draft.agent), draft.new_agent, draft.admin) if x)
    if agent_modes > 1:
        raise ValidationError(
            "specify at most one of --agent, --new-agent, or --admin",
            entity_kind="session",
            entity_name=name,
        )
    if not draft.new_agent and (draft.agent_name or draft.agent_template):
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

    if draft.workspace:
        draft.workspace_name = draft.workspace
    if draft.agent:
        draft.agent_name = draft.agent
    if draft.admin:
        draft.agent_name = None
        draft.new_agent = False


def _narrow_and_prompt_workspace(db: Database, draft: _PlanDraft, name: str) -> None:
    """S3-S4: narrow the VM anchor by an existing ``--agent``, then run
    the workspace prompt filtered to that anchor.

    If ``--vm`` and/or ``--agent`` were specified they already pin a VM.
    Load the agent row now (rather than in the later VM-anchor block) so
    we can:
      1. Cross-check ``--vm`` against the agent's VM before any prompt
         fires (no point prompting for a workspace when we know the
         command is inconsistent).
      2. Filter the workspace chooser to workspaces on the known VM, so
         the operator doesn't have to mentally exclude irrelevant entries
         (and so picking one on the wrong VM isn't reachable).
    """
    if not draft.new_agent and draft.agent_name is not None:
        draft.existing_agent = db.get_agent(draft.agent_name)
        if draft.existing_agent is None:
            raise NotFoundError(
                f"agent '{draft.agent_name}' not found",
                entity_kind="agent",
                entity_name=draft.agent_name,
            )
        if draft.known_vm is not None and draft.known_vm != draft.existing_agent.vm_name:
            raise ValidationError(
                f"VM mismatch: --vm={draft.known_vm}, agent '{draft.agent_name}'={draft.existing_agent.vm_name}",
                entity_kind="session",
                entity_name=name,
            )
        draft.known_vm = draft.existing_agent.vm_name

    # ===== Workspace prompt (force explicit choice even with one option) ===
    #
    # No auto-select: workspace is part of the session's identity, and a
    # single-workspace "shortcut" today silently changes behavior the day
    # the operator adds a second one. Always prompt. Include a
    # ``[Create new]`` option so the interactive UX is fully equivalent
    # to passing ``--new-workspace`` on the CLI. Filter to ``known_vm``
    # when other anchors pin one. Non-interactive: raise.

    if not draft.workspace_name and not draft.new_workspace:
        chosen_existing, draft.new_workspace = _mgr._prompt_workspace_choice(db, draft.known_vm)
        if chosen_existing is not None:
            draft.workspace_name = chosen_existing


def _validate_names_and_existence(db: Database, draft: _PlanDraft, name: str) -> None:
    """S5: default ephemeral names, validate every name, and run the pure
    DB existence checks (no SSH, no mutations)."""
    # Default ephemeral resource names to the session name when omitted.
    if draft.new_workspace and draft.workspace_name is None:
        draft.workspace_name = name
    if draft.new_agent and draft.agent_name is None:
        draft.agent_name = name
    assert draft.workspace_name is not None  # invariant after canonicalize + prompt

    validate_name(name)
    if draft.new_workspace:
        validate_name(draft.workspace_name)
    if draft.new_agent:
        assert draft.agent_name is not None
        validate_name(draft.agent_name)

    # DB existence checks. Session must not exist. Ephemeral workspace /
    # agent must not exist; existing workspace / agent must exist.
    if db.get_session(name) is not None:
        raise AlreadyExistsError(
            f"session '{name}' already exists",
            entity_kind="session",
            entity_name=name,
        )
    if draft.new_workspace and db.get_workspace(draft.workspace_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{draft.workspace_name}' already exists",
            entity_kind="workspace",
            entity_name=draft.workspace_name,
        )
    if draft.new_agent:
        assert draft.agent_name is not None  # defaulted to ``name`` above
        if db.get_agent(draft.agent_name) is not None:
            raise AlreadyExistsError(
                f"agent '{draft.agent_name}' already exists",
                entity_kind="agent",
                entity_name=draft.agent_name,
            )


def _lookup_workspace_and_prompt_mode(db: Database, draft: _PlanDraft, name: str, vm_name: str | None) -> None:
    """S6-S7: load an existing workspace (accreting its VM), then run the
    mode prompt filtered to the accreted VM.

    The existing-workspace load contributes its VM to ``known_vm`` before
    the mode prompt fires, so the mode prompt can filter agents by the
    workspace's VM and a downstream VM mismatch surfaces before the mode
    prompt rather than after. The mode prompt then fires before VM
    resolution so an operator's pick of an existing agent can pin the VM.
    """
    assert draft.workspace_name is not None  # defaulted in the validation section

    # ===== Existing-workspace lookup + VM-anchor accretion =================
    if not draft.new_workspace:
        draft.existing_ws = db.get_workspace(draft.workspace_name)
        if draft.existing_ws is None:
            raise NotFoundError(
                f"workspace '{draft.workspace_name}' not found",
                entity_kind="workspace",
                entity_name=draft.workspace_name,
            )
        if draft.known_vm is not None and draft.known_vm != draft.existing_ws.vm_name:
            anchor_label = "--vm" if vm_name is not None else f"agent '{draft.agent_name}'"
            raise ValidationError(
                f"VM mismatch: {anchor_label}={draft.known_vm}, "
                f"workspace '{draft.workspace_name}'={draft.existing_ws.vm_name}",
                entity_kind="session",
                entity_name=name,
            )
        draft.known_vm = draft.existing_ws.vm_name

    # ===== Mode prompt (force explicit choice; no silent default) ==========
    #
    # Fires before VM resolution so the operator's pick of an existing
    # agent can pin the VM (one less prompt for the common case). When
    # ``known_vm`` is set, the chooser filters to that VM's agents; when
    # not, it shows agents across all VMs (each labeled with its VM) and
    # picking one sets the VM. ``admin`` and ``[Create new agent]`` don't
    # pin a VM -- those paths fall through to the VM-prompt at the end.

    if draft.agent_name is None and not draft.new_agent and not draft.admin:
        vm_for_mode_prompt: VMRow | None = None
        if draft.known_vm is not None:
            vm_for_mode_prompt = db.get_vm(draft.known_vm)
            assert vm_for_mode_prompt is not None  # known_vm was sourced from a real row

        chosen_agent, draft.new_agent, draft.admin = _mgr._prompt_mode_choice(db, vm_for_mode_prompt)
        if chosen_agent is not None:
            # Existing-agent pick: the prompt already filtered by
            # ``known_vm`` (if set) OR the picked agent's VM becomes
            # the new known_vm. No vm-anchor cross-check needed -- the
            # filter / pick path enforces agreement by construction.
            draft.agent_name = chosen_agent
            draft.existing_agent = db.get_agent(draft.agent_name)
            assert draft.existing_agent is not None  # came from list_agents
            draft.known_vm = draft.existing_agent.vm_name

        # Re-run the agent-specific default / validation / existence
        # checks that the upfront block did for the flag path. The
        # workspace equivalents ran already because the workspace
        # prompt sits BEFORE that block; the mode prompt sits AFTER
        # because it may need to pin the VM via an existing-agent
        # pick. Without this, a ``[Create new agent]`` pick lands at
        # the eager-resolve SecretTarget with ``is_admin_mode=True``
        # (wrong scope) and asserts ``agent_name is not None``
        # inside the ephemeral-create block.
        if draft.new_agent:
            if draft.agent_name is None:
                draft.agent_name = name
            validate_name(draft.agent_name)
            if db.get_agent(draft.agent_name) is not None:
                raise AlreadyExistsError(
                    f"agent '{draft.agent_name}' already exists",
                    entity_kind="agent",
                    entity_name=draft.agent_name,
                )


def _resolve_target_vm(db: Database, draft: _PlanDraft, name: str, vm_name: str | None) -> None:
    """S8: resolve the target VM (prompting only if nothing pinned it) and
    cross-check every anchor against it.

    By this point every anchor (``vm_name``, existing workspace, existing
    agent, whether passed as a flag or picked from a prompt) has
    contributed to ``known_vm`` and the cross-checks fired as each anchor
    loaded. A still-``None`` ``known_vm`` means no anchor at all (e.g.
    ``--new-workspace --admin`` with no ``--vm``), so prompt. The final
    cross-check is defense-in-depth: a future refactor that adds a new
    anchor without piping it through ``known_vm`` would trip it.
    """
    if draft.known_vm is None:
        draft.vm = _mgr._prompt_vm(db)
    else:
        loaded_vm = db.get_vm(draft.known_vm)
        if loaded_vm is None:
            raise NotFoundError(
                f"VM '{draft.known_vm}' not found",
                entity_kind="vm",
                entity_name=draft.known_vm,
            )
        draft.vm = loaded_vm
    draft.target_vm_name = draft.vm.name

    vm_anchors: list[tuple[str, str]] = []
    if vm_name is not None:
        vm_anchors.append(("--vm", vm_name))
    if draft.existing_ws is not None:
        vm_anchors.append((f"workspace '{draft.workspace_name}'", draft.existing_ws.vm_name))
    if draft.existing_agent is not None:
        vm_anchors.append((f"agent '{draft.agent_name}'", draft.existing_agent.vm_name))
    if any(candidate != draft.target_vm_name for _, candidate in vm_anchors):
        detail = ", ".join(f"{src}={v}" for src, v in vm_anchors)
        raise ValidationError(
            f"VM mismatch: {detail}",
            entity_kind="session",
            entity_name=name,
        )


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
    draft = _PlanDraft(
        workspace=workspace,
        agent=agent,
        workspace_name=workspace_name,
        new_workspace=new_workspace,
        workspace_template=workspace_template,
        agent_name=agent_name,
        new_agent=new_agent,
        agent_template=agent_template,
        admin=admin,
        known_vm=vm_name,
    )

    _validate_and_canonicalize_flags(draft, name)
    _narrow_and_prompt_workspace(db, draft, name)
    _validate_names_and_existence(db, draft, name)
    _lookup_workspace_and_prompt_mode(db, draft, name, vm_name)
    _resolve_target_vm(db, draft, name, vm_name)

    assert draft.workspace_name is not None  # defaulted in the validation section
    assert draft.vm is not None and draft.target_vm_name is not None  # set by _resolve_target_vm
    return SessionPlan(
        name=name,
        workspace_name=draft.workspace_name,
        new_workspace=draft.new_workspace,
        workspace_template=draft.workspace_template,
        agent_name=draft.agent_name,
        new_agent=draft.new_agent,
        agent_template=draft.agent_template,
        existing_ws=draft.existing_ws,
        existing_agent=draft.existing_agent,
        vm=draft.vm,
        target_vm_name=draft.target_vm_name,
    )
