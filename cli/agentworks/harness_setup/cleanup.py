"""Retire workspace-owned associations that outlive its directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.git_excludes import GIT_EXCLUDE_ROLE, reconcile_workspace_excludes
from agentworks.errors import AgentworksError, StateError
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database, WorkspaceRow
    from agentworks.harness_setup.model import NativeClaim
    from agentworks.transports import Transport


def cleanup_workspace_excludes(db: Database, workspace: WorkspaceRow, runner: Callable[[], Transport]) -> None:
    """Under the VM mutation guard, retire known Git pairs before deleting evidence."""
    try:
        state = read_native_setup(db, "workspace", workspace.name)
    except StateError:
        output.warn(
            f"Shared artifact exclusion cleanup for workspace '{workspace.name}' could not be identified from "
            "its unreadable native setup evidence. Continuing workspace deletion; inspect shared Git metadata."
        )
        return
    failed = []
    target = None
    for record in tuple(state.records):
        if not any(claim.role == GIT_EXCLUDE_ROLE for claim in record.claims):
            continue
        current = record.model_copy(update={"complete": False, "pending_cleanup": True, "incomplete_reasons": ()})

        def checkpoint(claims: tuple[NativeClaim, ...]) -> None:
            nonlocal current, state
            current = current.model_copy(update={"claims": claims})
            state = replace_setup_record(state, current)
            write_native_setup(db, "workspace", workspace.name, state, operation="workspace-delete")

        def incomplete(reason: str, *, integration: str = record.integration) -> None:
            nonlocal current
            current = current.model_copy(update={"incomplete_reasons": (*current.incomplete_reasons, reason)})
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
            incomplete(str(error))
        if any(claim.role == GIT_EXCLUDE_ROLE for claim in current.claims):
            failed.append(record.integration)
    if failed:
        raise StateError(
            f"cannot delete workspace '{workspace.name}': shared artifact exclusion cleanup is unfinished "
            f"({', '.join(failed)})",
            entity_kind="workspace",
            entity_name=workspace.name,
            hint="The workspace and cleanup evidence were retained. Resolve the reported Git metadata conflict "
            "or access failure, then retry workspace delete.",
        )
