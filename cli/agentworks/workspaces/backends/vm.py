"""VM workspace backend -- operations via SSH to a VM."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import AlreadyExistsError
from agentworks.transports import transport
from agentworks.workspaces.acls import apply_workspace_acls
from agentworks.workspaces.tmuxinator import console_session_name, generate_config

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.ssh import SSHLogger
    from agentworks.workspaces.templates import ResolvedTemplate


def default_workspace_path(config: Config, ws_name: str) -> str:
    """The VM-side path a workspace named ``ws_name`` is created at:
    ``paths.vm_workspaces/<name>``. The single home of that convention,
    shared by the create/copy backends and the pending workspace node
    (which must know the path before the row exists); the ROW stays the
    source of truth for a live workspace (``workspace rehome`` can move
    one anywhere)."""
    return f"{config.paths.vm_workspaces}/{ws_name}"


def create_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    template: ResolvedTemplate,
    *,
    logger: SSHLogger | None = None,
) -> str:
    """Create a workspace on a VM. Returns the remote workspace path.

    Errors if the workspace directory already exists on the VM.

    Self-cleaning: the group and directory are created early, then the
    clone and configuration follow. A failure anywhere after those
    resources exist (most often the git clone) tears down exactly what
    this function made and re-raises the ORIGINAL error, so a failed
    create leaves no residue on the VM (issue #262). ``realize._cleanup``
    is gated on this function RETURNING, so it never covers a mid-create
    failure; this teardown is the only one for that window.
    """
    from agentworks.agents.grants import workspace_group

    assert vm.tailscale_host is not None
    target = transport(vm, config, logger=logger)

    workspace_path = default_workspace_path(config, ws_name)
    ws_group = workspace_group(ws_name)

    # Refuse to create if directory already exists
    exists = target.run(f"test -d {workspace_path}", check=False, timeout=10)
    if exists.ok:
        raise AlreadyExistsError(
            f"directory {workspace_path} already exists on the VM.",
            entity_kind="workspace",
            entity_name=ws_name,
            hint=(f"Remove it manually (ssh to the VM and 'sudo rm -rf {workspace_path}') or choose a different name."),
        )

    # Everything below creates on-VM resources (the group, then the
    # directory and its contents). If any step fails, tear down exactly
    # what was made and re-raise the ORIGINAL error so a failed create
    # leaves no residue (issue #262). The existence precheck above is
    # deliberately outside this block: an AlreadyExistsError means the
    # directory is NOT ours, so it must never be swept up by the teardown.
    try:
        # Create workspace group (idempotent), add admin, and set up directory with setgid
        target.run(f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'", sudo=True)
        target.run(f"usermod -aG {ws_group} {vm.admin_username}", sudo=True)
        target.run(f"mkdir -p {workspace_path}", sudo=True)
        target.run(f"chown {vm.admin_username}:{ws_group} {workspace_path}", sudo=True)
        target.run(f"chmod 2770 {workspace_path}", sudo=True)
        # The canonical workspace ACL is applied at the end (apply_workspace_acls),
        # after any clone and other content is placed, so existing entries are
        # covered too. This is the same recursive spec workspace repair applies,
        # so a first repair of this workspace is a true no-op.

        # Git clone if repo is set
        if template.repo:
            output.info(f"Cloning {template.repo}...")
            try:
                import shlex

                # `--` stops option parsing so a repo URL beginning with `-` can
                # never be read as a git flag; both operands are quoted for spaces.
                target.run(
                    f"git clone -- {shlex.quote(template.repo)} {shlex.quote(workspace_path)}",
                    timeout=300,
                )

                # Stamp the checkout with its configured git identity so commits
                # made here are attributed correctly. This is repo-local config
                # (the checkout's own .git/config), so it is actor-agnostic: any
                # agent, the admin, or a human over VS Code Remote picks it up,
                # and it overrides any per-user global identity. Identity is only
                # meaningful for a repo-backed workspace, so it rides the clone.
                for git_key, value in (
                    ("user.name", template.git_user_name),
                    ("user.email", template.git_user_email),
                ):
                    if value:
                        # --local is explicit so the write can only ever land in
                        # the checkout's .git/config, never the admin's global
                        # ~/.gitconfig (git config defaults to global outside a repo).
                        target.run(
                            f"git -C {shlex.quote(workspace_path)} config --local {git_key} {shlex.quote(value)}"
                        )

                # Ensure cloned files inherit the workspace group and subdirectories
                # have SGID so new files (including atomic writes) get the right group
                target.run(f"chgrp -R {ws_group} {shlex.quote(workspace_path)}", sudo=True)
                sgid_cmd = f"find {shlex.quote(workspace_path)} -type d -exec chmod g+s {{}} +"
                target.run(sgid_cmd, sudo=True, timeout=120)
            except Exception:
                if template.repo.startswith("git@"):
                    output.warn(
                        "Hint: SSH repo URLs are not supported. Use HTTPS URLs "
                        "and declare the needed git credential on this user template, then reinit."
                    )
                else:
                    output.warn(
                        "Hint: for private repos, declare the needed git credential on this user template, then reinit."
                    )
                raise

        # Tmuxinator config (no tasks yet at workspace creation time)
        if template.tmuxinator:
            tmux_config = generate_config(ws_name, workspace_path)
            target.write_file(f"{workspace_path}/.tmuxinator.yml", tmux_config)
            # Symlink so tmuxinator can find it by console session name
            session = console_session_name(ws_name)
            target.run("mkdir -p ~/.config/tmuxinator", timeout=10)
            target.run(
                f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{session}.yml",
                timeout=10,
            )

        # Canonical workspace ACL, applied last so every entry written above (a
        # clone, the tmuxinator config) is covered, and future entries inherit via
        # the default ACL. Shared with workspace repair so the two never drift and
        # a first repair of this workspace is a no-op.
        apply_workspace_acls(target, workspace_path)
    except Exception:
        # Best-effort teardown of exactly what was created above: the same
        # primitives the delete path uses (rm -rf the directory, groupdel the
        # group), so partially-created state is tolerated (a failure at
        # groupadd itself means the directory does not exist yet; rm -rf and
        # groupdel check=False both no-op on the missing pieces). A cleanup
        # failure must never mask the original create error, so it is caught
        # and surfaced as a warning while the original propagates.
        try:
            delete_vm_workspace(vm, config, ws_name, workspace_path, ws_group, logger=logger)
        except Exception as cleanup_err:
            output.warn(
                f"cleanup after failed workspace create '{ws_name}' failed: {cleanup_err}. "
                f"VM may have a residual directory ({workspace_path}) or group ({ws_group})."
            )
        raise

    return workspace_path


def delete_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    workspace_path: str,
    linux_group: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Delete a workspace from a VM.

    Tears down what :func:`create_vm_workspace` set up: the directory,
    the tmuxinator symlink, and the workspace's Linux group. ``groupadd``
    at create time has a symmetric ``groupdel`` here, so a deleted
    workspace leaves no residual group on the VM (issue #249). Callers
    pass the recorded ``linux_group`` rather than re-deriving it, so
    legacy workspaces (older ``ws--`` prefix) drop the group they
    actually own.

    Group removal runs last, once the directory is gone and after the
    caller has removed the agent members; ``groupdel`` removes the group
    regardless of remaining supplementary members (the admin's included).
    """
    from agentworks.ssh import SSHError

    assert vm.tailscale_host is not None
    target = transport(vm, config, logger=logger)

    try:
        target.run(f"rm -rf {workspace_path}", sudo=True, timeout=30)
        session = console_session_name(ws_name)
        target.run(f"rm -f ~/.config/tmuxinator/{session}.yml", check=False, timeout=10)
        # Remove the workspace's Linux group. check=False so a group that
        # is already gone (or, defensively, one groupdel refuses to remove)
        # is tolerated rather than failing the delete, mirroring the
        # best-effort group-membership teardown in agents/grants.py.
        target.run(f"/usr/sbin/groupdel {linux_group}", sudo=True, check=False, timeout=10)
    except SSHError as e:
        output.warn(f"remote cleanup failed: {e}")


def generate_vscode_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    workspace_path: str,
) -> Path:
    """Generate a .code-workspace file for VS Code SSH Remote.

    Returns the host path of the file written. It used to return ``str``,
    which forced every caller that reports it to an operator to re-wrap it
    to render the path the standard home-relative way.
    """
    from agentworks.ssh_config import ssh_host_alias

    # Use the SSH config alias so VS Code picks up the right host/user/key
    ssh_host = ssh_host_alias(vm.name, config.operator.ssh_host_prefix)

    ws_file = {
        "folders": [
            {
                "uri": f"vscode-remote://ssh-remote+{ssh_host}{workspace_path}",
            }
        ],
        "remoteAuthority": f"ssh-remote+{ssh_host}",
    }

    vscode_dir = config.paths.vscode_workspaces
    vscode_dir.mkdir(parents=True, exist_ok=True)
    vscode_path = vscode_dir / f"{ws_name}.code-workspace"
    vscode_path.write_text(json.dumps(ws_file, indent=2) + "\n")

    return vscode_path
