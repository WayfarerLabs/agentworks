"""Session delete: its runtime and definition, and the offers to remove what it left empty."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import SessionStatus
from agentworks.errors import (
    AgentworksError,
    BrokenStateError,
    StateError,
    UserAbort,
)
from agentworks.sessions._resource_cleanup import cleanup_now_empty_resource

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.execution.access import VMAccess
    from agentworks.secrets.policy import TtyInteractionPolicy


def delete_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Delete a session. Prompts if running/unknown (--yes to skip). --force for BROKEN.

    The gate, the secrets, the status and access probes and the confirmation
    all run before the session's lock set is taken, and the status probe
    persists nothing. Under the set the session row, the VM row, the
    consoles holding the session and the outcome of each probe are read
    again, and a change refuses before anything is torn down. The offers to
    delete a console, workspace or agent the delete left empty run after the
    set is released.
    """
    session = _mgr._require_session(db, name)
    _mgr._require_workspace(db, session.workspace_name)
    member_consoles = _delete_session_runtime(
        db, config, session=session, force=force, yes=yes, interaction=interaction
    )
    # Any console the FK cascade left with no configured sessions gets the
    # shared empty-console treatment (offer / report-but-keep, issue
    # #248/#261/#265): the wrapper owns the console parametrization,
    # cleanup_now_empty_resource owns the policy, and console remove-sessions
    # routes through the same wrapper so the two paths stay byte-identical.
    from agentworks.sessions.multi_console import offer_delete_if_empty_consoles

    offer_delete_if_empty_consoles(db, config, member_consoles, yes=yes)
    # Generalized workspace/agent cleanup (issue #266). The session row is
    # already gone, so the "now has no sessions" checks below reflect the
    # post-delete state. This is the single unified path for both the
    # session-created case (the old provenance-only offer) and every other
    # case where deleting this session happens to empty the resource:
    #   - interactive (no --yes): if the workspace / agent now has no
    #     sessions, OFFER to delete it, regardless of provenance.
    #   - --yes: auto-delete only what THIS session created (the
    #     created_workspace / created_agent provenance flags); otherwise
    #     report the resource is now empty and name the manual delete
    #     command, mirroring the console report-but-keep treatment above.
    # Workspace first, then agent; the order is load-bearing, not
    # arbitrary. Deleting the now-unused workspace tears down its grants:
    # the FK cascade on ``agent_workspace_grants`` removes every agent's
    # explicit grant rows for that workspace (and ``revoke_workspace_grants``
    # drops the matching Linux group membership). So if this session's
    # workspace was the agent's LAST standing grant, deleting it makes the
    # agent (checked next) a cleanup candidate in the same run. That
    # cascade is intended and desirable, and it is contingent on the
    # workspace ACTUALLY being deleted: the operator confirmed the offer,
    # or it auto-deleted under --yes because this session created it. If
    # the workspace is kept, the grant remains and the still-granted agent
    # stays guarded. Both run after the console cleanup so the "Session
    # deleted" line has already printed, and after the set is released;
    # each delete they trigger takes its own set, reads its rows again, and
    # refuses a resource that stopped being the candidate it was offered as
    # (a grant committed meanwhile keeps it).
    # ``session`` was snapshotted before ``db.delete_session``, so its
    # workspace_name / agent_name / created_* fields are still readable here.
    _cleanup_now_empty_workspace(db, config, session, yes=yes, interaction=interaction)
    _cleanup_now_empty_agent(db, config, session, yes=yes, interaction=interaction)


def _legacy_running_row(session: SessionRow) -> bool:
    """A row from before tmux sockets were recorded, with a live PID: it counts as running."""
    return session.socket_path is None and session.pid is not None and session.pid > 0


def _delete_status_needs_pid_repair(session: SessionRow) -> bool:
    """Whether the row's PID evidence must be repaired, which writes it, before its delete can decide its status.

    Repair can also give an old admin row its address, so such a row's
    status is decided only under the lock set.
    """
    return not _legacy_running_row(session) and (_mgr._needs_repair(session) or session.socket_path is None)


def _delete_session_status(db: Database, session: SessionRow, *, admin: VMAccess, force: bool) -> SessionStatus:
    """The session's status for its delete, refusing one that is unknown, or broken without ``force``.

    It only observes. A row that :func:`_delete_status_needs_pid_repair`
    flags is repaired (``_ensure_pid``) by the caller first, under the lock set.
    """
    name = session.name
    if _legacy_running_row(session):
        status = SessionStatus.RUNNING
    else:
        status = _mgr.check_session_status(session, admin=admin, server=_mgr.resolve_session_tmux_server(db, session))
    if status == SessionStatus.UNKNOWN:
        raise StateError(
            f"session '{name}' runtime state is unknown",
            entity_kind="session",
            entity_name=name,
            hint="Retry after transport access is reliable; no runtime was changed.",
        )
    if status == SessionStatus.BROKEN and not force:
        raise BrokenStateError(
            f"session '{name}' is broken (PID alive but tmux unreachable).",
            entity_kind="session",
            entity_name=name,
            hint="Use --force to delete.",
        )
    return status


def _delete_session_runtime(
    db: Database,
    config: Config,
    *,
    session: SessionRow,
    force: bool,
    yes: bool,
    interaction: TtyInteractionPolicy,
) -> list[str]:
    """Delete one selected definition and its runtime, asking first and acting under its lock set.

    The set holds the session, in agent mode its implicit grant (whose
    membership the delete may remove), and each console holding it, all
    exclusive. Returns the consoles the session was a member of.
    """
    name = session.name
    selected_session_uuid = session.session_uuid
    from agentworks.recheck import require_request_unchanged, require_unchanged, require_vm_unchanged, session_fields
    from agentworks.sessions.manager._lifecycle import _require_selected_session

    # Refuse a row the owner rule rejects before any VM work.
    _mgr.resolve_session_tmux_server(db, session)
    from agentworks.native_locks import probe_locks

    from ._intent import session_delete_request

    request = session_delete_request(db, session)
    probe_locks(db.path, request)
    with _mgr._prepare_vm(
        db,
        config,
        session,
        operation="session-delete",
        interaction=interaction,
    ) as (ws, vm_node, admin, log):
        vm = vm_node.row
        session = _require_selected_session(db, name, selected_session_uuid)
        # A row whose PID evidence needs repair is undecided until the set is held.
        status = (
            None
            if _delete_status_needs_pid_repair(session)
            else _delete_session_status(db, session, admin=admin, force=force)
        )

        # Pick the destructive-op access BEFORE prompting the operator.
        # For agent sessions, ``_verified_session_access`` probes direct agent
        # SSH; a pre-rollout agent surfaces here as an
        # actionable error rather than after the operator has already
        # confirmed the delete. The access is the owner's, so nothing
        # below elevates.
        _mgr._verified_session_access(session, vm=vm, config=config, db=db, admin=admin, logger=log)

        # Confirm before any destructive action
        if not yes and not output.confirm(f"Delete session '{name}'?"):
            raise UserAbort("delete cancelled")
        with vm_node.locked(request):
            current = _require_selected_session(db, name, selected_session_uuid)
            require_unchanged("session", name, session_fields(session), session_fields(current))
            require_vm_unchanged(db, vm)
            require_request_unchanged("session", name, request, session_delete_request(db, current))
            member_consoles = [c.name for c in db.list_consoles_for_session(name)]
            # Repair writes the row, and can give an old admin row its
            # dedicated socket, so the status is read from the repaired row.
            session = _mgr._ensure_pid(current, admin=admin, db=db)
            observed = _delete_session_status(db, session, admin=admin, force=force)
            if status is not None:
                require_unchanged("session", name, status, observed)
            status = observed
            owner = _mgr._verified_session_access(session, vm=vm, config=config, db=db, admin=admin, logger=log)

            session = _require_selected_session(db, name, selected_session_uuid)
            if status != SessionStatus.STOPPED:
                _mgr._teardown_session(
                    session,
                    access=owner,
                    target_owns_session=True,
                    db=db,
                    force=force,
                    legacy_running_authorized=True,
                )

            session = _require_selected_session(db, name, selected_session_uuid)

            from agentworks.artifacts.session import cleanup_session_artifacts

            cleanup_session_artifacts(db, session, owner)
            _require_selected_session(db, name, selected_session_uuid)

            # When the session's implicit grant is the agent's last on the
            # workspace, the membership goes first: a failed removal raises
            # with the session and grant rows in place, so a rerun retries it.
            if session.agent_name:
                from agentworks.agents.grants import holds_other_grant, remove_from_workspace_group

                agent = db.get_agent(session.agent_name)
                last_grant = not holds_other_grant(
                    db, session.agent_name, session.workspace_name, grant_type="implicit", session_name=name
                )
                if agent and last_grant:
                    try:
                        remove_from_workspace_group(
                            vm, config, db, agent.linux_user, session.workspace_name, logger=log
                        )
                    except AgentworksError as error:
                        error.add_hint(f"The session and its grant were kept; run 'agw session delete {name}' again.")
                        raise
            # The FK cascade on console_sessions removes the memberships with the row.
            db.delete_session(name)
            if session.agent_name:
                db.delete_agent_grant(session.agent_name, session.workspace_name, "implicit", session_name=name)

            # Best-effort console cleanup runs after all DB / workspace state has
            # settled. Stale tmux windows are recoverable cosmetic noise; if the
            # helper raises AgentworksError we skip the success message and any
            # created_workspace / created_agent cleanup below, which would reach
            # the same broken VM and just compound errors.
            if member_consoles:
                from agentworks.sessions.multi_console import kill_session_windows

                # Consoles are admin-owned (carve-out): admin manages
                # admin's tmux server, whatever the session's mode.
                kill_session_windows(admin, pairs=[(c, name) for c in member_consoles])

            output.info(f"Session '{name}' deleted")

            # Report the consoles that referenced this session (issue #248):
            # the cascade used to silently empty a console built around a
            # single session with no trace.
            if member_consoles:
                noun = "console" if len(member_consoles) == 1 else "consoles"
                output.info(f"Removed '{name}' from {noun}: {', '.join(member_consoles)}")
    return member_consoles


def _cleanup_now_empty_workspace(
    db: Database,
    config: Config,
    session: SessionRow,
    *,
    yes: bool,
    interaction: TtyInteractionPolicy,
) -> None:
    """Handle the deleted session's workspace when it now has no sessions.

    Call AFTER ``db.delete_session`` so the emptiness check is accurate. The
    offer / auto-delete / report-but-keep decision and the warn-on-failure
    guard are the shared :func:`cleanup_now_empty_resource` shape: offer
    interactively regardless of provenance, auto-delete a session-created
    workspace under --yes, otherwise report the empty workspace and name the
    manual command rather than aborting after the "Session deleted" line has
    printed (matching the console offer hardening from issue #261).

    One guard sits on top of that shape. Deleting the workspace cascades
    ``agent_workspace_grants``, silently revoking any agent's explicit,
    operator-authored standing grant on it (``grant-workspaces``; the deleted
    session's own agent counts too, while grant-all agents' materialized rows
    are blanket policy, not per-workspace intent, and do not). So when such
    a grant exists we refuse the --yes auto-delete even for a session-created
    workspace, downgrading it to report-but-keep and naming the granting
    agent(s); and the interactive offer discloses whose grants a delete would
    revoke. Grant-all agents are excluded by their flag (their materialized
    rows are blanket policy, not per-workspace intent); implicit grants never
    count. See :func:`workspace_external_explicit_granters`.
    """
    from agentworks.workspaces.manager import (
        delete_workspace,
        workspace_external_explicit_granters,
        workspace_has_sessions,
    )

    name = session.workspace_name
    if workspace_has_sessions(db, name):
        return

    granters = workspace_external_explicit_granters(db, name)
    if granters:
        granter_list = ", ".join(granters)
        empty_clause = f"now has no sessions (deleting revokes explicit grant(s) held by: {granter_list})"
        report_clause = f"now has no sessions but agent(s) {granter_list} hold explicit grants"
    else:
        empty_clause = "now has no sessions"
        report_clause = "now has no sessions"

    cleanup_now_empty_resource(
        kind="workspace",
        name=name,
        # A standing external grant vetoes auto-delete even for a session-created
        # workspace: passing created=False here routes --yes to report-but-keep
        # (the grant is preserved) while the interactive offer still fires with
        # the disclosing empty_clause above.
        created=session.created_workspace and not granters,
        # The delete refuses granters other than these.
        delete=lambda: delete_workspace(
            db,
            config,
            name,
            yes=True,
            only_with_granters=granters,
            interaction=interaction,
        ),
        manual_command=f"agw workspace delete {name}",
        yes=yes,
        empty_clause=empty_clause,
        report_clause=report_clause,
    )


def _cleanup_now_empty_agent(
    db: Database,
    config: Config,
    session: SessionRow,
    *,
    yes: bool,
    interaction: TtyInteractionPolicy,
) -> None:
    """Handle the deleted session's agent when it becomes a cleanup candidate.

    An agent is a candidate only when it has NO remaining sessions AND NO
    standing workspace grants (``agent_has_grants``): a standing grant is
    operator intent to use the agent, so a granted-but-sessionless agent is
    left alone rather than torn down (and its grants revoked) as a side effect
    of a session delete. When this session created the agent but other sessions
    still use it, report why the agent is being kept. Admin sessions
    (``agent_name is None``) have no agent to clean up and are skipped.
    Otherwise mirrors
    ``_cleanup_now_empty_workspace`` through the shared
    :func:`cleanup_now_empty_resource` shape: offer interactively regardless of
    provenance, auto-delete only a session-created agent under --yes, and
    report-but-keep any other candidate agent. A follow-on ``delete_agent``
    failure warns rather than aborts.
    """
    from agentworks.agents.manager import agent_has_grants, delete_agent

    name = session.agent_name
    if name is None:
        return
    remaining_sessions = db.list_sessions(agent_name=name)
    if remaining_sessions:
        if session.created_agent:
            output.info(
                f"Agent '{name}' was created with this session but remains in use by "
                f"{output.count(len(remaining_sessions), 'other session')}:"
            )
            for remaining_session in remaining_sessions:
                output.detail(remaining_session.name)
        return
    if agent_has_grants(db, name):
        return
    # Bind a non-optional local so the delete closure (which mypy widens
    # narrowed names back inside) still sees a plain str.
    agent_name: str = name

    cleanup_now_empty_resource(
        kind="agent",
        name=agent_name,
        created=session.created_agent,
        # The delete refuses an agent that gained a session or a grant.
        delete=lambda: delete_agent(
            db,
            config,
            name=agent_name,
            yes=True,
            only_if_unused=True,
            interaction=interaction,
        ),
        manual_command=f"agw agent delete {agent_name}",
        yes=yes,
        empty_clause="now has no sessions",
        report_clause="now has no sessions",
    )
