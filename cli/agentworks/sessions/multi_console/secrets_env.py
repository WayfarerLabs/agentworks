"""Eager-prompting SecretTarget builders and pane env composition.

Everything here answers one of two questions for a console pane (the
sidecar shells a console opens alongside a session's own tmux server):
"what secrets would opening this pane need?" (the ``_*_secret_target(s)``
builders, consumed by callers to eager-resolve before any tmux command
runs) and "what env does this pane actually get?" (``_resolve_pane_env``,
called at split time once the values are in hand).

Some of these names (``_pane_secret_target``, ``_admin_only_secret_target``,
``_console_build_secret_targets``, ``_restore_session_secret_targets``,
``_resolve_pane_env``) are monkeypatched by tests directly on the
``agentworks.sessions.multi_console`` package object. Because a patch there
only rebinds the *package's* attribute, not this module's own global, every
call site anywhere in the package (including the ones below that call a
sibling function defined in this very file) goes through the package object
at call time (``_mc.<name>(...)``) rather than a bare or directly-imported
reference, so the patch is honored no matter which entrypoint the test
drives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks.errors import NotFoundError
from agentworks.resources.access import admin_template

from .attach import _session_linux_user

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import ConsoleRow, ConsoleSessionRow, Database, SessionRow, VMRow
    from agentworks.resources.registry import Registry
    from agentworks.secrets import SecretTarget

# Deliberately matches no AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS entry, so it is
# only ever preservable via `setenv`. That is what makes it a clean capability
# probe: a var the allowlist already covers would pass even without the
# fragment. test_consoles pins the two against each other.
_SUDO_PRESERVE_PROBE_VAR = "AWPROBE"


def _pane_secret_target(
    db: Database,
    registry: Registry,
    *,
    vm: VMRow,
    session: SessionRow,
    is_admin_pane: bool,
) -> SecretTarget | None:
    """Build the SecretTarget for a console pane, for eager-prompting.

    Mirrors the scope-selection logic of ``_resolve_pane_env``. Console
    add-shell panes are sidecar shells rooted in a workspace (not in the
    session itself), so the scope chain stops at workspace:

    - Admin pane: vm + workspace + admin.
    - Agent pane: vm + workspace + agent.

    Session-template env is NOT included -- those vars are for the session
    itself, not for sidecar shells attached to its window. Returns
    ``None`` when the session row is missing fields the resolver would
    need.

    ``is_admin_pane`` is the PROMOTED value, not the operator-passed
    --admin flag. Callers must apply the same promotion ``_split_shell_pane``
    uses: ``use_admin = shell_admin_flag or session_user == admin_user``.
    Passing the raw flag for an admin-mode session (session_user ==
    admin_user) would route through the ``agent_name is None`` branch
    and silently return ``None``, breaking the eager-resolve guarantee
    for that shape.
    """
    from agentworks.agents.templates import resolve_template as _resolve_agent_template
    from agentworks.secrets import SecretTarget
    from agentworks.vms.templates import resolve_template as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_template

    workspace = db.get_workspace(session.workspace_name)
    if workspace is None:
        return None

    vm_tmpl = _resolve_vm_template(registry, vm.template)
    ws_tmpl = _resolve_ws_template(registry, workspace.template)

    if is_admin_pane:
        return SecretTarget(
            vm=vm_tmpl.env,
            workspace=ws_tmpl.env,
            admin=admin_template(registry, vm.admin_template or "default").env,
            label=f"console-pane:{session.name}/admin",
        )

    if session.agent_name is None:
        return None
    agent = db.get_agent(session.agent_name)
    if agent is None:
        return None
    agent_tmpl = _resolve_agent_template(registry, agent.template)
    return SecretTarget(
        vm=vm_tmpl.env,
        workspace=ws_tmpl.env,
        agent=agent_tmpl.env,
        label=f"console-pane:{session.name}/agent",
    )


def _admin_only_secret_target(
    registry: Registry,
    vm: VMRow,
    *,
    label: str,
) -> SecretTarget:
    """SecretTarget for an admin-only console pane (no workspace context).

    Used for ``console.admin_shell`` panes at build time -- a vanilla
    admin login shell with vm + admin scope. Workspace and session
    contexts don't apply (the admin shell isn't tied to either).

    Note: today ``_build_console_tmux`` creates the admin-shell window
    via ``tmux new-session -d ... 'exec $SHELL -l'`` with no SetEnv /
    ``tmux new-session -e`` flags, so the resolved env doesn't yet
    reach the admin shell. The eager-resolve here still produces the
    right operator-facing UX (prompt up front, before any tmux work);
    the admin-shell env-injection wiring consumes the same values dict
    when it lands as a follow-up.
    """
    from agentworks.secrets import SecretTarget
    from agentworks.vms.templates import resolve_template as _resolve_vm_template

    vm_tmpl = _resolve_vm_template(registry, vm.template)
    return SecretTarget(
        vm=vm_tmpl.env,
        admin=admin_template(registry, vm.admin_template or "default").env,
        label=label,
    )


def _console_build_secret_targets(
    db: Database,
    registry: Registry,
    *,
    console: ConsoleRow,
    vm: VMRow,
) -> list[SecretTarget]:
    """Build the SecretTarget list for every pane the console build path
    would open from scratch.

    The set covers panes that OPEN NEW SHELLS:

    - The admin shell window (when ``console.admin_shell`` is set):
      vm + admin scope.
    - For each session window: every configured shell pane (a session-
      attach pane joins the session's existing tmux server and consumes
      no new secrets, so it is skipped here).

    Same ``use_admin`` promotion as ``_split_shell_pane`` (shell admin
    flag OR session_user == admin_user) so the eager-resolve scope
    matches what the build path will actually consume.

    Sessions whose agent / workspace rows are missing get their shell
    panes skipped (matches ``_pane_secret_target``'s defensive
    fallthrough). The operator surfaces these via ``agw doctor``.
    """
    targets: list[SecretTarget] = []
    if console.admin_shell:
        targets.append(
            _mc._admin_only_secret_target(
                registry,
                vm,
                label=f"console={console.name}/admin-shell",
            ),
        )
    for member in db.list_console_sessions(console.name):
        session = db.get_session(member.session_name)
        if session is None:
            continue
        try:
            session_user = _session_linux_user(db, session, vm)
        except NotFoundError:
            continue
        for shell in member.shells:
            use_admin = shell["admin"] or session_user == vm.admin_username
            pane = _mc._pane_secret_target(
                db,
                registry,
                vm=vm,
                session=session,
                is_admin_pane=use_admin,
            )
            if pane is not None:
                targets.append(pane)
    return targets


def _restore_session_secret_targets(
    db: Database,
    registry: Registry,
    *,
    vm: VMRow,
    member: ConsoleSessionRow,
    indices: list[int],
) -> list[SecretTarget]:
    """SecretTargets for the specific missing shell-pane indices that
    ``restore_session`` will open.

    Targets are scoped precisely to the caller-supplied indices: the
    restore path's validation guards filter down to ``missing`` before
    invoking this helper, so over-approximating would risk prompting
    for secrets the command never actually consumes. (The window-
    missing rebuild path in ``restore_session`` calls
    ``_add_session_window`` directly; that path does its own
    enumeration and doesn't route through this helper.)
    """
    targets: list[SecretTarget] = []
    session = db.get_session(member.session_name)
    if session is None:
        return targets
    try:
        session_user = _session_linux_user(db, session, vm)
    except NotFoundError:
        return targets
    for idx in indices:
        shell = member.shells[idx]
        use_admin = shell["admin"] or session_user == vm.admin_username
        pane = _mc._pane_secret_target(
            db,
            registry,
            vm=vm,
            session=session,
            is_admin_pane=use_admin,
        )
        if pane is not None:
            targets.append(pane)
    return targets


def _resolve_pane_env(
    db: Database,
    registry: Registry,
    *,
    values: Mapping[str, str],
    vm: VMRow,
    session: SessionRow,
    pane_user: str,
    is_admin_pane: bool,
) -> dict[str, str]:
    """Compose env for a console add-shell pane attached to a session's window.

    Console add-shell panes are sidecar shells -- they're organized under
    a session's window in the console UI, but they're not *in* the session
    (separate process tree, not part of the session's tmux). Under the
    env-and-secrets identity taxonomy they're "admin or agent shell rooted
    in a workspace": they see workspace dynamic identity but NOT session
    identity, and their operator env stops at workspace scope. The
    sessions themselves (the agent's actual tmux server / shells) keep
    full session context -- they ARE the workload.

    Admin pane: admin + vm + workspace operator env; workspace dynamic
    identity.

    Agent pane: vm + workspace + agent operator env; workspace dynamic
    identity. ``AGENTWORKS_AGENT`` reaches the pane via the agent's
    per-user profile fragment (static identity), not via SetEnv.

    Returns ``{}`` when the session's row is missing fields that the env
    resolution needs (e.g. workspace lookup fails); the caller proceeds
    without env injection rather than raising mid-pane-split.
    """
    from agentworks.agents.templates import resolve_template as _resolve_agent_template
    from agentworks.env import ResourceContext, compose_env
    from agentworks.vms.templates import resolve_template as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_template

    workspace = db.get_workspace(session.workspace_name)
    if workspace is None:
        return {}

    vm_tmpl = _resolve_vm_template(registry, vm.template)
    ws_tmpl = _resolve_ws_template(registry, workspace.template)

    # No session context: add-shell panes are sidecar shells, not part of
    # the session itself. No agent_name in the ctx either -- the agent
    # identifier is per-user-static and lives in the on-disk profile
    # fragment, not in per-context SetEnv.
    from agentworks.vms.sites import site_platform_name

    ctx = ResourceContext(
        vm_name=vm.name,
        platform=site_platform_name(vm.site, registry),
        site=vm.site,
        user=pane_user,
        workspace_name=workspace.name,
        workspace_dir=workspace.workspace_path,
    )

    if is_admin_pane:
        return compose_env(
            values=values,
            ctx=ctx,
            vm=vm_tmpl.env,
            workspace=ws_tmpl.env,
            admin=admin_template(registry, vm.admin_template or "default").env,
        )

    if session.agent_name is None:
        # In theory unreachable given the caller's ``use_admin`` logic in
        # ``_split_shell_pane`` (an admin-mode session has session_user ==
        # admin_user, which forces is_admin_pane=True up there). Guarded
        # here against future invariant drift so a non-admin pane on a
        # session with no agent silently skips env rather than crashing.
        return {}

    agent = db.get_agent(session.agent_name)
    if agent is None:
        return {}
    agent_tmpl = _resolve_agent_template(registry, agent.template)
    return compose_env(
        values=values,
        ctx=ctx,
        vm=vm_tmpl.env,
        workspace=ws_tmpl.env,
        agent=agent_tmpl.env,
    )
