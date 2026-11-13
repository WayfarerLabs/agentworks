"""VM workspace backend: a workspace's directory, group, clone and tmuxinator projection on its VM."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from uuid import uuid4

import agentworks.execution as execution
from agentworks import output
from agentworks.errors import AlreadyExistsError
from agentworks.execution.access import Admin
from agentworks.execution.errors import (
    CommandFailed,
    ExecutionIncomplete,
    FileContentMismatch,
    FileLockTimeout,
    PathRefused,
    RequestRefused,
)
from agentworks.execution.requests import Command
from agentworks.linux_users import managed_home
from agentworks.workspaces.acls import apply_workspace_acls
from agentworks.workspaces.guest import (
    FIND_DEADLINE,
    PROBE_DEADLINE,
    REQUEST_FAILURES,
    ensure_group,
    path_occupied,
    script,
)
from agentworks.workspaces.tmuxinator import config_link, config_path, generate_config, link_config, write_config

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.vms.initializer.phase_a import OperationLog
    from agentworks.vms.initializer.steps import InitLog
    from agentworks.workspaces.templates import ResolvedTemplate

CLONE_DEADLINE = 300
"""Seconds a workspace's ``git clone`` may take."""

CLEANUP_DEADLINE = 30
"""Seconds a failed create's or a delete's directory removal may take."""


def default_workspace_path(config: Config, ws_name: str) -> str:
    """The VM-side path a workspace named ``ws_name`` is created at:
    ``paths.vm_workspaces/<name>``. The single home of that convention,
    shared by the create/copy backends and the pending workspace node
    (which must know the path before the row exists); the ROW stays the
    source of truth for a live workspace (``workspace rehome`` can move
    one anywhere)."""
    return f"{config.paths.vm_workspaces}/{ws_name}"


def admin_access(vm: VMRow, config: Config, logger: OperationLog | None) -> VMAccess:
    """The admin's access to ``vm``, recording into ``logger`` when there is one."""
    return execution.vm_access(vm, config, Admin(), log=None if logger is None else logger.record)


def _value_free_clone_failure(
    error: CommandFailed | ExecutionIncomplete | RequestRefused, *, ws_name: str, vm_name: str
) -> None:
    """Strip ``error`` of everything the guest returned, keeping its identity (I87).

    Git's stderr and the command line can hold credentials in the repository
    address, so the message, the hint and the result's streams are replaced
    and the chained errors dropped.
    """
    error.args = (f"Repository clone failed for workspace {ws_name!r} on VM {vm_name!r}.",)
    error.result = replace(error.result, stdout=b"", stderr=b"")
    error.entity_kind = "workspace"
    error.entity_name = ws_name
    error.hint = (
        "Inspect the SSH log for the clone's failure. A timed-out or interrupted guest Git process "
        "may still be running in its private attempt directory. "
        "Check the repository address, authentication and VM network access as appropriate. "
        "For private repositories, declare the cloning admin's Git credential on the admin template "
        "and reinitialize the VM. Managed Git credentials use HTTPS; "
        "SSH URLs need separately configured SSH access."
    )
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True


def create_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    template: ResolvedTemplate,
    *,
    logger: InitLog | None = None,
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
    admin = admin_access(vm, config, logger)

    workspace_path = default_workspace_path(config, ws_name)
    ws_group = workspace_group(ws_name)
    owner = f"{vm.admin_username}:{ws_group}"

    # Refuse to create if directory already exists
    if admin.run(path_occupied(workspace_path)).require_completed(vm.name) == 0:
        raise AlreadyExistsError(
            f"workspace path {workspace_path!r} already exists on the VM.",
            entity_kind="workspace",
            entity_name=ws_name,
            hint="Inspect and preserve its contents before removing it, or choose a different workspace name.",
        )

    # Reserve exclusively before the cleanup region: a raced-in path or a lost
    # response is not confirmation that the directory belongs to this attempt.
    admin.run(Command(("mkdir", "-p", "--", str(PurePosixPath(workspace_path).parent)), elevated=True), check=True)
    try:
        admin.run(Command(("mkdir", "--", workspace_path), elevated=True), check=True)
    except BaseException:
        output.warn(
            f"Workspace directory reservation at {workspace_path!r} was not confirmed. "
            "Any remaining path was left untouched; inspect and preserve its contents before retrying."
        )
        raise
    attempt: str | None = None
    clone_published = False
    generated_config: tuple[str, str] | None = None
    try:
        # Create workspace group (idempotent), add admin, and set up directory with setgid
        admin.run(ensure_group(ws_group), check=True)
        admin.run(Command(("usermod", "-aG", ws_group, vm.admin_username), elevated=True), check=True)
        admin.run(Command(("chown", owner, workspace_path), elevated=True), check=True)
        admin.run(Command(("chmod", "2770", workspace_path), elevated=True), check=True)
        # The canonical workspace ACL is applied at the end (apply_workspace_acls),
        # after any clone and other content is placed, so existing entries are
        # covered too. This is the same recursive spec workspace repair applies,
        # so a first repair of this workspace is a true no-op.

        # Git clone if repo is set
        if template.repo:
            output.info(f"Cloning repository for workspace {ws_name!r}...")
            candidate = f"{workspace_path}.agentworks-clone-{uuid4().hex}"
            try:
                admin.run(Command(("mkdir", "-m", "700", "--", candidate), elevated=True), check=True)
            except BaseException:
                output.warn(
                    f"Private clone directory reservation at {candidate!r} was not confirmed. "
                    "Any remaining path was left untouched for inspection."
                )
                raise
            attempt = candidate
            admin.run(Command(("chown", owner, candidate), elevated=True), check=True)
            checkout = f"{candidate}/checkout"
            # `--` stops option parsing so a repo URL beginning with `-` can
            # never be read as a git flag.
            try:
                admin.run(Command(("git", "clone", "--", template.repo, checkout), deadline=CLONE_DEADLINE), check=True)
            except REQUEST_FAILURES as error:
                _value_free_clone_failure(error, ws_name=ws_name, vm_name=vm.name)
                raise
            admin.run(Command(("chgrp", "-R", ws_group, checkout), elevated=True), check=True)
            admin.run(
                Command(
                    ("find", checkout, "-type", "d", "-exec", "chmod", "g+s", "{}", "+"),
                    elevated=True,
                    deadline=FIND_DEADLINE,
                ),
                check=True,
            )
            admin.run(Command(("chmod", "2770", checkout), elevated=True), check=True)
            # -T never follows a destination symlink or merges with a newly
            # populated directory. Both paths share a filesystem; a failed
            # or unconfirmed rename does not establish final ownership.
            admin.run(Command(("mv", "-T", "--", checkout, workspace_path), elevated=True), check=True)
            clone_published = True
            admin.run(Command(("rmdir", "--", candidate), elevated=True), check=True)
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
                    admin.run(Command(("git", "-C", workspace_path, "config", "--local", git_key, value)), check=True)

        # Tmuxinator config (no tasks yet at workspace creation time), linked
        # so tmuxinator can find it by console session name.
        if template.tmuxinator:
            tmux_config = generate_config(ws_name, workspace_path)
            generated_config = (config_path(workspace_path), hashlib.sha256(tmux_config.encode()).hexdigest())
            try:
                write_config(admin, workspace_path, tmux_config)
            except (PathRefused, FileLockTimeout, FileContentMismatch):
                # Nothing was written, so there is nothing of this create's to remove.
                generated_config = None
                raise
            link_config(
                admin,
                home=managed_home(vm.admin_username),
                ws_name=ws_name,
                workspace_path=workspace_path,
                replace_link=False,
            )

        # Canonical workspace ACL, applied last so every entry written above (a
        # clone, the tmuxinator config) is covered, and future entries inherit via
        # the default ACL. Shared with workspace repair so the two never drift and
        # a first repair of this workspace is a no-op.
        apply_workspace_acls(admin.files(), workspace_path)
    except BaseException:
        # A guest clone may survive the controller's cancellation or timeout.
        # Only its unique attempt path is recursively removed until publication
        # is confirmed. Preserve an occupied or unconfirmed final directory.
        if attempt is not None:
            try:
                admin.run(Command(("rm", "-rf", "--", attempt), elevated=True, deadline=CLEANUP_DEADLINE), check=True)
            except BaseException:
                output.warn(
                    f"Private clone attempt {attempt!r} could not be removed. "
                    "Inspect it and any surviving Git process before manual cleanup."
                )
        # Cleanup steps are independent: an unreachable private attempt must not
        # prevent reclaiming an empty final reservation and its group.
        if not clone_published and generated_config is not None:
            try:
                admin.files().remove_if(generated_config[0], expected=generated_config[1])
            except BaseException:
                output.warn(
                    f"Generated workspace config {generated_config[0]!r} could not be safely removed. "
                    "Any changed content was retained."
                )
        try:
            delete_vm_workspace(
                vm,
                config,
                ws_name,
                workspace_path,
                ws_group,
                logger=logger,
                empty_only=not clone_published,
            )
        except BaseException:
            output.warn(
                f"Cleanup after failed workspace create {ws_name!r} was not confirmed. "
                f"Inspect directory {workspace_path!r} and group {ws_group!r} before retrying."
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
    logger: InitLog | None = None,
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
    assert vm.tailscale_host is not None
    admin = admin_access(vm, config, logger)

    try:
        if empty_only:
            removed = admin.run(
                script(
                    'if test -e "$1" || test -L "$1"; then rmdir -- "$1"; fi',
                    workspace_path,
                    elevated=True,
                    deadline=CLEANUP_DEADLINE,
                )
            )
            if removed.require_completed(vm.name) != 0:
                output.warn(
                    f"Workspace directory {workspace_path!r} and group {linux_group!r} were retained because "
                    "workspace publication was not confirmed and the path could not be confirmed empty. "
                    "Inspect and preserve its contents before retrying."
                )
                return
        else:
            admin.run(
                Command(("rm", "-rf", "--", workspace_path), elevated=True, deadline=CLEANUP_DEADLINE), check=True
            )
        # The link only while it still points at this workspace's config. Agentworks
        # writes an absolute target, compared normalized without following links
        # (realpath -s -m), since an earlier release wrote it from the stored path as
        # spelled; a relative target is never ours, and realpath would read it from
        # the request's directory rather than the link's.
        admin.run(
            script(
                'if test -L "$1"; then t=$(readlink -- "$1"); case $t in /*)\n'
                '  if test "$(realpath -s -m -- "$t")" = "$2"; then rm -- "$1"; fi ;; esac; fi',
                config_link(managed_home(vm.admin_username), ws_name),
                config_path(workspace_path),
                deadline=PROBE_DEADLINE,
            )
        ).require_completed(vm.name)
        admin.run(_group_removal(linux_group)).require_completed(vm.name)
    except REQUEST_FAILURES:
        output.warn(
            f"Remote cleanup of workspace directory {workspace_path!r} and group {linux_group!r} was not confirmed. "
            "Inspect them before retrying."
        )


def remove_workspace_group(vm: VMRow, config: Config, linux_group: str, *, logger: InitLog | None = None) -> None:
    """Remove only the workspace's Linux group: delete's VM step when the stored path names no one directory."""
    assert vm.tailscale_host is not None
    try:
        admin_access(vm, config, logger).run(_group_removal(linux_group)).require_completed(vm.name)
    except REQUEST_FAILURES:
        output.warn(f"Removal of workspace group {linux_group!r} was not confirmed. Inspect it before retrying.")


def _group_removal(linux_group: str) -> Command:
    """``groupdel`` of the workspace's group, as root.

    Its status is not checked, so a group that is already gone (or,
    defensively, one groupdel refuses to remove) is tolerated rather than
    failing the delete, mirroring the best-effort group-membership teardown in
    agents/grants.py.
    """
    return Command(("/usr/sbin/groupdel", linux_group), elevated=True, deadline=PROBE_DEADLINE)


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
