"""Workspace lifecycle orchestration."""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name
from agentworks.db import InitStatus, VMStatus
from agentworks.workspaces.templates import ResolvedTemplate, resolve_template

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow


def create_workspace(
    db: Database,
    config: Config,
    *,
    name: str,
    vm_name: str | None = None,
    local: bool = False,
    template_name: str | None = None,
    open_vscode: bool = False,
) -> None:
    """Create a workspace on a VM or locally."""
    ws_name = name
    validate_name(ws_name)

    if db.get_workspace(ws_name) is not None:
        raise output.WorkspaceError(f"workspace '{ws_name}' already exists")

    # Resolve template
    template = resolve_template(config, template_name)

    if local:
        _create_local(db, config, ws_name, template_name=template.name, template=template, open_vscode=open_vscode)
    else:
        _create_vm(
            db,
            config,
            ws_name,
            vm_name=vm_name,
            template_name=template.name,
            template=template,
            open_vscode=open_vscode,
        )


def _create_local(
    db: Database,
    config: Config,
    ws_name: str,
    *,
    template_name: str,
    template: ResolvedTemplate,
    open_vscode: bool,
) -> None:
    from agentworks.workspaces.backends.local import create_local_workspace, delete_local_workspace

    workspace_path: str | None = None
    try:
        output.info(f"Creating local workspace '{ws_name}' (template: {template_name})...")
        workspace_path = create_local_workspace(config, ws_name, template)

        db.insert_workspace(ws_name, ws_type="local", workspace_path=workspace_path, template=template_name)
    except output.AgentworksError:
        if workspace_path:
            delete_local_workspace(ws_name, workspace_path)
        raise
    except Exception as e:
        if workspace_path:
            delete_local_workspace(ws_name, workspace_path)
        raise output.WorkspaceError(f"creating workspace: {e}") from None

    if open_vscode:
        subprocess.run(["code", workspace_path], check=False)

    output.info(f"Workspace '{ws_name}' created at {workspace_path}")


def _create_vm(
    db: Database,
    config: Config,
    ws_name: str,
    *,
    vm_name: str | None,
    template_name: str,
    template: ResolvedTemplate,
    open_vscode: bool,
) -> None:
    from agentworks.workspaces.backends.vm import (
        create_vm_workspace,
        delete_vm_workspace,
        generate_vscode_workspace,
    )

    vm = _resolve_vm(db, vm_name)

    _guard_vm_status(vm)

    _ensure_vm_running(db, config, vm)

    workspace_path: str | None = None
    vscode_path: str | None = None

    def _cleanup() -> None:
        if workspace_path:
            delete_vm_workspace(vm, config, ws_name, workspace_path)
        if vscode_path:
            from pathlib import Path

            Path(vscode_path).unlink(missing_ok=True)

    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(vm.name, "workspace-create")

    try:
        output.info(f"Creating workspace '{ws_name}' on VM '{vm.name}' (template: {template_name})...")
        workspace_path = create_vm_workspace(vm, config, ws_name, template, logger=ssh_logger)

        vscode_path = generate_vscode_workspace(vm, config, ws_name, workspace_path)
        output.detail(f"VS Code workspace: {vscode_path}")

        db.insert_workspace(
            ws_name,
            ws_type="vm",
            workspace_path=workspace_path,
            vm_name=vm.name,
            template=template_name,
        )
    except output.AgentworksError:
        ssh_logger.close()
        _cleanup()
        raise
    except Exception as e:
        ssh_logger.close()
        _cleanup()
        raise output.WorkspaceError(f"creating workspace: {e}\nSSH log: {ssh_logger.path}") from None

    # Add grant_all agents to the new workspace group
    grant_all_agents = db.list_agents_on_vm_with_grant_all(vm.name)
    if grant_all_agents:
        from agentworks.agents.manager import _add_to_workspace_group

        for agent in grant_all_agents:
            _add_to_workspace_group(vm, config, agent.linux_user, ws_name, logger=ssh_logger)
            db.insert_agent_grant(agent.name, ws_name, "explicit")
        output.detail(f"Added {len(grant_all_agents)} grant-all agent(s) to workspace")

    ssh_logger.close()

    if open_vscode:
        subprocess.run(["code", vscode_path], check=False)

    output.info(f"Workspace '{ws_name}' created")


def shell_workspace(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Open a plain shell into a workspace."""
    ws = db.get_workspace(name)
    if ws is None:
        raise output.WorkspaceError(f"workspace '{name}' not found")

    if ws.type == "local":
        from agentworks.workspaces.backends.local import shell_local_workspace

        db.update_workspace_last_seen(name)
        shell_local_workspace(ws.workspace_path)
    elif ws.type == "vm":
        vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
        if vm is None:
            raise output.VMError(f"VM '{ws.vm_name}' not found")

        _guard_vm_status(vm)
        _ensure_vm_running(db, config, vm)
        db.update_workspace_last_seen(name)

        from agentworks.workspaces.backends.vm import shell_vm_workspace

        shell_vm_workspace(vm, config, ws.workspace_path)
    else:
        raise output.WorkspaceError(f"unknown workspace type '{ws.type}'")


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

    if os.environ.get("TMUX") and not allow_nesting:
        raise output.WorkspaceError(
            "already inside a tmux session.\n"
            "Nesting is not recommended (prefix key conflicts,\n"
            "confusing detach behavior).\n"
            "Pass --allow-nesting to override."
        )

    ws = db.get_workspace(name)
    if ws is None:
        raise output.WorkspaceError(f"workspace '{name}' not found")

    if ws.type == "local":
        from agentworks.workspaces.backends.local import console_local_workspace

        db.update_workspace_last_seen(name)
        console_local_workspace(name, recreate=recreate)
    elif ws.type == "vm":
        vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
        if vm is None:
            raise output.VMError(f"VM '{ws.vm_name}' not found")

        _guard_vm_status(vm)
        _ensure_vm_running(db, config, vm)
        db.update_workspace_last_seen(name)

        from agentworks.workspaces.backends.vm import console_vm_workspace

        console_vm_workspace(vm, config, name, recreate=recreate)
    else:
        raise output.WorkspaceError(f"unknown workspace type '{ws.type}'")


def describe_workspace(
    db: Database,
    name: str,
) -> None:
    """Show workspace details."""
    ws = db.get_workspace(name)
    if ws is None:
        raise output.WorkspaceError(f"workspace '{name}' not found")

    output.info(f"Name:       {ws.name}")
    output.info(f"Type:       {ws.type}")
    output.info(f"VM:         {ws.vm_name or '-'}")
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
            output.detail(f"{s.name}  [{s.template}]  {s.status}  {mode_label}")
    else:
        output.detail("(none)")

    # Agents with grants (VM workspaces only)
    if ws.type == "vm" and ws.vm_name:
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
    vm_name: str | None = None,
    ws_type: str | None = None,
) -> None:
    """List workspaces."""
    workspaces = db.list_workspaces(vm_name=vm_name, ws_type=ws_type)
    if not workspaces:
        output.info("No workspaces found.")
        return

    def _tpl_name(t: str | None) -> str:
        if t is None or t == "(built-in)":
            return "default"
        return t

    rows = [(ws.name, ws.type, ws.vm_name or "-", _tpl_name(ws.template), ws.created_at) for ws in workspaces]

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    type_w = max(len("TYPE"), max(len(r[1]) for r in rows))
    vm_w = max(len("VM"), max(len(r[2]) for r in rows))
    tpl_w = max(len("TEMPLATE"), max(len(r[3]) for r in rows))

    header = f"{'NAME':<{name_w}}  {'TYPE':<{type_w}}  {'VM':<{vm_w}}  {'TEMPLATE':<{tpl_w}}  CREATED"
    output.info(header)
    output.info("-" * len(header))
    for ws_name, ws_type, vm_name, tpl, created in rows:
        output.info(f"{ws_name:<{name_w}}  {ws_type:<{type_w}}  {vm_name:<{vm_w}}  {tpl:<{tpl_w}}  {created}")


def repair_workspace(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Repair workspace infrastructure: group, permissions, ACLs, agent access."""
    from agentworks.agents.manager import AGENT_PREFIX, WS_GROUP_PREFIX
    from agentworks.ssh import SSHError, admin_exec_target, run_as_root

    ws = db.get_workspace(name)
    if ws is None:
        raise output.WorkspaceError(f"workspace '{name}' not found")

    if ws.type != "vm":
        raise output.WorkspaceError(f"workspace '{name}' is local, nothing to repair")

    assert ws.vm_name is not None
    vm = db.get_vm(ws.vm_name)
    if vm is None:
        raise output.VMError(f"VM '{ws.vm_name}' not found")

    target = admin_exec_target(vm, config)
    ws_group = f"{WS_GROUP_PREFIX}{name}"
    fixes = 0

    output.info(f"Repairing workspace '{name}' on VM '{vm.name}'...")

    # 0. Ensure acl package is installed (needed for setfacl)
    try:
        has_setfacl = run_as_root(target, "which setfacl", check=False)
        if not has_setfacl.ok:
            run_as_root(target, "apt-get install -y -qq acl", timeout=60)
            output.detail("Fixed: installed acl package")
            fixes += 1
        else:
            output.detail("OK: acl package")
    except SSHError as e:
        output.warn(f"acl package check failed: {e}")

    # 1. Ensure workspace group exists (with correct naming)
    try:
        # Check for old-style group and rename if needed
        old_group = f"ws-{name}"
        old_exists = run_as_root(target, f"getent group {old_group}", check=False)
        new_exists = run_as_root(target, f"getent group {ws_group}", check=False)

        if old_exists.ok and not new_exists.ok:
            run_as_root(target, f"groupmod -n {ws_group} {old_group}")
            output.detail(f"Fixed: renamed group {old_group} -> {ws_group}")
            fixes += 1
        elif not new_exists.ok:
            run_as_root(
                target,
                f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'",
            )
            output.detail(f"Fixed: created group {ws_group}")
            fixes += 1
        else:
            output.detail(f"OK: group {ws_group} exists")
    except SSHError as e:
        output.warn(f"group check failed: {e}")

    # 2. Ensure admin is in the group
    try:
        in_group = run_as_root(
            target,
            f"id -nG {vm.admin_username}",
            check=False,
        )
        if in_group.ok and ws_group not in in_group.stdout.split():
            run_as_root(target, f"usermod -aG {ws_group} {vm.admin_username}")
            output.detail(f"Fixed: added admin '{vm.admin_username}' to {ws_group}")
            fixes += 1
        else:
            output.detail(f"OK: admin in {ws_group}")
    except SSHError as e:
        output.warn(f"admin group check failed: {e}")

    # 3. Fix directory permissions (recursive chgrp so ACLs apply correctly)
    try:
        run_as_root(target, f"chown -R {vm.admin_username}:{ws_group} {ws.workspace_path}", timeout=120)
        run_as_root(target, f"chmod 2770 {ws.workspace_path}")
        # Set SGID on all subdirectories so new files inherit the workspace group.
        # This is critical for atomic-write tools (including Claude Code) that
        # create a temp file and rename it over the original.
        run_as_root(
            target,
            f"find {shlex.quote(ws.workspace_path)} -type d -exec chmod g+s {{}} +",
            timeout=120,
        )
        output.detail("OK: directory ownership and permissions")
    except SSHError as e:
        output.warn(f"permission fix failed: {e}")

    # 4. Fix ACLs
    # Default ACLs only apply to directories; use find to avoid warnings on files.
    # Effective ACLs apply to all entries and should not produce output.
    try:
        run_as_root(
            target,
            f"find {ws.workspace_path} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
            timeout=120,
        )
        run_as_root(
            target,
            f"setfacl -R -m g::rwx -m m::rwx {ws.workspace_path}",
            timeout=120,
        )
        output.detail("OK: ACLs")
    except SSHError as e:
        output.warn(f"ACL fix failed: {e}")

    # 5. Fix parent directory traversal
    try:
        run_as_root(
            target,
            f'sh -c \'p={ws.workspace_path}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done\'',
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

    # Get agents that ARE in the group (only agt-- prefixed users)
    try:
        group_info = run_as_root(target, f"getent group {ws_group}", check=False)
        current_members: set[str] = set()
        if group_info.ok and ":" in group_info.stdout:
            members_str = group_info.stdout.strip().split(":")[-1]
            if members_str:
                current_members = {m for m in members_str.split(",") if m.startswith(AGENT_PREFIX)}

        # Add missing agents
        to_add = granted_agents - current_members
        for user in sorted(to_add):
            run_as_root(target, f"usermod -aG {ws_group} {user}")
            output.detail(f"Fixed: added {user} to {ws_group}")
            fixes += 1

        # Remove agents that shouldn't be there
        to_remove = current_members - granted_agents
        for user in sorted(to_remove):
            run_as_root(target, f"gpasswd -d {user} {ws_group}", check=False)
            output.detail(f"Fixed: removed {user} from {ws_group}")
            fixes += 1

        if not to_add and not to_remove:
            output.detail(f"OK: agent group membership ({len(current_members)} agent(s))")
    except SSHError as e:
        output.warn(f"agent membership check failed: {e}")

    if fixes > 0:
        output.info(f"\nRepaired {fixes} issue(s)")
    else:
        output.info("\nNo issues found")


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
        raise output.WorkspaceError(f"workspace '{name}' not found")

    # Determine target path
    if target_path is not None:
        new_path = target_path
    elif ws.type == "vm":
        new_path = f"{config.paths.vm_workspaces}/{name}"
    else:
        new_path = str(config.paths.local_workspaces / name)

    old_path = ws.workspace_path

    if old_path == new_path:
        output.info(f"Workspace '{name}' is already at {new_path}")
        return

    # Safety: detect overlapping paths
    old_norm = old_path.rstrip("/") + "/"
    new_norm = new_path.rstrip("/") + "/"
    if new_norm.startswith(old_norm) or old_norm.startswith(new_norm):
        raise output.WorkspaceError("source and target paths overlap")

    # Block if workspace has running sessions
    from agentworks.db import SessionStatus

    sessions = db.list_sessions(workspace_name=name)
    running = [s for s in sessions if s.status == SessionStatus.RUNNING.value]
    if running:
        raise output.WorkspaceError(
            f"workspace '{name}' has {len(running)} running session(s). "
            "Stop them first with 'agentworks session stop'."
        )

    if ws.type == "vm":
        _rehome_vm(db, config, ws, new_path, remove_old=remove_old, yes=yes)
    elif ws.type == "local":
        _rehome_local(db, config, ws, new_path, remove_old=remove_old, yes=yes)
    else:
        raise output.WorkspaceError(f"unknown workspace type '{ws.type}'")


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

    from agentworks.agents.manager import WS_GROUP_PREFIX
    from agentworks.ssh import SSHError, SSHLogger, admin_exec_target, run_as_root
    from agentworks.ssh import run as ssh_run
    from agentworks.workspaces.backends.vm import generate_vscode_workspace

    ws_name = ws.name
    old_path = ws.workspace_path
    assert ws.vm_name is not None
    vm_name = ws.vm_name

    vm = db.get_vm(vm_name)
    if vm is None:
        raise output.VMError(f"VM '{vm_name}' not found")

    _guard_vm_status(vm)
    _ensure_vm_running(db, config, vm)

    target = admin_exec_target(vm, config)

    # Verify source exists
    src_check = ssh_run(target, f"test -d {old_path}", check=False, timeout=10)
    if not src_check.ok:
        raise output.WorkspaceError(f"source directory {old_path} does not exist on VM")

    # Verify target does not exist
    dst_check = ssh_run(target, f"test -d {new_path}", check=False, timeout=10)
    if dst_check.ok:
        raise output.WorkspaceError(f"target directory {new_path} already exists on VM")

    if not yes:
        output.info(f"Rehome workspace '{ws_name}':")
        output.detail(f"From: {old_path}")
        output.detail(f"To:   {new_path}")
        if remove_old:
            output.detail("Old directory will be REMOVED after copy")
        else:
            output.detail("Old directory will be LEFT IN PLACE")
        if not output.confirm("Proceed?"):
            raise output.UserAbort("rehome cancelled")

    ssh_logger = SSHLogger(vm.name, "workspace-rehome")
    ws_group = f"{WS_GROUP_PREFIX}{ws_name}"

    try:
        # Create target directory as root and chown to admin so rsync can write
        run_as_root(target, f"mkdir -p {new_path}", logger=ssh_logger)
        run_as_root(target, f"chown {vm.admin_username} {new_path}", logger=ssh_logger)

        # Copy with rsync (fall back to cp -a)
        output.info("Copying workspace...")
        has_rsync = ssh_run(target, "which rsync", check=False, timeout=10, logger=ssh_logger)
        if has_rsync.ok:
            ssh_run(target, f"rsync -a {old_path}/ {new_path}/", timeout=600, logger=ssh_logger)
        else:
            run_as_root(target, f"cp -a {old_path}/. {new_path}/", timeout=600, logger=ssh_logger)

        # Verify copy succeeded
        verify = ssh_run(target, f"test -d {new_path}", check=False, timeout=10, logger=ssh_logger)
        if not verify.ok:
            ssh_logger.close()
            raise output.WorkspaceError(
                f"copy verification failed, target directory not found\nSSH log: {ssh_logger.path}"
            )

        # Fix ownership, permissions, and ACLs on the new path
        output.info("Setting permissions...")
        run_as_root(target, f"chown {vm.admin_username}:{ws_group} {new_path}", logger=ssh_logger)
        run_as_root(target, f"chmod 2770 {new_path}", logger=ssh_logger)
        sgid_cmd = f"find {shlex.quote(new_path)} -type d -exec chmod g+s {{}} +"
        run_as_root(target, sgid_cmd, timeout=120, logger=ssh_logger)
        try:
            run_as_root(
                target,
                f"find {new_path} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
                timeout=120,
                logger=ssh_logger,
            )
            run_as_root(
                target,
                f"setfacl -R -m g::rwx -m m::rwx {new_path}",
                timeout=120,
                logger=ssh_logger,
            )
        except SSHError as e:
            output.warn(f"ACL setup failed: {e}")

        # Fix parent directory traversal
        run_as_root(
            target,
            f'sh -c \'p={new_path}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done\'',
            logger=ssh_logger,
        )

        # Regenerate tmuxinator config at new path
        from agentworks.ssh import write_file
        from agentworks.workspaces.tmuxinator import console_session_name, generate_config

        tmux_config = generate_config(ws_name, new_path)
        write_file(target, f"{new_path}/.tmuxinator.yml", tmux_config, logger=ssh_logger)
        session = console_session_name(ws_name)
        ssh_run(target, "mkdir -p ~/.config/tmuxinator", timeout=10, logger=ssh_logger)
        ssh_run(
            target,
            f"ln -sf {new_path}/.tmuxinator.yml ~/.config/tmuxinator/{session}.yml",
            timeout=10,
            logger=ssh_logger,
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
            run_as_root(target, f"rm -rf {old_path}", timeout=60, logger=ssh_logger)
            output.info("Old directory removed")
        else:
            output.info(f"\nOld directory left in place at {old_path}")
            output.info("Remove it manually when ready, or re-run with --remove-old")

    except output.AgentworksError:
        ssh_logger.close()
        raise
    except Exception as e:
        ssh_logger.close()
        raise output.WorkspaceError(
            f"during rehome: {e}\n"
            f"SSH log: {ssh_logger.path}\n"
            "The database was NOT updated. The workspace is still at the original path."
        ) from None

    ssh_logger.close()
    output.info(f"\nWorkspace '{ws_name}' rehomed to {new_path}")


def _rehome_local(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
    new_path: str,
    *,
    remove_old: bool,
    yes: bool,
) -> None:
    """Rehome a local workspace."""
    import shutil
    from pathlib import Path


    ws_name = ws.name
    old_path = ws.workspace_path

    old_dir = Path(old_path)
    new_dir = Path(new_path)

    if not old_dir.exists():
        raise output.WorkspaceError(f"source directory {old_path} does not exist")

    if new_dir.exists():
        raise output.WorkspaceError(f"target directory {new_path} already exists")

    if not yes:
        output.info(f"Rehome workspace '{ws_name}':")
        output.detail(f"From: {old_path}")
        output.detail(f"To:   {new_path}")
        if remove_old:
            output.detail("Old directory will be REMOVED after copy")
        else:
            output.detail("Old directory will be LEFT IN PLACE")
        if not output.confirm("Proceed?"):
            raise output.UserAbort("rehome cancelled")

    # Copy
    output.info("Copying workspace...")
    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(old_path, new_path, symlinks=True)

    # Verify
    if not new_dir.exists():
        raise output.WorkspaceError("copy verification failed, target directory not found")

    # Regenerate tmuxinator config at new path
    from agentworks.workspaces.tmuxinator import console_session_name, generate_config

    tmux_file = new_dir / ".tmuxinator.yml"
    if tmux_file.exists() or (old_dir / ".tmuxinator.yml").exists():
        tmux_config = generate_config(ws_name, new_path)
        tmux_file.write_text(tmux_config)
        session = console_session_name(ws_name)
        tmux_config_dir = Path.home() / ".config" / "tmuxinator"
        tmux_config_dir.mkdir(parents=True, exist_ok=True)
        link = tmux_config_dir / f"{session}.yml"
        link.unlink(missing_ok=True)
        link.symlink_to(tmux_file)

    # Update database
    db.update_workspace_path(ws_name, new_path)
    output.info(f"Database updated: workspace_path = {new_path}")

    # Handle old directory
    if remove_old:
        output.info(f"Removing old directory {old_path}...")
        shutil.rmtree(old_path)
        output.info("Old directory removed")
    else:
        output.info(f"\nOld directory left in place at {old_path}")
        output.info("Remove it manually when ready, or re-run with --remove-old")

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
        raise output.WorkspaceError(f"workspace '{name}' not found")

    # Check for sessions
    session_count = len(db.list_sessions(workspace_name=name))
    if session_count > 0 and not force:
        raise output.WorkspaceError(
            f"workspace '{name}' has {session_count} session(s). Delete them first, or use --force."
        )

    if not yes:
        msg = f"Delete workspace '{name}'?"
        if session_count > 0:
            msg += f" ({session_count} session(s) will also be deleted)"
        if not output.confirm(msg):
            raise output.UserAbort("delete cancelled")

    # Create SSH logger for VM operations
    ssh_logger = None
    if ws.type == "vm" and ws.vm_name:
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(ws.vm_name, "workspace-delete")

    # Kill running sessions and delete session records
    if ws.type == "vm" and ws.vm_name:
        vm = db.get_vm(ws.vm_name)
        if vm is not None and vm.tailscale_host is not None:
            from functools import partial

            from agentworks.sessions.manager import _effective_socket_path
            from agentworks.sessions.tmux import force_kill_session, kill_session
            from agentworks.ssh import admin_exec_target, run

            target = admin_exec_target(vm, config, logger=ssh_logger)
            run_command = partial(run, target, logger=ssh_logger)
            for session in db.list_sessions(workspace_name=name):
                sock = _effective_socket_path(db, session)
                killed = kill_session(session.name, run_command=run_command, socket_path=sock)
                if not killed and sock:
                    force_kill_session(target, session.name, sock)
    db.delete_sessions_for_workspace(name)

    # Revoke agent workspace grants (agents are VM-scoped, not deleted with workspaces)
    if ws.type == "vm" and ws.vm_name:
        vm_for_grants = db.get_vm(ws.vm_name)
        if vm_for_grants:
            from agentworks.agents.manager import revoke_workspace_grants

            revoke_workspace_grants(db, config, name, vm_for_grants)

    if ws.type == "local":
        from agentworks.workspaces.backends.local import delete_local_workspace

        delete_local_workspace(name, ws.workspace_path)
    elif ws.type == "vm" and ws.vm_name:
        vm = db.get_vm(ws.vm_name)
        if vm is not None:
            from agentworks.workspaces.backends.vm import delete_vm_workspace

            delete_vm_workspace(vm, config, name, ws.workspace_path, logger=ssh_logger)

    if ssh_logger is not None:
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
    local: bool = False,
) -> None:
    """Copy a workspace to a new location."""
    import tempfile
    from pathlib import Path

    from agentworks.ssh import admin_exec_target

    validate_name(dest_name)

    src_ws = db.get_workspace(source_name)
    if src_ws is None:
        raise output.WorkspaceError(f"workspace '{source_name}' not found")

    if db.get_workspace(dest_name) is not None:
        raise output.WorkspaceError(f"workspace '{dest_name}' already exists")

    # Create a temp file for the archive
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        # --- Pack from source ---
        if src_ws.type == "local":
            output.info(f"Packing workspace '{source_name}'...")
            result = subprocess.run(
                ["tar", "czf", str(tmp_path), "-C", src_ws.workspace_path, "."],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                raise output.WorkspaceError(f"tar failed: {result.stderr.strip()}")
        elif src_ws.type == "vm":
            src_vm = db.get_vm(src_ws.vm_name)  # type: ignore[arg-type]
            if src_vm is None:
                raise output.VMError(f"VM '{src_ws.vm_name}' not found")
            _guard_vm_status(src_vm)
            _ensure_vm_running(db, config, src_vm)
            if src_vm.tailscale_host is None:
                raise output.VMError(f"VM '{src_vm.name}' has no Tailscale address")

            src_exec = admin_exec_target(src_vm, config)
            assert src_exec.ssh is not None
            src_ssh = src_exec.ssh
            output.info(f"Packing workspace '{source_name}' from VM '{src_vm.name}'...")

            # Stream tar from VM to local temp file
            ssh_args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
            if src_ssh.identity_file is not None:
                ssh_args.extend(["-i", str(src_ssh.identity_file)])
            ssh_args.append(f"{src_ssh.user}@{src_ssh.host}")
            ssh_args.append(f"tar czf - -C {src_ws.workspace_path} .")

            with open(tmp_path, "wb") as f:
                proc = subprocess.run(ssh_args, stdout=f, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                stderr = proc.stderr.decode() if proc.stderr else ""
                raise output.WorkspaceError(f"pack failed: {stderr.strip()}")
        else:
            raise output.WorkspaceError(f"unknown workspace type '{src_ws.type}'")

        # --- Unpack to destination ---
        if local:
            workspace_path = str(config.paths.local_workspaces / dest_name)
            Path(workspace_path).mkdir(parents=True, exist_ok=True)

            output.info(f"Unpacking to local workspace '{dest_name}'...")
            result = subprocess.run(
                ["tar", "xzf", str(tmp_path), "-C", workspace_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                raise output.WorkspaceError(f"tar failed: {result.stderr.strip()}")

            db.insert_workspace(
                dest_name,
                ws_type="local",
                workspace_path=workspace_path,
                template="copied",
            )
        else:
            from agentworks.ssh import SSHLogger, copy_to, run, run_as_root

            dest_vm = _resolve_vm(db, vm_name)
            _guard_vm_status(dest_vm)
            _ensure_vm_running(db, config, dest_vm)
            if dest_vm.tailscale_host is None:
                raise output.VMError(f"VM '{dest_vm.name}' has no Tailscale address")

            lg = SSHLogger(dest_vm.name, "workspace-copy")
            dest_target = admin_exec_target(dest_vm, config)
            workspace_path = f"{config.paths.vm_workspaces}/{dest_name}"

            ws_group = f"ws--{dest_name}"

            output.info(f"Unpacking to workspace '{dest_name}' on VM '{dest_vm.name}'...")

            # Set up group, directory, and permissions (same as create_vm_workspace)
            run_as_root(
                dest_target,
                f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'",
                logger=lg,
            )
            run_as_root(dest_target, f"usermod -aG {ws_group} {dest_vm.admin_username}", logger=lg)
            run_as_root(dest_target, f"mkdir -p {workspace_path}", timeout=10, logger=lg)
            run_as_root(dest_target, f"chown {dest_vm.admin_username}:{ws_group} {workspace_path}", logger=lg)
            run_as_root(dest_target, f"chmod 2770 {workspace_path}", logger=lg)
            run_as_root(dest_target, f"setfacl -d -m g::rwx -m m::rwx {workspace_path}", logger=lg)

            # Unpack archive and fix ownership
            remote_tmp = f"/tmp/{dest_name}-copy.tgz"
            copy_to(dest_target, tmp_path, remote_tmp, timeout=300)
            run_as_root(dest_target, f"tar xzf {remote_tmp} -C {workspace_path}", timeout=120, logger=lg)
            run(dest_target, f"rm -f {remote_tmp}", check=False, timeout=10, logger=lg)
            run_as_root(
                dest_target,
                f"chown -R {dest_vm.admin_username}:{ws_group} {workspace_path}",
                timeout=60,
                logger=lg,
            )
            run_as_root(
                dest_target,
                f"find {shlex.quote(workspace_path)} -type d -exec chmod g+s {{}} +",
                timeout=120,
                logger=lg,
            )

            db.insert_workspace(
                dest_name,
                ws_type="vm",
                vm_name=dest_vm.name,
                workspace_path=workspace_path,
                template="copied",
            )

            # Generate tmuxinator config and VS Code workspace
            from agentworks.ssh import write_file
            from agentworks.workspaces.backends.vm import generate_vscode_workspace
            from agentworks.workspaces.tmuxinator import console_session_name, generate_config

            tmux_config = generate_config(dest_name, workspace_path)
            write_file(dest_target, f"{workspace_path}/.tmuxinator.yml", tmux_config, logger=lg)
            session = console_session_name(dest_name)
            run(dest_target, "mkdir -p ~/.config/tmuxinator", timeout=10, logger=lg)
            run(
                dest_target,
                f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{session}.yml",
                timeout=10,
                logger=lg,
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
            raise output.VMError(
                f"VM '{vm.name}' is in 'failed' state. Run 'vm delete' and recreate."
            )
        else:
            raise output.VMError(
                f"VM '{vm.name}' initialization is not complete (status: {vm.init_status})."
            )


def _resolve_vm(db: Database, vm_name: str | None) -> VMRow:
    """Resolve which VM to use: explicit, auto-select if 1, or error."""
    if vm_name is not None:
        vm = db.get_vm(vm_name)
        if vm is None:
            raise output.VMError(f"VM '{vm_name}' not found")
        return vm

    vms = db.list_vms()
    usable_statuses = {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}
    usable_vms = [v for v in vms if v.init_status in usable_statuses]

    if len(usable_vms) == 0:
        raise output.VMError("no VMs available. Create one with 'agentworks vm create'.")

    if len(usable_vms) == 1:
        output.info(f"Using VM '{usable_vms[0].name}'")
        return usable_vms[0]

    options = [f"{v.name}  ({v.platform})" for v in usable_vms]
    idx = output.choose("Select a VM:", options)
    return usable_vms[idx]


def _ensure_vm_running(db: Database, config: Config, vm: VMRow) -> None:
    """Auto-start a stopped/deallocated VM and verify Tailscale connectivity."""
    from agentworks.vms.manager import _ensure_tailscale, _get_provisioner_for_vm

    provisioner = _get_provisioner_for_vm(db, vm)
    status = provisioner.status(vm)

    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        output.info(f"VM '{vm.name}' is {status.value}. Starting...")
        provisioner.start(vm)
        output.info(f"VM '{vm.name}' started")
        _ensure_tailscale(db, config, vm, provisioner)
