"""Workspace copying: the source tree as an opaque tar stream, through the workstation, into a new workspace."""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.agents.grants import MAX_WORKSPACE_NAME_LENGTH
from agentworks.errors import AgentworksError, AlreadyExistsError, ExternalError, NotFoundError, StateError
from agentworks.execution.requests import Command, FromFile, ToFile
from agentworks.linux_users import managed_home
from agentworks.naming import validate_name
from agentworks.path_rendering import format_host_path
from agentworks.vms.initializer.steps import status
from agentworks.vms.manager import gated_vm_boundary
from agentworks.workspaces.backends.vm import default_workspace_path
from agentworks.workspaces.guest import FIND_DEADLINE, ensure_group, path_occupied
from agentworks.workspaces.manager._common import _guard_vm_status, _resolve_vm, _workspace_scope

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.initializer.phase_a import OperationLog
    from agentworks.vms.initializer.steps import InitLog

OWNERSHIP_DEADLINE = 60
"""Seconds the recursive ``chown`` of the extracted tree may take."""


def copy_workspace(
    db: Database,
    config: Config,
    source_name: str,
    *,
    dest_name: str,
    vm_name: str | None = None,
    interaction: TtyInteractionPolicy,
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

    The tree goes VM, workstation, VM as a gzipped tar stream that is
    never extracted on the workstation, so modes and links survive any
    workstation; owners do not, since the destination extracts without them
    and then gives the tree to the admin and the workspace group. Nothing is
    staged on the destination VM: it extracts from the request's input. The
    source read is logged to the source VM's own operation log
    (``workspace-copy-source``), since the destination is chosen only after it.
    """
    import contextlib
    import tempfile
    from pathlib import Path

    from agentworks.bootstrap import load_request_registry
    from agentworks.vms.manager import operation_log
    from agentworks.workspaces.backends.vm import admin_access

    validate_name(dest_name, max_length=MAX_WORKSPACE_NAME_LENGTH)

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

    with tempfile.TemporaryDirectory(prefix="agentworks-workspace-copy-") as scratch:
        # ToFile creates its file, so the archive's path is new in a private directory.
        archive = Path(scratch) / "workspace.tgz"
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
            registry = load_request_registry(config, live_database=db)
            _keepalive_stack.enter_context(
                gated_vm_boundary(
                    db,
                    config,
                    registry,
                    src_vm,
                    scope=_workspace_scope(db, src_vm, source_name),
                    interaction=interaction,
                )
            )
            if src_vm.tailscale_host is None:
                raise StateError(
                    f"VM '{src_vm.name}' has no Tailscale address",
                    entity_kind="vm",
                    entity_name=src_vm.name,
                )

            output.info(f"Packing workspace '{source_name}' from VM '{src_vm.name}'...")
            with operation_log(src_vm.name, "workspace-copy-source") as source_log:
                # Unelevated, as the admin, who can read every workspace. The
                # sink removes a partial archive on any other completion.
                try:
                    admin_access(src_vm, config, source_log).run(
                        Command(("tar", "czf", "-", "-C", src_ws.workspace_path, ".")),
                        stdout=ToFile(archive),
                        check=True,
                    )
                except AgentworksError as error:
                    error.add_hint(f"SSH log: {source_log.display_path}")
                    raise
                except Exception as error:
                    raise _copy_failure(error, source_name, source_log) from error

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
                        interaction=interaction,
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

            # Exactly-once close, however the span ends (a cancellation, the
            # sanctioned KeyboardInterrupt re-raise out of
            # materialize_grant_all_agents, or an error from any request), so
            # the footer and the in-flight traceback land in the per-VM log.
            with operation_log(dest_vm.name, "workspace-copy") as lg:
                try:
                    _unpack(db, config, dest_vm, dest_name, archive, lg)
                except AgentworksError as error:
                    error.add_hint(f"SSH log: {lg.display_path}")
                    raise
                except Exception as error:
                    raise _copy_failure(error, dest_name, lg) from error

    output.result(f"Workspace '{source_name}' copied to '{dest_name}'")


def _copy_failure(error: Exception, ws_name: str, log: OperationLog) -> ExternalError:
    """An untyped failure (a local read or write error) as the copy's error, naming the log."""
    return ExternalError(
        f"copying workspace: {error}", entity_kind="workspace", entity_name=ws_name, hint=f"SSH log: {log.display_path}"
    )


def _unpack(db: Database, config: Config, dest_vm: VMRow, dest_name: str, archive: Path, lg: InitLog) -> None:
    """Extract ``archive`` into the new workspace ``dest_name`` on ``dest_vm`` and record it."""
    from agentworks.agents.grants import materialize_grant_all_agents, workspace_group
    from agentworks.workspaces.acls import apply_workspace_acls
    from agentworks.workspaces.backends.vm import admin_access, generate_vscode_workspace
    from agentworks.workspaces.tmuxinator import generate_config, publish_tmuxinator_config

    dest = admin_access(dest_vm, config, lg)
    workspace_path = default_workspace_path(config, dest_name)
    ws_group = workspace_group(dest_name)
    owner = f"{dest_vm.admin_username}:{ws_group}"

    output.info(f"Unpacking to workspace '{dest_name}' on VM '{dest_vm.name}'...")

    # Refuse an occupied destination and reserve it exclusively, as create does,
    # so nothing is extracted into another directory's content.
    if status(dest, path_occupied(workspace_path)) == 0:
        raise AlreadyExistsError(
            f"workspace path {workspace_path!r} already exists on VM '{dest_vm.name}'.",
            entity_kind="workspace",
            entity_name=dest_name,
            hint="Inspect and preserve its contents before removing it, or choose a different workspace name.",
        )
    dest.run(Command(("mkdir", "-p", "--", posixpath.dirname(workspace_path)), elevated=True), check=True)
    dest.run(Command(("mkdir", "--", workspace_path), elevated=True), check=True)

    # Set up group, directory, and permissions (same as create_vm_workspace)
    dest.run(ensure_group(ws_group), check=True)
    dest.run(Command(("usermod", "-aG", ws_group, dest_vm.admin_username), elevated=True), check=True)
    dest.run(Command(("chown", owner, workspace_path), elevated=True), check=True)
    dest.run(Command(("chmod", "2770", workspace_path), elevated=True), check=True)

    # The archive comes from another VM, so extraction keeps GNU tar's
    # defaults and never passes -P: leading '/' is stripped, '..' members
    # are refused, and a link that is absolute or climbs is a placeholder
    # until the end, so no member is written through it. Every extracted
    # file is root's until the chown below. The directory is 2770 for the
    # admin and the group, and agents get their grants only afterwards, so
    # no agent reaches the partial tree. Neither half of the copy has a
    # deadline: a tree takes as long as the link needs, and the connection's
    # keepalives end a stalled one.
    # A local read error fails this request, but tar still sees its input end
    # there and may already have written the members before the cut; gzip
    # only makes it fail rather than finish. The copy then fails before it
    # records a row or grants access, and the partial tree stays in the
    # reserved 2770 directory.
    dest.run(
        Command(("tar", "xzf", "-", "--no-same-owner", "-C", workspace_path), elevated=True, stdin=FromFile(archive)),
        check=True,
    )
    # Neither follows a link: -P states the physical walk both default to.
    dest.run(
        Command(("chown", "-R", "-P", owner, workspace_path), elevated=True, deadline=OWNERSHIP_DEADLINE), check=True
    )
    dest.run(
        Command(
            ("find", "-P", workspace_path, "-type", "d", "-exec", "chmod", "g+s", "{}", "+"),
            elevated=True,
            deadline=FIND_DEADLINE,
        ),
        check=True,
    )
    # The archive's own '.' member set the directory's mode to the source's.
    dest.run(Command(("chmod", "2770", workspace_path), elevated=True), check=True)

    # Canonical workspace ACL, applied AFTER the unpack and the recursive
    # chown/SGID so the recursive access ACL covers every unpacked entry
    # and the per-directory default ACL is seeded for future entries.
    # The same shared spec workspace create and repair apply, so a copied
    # workspace lands in the canonical state (its first repair is an ACL
    # no-op) and, per #254, denies `other` on both the access and default
    # ACLs, which the old inline top-dir-only default did not.
    apply_workspace_acls(dest.files(), workspace_path)

    # Before the row, so a refused config (a link in its path) leaves no row.
    publish_tmuxinator_config(
        dest,
        home=managed_home(dest_vm.admin_username),
        ws_name=dest_name,
        workspace_path=workspace_path,
        text=generate_config(dest_name, workspace_path),
        replace_link=True,
    )

    db.insert_workspace(
        dest_name,
        vm_name=dest_vm.name,
        workspace_path=workspace_path,
        template="copied",
        linux_group=ws_group,
    )

    # Materialize grant_all agents on the destination VM onto the new
    # workspace, exactly as workspace create's realization body does after
    # ITS row insert (issue #321): every workspace-inserting path runs this
    # pass, or grant_all agents silently lack access to the copied workspace
    # and reinit's row-driven reconcile (#280) can never restore it.
    materialize_grant_all_agents(db, config, dest_vm, dest_name, logger=lg)

    vscode_path = generate_vscode_workspace(dest_vm, config, dest_name, workspace_path)
    output.detail(f"VS Code workspace: {format_host_path(vscode_path)}")
