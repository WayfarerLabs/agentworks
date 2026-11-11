"""Retire workspace-owned associations that outlive its directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.git_excludes import GIT_EXCLUDE_ROLE, reconcile_workspace_excludes
from agentworks.errors import AgentworksError, StateError
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database, WorkspaceRow
    from agentworks.execution.access import VMAccess
    from agentworks.harness_setup.model import NativeClaim


def cleanup_workspace_excludes(db: Database, workspace: WorkspaceRow, access: Callable[[], VMAccess]) -> None:
    """Try known Git retirement without adding a parent-deletion prerequisite."""
    _workspace_excludes(db, workspace, access)


def rehome_workspace_excludes(db: Database, workspace: WorkspaceRow, new_path: str, access: VMAccess) -> None:
    """Transfer known pairs after copying, while the actual old owner still exists."""
    _workspace_excludes(db, workspace, lambda: access, new_path=new_path)


def _workspace_excludes(
    db: Database,
    workspace: WorkspaceRow,
    access: Callable[[], VMAccess],
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
    admin = None
    for record in tuple(state.records):
        if not any(claim.role == GIT_EXCLUDE_ROLE for claim in record.claims) and not (
            new_path is not None and record.artifact_files
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
            if admin is None:
                admin = access()
            reconcile_workspace_excludes(
                admin,
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
            incomplete(str(error))
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
                if admin is None:
                    admin = access()
                reconcile_workspace_excludes(
                    admin,
                    new_path,
                    record.integration,
                    files,
                    record.claims,
                    checkpoint,
                    group=workspace.linux_group,
                    report_incomplete=incomplete,
                    prior_workspace=workspace.workspace_path,
                )
            except AgentworksError as error:
                if error is persistence_failure:
                    raise
                incomplete(str(error))
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
