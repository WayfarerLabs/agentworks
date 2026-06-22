"""Workspace lifecycle orchestration."""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name
from agentworks.db import InitStatus, VMStatus
from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.vms.manager import keep_vm_active
from agentworks.workspaces.templates import resolve_template

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.transports import Transport


def create_workspace(
    db: Database,
    config: Config,
    *,
    name: str,
    vm_name: str | None = None,
    template_name: str | None = None,
    open_vscode: bool = False,
) -> None:
    """Create a workspace on a VM."""
    from agentworks.agents.manager import workspace_group
    from agentworks.ssh import SSHLogger
    from agentworks.workspaces.backends.vm import (
        create_vm_workspace,
        delete_vm_workspace,
        generate_vscode_workspace,
    )

    ws_name = name
    validate_name(ws_name)

    if db.get_workspace(ws_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{ws_name}' already exists",
            entity_kind="workspace",
            entity_name=ws_name,
        )

    template = resolve_template(config, template_name)
    template_resolved_name = template.name

    vm = _resolve_vm(db, vm_name)
    _guard_vm_status(vm)
    _ensure_vm_running(db, config, vm)
    with keep_vm_active(db, config, vm):

        workspace_path: str | None = None
        vscode_path: str | None = None

        ssh_logger = SSHLogger(vm.name, "workspace-create")

        def _cleanup() -> None:
            if workspace_path:
                delete_vm_workspace(vm, config, ws_name, workspace_path, logger=ssh_logger)
            if vscode_path:
                from pathlib import Path

                Path(vscode_path).unlink(missing_ok=True)

        def _safe_cleanup() -> None:
            # Rollback failures must not mask the original KI/exception. Surface
            # them as a warning (with workspace name and SSH log path for the
            # user to follow up on) and continue propagating the original error.
            try:
                _cleanup()
            except Exception as cleanup_err:
                output.warn(
                    f"rollback during workspace create '{ws_name}' failed: {cleanup_err}. "
                    f"VM may have residual files or VS Code workspace file. "
                    f"SSH log: {ssh_logger.path}"
                )

        # Outer try/finally ensures the SSH logger is closed exactly once, AFTER
        # any rollback commands have been logged. Closing earlier would write the
        # "Finished" footer before the rollback section, making the log misleading.
        try:
            try:
                output.info(
                    f"Creating workspace '{ws_name}' on VM '{vm.name}' (template: {template_resolved_name})..."
                )
                workspace_path = create_vm_workspace(vm, config, ws_name, template, logger=ssh_logger)

                vscode_path = generate_vscode_workspace(vm, config, ws_name, workspace_path)
                output.detail(f"VS Code workspace: {vscode_path}")

                db.insert_workspace(
                    ws_name,
                    workspace_path=workspace_path,
                    vm_name=vm.name,
                    template=template_resolved_name,
                    linux_group=workspace_group(ws_name),
                )
            except KeyboardInterrupt:
                output.warn(f"Cancelling workspace create '{ws_name}'... rolling back.")
                _safe_cleanup()
                raise
            except AgentworksError:
                _safe_cleanup()
                raise
            except Exception as e:
                _safe_cleanup()
                raise ExternalError(
                    f"creating workspace: {e}",
                    entity_kind="workspace",
                    entity_name=ws_name,
                    hint=f"SSH log: {ssh_logger.path}",
                ) from e

            # Add grant_all agents to the new workspace group. Best-effort: the
            # workspace itself was already created and inserted above, so a
            # per-agent failure (DB error, SSH hiccup) should not abort the
            # whole command. Surface failures as warnings and report accurate
            # counts so the user can re-grant manually with
            # 'agent grant-workspaces'.
            #
            # DB grant is inserted BEFORE the on-VM group add. If the order were
            # reversed and the DB write failed after the group add, the agent
            # would have VM-side membership with no DB grant backing it (a
            # silent authorization drift). With this ordering, a group-add
            # failure can be cleanly compensated by deleting the just-inserted
            # grant row.
            grant_all_agents = db.list_agents_on_vm_with_grant_all(vm.name)
            if grant_all_agents:
                from agentworks.agents.manager import _add_to_workspace_group

                added = 0
                failed: list[str] = []
                for agent in grant_all_agents:
                    try:
                        db.insert_agent_grant(agent.name, ws_name, "explicit")
                    except KeyboardInterrupt:
                        # sqlite commits inside a C call; KI can surface after
                        # the commit but before we move on, leaving an inserted
                        # row. Best-effort revert and re-raise to preserve the
                        # SIGINT contract.
                        output.warn(
                            f"Cancelled while inserting grant for agent '{agent.name}' on "
                            f"workspace '{ws_name}'. Reverting in case the insert committed."
                        )
                        _revert_grant_on_failure(db, agent.name, ws_name)
                        raise
                    except Exception as e:
                        failed.append(agent.name)
                        output.warn(
                            f"Failed to insert grant for agent '{agent.name}' on workspace "
                            f"'{ws_name}': {e}"
                        )
                        continue
                    try:
                        _add_to_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=ssh_logger)
                        added += 1
                    except KeyboardInterrupt:
                        # KI is a BaseException and slips past `except Exception`,
                        # so it needs its own branch. Without this, Ctrl-C during
                        # the SSH call would leave a committed grant row with no
                        # VM-side group membership (silent authorization drift).
                        output.warn(
                            f"Cancelled while adding agent '{agent.name}' to workspace "
                            f"'{ws_name}' group. Reverting just-inserted DB grant."
                        )
                        _revert_grant_on_failure(db, agent.name, ws_name)
                        raise
                    except Exception as e:
                        failed.append(agent.name)
                        output.warn(
                            f"Failed to add agent '{agent.name}' to workspace '{ws_name}' "
                            f"group: {e}. Reverting DB grant to keep state consistent."
                        )
                        _revert_grant_on_failure(db, agent.name, ws_name)
                if added:
                    output.detail(f"Added {added} grant-all agent(s) to workspace")
                if failed:
                    output.warn(
                        f"Grant-all agents not added: {', '.join(failed)}. "
                        f"Re-grant manually with 'agent grant-workspaces <name> {ws_name}'."
                    )
        finally:
            ssh_logger.close()

        if open_vscode:
            # vscode_path was assigned inside the inner try before any rollback
            # could occur; reaching here means the try block completed without
            # raising, so vscode_path is set. Assert for the type-checker.
            assert vscode_path is not None
            subprocess.run(["code", vscode_path], check=False)

        output.info(f"Workspace '{ws_name}' created")


def shell_workspace(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Open a plain shell into a workspace."""
    from agentworks.workspaces.backends.vm import shell_vm_workspace

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

    _guard_vm_status(vm)
    _ensure_vm_running(db, config, vm)
    db.update_workspace_last_seen(name)
    with keep_vm_active(db, config, vm):
        shell_vm_workspace(vm, config, ws.workspace_path)


def console_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    allow_nesting: bool = False,
    recreate: bool = False,
) -> None:
    """Open the workspace console (tmuxinator session with sessions)."""
    import os

    from agentworks.workspaces.backends.vm import console_vm_workspace

    if os.environ.get("TMUX") and not allow_nesting:
        raise StateError(
            "already inside a tmux session. Nesting is not recommended "
            "(prefix key conflicts, confusing detach behavior).",
            hint="Pass --allow-nesting to override.",
        )

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

    _guard_vm_status(vm)
    _ensure_vm_running(db, config, vm)
    db.update_workspace_last_seen(name)
    with keep_vm_active(db, config, vm):
        console_vm_workspace(vm, config, name, recreate=recreate)


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
    if ws.last_seen_at:
        output.info(f"Last Seen:  {ws.last_seen_at}")

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
) -> None:
    """List workspaces."""
    workspaces = db.list_workspaces(vm_name=vm_name)
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

    Same semantic as `vm reinit` and `agent reinit`: the declared state in
    the DB is the source of truth; this reinit converges live state to
    match.
    """
    from agentworks.agents.manager import AGENT_PREFIX
    from agentworks.ssh import SSHError
    from agentworks.transports import transport
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

    target = transport(vm, config)
    with keep_vm_active(db, config, vm):
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
            output.info(f"\nApplied {fixes} fix(es)")
        else:
            output.info("\nAlready up to date")


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


def _rehome_partial_state_hint(
    db: Database, ws_name: str, old_path: str, new_path: str
) -> str:
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
            s for s in sessions
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
    """Rehome a VM workspace."""

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
    _ensure_vm_running(db, config, vm)
    with keep_vm_active(db, config, vm):

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
                output.info(f"Database updated: workspace_path = {new_path}")

                # Regenerate VS Code workspace file
                vscode_path = generate_vscode_workspace(vm, config, ws_name, new_path)
                output.info(f"VS Code workspace updated: {vscode_path}")

                # Handle old directory
                if remove_old:
                    output.info(f"Removing old directory {old_path}...")
                    target.run(f"rm -rf {op}", sudo=True, timeout=60)
                    output.info("Old directory removed")
                else:
                    output.info(f"\nOld directory left in place at {old_path}")
                    output.info("Remove it manually when ready, or re-run with --remove-old")

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
                    hint=(
                        f"SSH log: {ssh_logger.path}. "
                        f"{_rehome_partial_state_hint(db, ws_name, old_path, new_path)}"
                    ),
                ) from e
        finally:
            ssh_logger.close()

        output.info(f"\nWorkspace '{ws_name}' rehomed to {new_path}")


def delete_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Delete a workspace."""

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

    # Kill running sessions (status-aware) and delete session records
    vm = db.get_vm(ws.vm_name)
    # console_pairs is populated only when we have live SSH access; the
    # post-delete cleanup is best-effort and skips when target is None.
    target: Transport | None = None
    console_pairs: list[tuple[str, str]] = []
    with contextlib.ExitStack() as _keepalive_stack:
        if vm is not None:
            _keepalive_stack.enter_context(keep_vm_active(db, config, vm))

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
            console_pairs = [
                (c.name, s.name)
                for s in sessions
                for c in db.list_consoles_for_session(s.name)
            ]
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
                    if session.pid and session.pid > 0 and force_kill_tmux_server(
                        session.pid, target=target, socket_path=session.socket_path,
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
            from agentworks.agents.manager import revoke_workspace_grants

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
    """Copy a workspace to a new VM workspace."""
    import contextlib
    import tempfile
    from pathlib import Path

    from agentworks.agents.manager import workspace_group
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
            _ensure_vm_running(db, config, src_vm)
            _keepalive_stack.enter_context(keep_vm_active(db, config, src_vm))
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
            _ensure_vm_running(db, config, dest_vm)
            if dest_vm.name != src_vm.name:
                _keepalive_stack.enter_context(keep_vm_active(db, config, dest_vm))
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

    output.info(f"Workspace '{source_name}' copied to '{dest_name}'")


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

    options = [f"{v.name}  ({v.platform})" for v in usable_vms]
    idx = output.choose("Select a VM:", options)
    return usable_vms[idx]


def _ensure_vm_running(db: Database, config: Config, vm: VMRow) -> None:
    """Auto-start a stopped/deallocated VM and verify Tailscale connectivity."""
    from agentworks.vms.manager import _ensure_tailscale, get_provisioner_for_vm

    provisioner = get_provisioner_for_vm(db, vm)
    status = provisioner.status(vm)

    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        output.info(f"VM '{vm.name}' is {status.value}. Starting...")
        # Probe + start happen BEFORE keep_vm_active because the WSL2
        # keepalive subprocess boots a stopped distro as a side effect; see
        # the matching note in vms/manager.start_vm. Tailscale verification
        # then runs inside the keepalive so a freshly booted WSL2 distro
        # doesn't idle-shut while we wait for tailscaled to come up
        # (handshake can exceed WSL2's default ~60s vmIdleTimeout).
        provisioner.start(vm)
        output.info(f"VM '{vm.name}' started")
        with keep_vm_active(db, config, vm):
            _ensure_tailscale(db, config, vm)
