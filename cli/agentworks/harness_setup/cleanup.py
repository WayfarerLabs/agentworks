"""Retire workspace-owned associations that outlive its directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.git_excludes import GIT_EXCLUDE_ROLE, reconcile_workspace_excludes
from agentworks.artifacts.native.mcp import MCP_CLAIM_ROLE
from agentworks.errors import AgentworksError, StateError
from agentworks.harness_setup.dispatch import destination_id
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup
from agentworks.native_files import native_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database, WorkspaceRow
    from agentworks.harness_setup.model import NativeClaim, SetupRecord
    from agentworks.transports import Transport


def cleanup_workspace_excludes(db: Database, workspace: WorkspaceRow, runner: Callable[[], Transport]) -> None:
    """Try known Git retirement without adding a parent-deletion prerequisite."""
    _workspace_excludes(db, workspace, runner)


def rehome_workspace_artifacts(db: Database, workspace: WorkspaceRow, new_path: str, runner: Transport) -> None:
    """Transfer associations after copying, while the actual old owner still exists."""
    _workspace_excludes(db, workspace, lambda: runner, new_path=new_path)


def _relocated_record(record: SetupRecord, old_path: str, new_path: str, identity: str) -> SetupRecord:
    """Rebase metadata under verified workspace roots without using it for file I/O."""
    old_path, new_path = native_path(old_path), native_path(new_path)

    def relocate(path: str) -> str:
        path = native_path(path)
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
    runner: Callable[[], Transport],
    *,
    new_path: str | None = None,
) -> None:
    operation = "workspace-rehome" if new_path is not None else "workspace-delete"
    action = "workspace rehome" if new_path is not None else "workspace deletion"
    try:
        state = read_native_setup(db, "workspace", workspace.name)
    except StateError:
        output.warn(
            f"Shared artifact exclusion cleanup for workspace '{workspace.name}' could not be identified from "
            f"its unreadable native setup evidence. Continuing {action}; inspect shared Git metadata."
        )
        return
    failed = []
    target = None
    for record in tuple(state.records):
        if not any(claim.role == GIT_EXCLUDE_ROLE for claim in record.claims) and not (
            new_path is not None
            and (record.artifact_files or any(claim.role == MCP_CLAIM_ROLE for claim in record.claims))
        ):
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
            reason = sanitize_setup_diagnostic(reason)
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
            if target is None:
                target = runner()
            reconcile_workspace_excludes(
                target,
                workspace.workspace_path,
                record.integration,
                (),
                current.claims,
                checkpoint,
                group=workspace.linux_group,
                report_incomplete=incomplete,
            )
        except AgentworksError as error:
            if error is persistence_failure:
                raise
            incomplete(sanitize_setup_diagnostic(str(error), logger=target.logger if target is not None else None))
        if new_path is not None:
            # The snapshot predates old-location retirement: a complete .git
            # copy still contains those pairs even when the old ones are gone.
            files = tuple(
                file.model_copy(
                    update={
                        "path": new_path + file.path.removeprefix(workspace.workspace_path),
                        "package_root": None,
                    }
                )
                for file in record.artifact_files
                if file.path.startswith(workspace.workspace_path + "/")
            )
            try:
                if target is None:
                    target = runner()
                relocated = (
                    _relocated_record(record, workspace.workspace_path, new_path, record.destination_id)
                    if any(claim.role == MCP_CLAIM_ROLE for claim in record.claims)
                    else None
                )
                reconcile_workspace_excludes(
                    target,
                    new_path,
                    record.integration,
                    files,
                    record.claims,
                    checkpoint,
                    group=workspace.linux_group,
                    report_incomplete=incomplete,
                    prior_workspace=workspace.workspace_path,
                    shared_paths=tuple(
                        claim.destination
                        for claim in (() if relocated is None else relocated.claims)
                        if claim.role == MCP_CLAIM_ROLE
                    ),
                )
                if any(claim.role == MCP_CLAIM_ROLE for claim in current.claims):
                    vm = db.get_vm(workspace.vm_name)
                    if vm is None:
                        raise StateError("workspace VM is missing while relocating artifact evidence")
                    current = _relocated_record(
                        current,
                        workspace.workspace_path,
                        new_path,
                        destination_id(vm, target, location=new_path),
                    )
                    checkpoint(current.claims)
            except AgentworksError as error:
                if error is persistence_failure:
                    raise
                incomplete(sanitize_setup_diagnostic(str(error), logger=target.logger if target is not None else None))
        if incomplete_seen or any(
            claim.role == GIT_EXCLUDE_ROLE and (new_path is None or claim.source != new_path)
            for claim in current.claims
        ):
            failed.append(record.integration)
    if failed:
        output.warn(
            f"Shared artifact exclusion cleanup for workspace '{workspace.name}' is unfinished "
            f"({', '.join(failed)}). Continuing {action}; inspect the reported Git metadata for retained exclusions."
        )
