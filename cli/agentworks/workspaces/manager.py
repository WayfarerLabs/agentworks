"""Workspace lifecycle orchestration."""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name
from agentworks.db import InitStatus
from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.vms.manager import gated_vm_boundary

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.resources.registry import Registry
    from agentworks.transports import Transport
    from agentworks.vms.nodes import LiveVMNode


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


def reinit_workspace(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Re-run workspace initialization to converge live VM state to the DB.

    Idempotent and forward-only. Steps split into two shapes:

    - **Detection-based** (group existence, admin membership, agent group
      membership against the grant table): probe live state first and only
      apply a fix when state diverges. Report `Fixed:` when a fix ran,
      `OK:` when no change was needed.
    - **Always-applied** (directory ownership, permissions, SGID, ACLs,
      parent-directory traversal): re-run their canonical commands every
      time; the underlying chown/chmod/setfacl are no-ops on already-correct
      state. Report `OK:` on success.

    Git identity (the template's `git_user_name` / `git_user_email`)
    converges here too, detection-based: an identity added or changed on
    the template after create is stamped into the checkout's repo-local
    config, and an already-correct value reports `OK:`.

    Same semantic as `vm reinit` and `agent reinit`: the declared state in
    the DB is the source of truth; this reinit converges live state to
    match.

    Orchestrated (``vms.manager.gated_vm_boundary``, WORKSPACE scope):
    the graph is the live VM alone (the workspace has no capability
    instances and nothing realization-shaped; convergence mutates
    through the VM transport), the activation gate replaces this
    command's ``keep_active``, opening BEFORE the preflight sweep with
    its just-in-time values seeding the boundary resolver, and the
    whole SSH convergence body runs inside the held-active span. The
    not-found checks stay pre-boundary: a refusal costs zero prompts,
    zero resolves, and zero gate events.
    """
    from agentworks.agents.manager import AGENT_PREFIX
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHError
    from agentworks.transports import transport

    # build_registry runs first so framework miss-policies fire before
    # any DB / VM business logic.
    registry = build_registry(config)

    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    vm = db.get_vm(ws.vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{ws.vm_name}' not found",
            entity_kind="vm",
            entity_name=ws.vm_name,
        )

    with gated_vm_boundary(db, config, registry, vm, scope=_workspace_scope(db, vm, name)):
        target = transport(vm, config)
        ws_group = ws.linux_group
        fixes = 0

        output.info(f"Reinitializing workspace '{name}' on VM '{vm.name}'...")

        # 0. Ensure acl package is installed (needed for setfacl)
        try:
            has_setfacl = target.run("which setfacl", sudo=True, check=False)
            if not has_setfacl.ok:
                target.run("apt-get install -y -qq acl", sudo=True, timeout=60)
                output.detail("Fixed: installed acl package")
                fixes += 1
            else:
                output.detail("OK: acl package")
        except SSHError as e:
            output.warn(f"acl package check failed: {e}")

        # 1. Ensure the workspace group recorded in the DB exists on the VM.
        try:
            group_exists = target.run(f"getent group {ws_group}", sudo=True, check=False)
            if not group_exists.ok:
                target.run(
                    f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'",
                    sudo=True,
                )
                output.detail(f"Fixed: created group {ws_group}")
                fixes += 1
            else:
                output.detail(f"OK: group {ws_group} exists")
        except SSHError as e:
            output.warn(f"group check failed: {e}")

        # 2. Ensure admin is in the group
        try:
            in_group = target.run(
                f"id -nG {vm.admin_username}",
                sudo=True,
                check=False,
            )
            if in_group.ok and ws_group not in in_group.stdout.split():
                target.run(f"usermod -aG {ws_group} {vm.admin_username}", sudo=True)
                output.detail(f"Fixed: added admin '{vm.admin_username}' to {ws_group}")
                fixes += 1
            else:
                output.detail(f"OK: admin in {ws_group}")
        except SSHError as e:
            output.warn(f"admin group check failed: {e}")

        # 3. Fix directory permissions (recursive chgrp so ACLs apply correctly)
        try:
            target.run(f"chown -R {vm.admin_username}:{ws_group} {ws.workspace_path}", sudo=True, timeout=120)
            target.run(f"chmod 2770 {ws.workspace_path}", sudo=True)
            # Set SGID on all subdirectories so new files inherit the workspace group.
            # This is critical for atomic-write tools (including Claude Code) that
            # create a temp file and rename it over the original.
            target.run(
                f"find {shlex.quote(ws.workspace_path)} -type d -exec chmod g+s {{}} +",
                sudo=True,
                timeout=120,
            )
            output.detail("OK: directory ownership and permissions")
        except SSHError as e:
            output.warn(f"permission fix failed: {e}")

        # 4. Fix ACLs
        # Default ACLs only apply to directories; use find to avoid warnings on files.
        # Effective ACLs apply to all entries and should not produce output.
        try:
            target.run(
                f"find {ws.workspace_path} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
                sudo=True,
                timeout=120,
            )
            target.run(
                f"setfacl -R -m g::rwx -m m::rwx {ws.workspace_path}",
                sudo=True,
                timeout=120,
            )
            output.detail("OK: ACLs")
        except SSHError as e:
            output.warn(f"ACL fix failed: {e}")

        # 5. Fix parent directory traversal
        try:
            target.run(
                f'sh -c \'p={ws.workspace_path}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done\'',
                sudo=True,
            )
            output.detail("OK: parent traversal")
        except SSHError as e:
            output.warn(f"parent traversal fix failed: {e}")

        # 5b. Converge the checkout's git identity. Repo-local config is
        # actor-agnostic and idempotent, so identity joins the reinit
        # convergence set: an identity added or changed on the template
        # after create lands here (detection-based, so an unchanged value
        # reports OK). Only meaningful when the workspace is a git repo; a
        # declared identity on a repo-less workspace is a no-op.
        fixes += _reinit_git_identity(target, registry, ws)

        # 6. Reconcile agent group membership
        # Get agents that SHOULD be in the group (have any grant)
        granted_agents = set()
        all_agents = db.list_agents(vm_name=vm.name)
        for agent in all_agents:
            if db.has_any_grant(agent.name, name):
                granted_agents.add(agent.linux_user)

        # Get agents that ARE in the group. The agt- prefix check covers both
        # current agents and legacy ones (whose names start with agt--).
        try:
            group_info = target.run(f"getent group {ws_group}", sudo=True, check=False)
            current_members: set[str] = set()
            if group_info.ok and ":" in group_info.stdout:
                members_str = group_info.stdout.strip().split(":")[-1]
                if members_str:
                    current_members = {m for m in members_str.split(",") if m.startswith(AGENT_PREFIX)}

            # Add missing agents
            to_add = granted_agents - current_members
            for user in sorted(to_add):
                target.run(f"usermod -aG {ws_group} {user}", sudo=True)
                output.detail(f"Fixed: added {user} to {ws_group}")
                fixes += 1

            # Remove agents that shouldn't be there
            to_remove = current_members - granted_agents
            for user in sorted(to_remove):
                target.run(f"gpasswd -d {user} {ws_group}", sudo=True, check=False)
                output.detail(f"Fixed: removed {user} from {ws_group}")
                fixes += 1

            if not to_add and not to_remove:
                output.detail(f"OK: agent group membership ({len(current_members)} agent(s))")
        except SSHError as e:
            output.warn(f"agent membership check failed: {e}")

        if fixes > 0:
            output.result(f"\nApplied {fixes} fix(es)")
        else:
            output.result("\nAlready up to date")


def _reinit_git_identity(
    target: Transport,
    registry: Registry,
    ws: WorkspaceRow,
) -> int:
    """Converge the checkout's repo-local git identity to its template.

    Mirrors the create-time stamp in
    ``workspaces.backends.vm.create_vm_workspace``: the identity lives in
    the checkout's own ``.git/config`` (actor-agnostic), so re-applying it
    is idempotent. Detection-based, so an already-correct value reports
    ``OK`` and only a real change counts as a fix.

    Three no-op cases: the template (or its resolution) is gone, the
    template declares no identity, or the on-disk workspace path is not a
    git checkout (probed with ``git rev-parse``). Note the last check is on
    the checkout, not the template's ``repo`` field: a workspace whose repo
    was later dropped from the template keeps its existing checkout, so its
    identity still converges.

    Returns the number of identity fields it actually changed.
    """
    from agentworks.errors import ConfigError
    from agentworks.ssh import SSHError
    from agentworks.workspaces.templates import resolve_template

    try:
        tmpl = resolve_template(registry, ws.template)
    except (ValueError, ConfigError):
        # The workspace's template is gone from config; nothing to converge
        # toward. Ownership/permission convergence above does not need it.
        return 0

    declared = [
        (key, value)
        for key, value in (
            ("user.name", tmpl.git_user_name),
            ("user.email", tmpl.git_user_email),
        )
        if value
    ]
    if not declared:
        return 0

    quoted_path = shlex.quote(ws.workspace_path)
    try:
        is_repo = target.run(f"git -C {quoted_path} rev-parse --git-dir", check=False)
        if not is_repo.ok:
            # "not a git repository" is the expected, quiet no-op (a
            # workspace created without a repo). Any other probe failure
            # (git missing, a broken checkout, permissions) is a real
            # problem the operator should see, not a silent OK.
            stderr = (is_repo.stderr or "").strip()
            if "not a git repository" in stderr.lower():
                output.detail("OK: git identity (workspace has no repo)")
            else:
                output.warn(
                    f"git identity skipped: could not probe {ws.workspace_path} "
                    f"as a git repo ({stderr or 'unknown error'})"
                )
            return 0

        fixed = 0
        for key, value in declared:
            current = target.run(f"git -C {quoted_path} config --local --get {key}", check=False)
            if current.ok and current.stdout.strip() == value:
                output.detail(f"OK: git {key}")
                continue
            target.run(f"git -C {quoted_path} config --local {key} {shlex.quote(value)}")
            output.detail(f"Fixed: git {key}")
            fixed += 1
        return fixed
    except SSHError as e:
        output.warn(f"git identity check failed: {e}")
        return 0


def _revert_grant_on_failure(db: Database, agent_name: str, ws_name: str) -> None:
    """Best-effort: drop a just-inserted explicit grant after the on-VM
    group add failed (or was cancelled). Used by the grant-all loop in
    create_workspace to keep DB and VM authorization aligned. A failure
    to revert is logged but does not raise, so it never masks the
    caller's original exception (or KeyboardInterrupt)."""
    try:
        db.delete_agent_grant(agent_name, ws_name, "explicit")
    except Exception as revert_err:
        output.warn(
            f"Could not revert grant for '{agent_name}' on workspace '{ws_name}': "
            f"{revert_err}. DB has a grant row with no VM-side group membership; "
            f"re-run 'agent grant-workspaces {agent_name} {ws_name}' or "
            f"revoke explicitly."
        )


def _rehome_partial_state_hint(db: Database, ws_name: str, old_path: str, new_path: str) -> str:
    """Describe the actual DB state after a rehome failure / cancellation.

    The rehome flow copies files to the new path, then updates the DB. KI or
    an exception can land before OR after the DB update, so we read the row
    back to give the user an accurate picture rather than asserting one way.

    This is called from the KeyboardInterrupt / exception handler, so any
    DB error here would mask the original error. Catch broadly and fall
    back to a generic hint.
    """
    try:
        ws = db.get_workspace(ws_name)
    except Exception as e:
        return f"DB state could not be read ({e}); manual inspection needed."
    if ws is None:
        return "Workspace row is missing from the DB; manual cleanup may be needed."
    if ws.workspace_path == new_path:
        return (
            f"DB now points to {new_path}, but the on-VM move may be incomplete. "
            f"Use 'workspace describe {ws_name}' and verify the directory."
        )
    return (
        f"DB still points to {old_path}. A partial copy of the workspace may exist "
        f"at {new_path}; verify and clean up if needed."
    )


def rehome_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    target_path: str | None = None,
    remove_old: bool = False,
    yes: bool = False,
) -> None:
    """Move a workspace to a new directory path."""
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    # Determine target path
    new_path = target_path if target_path is not None else f"{config.paths.vm_workspaces}/{name}"

    old_path = ws.workspace_path

    if old_path == new_path:
        output.info(f"Workspace '{name}' is already at {new_path}")
        return

    # Safety: detect overlapping paths
    old_norm = old_path.rstrip("/") + "/"
    new_norm = new_path.rstrip("/") + "/"
    if new_norm.startswith(old_norm) or old_norm.startswith(new_norm):
        raise ValidationError(
            "source and target paths overlap",
            entity_kind="workspace",
            entity_name=name,
        )

    # Block unless all sessions are STOPPED
    from agentworks.db import PID_STOPPED, SessionStatus
    from agentworks.sessions.manager import batch_check_all_sessions, ensure_pids_batch

    sessions = db.list_sessions(workspace_name=name)
    if sessions:
        try:
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            status_map = batch_check_all_sessions(sessions, db=db, config=config)
        except Exception as exc:
            raise ExternalError(
                f"cannot verify session status for workspace '{name}' (VM may be unreachable): {exc}",
                entity_kind="workspace",
                entity_name=name,
            ) from exc
        not_stopped = [
            s
            for s in sessions
            if s.pid != PID_STOPPED and status_map.get(s.name, SessionStatus.UNKNOWN) != SessionStatus.STOPPED
        ]
        if not_stopped:
            names = ", ".join(s.name for s in not_stopped)
            raise StateError(
                f"workspace '{name}' has {len(not_stopped)} non-stopped session(s) ({names}).",
                entity_kind="workspace",
                entity_name=name,
                hint="Stop or delete the listed sessions first.",
            )

    _rehome_vm(db, config, ws, new_path, remove_old=remove_old, yes=yes)


def _rehome_vm(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
    new_path: str,
    *,
    remove_old: bool,
    yes: bool,
) -> None:
    """Rehome a VM workspace.

    Orchestrated (``vms.manager.gated_vm_boundary``, WORKSPACE scope):
    the graph is the live VM alone, the activation gate replaces this
    command's ``keep_active``, and the whole move runs inside the
    held-active span. The not-found check and the VM-status guard stay
    pre-boundary; the source / target directory existence checks and
    the confirm prompt stay INSIDE the span exactly where they were:
    the checks need SSH (inherently post-gate) and the confirm renders
    their results, so they cannot move earlier without changing what
    the operator confirms.
    """

    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHError, SSHLogger
    from agentworks.transports import transport
    from agentworks.workspaces.backends.vm import generate_vscode_workspace

    ws_name = ws.name
    old_path = ws.workspace_path
    vm_name = ws.vm_name

    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )

    _guard_vm_status(vm)
    registry = build_registry(config)
    with gated_vm_boundary(db, config, registry, vm, scope=_workspace_scope(db, vm, ws_name)):
        target = transport(vm, config)

        # Verify source exists
        src_check = target.run(f"test -d {old_path}", check=False, timeout=10)
        if not src_check.ok:
            raise StateError(
                f"source directory {old_path} does not exist on VM",
                entity_kind="workspace",
                entity_name=ws_name,
            )

        # Verify target does not exist
        dst_check = target.run(f"test -d {new_path}", check=False, timeout=10)
        if dst_check.ok:
            raise StateError(
                f"target directory {new_path} already exists on VM",
                entity_kind="workspace",
                entity_name=ws_name,
            )

        if not yes:
            output.info(f"Rehome workspace '{ws_name}':")
            output.detail(f"From: {old_path}")
            output.detail(f"To:   {new_path}")
            if remove_old:
                output.detail("Old directory will be REMOVED after copy")
            else:
                output.detail("Old directory will be LEFT IN PLACE")
            if not output.confirm("Proceed?"):
                raise UserAbort("rehome cancelled")

        ssh_logger = SSHLogger(vm.name, "workspace-rehome")
        target = transport(vm, config, logger=ssh_logger)
        ws_group = ws.linux_group

        # Shell-quote paths once up front; both are interpolated into many shell
        # commands below. Without this, any space or shell-special character in
        # a workspace path breaks the command (and exposes an injection surface
        # if a path is ever supplied by an attacker-controlled source).
        np = shlex.quote(new_path)
        op = shlex.quote(old_path)

        # Outer try/finally ensures the SSH logger is closed exactly once. Earlier
        # versions called close() in every except branch AND on the success path,
        # which double-wrote the "Finished" footer when an inner raise re-entered
        # an outer except.
        try:
            try:
                # Create target directory as root and chown to admin so rsync can write
                target.run(f"mkdir -p {np}", sudo=True)
                target.run(f"chown {vm.admin_username} {np}", sudo=True)

                # Copy with rsync (fall back to cp -a). Trailing slash matters for
                # rsync semantics ("contents of source into target"); putting it
                # AFTER the quoted path works because adjacent quoted/unquoted
                # tokens concatenate in shell.
                output.info("Copying workspace...")
                has_rsync = target.run("which rsync", check=False, timeout=10)
                if has_rsync.ok:
                    target.run(f"rsync -a {op}/ {np}/", timeout=600)
                else:
                    target.run(f"cp -a {op}/. {np}/", sudo=True, timeout=600)

                # Verify copy succeeded
                verify = target.run(f"test -d {np}", check=False, timeout=10)
                if not verify.ok:
                    raise ExternalError(
                        "copy verification failed, target directory not found",
                        entity_kind="workspace",
                        entity_name=ws_name,
                        hint=f"SSH log: {ssh_logger.path}",
                    )

                # Fix ownership, permissions, and ACLs on the new path
                output.info("Setting permissions...")
                target.run(f"chown {vm.admin_username}:{ws_group} {np}", sudo=True)
                target.run(f"chmod 2770 {np}", sudo=True)
                target.run(f"find {np} -type d -exec chmod g+s {{}} +", sudo=True, timeout=120)
                try:
                    target.run(
                        f"find {np} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
                        sudo=True,
                        timeout=120,
                    )
                    target.run(
                        f"setfacl -R -m g::rwx -m m::rwx {np}",
                        sudo=True,
                        timeout=120,
                    )
                except SSHError as e:
                    output.warn(f"ACL setup failed: {e}")

                # Fix parent directory traversal. sudo=True already wraps the
                # command in `sudo -n bash -c '<quoted>'`, so the script runs in
                # a single bash context. No extra `sh -c '...'` indirection is
                # needed (and the explicit wrapper made path quoting impossible
                # to do safely).
                target.run(
                    f'p={np}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done',
                    sudo=True,
                )

                # Regenerate tmuxinator config at new path. write_file passes
                # remote_path to scp as a subprocess arg (not interpolated into a
                # local shell), so f-string concatenation is safe on the client
                # side. The remote scp/sftp handler may still interpret the path
                # per its own rules; if a future change funnels untrusted paths
                # through here, revisit.
                from agentworks.workspaces.tmuxinator import console_session_name, generate_config

                tmux_config = generate_config(ws_name, new_path)
                target.write_file(f"{new_path}/.tmuxinator.yml", tmux_config)
                session = console_session_name(ws_name)
                target.run("mkdir -p ~/.config/tmuxinator", timeout=10)
                # Keep ~/.config/tmuxinator/ literal so tilde expansion still
                # happens; quote just the filename for layered defense.
                target.run(
                    f"ln -sf {np}/.tmuxinator.yml ~/.config/tmuxinator/{shlex.quote(session)}.yml",
                    timeout=10,
                )

                # Update database
                db.update_workspace_path(ws_name, new_path)
                output.detail(f"Database updated: workspace_path = {new_path}")

                # Regenerate VS Code workspace file
                vscode_path = generate_vscode_workspace(vm, config, ws_name, new_path)
                output.detail(f"VS Code workspace updated: {vscode_path}")

                # Handle old directory
                if remove_old:
                    output.info(f"Removing old directory {old_path}...")
                    target.run(f"rm -rf {op}", sudo=True, timeout=60)
                    output.detail("Old directory removed")
                else:
                    output.info(f"Old directory left in place at {old_path}")
                    output.detail("Remove it manually when ready, or re-run with --remove-old")

            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling workspace rehome '{ws_name}'. "
                    f"{_rehome_partial_state_hint(db, ws_name, old_path, new_path)} "
                    f"SSH log: {ssh_logger.path}"
                )
                raise
            except AgentworksError:
                raise
            except Exception as e:
                raise ExternalError(
                    f"during rehome: {e}",
                    entity_kind="workspace",
                    entity_name=ws_name,
                    hint=(f"SSH log: {ssh_logger.path}. {_rehome_partial_state_hint(db, ws_name, old_path, new_path)}"),
                ) from e
        finally:
            ssh_logger.close()

        output.result(f"Workspace '{ws_name}' rehomed to {new_path}")


def delete_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
    vm_node: LiveVMNode | None = None,
) -> None:
    """Delete a workspace.

    Orchestrated on the standalone path (``vm_node=None``, the command
    root and ``delete_session``'s workspace-cleanup call):
    ``vms.manager.gated_vm_boundary`` composes the live-VM graph at
    WORKSPACE scope, the activation gate's held-active span covers the
    session-kill and on-VM removal work. The sessions guard, the confirm
    gate, and the not-found check stay pre-boundary: a refusal costs
    zero prompts, zero resolves, and zero gate events. A missing VM row
    skips the boundary entirely (DB-only cleanup), and a VM without a
    Tailscale address skips only the SSH session-kill block, exactly
    the imperative shape.

    ``vm_node`` is the nested-teardown path (session create's ephemeral
    ROLLBACK, where ``PendingWorkspaceNode.teardown`` runs INSIDE the
    caller's held activation gate). That gate already converged the VM
    and holds it active across the whole unwind, so this path composes
    NO second boundary and resolves NOTHING: it trusts the caller's
    gate and re-enters only the keepalive hold, reaching the platform
    through the node's own site edge. Passing the node (never a bare
    platform) is what keeps a teardown from silently falling into the
    boundary-building standalone branch.
    """

    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    # Check for sessions
    session_count = len(db.list_sessions(workspace_name=name))
    if session_count > 0 and not force:
        raise StateError(
            f"workspace '{name}' has {session_count} session(s).",
            entity_kind="workspace",
            entity_name=name,
            hint="Delete the sessions first, or pass --force to also delete them.",
        )

    if not yes:
        msg = f"Delete workspace '{name}'?"
        if session_count > 0:
            msg += f" ({session_count} session(s) will also be deleted)"
        if not output.confirm(msg):
            raise UserAbort("delete cancelled")

    # Create SSH logger for VM operations
    import contextlib

    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(ws.vm_name, "workspace-delete")
    output.info(f"Deleting workspace '{name}' on VM '{ws.vm_name}'...")

    # Kill running sessions (status-aware) and delete session records
    vm = db.get_vm(ws.vm_name)
    # console_pairs is populated only when we have live SSH access; the
    # post-delete cleanup is best-effort and skips when target is None.
    target: Transport | None = None
    console_pairs: list[tuple[str, str]] = []
    with contextlib.ExitStack() as _keepalive_stack:
        if vm is not None:
            if vm_node is None:
                # The standalone composition root: build the boundary here.
                from agentworks.bootstrap import build_registry

                registry = build_registry(config)
                _keepalive_stack.enter_context(
                    gated_vm_boundary(
                        db,
                        config,
                        registry,
                        vm,
                        scope=_workspace_scope(db, vm, name),
                    )
                )
            else:
                # The nested-teardown path: the caller's composition
                # already converged the VM and holds its activation gate
                # open across this unwind, so we compose no second
                # boundary and resolve nothing; we re-enter only the
                # keepalive hold, reaching the platform through the
                # node's own site edge.
                #
                # That hold keeps the NODE's VM active, but the delete
                # body issues its SSH + DB work against the workspace's
                # own VM (``ws.vm_name``). Enforce that they are the same
                # VM: a mismatched node would silently hold one VM active
                # while operating on another. Unreachable today (the
                # pending nodes always pass their own ``self._vm``), so
                # this is a loud guard on a teardown-wiring bug, not a
                # runtime branch we expect to take.
                if vm_node.row.name != ws.vm_name:
                    raise StateError(
                        f"nested teardown of workspace '{name}' was "
                        f"handed a VM node for '{vm_node.row.name}', but "
                        f"the workspace is on '{ws.vm_name}'; the node "
                        f"handed to a teardown must be the entity's own "
                        f"VM node (teardown-wiring bug).",
                        entity_kind="workspace",
                        entity_name=name,
                    )
                _keepalive_stack.enter_context(vm_node.hold_active())

        if vm is not None and vm.tailscale_host is not None:
            from agentworks.db import SessionStatus
            from agentworks.sessions.manager import (
                check_session_status,
                ensure_pids_batch,
            )
            from agentworks.sessions.tmux import force_kill_tmux_server, kill_session
            from agentworks.transports import transport

            target = transport(vm, config, logger=ssh_logger)
            sessions = db.list_sessions(workspace_name=name)
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            # Snapshot console memberships before the FK cascade clears them.
            console_pairs = [(c.name, s.name) for s in sessions for c in db.list_consoles_for_session(s.name)]
            unstoppable: list[str] = []
            for session in sessions:
                status = check_session_status(session, target=target)
                if status == SessionStatus.OK:
                    if not kill_session(session.name, run_command=target.run, socket_path=session.socket_path):
                        # Race: session may have exited between check and kill. Recheck.
                        recheck = check_session_status(session, target=target)
                        if recheck != SessionStatus.STOPPED:
                            unstoppable.append(session.name)
                            continue
                elif status == SessionStatus.BROKEN:
                    if (
                        session.pid
                        and session.pid > 0
                        and force_kill_tmux_server(
                            session.pid,
                            target=target,
                            socket_path=session.socket_path,
                        )
                    ):
                        pass  # killed successfully
                    else:
                        unstoppable.append(session.name)
                elif status == SessionStatus.UNKNOWN:
                    unstoppable.append(session.name)
            if unstoppable:
                raise StateError(
                    f"cannot delete workspace '{name}': {len(unstoppable)} session(s) could not be stopped "
                    f"({', '.join(unstoppable)}).",
                    entity_kind="workspace",
                    entity_name=name,
                    hint="Resolve the stuck sessions manually before retrying.",
                )
        db.delete_sessions_for_workspace(name)

        # Best-effort: take down dangling 'Waiting for session...' windows in any
        # console that listed one of these sessions. Skips when we have no live
        # target (VM down or never had a tailnet host).
        if target is not None and console_pairs:
            from agentworks.sessions.multi_console import kill_session_windows

            kill_session_windows(target, pairs=console_pairs)

        # Revoke agent workspace grants (agents are VM-scoped, not deleted with workspaces)
        if vm is not None:
            from agentworks.agents.grants import revoke_workspace_grants

            revoke_workspace_grants(db, config, name, vm)

        if vm is not None:
            from agentworks.workspaces.backends.vm import delete_vm_workspace

            delete_vm_workspace(vm, config, name, ws.workspace_path, logger=ssh_logger)

        ssh_logger.close()

        # Remove .code-workspace file
        vscode_path = config.paths.vscode_workspaces / f"{name}.code-workspace"
        vscode_path.unlink(missing_ok=True)

        db.delete_workspace(name)
        output.info(f"Workspace '{name}' deleted")


def copy_workspace(
    db: Database,
    config: Config,
    source_name: str,
    *,
    dest_name: str,
    vm_name: str | None = None,
) -> None:
    """Copy a workspace to a new VM workspace.

    Orchestrated (``vms.manager.gated_vm_boundary``, WORKSPACE scope),
    the first two-VM command: the composition stays SEQUENTIAL per VM,
    exactly the imperative shape, rather than a coalesced multi-root
    single-boundary graph. The imperative command ran two separate
    binds (two prompt sessions, one per site, when source and dest
    differ), and the dest VM is only known mid-command
    (``_resolve_vm`` may interactively prompt); coalescing would merge
    prompt sessions AND hoist the interactive chooser, both behavior
    changes beyond parity. The source boundary (source workspace's
    scope) is entered on the ExitStack before the pack; when the dest
    VM differs, a SECOND boundary (dest workspace's scope) nests on
    the same stack so both VMs stay held; the same-VM case reuses the
    source composition with no second boundary. The multi-root walk
    stays available for the batch commands that already coalesce.
    """
    import contextlib
    import tempfile
    from pathlib import Path

    from agentworks.agents.grants import workspace_group
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHLogger
    from agentworks.transports import SSHTransport, transport

    validate_name(dest_name)

    src_ws = db.get_workspace(source_name)
    if src_ws is None:
        raise NotFoundError(
            f"workspace '{source_name}' not found",
            entity_kind="workspace",
            entity_name=source_name,
        )

    if db.get_workspace(dest_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{dest_name}' already exists",
            entity_kind="workspace",
            entity_name=dest_name,
        )

    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with contextlib.ExitStack() as _keepalive_stack:
            # --- Pack from source ---
            src_vm = db.get_vm(src_ws.vm_name)
            if src_vm is None:
                raise NotFoundError(
                    f"VM '{src_ws.vm_name}' not found",
                    entity_kind="vm",
                    entity_name=src_ws.vm_name,
                )
            _guard_vm_status(src_vm)
            registry = build_registry(config)
            _keepalive_stack.enter_context(
                gated_vm_boundary(
                    db,
                    config,
                    registry,
                    src_vm,
                    scope=_workspace_scope(db, src_vm, source_name),
                )
            )
            if src_vm.tailscale_host is None:
                raise StateError(
                    f"VM '{src_vm.name}' has no Tailscale address",
                    entity_kind="vm",
                    entity_name=src_vm.name,
                )

            src_exec = transport(src_vm, config)
            # transport() returns SSHTransport for Tailscale-backed VMs; this
            # path streams scp/tar over the SSH channel and needs the raw argv.
            assert isinstance(src_exec, SSHTransport)
            output.info(f"Packing workspace '{source_name}' from VM '{src_vm.name}'...")

            # Stream tar from VM to local temp file
            ssh_args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
            if src_exec.identity_file is not None:
                ssh_args.extend(["-i", str(src_exec.identity_file)])
            ssh_args.append(f"{src_exec.user}@{src_exec.host}")
            ssh_args.append(f"tar czf - -C {src_ws.workspace_path} .")

            with open(tmp_path, "wb") as f:
                proc = subprocess.run(ssh_args, stdout=f, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                stderr = proc.stderr.decode() if proc.stderr else ""
                raise ExternalError(
                    f"pack failed: {stderr.strip()}",
                    entity_kind="workspace",
                    entity_name=source_name,
                )

            # --- Unpack to destination VM ---
            dest_vm = _resolve_vm(db, vm_name)
            _guard_vm_status(dest_vm)
            if dest_vm.name != src_vm.name:
                _keepalive_stack.enter_context(
                    gated_vm_boundary(
                        db,
                        config,
                        registry,
                        dest_vm,
                        scope=_workspace_scope(db, dest_vm, dest_name),
                    )
                )
            # Same VM: the source boundary and held span above already
            # gate and hold it; a second boundary would re-run the
            # resolve pass.
            if dest_vm.tailscale_host is None:
                raise StateError(
                    f"VM '{dest_vm.name}' has no Tailscale address",
                    entity_kind="vm",
                    entity_name=dest_vm.name,
                )

            lg = SSHLogger(dest_vm.name, "workspace-copy")
            dest_target = transport(dest_vm, config, logger=lg)

            workspace_path = f"{config.paths.vm_workspaces}/{dest_name}"
            ws_group = workspace_group(dest_name)

            output.info(f"Unpacking to workspace '{dest_name}' on VM '{dest_vm.name}'...")

            # Set up group, directory, and permissions (same as create_vm_workspace)
            dest_target.run(
                f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'",
                sudo=True,
            )
            dest_target.run(f"usermod -aG {ws_group} {dest_vm.admin_username}", sudo=True)
            dest_target.run(f"mkdir -p {workspace_path}", sudo=True, timeout=10)
            dest_target.run(f"chown {dest_vm.admin_username}:{ws_group} {workspace_path}", sudo=True)
            dest_target.run(f"chmod 2770 {workspace_path}", sudo=True)
            dest_target.run(f"setfacl -d -m g::rwx -m m::rwx {workspace_path}", sudo=True)

            # Unpack archive and fix ownership
            remote_tmp = f"/tmp/{dest_name}-copy.tgz"
            dest_target.copy_to(tmp_path, remote_tmp, timeout=300)
            dest_target.run(f"tar xzf {remote_tmp} -C {workspace_path}", sudo=True, timeout=120)
            dest_target.run(f"rm -f {remote_tmp}", check=False, timeout=10)
            dest_target.run(
                f"chown -R {dest_vm.admin_username}:{ws_group} {workspace_path}",
                sudo=True,
                timeout=60,
            )
            dest_target.run(
                f"find {shlex.quote(workspace_path)} -type d -exec chmod g+s {{}} +",
                sudo=True,
                timeout=120,
            )

            db.insert_workspace(
                dest_name,
                vm_name=dest_vm.name,
                workspace_path=workspace_path,
                template="copied",
                linux_group=ws_group,
            )

            # Generate tmuxinator config and VS Code workspace
            from agentworks.workspaces.backends.vm import generate_vscode_workspace
            from agentworks.workspaces.tmuxinator import console_session_name, generate_config

            tmux_config = generate_config(dest_name, workspace_path)
            dest_target.write_file(f"{workspace_path}/.tmuxinator.yml", tmux_config)
            session = console_session_name(dest_name)
            dest_target.run("mkdir -p ~/.config/tmuxinator", timeout=10)
            dest_target.run(
                f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{session}.yml",
                timeout=10,
            )
            vscode_path = generate_vscode_workspace(dest_vm, config, dest_name, workspace_path)
            output.detail(f"VS Code workspace: {vscode_path}")
            lg.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    output.result(f"Workspace '{source_name}' copied to '{dest_name}'")


def _workspace_scope(db: Database, vm: VMRow, ws_name: str) -> OperationScope:
    """The workspace commands' shared WORKSPACE-level operation scope:
    the operation is about the workspace (on this VM), even when the
    composed graph is the live VM alone. The WORKSPACE level's field
    rules (required vm + workspace; forbidden agent, session) are
    enforced by the scope's own constructor."""
    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.db import SYSTEM_SLUG_KEY

    return OperationScope(
        level=ScopeLevel.WORKSPACE,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm.name,
        workspace=ws_name,
    )


def _guard_vm_status(vm: VMRow) -> None:
    """Block operations on VMs that are not usable (failed or in-progress)."""
    usable = {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}
    if vm.init_status not in usable:
        if vm.init_status == InitStatus.FAILED.value:
            raise StateError(
                f"VM '{vm.name}' is in 'failed' state.",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Run 'vm delete' and recreate.",
            )
        else:
            raise StateError(
                f"VM '{vm.name}' initialization is not complete (status: {vm.init_status}).",
                entity_kind="vm",
                entity_name=vm.name,
            )


def _resolve_vm(db: Database, vm_name: str | None) -> VMRow:
    """Resolve which VM to use: explicit, auto-select if 1, or error."""
    if vm_name is not None:
        vm = db.get_vm(vm_name)
        if vm is None:
            raise NotFoundError(
                f"VM '{vm_name}' not found",
                entity_kind="vm",
                entity_name=vm_name,
            )
        return vm

    vms = db.list_vms()
    usable_statuses = {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}
    usable_vms = [v for v in vms if v.init_status in usable_statuses]

    if len(usable_vms) == 0:
        raise NotFoundError(
            "no VMs available.",
            entity_kind="vm",
            hint="Create one with 'agw vm create'.",
        )

    if len(usable_vms) == 1:
        output.info(f"Using VM '{usable_vms[0].name}'")
        return usable_vms[0]

    options = [f"{v.name}  ({v.site})" for v in usable_vms]
    idx = output.choose("Select a VM:", options)
    return usable_vms[idx]
