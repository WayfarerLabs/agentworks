"""Workspace rehoming (moving a workspace to a new directory path)."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import (
    AgentworksError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.execution.files import guest_path
from agentworks.execution.requests import Command
from agentworks.path_rendering import format_host_path
from agentworks.recheck import require_unchanged, require_vm_unchanged, workspace_fields
from agentworks.remote_presence import TEST_FALSE_STATUS, probe_presence
from agentworks.secrets.policy import require_exact_tty_interaction_policy
from agentworks.vms.initializer.steps import describe_failure
from agentworks.vms.manager import gated_vm_boundary
from agentworks.workspaces.guest import (
    FIND_DEADLINE,
    PROBE_DEADLINE,
    REQUEST_FAILURES,
    open_parent_traversal,
    path_occupied,
    script,
    stored_workspace_path,
)
from agentworks.workspaces.manager._common import _guard_vm_status, _workspace_scope
from agentworks.workspaces.manager.repair import _rehome_partial_state_hint

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.execution.access import VMAccess
    from agentworks.secrets.policy import TtyInteractionPolicy


COPY_DEADLINE = 600
"""Seconds the copy of the tree to its new path may take."""

REMOVE_OLD_DEADLINE = 60
"""Seconds the removal of the old directory may take."""


def rehome_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    target_path: str | None = None,
    remove_old: bool = False,
    yes: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Move a workspace to a new directory path.

    ``interaction`` is checked first, ahead of the activation gate and the
    sessions check, which reach the VM: a policy the boundary would reject
    refuses before anything is touched.
    """
    require_exact_tty_interaction_policy(interaction)
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    # Determine target path
    from agentworks.workspaces.backends.vm import default_workspace_path

    new_path = _target_directory(
        target_path if target_path is not None else default_workspace_path(config, name), ws_name=name
    )
    # Compared normalized: a stored path can be spelled `/data//ws` or `/data/ws/`; one that
    # is not absolute is refused here, before anything runs.
    old_path = stored_workspace_path(ws.workspace_path)

    if old_path == new_path:
        output.info(f"Workspace '{name}' is already at {new_path}")
        return

    # As spelled here, before anything runs; as the guest resolves them, inside the span.
    _refuse_overlap(old_path, new_path, ws_name=name)

    _rehome_vm(db, config, ws, new_path, remove_old=remove_old, yes=yes, interaction=interaction)


def _refuse_unstopped_sessions(db: Database, config: Config, ws_name: str, *, repair_pids: bool) -> None:
    """Refuse a rehome while any of the workspace's sessions is not stopped.

    Without ``repair_pids`` it only observes, persisting nothing, as the
    check before the confirmation must. Under the guard it first repairs
    missing PID evidence (``ensure_pids_batch``), which writes the rows.
    """
    from agentworks.db import PID_STOPPED, SessionStatus
    from agentworks.sessions.manager import ensure_pids_batch, observe_session_statuses

    sessions = db.list_sessions(workspace_name=ws_name)
    if not sessions:
        return
    try:
        if repair_pids:
            sessions = ensure_pids_batch(sessions, db=db, config=config)
        status_map = observe_session_statuses(sessions, db=db, config=config)
    except Exception as exc:
        raise ExternalError(
            f"cannot verify session status for workspace '{ws_name}' (VM may be unreachable): {exc}",
            entity_kind="workspace",
            entity_name=ws_name,
        ) from exc
    not_stopped = [
        s
        for s in sessions
        if s.pid != PID_STOPPED and status_map.get(s.name, SessionStatus.UNKNOWN) != SessionStatus.STOPPED
    ]
    if not_stopped:
        names = ", ".join(s.name for s in not_stopped)
        raise StateError(
            f"workspace '{ws_name}' has {len(not_stopped)} non-stopped session(s) ({names}).",
            entity_kind="workspace",
            entity_name=ws_name,
            hint="Stop or delete the listed sessions first.",
        )


def _refuse_unusable_paths(probe: VMAccess, old_path: str, new_path: str, *, ws_name: str) -> None:
    """Refuse a missing source, an occupied target, or paths the guest resolves into each other."""
    if not _is_directory(probe, old_path):
        raise StateError(
            f"source directory {old_path} does not exist on VM",
            entity_kind="workspace",
            entity_name=ws_name,
        )
    # Anything at the target, a dangling link included, refuses it, as create and copy refuse theirs.
    if probe_presence(probe, path_occupied(new_path), absent_status=TEST_FALSE_STATUS) is not None:
        raise StateError(
            f"target path {new_path} is occupied on VM",
            entity_kind="workspace",
            entity_name=ws_name,
        )
    # A link can make different spellings one directory, and the copy would
    # then write into its own source before --remove-old removes it.
    _refuse_overlap(*_resolved_paths(probe, old_path, new_path), ws_name=ws_name)


def _rehome_vm(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
    new_path: str,
    *,
    remove_old: bool,
    yes: bool,
    interaction: TtyInteractionPolicy,
) -> None:
    """Rehome a VM workspace.

    Orchestrated (``vms.manager.gated_vm_boundary``, WORKSPACE scope):
    the graph is the live VM alone, the activation gate replaces this
    command's ``keep_active``, and the whole move runs inside the
    held-active span. The not-found check and the VM-status guard stay
    pre-boundary. Inside the span and before the native mutation guard,
    the command only observes: the sessions check persists nothing, the
    source / target directory checks need SSH, and the confirm renders
    the stored and new paths. Under the guard the workspace row is read
    again, PID evidence is repaired, and the sessions check and the
    directory checks run again, since a session can start or a path can
    change between the confirmation and the guard.
    """
    from agentworks.bootstrap import load_request_registry
    from agentworks.harness_setup.locking import native_mutation_guard
    from agentworks.vms.manager import operation_log
    from agentworks.workspaces.acls import apply_workspace_acls
    from agentworks.workspaces.backends.vm import admin_access, generate_vscode_workspace

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
    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        scope=_workspace_scope(db, vm, ws_name),
        interaction=interaction,
    ) as (vm_node, _resolver, _ctx):
        vm = vm_node.row
        probe = admin_access(vm, config, None)
        _refuse_unstopped_sessions(db, config, ws_name, repair_pids=False)
        _refuse_unusable_paths(probe, old_path, new_path, ws_name=ws_name)

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

        # The log closes exactly once, however the move ends.
        with native_mutation_guard(db.path, vm.name), operation_log(vm.name, "workspace-rehome") as log:
            require_vm_unchanged(db, vm)
            require_unchanged("workspace", ws_name, workspace_fields(ws), workspace_fields(db.get_workspace(ws_name)))
            _refuse_unstopped_sessions(db, config, ws_name, repair_pids=True)
            _refuse_unusable_paths(probe, old_path, new_path, ws_name=ws_name)
            admin = admin_access(vm, config, log)
            # Reserve exclusively: a preflight check cannot rule out a raced-in
            # target, and an unanswered mkdir does not establish ownership.
            parent = str(PurePosixPath(new_path).parent)
            admin.run(Command(("mkdir", "-p", "--", parent), elevated=True), check=True)
            try:
                admin.run(Command(("mkdir", "--", new_path), elevated=True), check=True)
            except BaseException:
                output.warn(
                    f"Rehome target reservation at {new_path} was not confirmed. "
                    "Any remaining path was left untouched; inspect and preserve its contents before retrying. "
                    f"Operation log: {log.display_path}"
                )
                raise

            try:
                # Let the admin write into the confirmed new target.
                admin.run(Command(("chown", vm.admin_username, new_path), elevated=True), check=True)

                # Copy with rsync (fall back to cp -a). The trailing slash
                # matters for rsync: the contents of the source into the target.
                output.info("Copying workspace...")
                has_rsync = admin.run(Command(("which", "rsync"), deadline=PROBE_DEADLINE))
                if has_rsync.require_completed(vm.name) == 0:
                    admin.run(
                        Command(("rsync", "-a", "--", f"{old_path}/", f"{new_path}/"), deadline=COPY_DEADLINE),
                        check=True,
                    )
                else:
                    admin.run(
                        Command(
                            ("cp", "-a", "--", f"{old_path}/.", f"{new_path}/"), elevated=True, deadline=COPY_DEADLINE
                        ),
                        check=True,
                    )

                if not _is_directory(admin, new_path):
                    raise ExternalError(
                        "copy verification failed, target directory not found",
                        entity_kind="workspace",
                        entity_name=ws_name,
                    )

                # Fix ownership, permissions, and ACLs on the new path
                output.info("Setting permissions...")
                admin.run(
                    Command(("chown", f"{vm.admin_username}:{ws.linux_group}", new_path), elevated=True), check=True
                )
                admin.run(Command(("chmod", "2770", new_path), elevated=True), check=True)
                admin.run(
                    Command(
                        ("find", new_path, "-type", "d", "-exec", "chmod", "g+s", "{}", "+"),
                        elevated=True,
                        deadline=FIND_DEADLINE,
                    ),
                    check=True,
                )
                # Canonical workspace ACL, the same shared spec workspace create
                # and repair apply, so a rehomed workspace lands in the canonical
                # state (its first repair is an ACL no-op). Per #254 this now also
                # denies `other` on both the access and default ACLs, which the
                # old hand-rolled block here did not. ACL failure stays non-fatal
                # (warn and continue, exactly as before): the rsync copy already
                # succeeded, so a later repair converges the tree.
                try:
                    apply_workspace_acls(admin.files(), new_path)
                except REQUEST_FAILURES as e:
                    output.warn(f"ACL setup failed: {describe_failure(e, log.sanitize)}")

                # Only true ancestors of the new path get a+x; its own mode is
                # the canonical 2770 set above.
                admin.run(open_parent_traversal(new_path), check=True)

                # Retire old-location and copied Git pairs while the old row
                # still authorizes their source, then establish the new pairs.
                from agentworks.harness_setup.cleanup import rehome_workspace_artifacts

                rehome_workspace_artifacts(db, ws, new_path, admin, redactions=log.redactions)

                # Update database
                db.update_workspace_path(ws_name, new_path)
                output.detail(f"Database updated: workspace_path = {new_path}")

                # Regenerate VS Code workspace file
                vscode_path = generate_vscode_workspace(vm, config, ws_name, new_path)
                output.detail(f"VS Code workspace updated: {format_host_path(vscode_path)}")

                # Handle old directory
                if remove_old:
                    output.info(f"Removing old directory {old_path}...")
                    admin.run(
                        Command(("rm", "-rf", "--", old_path), elevated=True, deadline=REMOVE_OLD_DEADLINE), check=True
                    )
                    output.detail("Old directory removed")
                else:
                    output.info(f"Old directory left in place at {old_path}")
                    output.detail("Remove it manually when ready, or re-run with --remove-old")

            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling workspace rehome '{ws_name}'. "
                    f"{_rehome_partial_state_hint(db, ws_name, old_path, new_path)} "
                    f"Operation log: {log.display_path}"
                )
                raise
            except AgentworksError as e:
                e.add_hint(
                    f"Operation log: {log.display_path}. {_rehome_partial_state_hint(db, ws_name, old_path, new_path)}"
                )
                raise
            except Exception as e:
                raise ExternalError(
                    f"during rehome: {e}",
                    entity_kind="workspace",
                    entity_name=ws_name,
                    hint=(
                        f"Operation log: {log.display_path}. "
                        f"{_rehome_partial_state_hint(db, ws_name, old_path, new_path)}"
                    ),
                ) from e

        output.result(f"Workspace '{ws_name}' rehomed to {new_path}")


def _refuse_overlap(old_path: str, new_path: str, *, ws_name: str) -> None:
    """Refuse a target that is the source, inside it, or contains it."""
    if (new_path + "/").startswith(old_path + "/") or (old_path + "/").startswith(new_path + "/"):
        raise ValidationError(
            "source and target paths overlap",
            entity_kind="workspace",
            entity_name=ws_name,
        )


def _resolved_paths(access: VMAccess, old_path: str, new_path: str) -> tuple[str, str]:
    """The source and the target as the guest resolves them, every link followed.

    The source exists (``realpath -e``); the target need not yet (``realpath
    -m``). It runs as root, as the target's creation does (``mkdir``):
    ``realpath -m`` prints a component it cannot search unresolved, and still
    succeeds. Boundary: the paths are a guest process's output, so anything
    but two absolute paths is a ``StateError``.
    """
    result = access.run(
        script(
            'realpath -z -e -- "$1" && realpath -z -m -- "$2"',
            old_path,
            new_path,
            elevated=True,
            deadline=PROBE_DEADLINE,
        ),
        check=True,
    )
    resolved = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    if len(resolved) != 3 or resolved[2] or not all(path.startswith("/") for path in resolved[:2]):
        raise StateError(
            f"cannot resolve {old_path} and {new_path} on VM '{access.vm_name}'",
            entity_kind="vm",
            entity_name=access.vm_name,
        )
    return resolved[0], resolved[1]


def _is_directory(access: VMAccess, path: str) -> bool:
    """Whether a directory is at ``path``, as the admin sees it; a probe that does not answer raises."""
    probe = Command(("test", "-d", path), deadline=PROBE_DEADLINE)
    return probe_presence(access, probe, absent_status=TEST_FALSE_STATUS) is not None


def _target_directory(path: str, *, ws_name: str) -> str:
    """``path`` as a rehome target: the boundary of the operator's ``--target``.

    A trailing ``/`` names the same directory, so it is dropped. Anything
    else that is not an absolute, normalized path (a relative path, an empty,
    ``.`` or ``..`` component, the root) is refused before anything runs:
    the copy would read ``host:path`` as a remote, and the guest steps need
    the directory's own absolute spelling.
    """
    try:
        return guest_path(path.rstrip("/"))
    except StateError:
        raise ValidationError(
            f"rehome target must be an absolute directory path with no empty, '.' or '..' component, not {path!r}",
            entity_kind="workspace",
            entity_name=ws_name,
        ) from None
