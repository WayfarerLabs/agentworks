"""VM workspace backend using SSH to a VM."""

from __future__ import annotations

import json
import shlex
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from uuid import uuid4

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

    Reserve the final directory before creating its group. Clone into a private,
    unique sibling directory and publish only a confirmed successful clone, so
    an interrupted guest Git process cannot affect a later use of the name.
    Failures reclaim confirmed resources and preserve the original error.
    An unconfirmed reservation/publication or newly occupied reservation can
    leave residue for inspection; retry never adopts it implicitly.
    """
    from agentworks.agents.grants import workspace_group

    assert vm.tailscale_host is not None
    target = transport(vm, config, logger=logger)

    workspace_path = default_workspace_path(config, ws_name)
    quoted_workspace = shlex.quote(workspace_path)
    ws_group = workspace_group(ws_name)

    # Refuse to create if directory already exists
    exists = target.run(
        f"test -d {quoted_workspace} || test -e {quoted_workspace} || test -L {quoted_workspace}",
        check=False,
        timeout=10,
    )
    if exists.ok:
        raise AlreadyExistsError(
            f"workspace path {workspace_path!r} already exists on the VM.",
            entity_kind="workspace",
            entity_name=ws_name,
            hint="Inspect and preserve its contents before removing it, or choose a different workspace name.",
        )

    # Reserve exclusively before the cleanup region: a raced-in path or a lost
    # response is not confirmation that the directory belongs to this attempt.
    parent = shlex.quote(str(PurePosixPath(workspace_path).parent))
    target.run(f"mkdir -p -- {parent}", sudo=True)
    try:
        target.run(f"mkdir -- {quoted_workspace}", sudo=True)
    except BaseException:
        output.warn(
            f"Workspace directory reservation at {workspace_path!r} was not confirmed. "
            "Any remaining path was left untouched; inspect and preserve its contents before retrying."
        )
        raise
    attempt: str | None = None
    clone_published = False
    try:
        # Create workspace group (idempotent), add admin, and set up directory with setgid
        target.run(f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'", sudo=True)
        target.run(f"usermod -aG {ws_group} {vm.admin_username}", sudo=True)
        target.run(f"chown {vm.admin_username}:{ws_group} {quoted_workspace}", sudo=True)
        target.run(f"chmod 2770 {quoted_workspace}", sudo=True)
        # The canonical workspace ACL is applied at the end (apply_workspace_acls),
        # after any clone and other content is placed, so existing entries are
        # covered too. This is the same recursive spec workspace repair applies,
        # so a first repair of this workspace is a true no-op.

        # Git clone if repo is set
        if template.repo:
            output.info(f"Cloning {template.repo}...")
            candidate = f"{workspace_path}.agentworks-clone-{uuid4().hex}"
            quoted_attempt = shlex.quote(candidate)
            try:
                target.run(f"mkdir -m 700 -- {quoted_attempt}", sudo=True)
            except BaseException:
                output.warn(
                    f"Private clone directory reservation at {candidate!r} was not confirmed. "
                    "Any remaining path was left untouched for inspection."
                )
                raise
            attempt = candidate
            target.run(f"chown {vm.admin_username}:{ws_group} {quoted_attempt}", sudo=True)
            checkout = shlex.quote(f"{candidate}/checkout")
            # `--` stops option parsing so a repo URL beginning with `-` can
            # never be read as a git flag; both operands are quoted for spaces.
            target.run(
                f"git clone -- {shlex.quote(template.repo)} {checkout}",
                timeout=300,
            )
            target.run(f"chgrp -R {ws_group} {checkout}", sudo=True)
            target.run(f"find {checkout} -type d -exec chmod g+s {{}} +", sudo=True, timeout=120)
            target.run(f"chmod 2770 {checkout}", sudo=True)
            # -T never follows a destination symlink or merges with a newly
            # populated directory. Both paths share a filesystem; a failed
            # or unconfirmed rename does not establish final ownership.
            target.run(f"mv -T -- {checkout} {quoted_workspace}", sudo=True)
            clone_published = True
            target.run(f"rmdir -- {quoted_attempt}", sudo=True)
            attempt = None

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
                    target.run(f"git -C {quoted_workspace} config --local {git_key} {shlex.quote(value)}")

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
    except BaseException:
        # A guest clone may survive the controller's cancellation or timeout.
        # Only its unique attempt path is recursively removed until publication
        # is confirmed. Preserve an occupied or unconfirmed final directory.
        try:
            if attempt is not None:
                target.run(f"rm -rf -- {shlex.quote(attempt)}", sudo=True, timeout=30)
            delete_vm_workspace(
                vm,
                config,
                ws_name,
                workspace_path,
                ws_group,
                logger=logger,
                empty_only=bool(template.repo) and not clone_published,
            )
        except BaseException as cleanup_err:
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
    empty_only: bool = False,
) -> None:
    """Delete a workspace from a VM.

    Tears down what :func:`create_vm_workspace` set up: the directory,
    the tmuxinator symlink, and the workspace's Linux group. ``groupadd``
    at create time has a symmetric ``groupdel`` here, so a deleted
    workspace leaves no residual group on the VM (issue #249). Callers
    pass the recorded ``linux_group`` rather than re-deriving it, so
    legacy workspaces (older ``ws--`` prefix) drop the group they
    actually own.

    ``empty_only`` removes only a failed create's empty reservation; an occupied
    or unconfirmed directory and its group stay available for inspection.
    Group removal runs last, once the directory is gone and after the
    caller has removed the agent members; ``groupdel`` removes the group
    regardless of remaining supplementary members (the admin's included).
    """
    from agentworks.ssh import SSHError

    assert vm.tailscale_host is not None
    target = transport(vm, config, logger=logger)

    try:
        quoted = shlex.quote(workspace_path)
        if empty_only:
            removed = target.run(
                f"if test -e {quoted} || test -L {quoted}; then rmdir -- {quoted}; fi",
                sudo=True,
                check=False,
                timeout=30,
            )
            if not removed.ok:
                output.warn(
                    f"Workspace directory {workspace_path!r} and group {linux_group!r} were retained because "
                    "the clone's publication was not confirmed and the path could not be confirmed empty. "
                    "Inspect and preserve its contents before retrying."
                )
                return
        else:
            target.run(f"rm -rf {quoted}", sudo=True, timeout=30)
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
