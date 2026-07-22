"""Workspace creation, description, and listing."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name
from agentworks.errors import AlreadyExistsError, NotFoundError
from agentworks.workspaces.manager._common import _guard_vm_status, _resolve_vm, _workspace_scope

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database


def create_workspace(
    db: Database,
    config: Config,
    *,
    name: str,
    vm_name: str | None = None,
    template_name: str | None = None,
    open_vscode: bool = False,
) -> None:
    """Create a workspace on a VM.

    Orchestrated: the graph derives from the VM's row (its site field
    is the edge to the vm-site node) and the pending workspace node's
    VM edge; the activation gate replaces this command's
    ``keep_active``, opening BEFORE the preflight sweep with its
    just-in-time values seeding the boundary resolver; the mutation is
    the phase-free realization body
    (:func:`agentworks.workspaces.realize.realize_workspace`), the
    single copy shared with the orchestrated session create. The
    completed workspace is never rollback-tracked (the body cleans its
    own partial files, and a failure after the row exists keeps the
    workspace, exactly the imperative shape), so no realization log
    exists here.
    """
    from agentworks.bootstrap import build_registry

    # build_registry runs first so framework miss-policies fire before
    # any template / DB / VM business logic.
    registry = build_registry(config)

    ws_name = name
    validate_name(ws_name)

    if db.get_workspace(ws_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{ws_name}' already exists",
            entity_kind="workspace",
            entity_name=ws_name,
        )

    # Cheap validation FIRST, before the gate and before any secret is
    # touched: template resolution, the repo advisories (config-only,
    # no tokens), and the VM init-status guard all fail with zero
    # prompts and zero VM starts, the same bail-early precedence every
    # migrated sibling keeps.
    from agentworks.workspaces.templates import resolve_template

    template = resolve_template(registry, template_name)

    # Advise if the resolved template's repo remote will not resolve
    # cleanly against the declared git credentials (config-only, no
    # tokens). Each credential judges the URL by its own host/scope
    # semantics; see git_credentials.remote_advisories. Only the single
    # template actually being used is checked, and only here at use time.
    if template.repo:
        from agentworks.git_credentials import remote_advisories

        for advisory in remote_advisories(registry, template.repo):
            output.warn(advisory)

    vm = _resolve_vm(db, vm_name)
    _guard_vm_status(vm)

    # BUILD: the command names its direct resources (this VM, the
    # chosen workspace name) and constructs the pending workspace node
    # with its VM edge attached; the walk assembles the graph.
    # Construction is cheap and touches no secret machinery; the walk
    # union below is the boundary's source. Nothing resolves yet.
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node
    from agentworks.workspaces.nodes import pending_workspace_node

    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm)

    pending_workspace = pending_workspace_node(db, config, ws_name, vm_node, template_name)
    nodes = walk(pending_workspace)
    # The walk supplies the boundary union (the site's config secrets;
    # a workspace template's env secrets are runtime inputs, delivered
    # where sessions run, so they stay out of it: hermetic
    # provisioning, the same pin the vm-template node carries).
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    scope = _workspace_scope(db, vm, ws_name)

    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # The preflight boundary: the sweep covers every participating
        # node, then the site's config secrets resolve in one pass (or
        # arrive pre-seeded from the gate). This command has never
        # framed phases, so no banners here; the realize body never
        # frames either.
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))
        resolver.resolve()

        from agentworks.workspaces.realize import realize_workspace

        vscode_path = realize_workspace(
            db,
            config,
            registry,
            name=ws_name,
            vm=vm,
            template=template,
        )
        # Bookkeeping only, deliberately not via a realization log:
        # this command never unwinds a realized workspace (a failure
        # after the row exists keeps the workspace, as the imperative
        # command did), and the body already cleaned up its own
        # partial files before re-raising.
        pending_workspace.mark_realized()

        if open_vscode:
            subprocess.run(["code", vscode_path], check=False)


def describe_workspace(
    db: Database,
    name: str,
) -> None:
    """Show workspace details."""
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    output.info(f"Name:       {ws.name}")
    output.info(f"VM:         {ws.vm_name}")
    output.info(f"Template:   {ws.template or 'default'}")
    output.info(f"Path:       {ws.workspace_path}")
    output.info(f"Created:    {ws.created_at}")

    # Sessions
    sessions = db.list_sessions(workspace_name=name)
    output.info(f"\nSessions ({len(sessions)}):")
    if sessions:
        for s in sessions:
            mode_label = f"agent: {s.agent_name}" if s.agent_name else "admin"
            output.detail(f"{s.name}  [{s.template}]  {mode_label}")
    else:
        output.detail("(none)")

    # Agents with grants
    agents = db.list_agents(vm_name=ws.vm_name)
    granted = [a for a in agents if db.has_any_grant(a.name, name)]
    output.info(f"\nAgents with access ({len(granted)}):")
    if granted:
        for agent in granted:
            output.detail(f"{agent.name}  (user: {agent.linux_user})")
    else:
        output.detail("(none)")


def list_workspaces(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
    names_only: bool = False,
) -> None:
    """List workspaces.

    With ``names_only=True``, emit one workspace name per line and
    skip the table render. Used by shell completion (see issue #147).
    """
    workspaces = db.list_workspaces(vm_name=vm_name)

    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No workspaces found" line below
        # is for human readers only.
        for ws in workspaces:
            output.info(ws.name)
        return

    if not workspaces:
        output.info("No workspaces found.")
        return

    def _tpl_name(t: str | None) -> str:
        if t is None or t == "(built-in)":
            return "default"
        return t

    rows = [(ws.name, ws.vm_name, _tpl_name(ws.template), ws.created_at) for ws in workspaces]

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    vm_w = max(len("VM"), max(len(r[1]) for r in rows))
    tpl_w = max(len("TEMPLATE"), max(len(r[2]) for r in rows))

    header = f"{'NAME':<{name_w}}  {'VM':<{vm_w}}  {'TEMPLATE':<{tpl_w}}  CREATED"
    output.info(header)
    output.info("-" * len(header))
    for ws_name, ws_vm, tpl, created in rows:
        output.info(f"{ws_name:<{name_w}}  {ws_vm:<{vm_w}}  {tpl:<{tpl_w}}  {created}")
