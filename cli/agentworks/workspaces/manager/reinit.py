"""Workspace reinit (live-state convergence) and its supporting helpers.

``_rehome_partial_state_hint`` lives here (rather than in ``rehome.py``)
purely to keep both files comfortably under the line-count budget; it is
used by ``rehome._rehome_vm``, which imports it from this module.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import NotFoundError
from agentworks.vms.manager import gated_vm_boundary
from agentworks.workspaces.manager._common import _workspace_scope

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.resources.registry import Registry
    from agentworks.transports import Transport


def reinit_workspace(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Re-run workspace initialization to converge live VM state to the DB.

    Idempotent and forward-only. Steps split into two shapes:

    - **Detection-based** (group existence, admin membership, agent group
      membership against the grant table): probe live state first and only
      apply a fix when state diverges. Report `Fixed:` when a fix ran,
      `OK:` when no change was needed.
    - **Always-applied** (directory ownership, permissions, SGID, ACLs,
      parent-directory traversal): re-run their canonical commands every
      time; the underlying chown/chmod/setfacl are no-ops on already-correct
      state. Report `OK:` on success.

    Git identity (the template's `git_user_name` / `git_user_email`)
    converges here too, detection-based: an identity added or changed on
    the template after create is stamped into the checkout's repo-local
    config, and an already-correct value reports `OK:`.

    Same semantic as `vm reinit` and `agent reinit`: the declared state in
    the DB is the source of truth; this reinit converges live state to
    match.

    Orchestrated (``vms.manager.gated_vm_boundary``, WORKSPACE scope):
    the graph is the live VM alone (the workspace has no capability
    instances and nothing realization-shaped; convergence mutates
    through the VM transport), the activation gate replaces this
    command's ``keep_active``, opening BEFORE the preflight sweep with
    its just-in-time values seeding the boundary resolver, and the
    whole SSH convergence body runs inside the held-active span. The
    not-found checks stay pre-boundary: a refusal costs zero prompts,
    zero resolves, and zero gate events.
    """
    from agentworks.agents.manager import AGENT_PREFIX
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHError
    from agentworks.transports import transport

    # build_registry runs first so framework miss-policies fire before
    # any DB / VM business logic.
    registry = build_registry(config)

    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    vm = db.get_vm(ws.vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{ws.vm_name}' not found",
            entity_kind="vm",
            entity_name=ws.vm_name,
        )

    with gated_vm_boundary(db, config, registry, vm, scope=_workspace_scope(db, vm, name)):
        target = transport(vm, config)
        ws_group = ws.linux_group
        fixes = 0

        output.info(f"Reinitializing workspace '{name}' on VM '{vm.name}'...")

        # 0. Ensure acl package is installed (needed for setfacl)
        try:
            has_setfacl = target.run("which setfacl", sudo=True, check=False)
            if not has_setfacl.ok:
                target.run("apt-get install -y -qq acl", sudo=True, timeout=60)
                output.detail("Fixed: installed acl package")
                fixes += 1
            else:
                output.detail("OK: acl package")
        except SSHError as e:
            output.warn(f"acl package check failed: {e}")

        # 1. Ensure the workspace group recorded in the DB exists on the VM.
        try:
            group_exists = target.run(f"getent group {ws_group}", sudo=True, check=False)
            if not group_exists.ok:
                target.run(
                    f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'",
                    sudo=True,
                )
                output.detail(f"Fixed: created group {ws_group}")
                fixes += 1
            else:
                output.detail(f"OK: group {ws_group} exists")
        except SSHError as e:
            output.warn(f"group check failed: {e}")

        # 2. Ensure admin is in the group
        try:
            in_group = target.run(
                f"id -nG {vm.admin_username}",
                sudo=True,
                check=False,
            )
            if in_group.ok and ws_group not in in_group.stdout.split():
                target.run(f"usermod -aG {ws_group} {vm.admin_username}", sudo=True)
                output.detail(f"Fixed: added admin '{vm.admin_username}' to {ws_group}")
                fixes += 1
            else:
                output.detail(f"OK: admin in {ws_group}")
        except SSHError as e:
            output.warn(f"admin group check failed: {e}")

        # 3. Fix directory permissions (recursive chgrp so ACLs apply correctly)
        try:
            target.run(f"chown -R {vm.admin_username}:{ws_group} {ws.workspace_path}", sudo=True, timeout=120)
            target.run(f"chmod 2770 {ws.workspace_path}", sudo=True)
            # Set SGID on all subdirectories so new files inherit the workspace group.
            # This is critical for atomic-write tools (including Claude Code) that
            # create a temp file and rename it over the original.
            target.run(
                f"find {shlex.quote(ws.workspace_path)} -type d -exec chmod g+s {{}} +",
                sudo=True,
                timeout=120,
            )
            output.detail("OK: directory ownership and permissions")
        except SSHError as e:
            output.warn(f"permission fix failed: {e}")

        # 4. Fix ACLs
        # Default ACLs only apply to directories; use find to avoid warnings on files.
        # Effective ACLs apply to all entries and should not produce output.
        try:
            target.run(
                f"find {ws.workspace_path} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
                sudo=True,
                timeout=120,
            )
            target.run(
                f"setfacl -R -m g::rwx -m m::rwx {ws.workspace_path}",
                sudo=True,
                timeout=120,
            )
            output.detail("OK: ACLs")
        except SSHError as e:
            output.warn(f"ACL fix failed: {e}")

        # 5. Fix parent directory traversal
        try:
            target.run(
                f'sh -c \'p={ws.workspace_path}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done\'',
                sudo=True,
            )
            output.detail("OK: parent traversal")
        except SSHError as e:
            output.warn(f"parent traversal fix failed: {e}")

        # 5b. Converge the checkout's git identity. Repo-local config is
        # actor-agnostic and idempotent, so identity joins the reinit
        # convergence set: an identity added or changed on the template
        # after create lands here (detection-based, so an unchanged value
        # reports OK). Only meaningful when the workspace is a git repo; a
        # declared identity on a repo-less workspace is a no-op.
        fixes += _reinit_git_identity(target, registry, ws)

        # 6. Reconcile agent group membership
        # Get agents that SHOULD be in the group (have any grant)
        granted_agents = set()
        all_agents = db.list_agents(vm_name=vm.name)
        for agent in all_agents:
            if db.has_any_grant(agent.name, name):
                granted_agents.add(agent.linux_user)

        # Get agents that ARE in the group. The agt- prefix check covers both
        # current agents and legacy ones (whose names start with agt--).
        try:
            group_info = target.run(f"getent group {ws_group}", sudo=True, check=False)
            current_members: set[str] = set()
            if group_info.ok and ":" in group_info.stdout:
                members_str = group_info.stdout.strip().split(":")[-1]
                if members_str:
                    current_members = {m for m in members_str.split(",") if m.startswith(AGENT_PREFIX)}

            # Add missing agents
            to_add = granted_agents - current_members
            for user in sorted(to_add):
                target.run(f"usermod -aG {ws_group} {user}", sudo=True)
                output.detail(f"Fixed: added {user} to {ws_group}")
                fixes += 1

            # Remove agents that shouldn't be there
            to_remove = current_members - granted_agents
            for user in sorted(to_remove):
                target.run(f"gpasswd -d {user} {ws_group}", sudo=True, check=False)
                output.detail(f"Fixed: removed {user} from {ws_group}")
                fixes += 1

            if not to_add and not to_remove:
                output.detail(f"OK: agent group membership ({len(current_members)} agent(s))")
        except SSHError as e:
            output.warn(f"agent membership check failed: {e}")

        if fixes > 0:
            output.result(f"\nApplied {fixes} fix(es)")
        else:
            output.result("\nAlready up to date")


def _reinit_git_identity(
    target: Transport,
    registry: Registry,
    ws: WorkspaceRow,
) -> int:
    """Converge the checkout's repo-local git identity to its template.

    Mirrors the create-time stamp in
    ``workspaces.backends.vm.create_vm_workspace``: the identity lives in
    the checkout's own ``.git/config`` (actor-agnostic), so re-applying it
    is idempotent. Detection-based, so an already-correct value reports
    ``OK`` and only a real change counts as a fix.

    Three no-op cases: the template (or its resolution) is gone, the
    template declares no identity, or the on-disk workspace path is not a
    git checkout (probed with ``git rev-parse``). Note the last check is on
    the checkout, not the template's ``repo`` field: a workspace whose repo
    was later dropped from the template keeps its existing checkout, so its
    identity still converges.

    Returns the number of identity fields it actually changed.
    """
    from agentworks.errors import ConfigError
    from agentworks.ssh import SSHError
    from agentworks.workspaces.templates import resolve_template

    try:
        tmpl = resolve_template(registry, ws.template)
    except (ValueError, ConfigError):
        # The workspace's template is gone from config; nothing to converge
        # toward. Ownership/permission convergence above does not need it.
        return 0

    declared = [
        (key, value)
        for key, value in (
            ("user.name", tmpl.git_user_name),
            ("user.email", tmpl.git_user_email),
        )
        if value
    ]
    if not declared:
        return 0

    quoted_path = shlex.quote(ws.workspace_path)
    try:
        is_repo = target.run(f"git -C {quoted_path} rev-parse --git-dir", check=False)
        if not is_repo.ok:
            # "not a git repository" is the expected, quiet no-op (a
            # workspace created without a repo). Any other probe failure
            # (git missing, a broken checkout, permissions) is a real
            # problem the operator should see, not a silent OK.
            stderr = (is_repo.stderr or "").strip()
            if "not a git repository" in stderr.lower():
                output.detail("OK: git identity (workspace has no repo)")
            else:
                output.warn(
                    f"git identity skipped: could not probe {ws.workspace_path} "
                    f"as a git repo ({stderr or 'unknown error'})"
                )
            return 0

        fixed = 0
        for key, value in declared:
            current = target.run(f"git -C {quoted_path} config --local --get {key}", check=False)
            if current.ok and current.stdout.strip() == value:
                output.detail(f"OK: git {key}")
                continue
            target.run(f"git -C {quoted_path} config --local {key} {shlex.quote(value)}")
            output.detail(f"Fixed: git {key}")
            fixed += 1
        return fixed
    except SSHError as e:
        output.warn(f"git identity check failed: {e}")
        return 0


def _revert_grant_on_failure(db: Database, agent_name: str, ws_name: str) -> None:
    """Best-effort: drop a just-inserted explicit grant after the on-VM
    group add failed (or was cancelled). Used by the grant-all loop in
    create_workspace to keep DB and VM authorization aligned. A failure
    to revert is logged but does not raise, so it never masks the
    caller's original exception (or KeyboardInterrupt)."""
    try:
        db.delete_agent_grant(agent_name, ws_name, "explicit")
    except Exception as revert_err:
        output.warn(
            f"Could not revert grant for '{agent_name}' on workspace '{ws_name}': "
            f"{revert_err}. DB has a grant row with no VM-side group membership; "
            f"re-run 'agent grant-workspaces {agent_name} {ws_name}' or "
            f"revoke explicitly."
        )


def _rehome_partial_state_hint(db: Database, ws_name: str, old_path: str, new_path: str) -> str:
    """Describe the actual DB state after a rehome failure / cancellation.

    The rehome flow copies files to the new path, then updates the DB. KI or
    an exception can land before OR after the DB update, so we read the row
    back to give the user an accurate picture rather than asserting one way.

    This is called from the KeyboardInterrupt / exception handler, so any
    DB error here would mask the original error. Catch broadly and fall
    back to a generic hint.
    """
    try:
        ws = db.get_workspace(ws_name)
    except Exception as e:
        return f"DB state could not be read ({e}); manual inspection needed."
    if ws is None:
        return "Workspace row is missing from the DB; manual cleanup may be needed."
    if ws.workspace_path == new_path:
        return (
            f"DB now points to {new_path}, but the on-VM move may be incomplete. "
            f"Use 'workspace describe {ws_name}' and verify the directory."
        )
    return (
        f"DB still points to {old_path}. A partial copy of the workspace may exist "
        f"at {new_path}; verify and clean up if needed."
    )
