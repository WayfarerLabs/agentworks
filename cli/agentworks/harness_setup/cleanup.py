"""Retire workspace-owned associations that outlive its directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.git_excludes import GIT_EXCLUDE_ROLE, reconcile_workspace_excludes
from agentworks.artifacts.native.mcp import MCP_CLAIM_ROLE
from agentworks.errors import AgentworksError, StateError, UserAbort
from agentworks.execution.files import guest_path
from agentworks.harness_setup.dispatch import destination_id
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup
from agentworks.workspaces.guest import stored_workspace_path

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from agentworks.db import Database, WorkspaceRow
    from agentworks.execution.access import VMAccess
    from agentworks.harness_setup.model import NativeClaim, SetupRecord


def cleanup_workspace_excludes(
    db: Database, workspace: WorkspaceRow, access: Callable[[], VMAccess], *, redactions: Iterable[str]
) -> None:
    """Try known Git retirement without adding a parent-deletion prerequisite.

    ``redactions`` are the values the operation's log redacts, kept out of its diagnostics too.
    """
    _workspace_excludes(db, workspace, access, redactions=tuple(redactions))


def rehome_workspace_artifacts(
    db: Database, workspace: WorkspaceRow, new_path: str, access: VMAccess, *, redactions: Iterable[str]
) -> None:
    """Transfer associations after copying, while the actual old owner still exists.

    ``redactions`` are the values the operation's log redacts, kept out of its diagnostics too.
    """
    _workspace_excludes(db, workspace, lambda: access, new_path=new_path, redactions=tuple(redactions))


def _relocated_record(record: SetupRecord, old_path: str, new_path: str, identity: str) -> SetupRecord:
    """Rebase metadata under verified workspace roots without using it for file I/O."""
    old_path, new_path = guest_path(old_path), guest_path(new_path)

    def relocate(path: str) -> str:
        path = guest_path(path)
        if not path.startswith(old_path + "/"):
            raise StateError("workspace artifact evidence is outside its owning workspace")
        return new_path + path.removeprefix(old_path)

    return record.model_copy(
        update={
            "destination_id": identity,
            "claims": tuple(
                claim.model_copy(update={"destination": relocate(claim.destination)})
                if claim.role == MCP_CLAIM_ROLE
                else claim
                for claim in record.claims
            ),
            "artifact_files": tuple(
                file.model_copy(
                    update={
                        "path": relocate(file.path),
                        "package_root": None if file.package_root is None else relocate(file.package_root),
                    }
                )
                for file in record.artifact_files
            ),
            "skipped": tuple(skip.model_copy(update={"path": relocate(skip.path)}) for skip in record.skipped),
        }
    )


def _workspace_excludes(
    db: Database,
    workspace: WorkspaceRow,
    access: Callable[[], VMAccess],
    *,
    new_path: str | None = None,
    redactions: tuple[str, ...],
) -> None:
    """Retire, and on rehome re-establish, the workspace's Git exclusions.

    The row's stored path is read normalized (``stored_workspace_path``), as
    an earlier release may have stored it with a trailing ``/`` or a leading
    ``//``; both callers have already refused one that is not absolute.
    """
    operation = "workspace-rehome" if new_path is not None else "workspace-delete"
    action = "workspace rehome" if new_path is not None else "workspace deletion"
    old_path = stored_workspace_path(workspace.workspace_path)
    try:
        state = read_native_setup(db, "workspace", workspace.name)
    except StateError:
        output.warn(
            f"Artifact metadata maintenance for workspace '{workspace.name}' could not be identified from "
            f"its unreadable native setup evidence. Continuing {action}; inspect shared Git metadata."
        )
        return
    failed = []
    admin = None
    identity = None
    identity_failure: AgentworksError | None = None
    for record in tuple(state.records):
        if new_path is None and not any(claim.role == GIT_EXCLUDE_ROLE for claim in record.claims):
            continue
        current = record
        if new_path is None:
            current = current.model_copy(update={"complete": False, "pending_cleanup": True})
        persistence_failure: AgentworksError | None = None
        incomplete_seen = False

        def checkpoint(claims: tuple[NativeClaim, ...]) -> None:
            nonlocal current, state, persistence_failure
            current = current.model_copy(update={"claims": claims})
            state = replace_setup_record(state, current)
            try:
                write_native_setup(db, "workspace", workspace.name, state, operation=operation)
            except AgentworksError as error:
                persistence_failure = error
                raise

        def incomplete(reason: str, *, integration: str = record.integration) -> None:
            nonlocal current, incomplete_seen
            incomplete_seen = True
            reason = sanitize_setup_diagnostic(reason, redactions=redactions)
            current = current.model_copy(
                update={
                    "complete": False,
                    "pending_cleanup": True,
                    "incomplete_reasons": (*current.incomplete_reasons, reason),
                }
            )
            checkpoint(current.claims)
            output.warn(f"Workspace '{workspace.name}' via {integration}: {reason}")

        checkpoint(current.claims)
        try:
            if admin is None:
                admin = access()
            reconcile_workspace_excludes(
                admin,
                old_path,
                record.integration,
                (),
                current.claims,
                checkpoint,
                group=workspace.linux_group,
                report_incomplete=incomplete,
                redactions=redactions,
            )
        except UserAbort:
            raise
        except AgentworksError as error:
            if error is persistence_failure:
                raise
            incomplete(str(error))
        if new_path is not None:
            relocated = None
            try:
                relocated = _relocated_record(record, old_path, new_path, record.destination_id)
            except StateError as error:
                incomplete(str(error))
            # The snapshot predates old-location retirement: a complete .git
            # copy still contains those pairs even when the old ones are gone.
            try:
                if admin is None:
                    admin = access()
                reconcile_workspace_excludes(
                    admin,
                    new_path,
                    record.integration,
                    () if relocated is None else relocated.artifact_files,
                    record.claims,
                    checkpoint,
                    group=workspace.linux_group,
                    report_incomplete=incomplete,
                    redactions=redactions,
                    prior_workspace=old_path,
                    shared_paths=tuple(
                        claim.destination
                        for claim in (() if relocated is None else relocated.claims)
                        if claim.role == MCP_CLAIM_ROLE
                    ),
                )
            except UserAbort:
                raise
            except AgentworksError as error:
                if error is persistence_failure:
                    raise
                incomplete(str(error))
            # Safe placement evidence follows the copy independently of Git
            # cleanup. Keep its current claims and any unresolved cleanup status.
            if relocated is not None:
                try:
                    if admin is None:
                        admin = access()
                    if identity_failure is not None:
                        raise identity_failure
                    if identity is None:
                        vm = db.get_vm(workspace.vm_name)
                        if vm is None:
                            raise StateError("workspace VM is missing while relocating artifact evidence")
                        identity = destination_id(vm, admin, location=new_path)
                    current = _relocated_record(current, old_path, new_path, identity)
                except UserAbort:
                    raise
                except AgentworksError as error:
                    if identity is None:
                        identity_failure = error
                    incomplete(str(error))
                else:
                    checkpoint(current.claims)
        retained = tuple(
            claim
            for claim in current.claims
            if claim.role == GIT_EXCLUDE_ROLE and (new_path is None or claim.source != new_path)
        )
        if incomplete_seen or retained:
            failed.append(record.integration)
            for claim in retained:
                output.warn(
                    sanitize_setup_diagnostic(
                        f"Workspace '{workspace.name}' via {record.integration}: retained artifact exclusion "
                        f"'{claim.identifier}' at recorded path '{claim.destination}'.",
                        redactions=redactions,
                    )
                )
    if failed:
        output.warn(
            f"Artifact metadata maintenance for workspace '{workspace.name}' is unfinished "
            f"({', '.join(failed)}). Continuing {action}; inspect the reported setup evidence and Git metadata."
        )
