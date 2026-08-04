"""Session delete, describe, list, and attach operations."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import (
    BrokenStateError,
    ExternalError,
    StateError,
    UserAbort,
)
from agentworks.sessions._resource_cleanup import cleanup_now_empty_resource
from agentworks.sessions.tmux import AGENT_SOCKET_ROOT

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.sessions.tmux import RunCommand


def delete_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Delete a session. Prompts if running/unknown (--yes to skip). --force for BROKEN."""
    session = _mgr._require_session(db, name)
    with _mgr._prepare_vm(db, config, session, operation="session-delete") as (
        ws,
        vm,
        _run_command,
        _run_as_root,
        admin_target,
    ):
        session = _mgr._ensure_pid(session, target=admin_target, db=db)
        status = _mgr.check_session_status(session, target=admin_target)

        # UNKNOWN is impossible here -- _ensure_pid raises on unresolvable sessions
        if status == SessionStatus.BROKEN and not force:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
                hint="Use --force to delete.",
            )

        # Pick the destructive-op transport BEFORE prompting the operator.
        # For agent sessions, ``_build_session_target`` probes direct agent
        # SSH; a pre-rollout agent surfaces here as an
        # actionable error rather than after the operator has already
        # confirmed the delete. The helper returns a same-uid target, so
        # no sudo is needed for the destructive ops below.
        session_target = _mgr._build_session_target(session, vm=vm, config=config, db=db, admin_target=admin_target)
        session_run_command: RunCommand = session_target.run
        kill_sudo = False

        # Confirm before any destructive action
        if not yes and not output.confirm(f"Delete session '{name}'?"):
            raise UserAbort("delete cancelled")

        # Now kill if needed
        if status == SessionStatus.OK:
            sock = session.socket_path
            if not _mgr._kill_session(name, run_command=session_run_command, socket_path=sock):
                # Race: session may have exited between check and kill. Recheck.
                recheck = _mgr.check_session_status(session, target=admin_target)
                if recheck != SessionStatus.STOPPED:
                    raise ExternalError(
                        f"failed to stop session '{name}' for deletion",
                        entity_kind="session",
                        entity_name=name,
                    )
        elif status == SessionStatus.BROKEN:
            from agentworks.sessions.tmux import force_kill_tmux_server

            output.warn(f"Session '{name}' is broken (tmux unreachable), force-killing via PID")
            assert session.pid is not None
            killed = force_kill_tmux_server(
                session.pid,
                target=session_target,
                socket_path=session.socket_path,
                log=output.detail,
                use_sudo=kill_sudo,
            )
            if not killed:
                raise ExternalError(
                    f"failed to kill PID {session.pid} for session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                )

        # Clean up socket if the server is dead (don't remove a live socket)
        sock = session.socket_path
        if sock and sock.startswith(AGENT_SOCKET_ROOT + "/"):
            post_status = _mgr.check_session_status(session, target=admin_target)
            if post_status == SessionStatus.STOPPED:
                session_target.run(f"rm -f {shlex.quote(sock)}", sudo=kill_sudo, check=False)
            else:
                output.warn(f"Session '{name}' status is {post_status.value} after delete, socket preserved at {sock}")

        # Capture console memberships before delete; the FK cascade on
        # console_sessions zeroes the join table the moment the session row goes.
        member_consoles = [c.name for c in db.list_consoles_for_session(name)]

        db.delete_session(name)

        # Clean up implicit grant for this session
        if session.agent_name:
            db.delete_agent_grant(session.agent_name, session.workspace_name, "implicit", session_name=name)
            # If no grants remain, remove from workspace group
            if not db.has_any_grant(session.agent_name, session.workspace_name):
                from agentworks.agents.grants import remove_from_workspace_group

                agent = db.get_agent(session.agent_name)
                if agent:
                    remove_from_workspace_group(vm, config, db, agent.linux_user, session.workspace_name)

        _mgr._regenerate_tmuxinator(db, config, vm, ws)

        # Best-effort console cleanup runs after all DB / tmuxinator state has
        # settled. Stale tmux windows are recoverable cosmetic noise; if the
        # helper raises AgentworksError we skip the success message and any
        # created_workspace / created_agent cleanup below -- those would re-use
        # the same broken transport and just compound errors.
        if member_consoles:
            from agentworks.sessions.multi_console import kill_session_windows

            # Consoles are admin-owned (carve-out): admin manages
            # admin's tmux server. Use admin_target regardless of session mode.
            kill_session_windows(admin_target, pairs=[(c, name) for c in member_consoles])

        output.info(f"Session '{name}' deleted")

        # Report the consoles that referenced this session, and handle any
        # left empty by the FK cascade. ``member_consoles`` was snapshotted
        # before the delete (the cascade zeroes ``console_sessions``), so a
        # member whose configured-session count is now zero has been emptied.
        # This is the operator signal issue #248 asks for: the cascade used to
        # silently empty a console built around a single session with no trace.
        if member_consoles:
            noun = "console" if len(member_consoles) == 1 else "consoles"
            output.info(f"Removed '{name}' from {noun}: {', '.join(member_consoles)}")
            # Any member left with no configured sessions gets the shared
            # empty-console treatment (offer / report-but-keep, issue
            # #248/#261/#265): the wrapper owns the console parametrization,
            # cleanup_now_empty_resource owns the policy, and console
            # remove-sessions routes through the same wrapper so the two
            # paths stay byte-identical.
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
        # deleted" line has already printed. ``session`` was snapshotted before
        # ``db.delete_session``, so its workspace_name / agent_name / created_*
        # fields are still readable here.
        _cleanup_now_empty_workspace(db, config, session, yes=yes)
        _cleanup_now_empty_agent(db, config, session, yes=yes)


def _cleanup_now_empty_workspace(
    db: Database,
    config: Config,
    session: SessionRow,
    *,
    yes: bool,
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
        delete=lambda: delete_workspace(db, config, name, yes=True),
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
) -> None:
    """Handle the deleted session's agent when it becomes a cleanup candidate.

    An agent is a candidate only when it has NO remaining sessions AND NO
    explicit workspace grants (``agent_is_unused``): a standing explicit
    grant is operator intent to use the agent, so a granted-but-sessionless
    agent is left alone rather than torn down (and its grants revoked) as a
    side effect of a session delete. Admin sessions (``agent_name is None``)
    have no agent to clean up and are skipped. Otherwise mirrors
    ``_cleanup_now_empty_workspace`` through the shared
    :func:`cleanup_now_empty_resource` shape: offer interactively regardless of
    provenance, auto-delete only a session-created agent under --yes, and
    report-but-keep any other candidate agent. A follow-on ``delete_agent``
    failure warns rather than aborts.
    """
    from agentworks.agents.manager import agent_is_unused, delete_agent

    name = session.agent_name
    if name is None:
        return
    if not agent_is_unused(db, name):
        return
    # Bind a non-optional local so the delete closure (which mypy widens
    # narrowed names back inside) still sees a plain str.
    agent_name: str = name

    cleanup_now_empty_resource(
        kind="agent",
        name=agent_name,
        created=session.created_agent,
        delete=lambda: delete_agent(db, config, name=agent_name, yes=True),
        manual_command=f"agw agent delete {agent_name}",
        yes=yes,
        empty_clause="now has no sessions",
        report_clause="now has no sessions",
    )


def describe_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Show session details.

    Runs inside ``_prepare_vm``'s gate span: a hold the imperative
    body did not take (it gated and discarded the platform). The
    superset is a no-op everywhere but WSL2, where it anchors the
    status probes against the idle timer.
    """
    session = _mgr._require_session(db, name)
    # Resolve the harness_integration for the display block (config-only, no
    # vm/target dependency) before entering the boundary span.
    # ``_display_registry`` guards the build for consistency with
    # ``list_sessions``, but note ``_prepare_vm`` below ALSO builds the
    # registry (for the VM gate) and is not guarded: describe genuinely
    # needs a valid registry to probe live status, so a truly broken
    # registry aborts describe there regardless. The "-" fallback here
    # is thus defensive, not a graceful-degrade path describe can reach.
    harness_integration_label = _mgr._display_harness_integration(_mgr._display_registry(config), session.template)
    with _mgr._prepare_vm(db, config, session, operation=None) as (
        _ws,
        vm,
        _run_command,
        _run_as_root,
        target,
    ):
        session = _mgr._ensure_pid(session, target=target, db=db)

        status = _mgr.check_session_status(session, target=target)

        # Build status label with PID if running and current boot
        if status == SessionStatus.OK and session.pid and session.pid > 0:
            status_label = f"running (PID {session.pid})"
        elif status == SessionStatus.BROKEN and session.pid and session.pid > 0:
            status_label = f"broken (PID {session.pid} alive, tmux unreachable)"
        else:
            status_label = {
                SessionStatus.OK: "running",
                SessionStatus.STOPPED: "stopped",
                SessionStatus.BROKEN: "broken",
                SessionStatus.UNKNOWN: "unknown",
            }[status]

        mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"

        output.info(f"Name:       {session.name}")
        output.info(f"Workspace:  {session.workspace_name}")
        output.info(f"VM:         {vm.name}")
        output.info(f"Template:   {session.template}")
        output.info(f"Harness integration: {harness_integration_label}")
        output.info(f"Mode:       {mode_label}")
        output.info(f"Status:     {status_label}")
        output.info(f"Created:    {session.created_at}")
        output.info(f"Updated:    {session.updated_at}")


def list_sessions(
    db: Database,
    config: Config,
    *,
    workspace_name: str | list[str] | None = None,
    vm_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    admin_only: bool = False,
    no_status: bool = False,
    names_only: bool = False,
) -> None:
    """List sessions with batched status checks (one SSH call per VM, parallel).

    Status resolution is has-session-first; PID/boot_id are only used as a
    follow-up when agent checks fail.

    With ``names_only=True``, emit one session name per line and
    skip both the SSH status batch and the table render. Used by
    shell completion (see issue #147); the order matches the table's
    workspace-grouped order so completion stays stable.
    """
    sessions = _mgr.filter_sessions(
        db,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )

    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No sessions found" line below is
        # for human readers only. Match the table's workspace-grouped
        # order so completion stays stable across renderers.
        names_by_ws: dict[str, list[SessionRow]] = {}
        for session in sessions:
            names_by_ws.setdefault(session.workspace_name, []).append(session)
        for ws_name in sorted(names_by_ws):
            for session in names_by_ws[ws_name]:
                output.info(session.name)
        return

    if not sessions:
        output.info("No sessions found.")
        return

    # Auto-repair sessions with missing PIDs, then batch check.
    # The status path SSHes to every involved VM; anchor each one (no-op
    # on non-WSL2) so the probe doesn't lose them mid-check.
    status_keepalive_vms: list[VMRow] = [] if no_status else _mgr._distinct_vms_for_sessions(db, sessions)

    status_map: dict[str, SessionStatus] = {}
    with _mgr._batch_vm_boundary(db, config, status_keepalive_vms):
        if not no_status:
            sessions = _mgr.ensure_pids_batch(sessions, db=db, config=config)
            status_map = _mgr.batch_check_all_sessions(sessions, db=db, config=config)

    # Resolve each session's concrete harness integration for the display column.
    # build_registry and resolve_template are config-only (no SSH), so
    # this is cheap; still, resolve each DISTINCT template at most once
    # and guard both the registry build and each resolution so a bad
    # registry or one bad template shows "-" rather than aborting the
    # whole listing.
    registry = _mgr._display_registry(config)
    harness_integration_by_template: dict[str, str] = {}

    def _harness_integration_for(template_name: str) -> str:
        if template_name not in harness_integration_by_template:
            harness_integration_by_template[template_name] = _mgr._display_harness_integration(registry, template_name)
        return harness_integration_by_template[template_name]

    # Build table rows grouped by workspace
    by_workspace: dict[str, list[SessionRow]] = {}
    for session in sessions:
        by_workspace.setdefault(session.workspace_name, []).append(session)

    rows: list[tuple[str, str, str, str, str, str, str]] = []
    broken_names = []
    unknown_names = []
    for ws_name, ws_sessions in sorted(by_workspace.items()):
        ws = db.get_workspace(ws_name)
        vm_name = ws.vm_name if ws else "-"

        for session in ws_sessions:
            if no_status:
                status = "-"
            elif session.pid == PID_STOPPED:
                status = "stopped"
            elif session.pid is None or session.boot_id is None:
                status = "unknown"
            elif session.name in status_map:
                s_status = status_map[session.name]
                status = {
                    SessionStatus.OK: "running",
                    SessionStatus.STOPPED: "stopped",
                    SessionStatus.BROKEN: "broken",
                    SessionStatus.UNKNOWN: "unknown",
                }[s_status]
            else:
                # No status available (VM unreachable or SSH failure during batch check)
                status = "-"
            mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"
            rows.append(
                (
                    session.name,
                    ws_name,
                    vm_name,
                    session.template,
                    _harness_integration_for(session.template),
                    mode_label,
                    status,
                )
            )
            if status == "broken":
                broken_names.append(session.name)
            elif status == "unknown":
                unknown_names.append(session.name)

    if not rows:
        output.info("No sessions found.")
        return

    headers = ["NAME", "WORKSPACE", "VM", "TEMPLATE", "HARNESS INTEGRATION", "MODE", "STATUS"]
    for line in output.render_table(headers, rows):
        output.info(line)

    if broken_names or unknown_names:
        output.info("")
        if broken_names:
            output.warn(
                f"{len(broken_names)} session(s) are broken (tmux unreachable): "
                f"{', '.join(broken_names)}. Use resume/stop/delete --force."
            )
        if unknown_names:
            output.warn(
                f"{len(unknown_names)} session(s) have unknown status: "
                f"{', '.join(unknown_names)}. Status could not be determined."
            )


def attach_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> int:
    """Attach to a session's tmux session (interactive).

    Returns the interactive session's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service),
    mirroring :func:`agentworks.vms.manager.exec_vm`.
    """
    from agentworks.sessions.tmux import tmux_cmd

    session = _mgr._require_session(db, name)
    with _mgr._prepare_vm(db, config, session, operation="session-attach") as (
        _ws,
        _vm,
        _run_command,
        _run_as_root,
        target,
    ):
        session = _mgr._ensure_pid(session, target=target, db=db)
        status = _mgr.check_session_status(session, target=target)

        if status == SessionStatus.STOPPED:
            raise StateError(
                f"session '{name}' is not running",
                entity_kind="session",
                entity_name=name,
            )
        if status == SessionStatus.BROKEN:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
            )

        q_session = shlex.quote(name)
        return target.interactive(tmux_cmd(f"attach -t {q_session}", session.socket_path))
