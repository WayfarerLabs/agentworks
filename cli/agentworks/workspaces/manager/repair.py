"""Workspace repair (live-state convergence) and its supporting helpers.

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
from agentworks.workspaces.acls import apply_workspace_acls
from agentworks.workspaces.manager._common import _workspace_scope

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.resources.registry import Registry
    from agentworks.transports import Transport


def repair_workspace(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Repair a workspace by converging its live VM state to the DB.

    Idempotent and forward-only. Every step is detection-based: it
    reports `Fixed:` only when it actually changed live state and `OK:`
    when state was already correct, so the closing `Repaired N issue(s)`
    / `No issues found` is truthful. Steps split into two shapes by HOW
    they detect a change:

    - **Probe-first** (group existence, admin membership, agent group
      membership against the grant table): read live state, then apply a
      fix only when it diverges.
    - **Apply-and-observe** (directory ownership, permissions, SGID,
      ACLs, parent-directory traversal): re-run their canonical
      commands every time (the underlying chown/chmod/setfacl are no-ops
      on already-correct state) but observe whether anything actually
      changed. chown/chmod carry `-c`, coreutils' own changed/unchanged
      signal (a line per entry they alter, silence otherwise); setfacl
      has no such signal, so its step snapshots the tree's ACLs before
      and after and compares. A real change reports `Fixed:`, an
      unchanged run `OK:`. The ACL step applies the canonical spec through
      the shared `apply_workspace_acls` helper, the same one `workspace
      create` uses, so a freshly created workspace is already converged
      and its first repair reports `OK: ACLs`.

    Git identity (the template's `git_user_name` / `git_user_email`)
    converges here too, detection-based: an identity added or changed on
    the template after create is stamped into the checkout's repo-local
    config, and an already-correct value reports `OK:`.

    This is the workspace analog of the `vm reinit` / `agent reinit`
    convergence (the declared state in the DB is the source of truth, and
    live state is converged to match), but it is honestly named `repair`
    because that is what it does: reconcile a workspace's on-VM
    infrastructure (group, ownership, permissions, ACLs, parent traversal,
    agent access, git identity) with what the DB declares.

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
        quoted_ws = shlex.quote(ws.workspace_path)
        fixes = 0

        output.info(f"Repairing workspace '{name}' on VM '{vm.name}'...")

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

        # 3. Fix directory ownership, permissions, and SGID (recursive chgrp
        # so ACLs apply correctly). The -c (changes) flag makes chown/chmod
        # print a line per entry they actually alter and stay silent
        # otherwise, so a non-empty result across the three commands means
        # we genuinely converged something (Fixed) rather than re-applied a
        # no-op (OK). Note 2770 already carries the SGID bit on the top
        # directory; the recursive g+s below re-applies it to subdirectories.
        # On a badly-damaged large tree this stdout can be sizable (one line
        # per altered entry), but detection only needs its truthiness, so we
        # never parse it, just test whether anything was printed.
        try:
            owner = target.run(f"chown -R -c {vm.admin_username}:{ws_group} {quoted_ws}", sudo=True, timeout=120)
            mode = target.run(f"chmod -c 2770 {quoted_ws}", sudo=True)
            # Set SGID on all subdirectories so new files inherit the workspace group.
            # This is critical for atomic-write tools (including Claude Code) that
            # create a temp file and rename it over the original.
            sgid = target.run(
                f"find {quoted_ws} -type d -exec chmod -c g+s {{}} +",
                sudo=True,
                timeout=120,
            )
            if owner.stdout.strip() or mode.stdout.strip() or sgid.stdout.strip():
                output.detail("Fixed: directory ownership and permissions")
                fixes += 1
            else:
                output.detail("OK: directory ownership and permissions")
        except SSHError as e:
            output.warn(f"permission fix failed: {e}")

        # 4. Fix ACLs
        # apply_workspace_acls applies the canonical group ACL, the SAME spec
        # workspace create applies, so a freshly created workspace is already
        # converged and this reports OK on a first repair.
        # setfacl has no changed/unchanged signal, so snapshot the tree's ACLs
        # before and after and compare. A naive whole-output compare would be
        # fooled by ordinary churn: a file created, renamed, or deleted by an
        # active session between the two snapshots (exactly the atomic-write
        # temp-file renames step 3 calls out) would shift the output and read
        # as a spurious fix. So we compare per-path ACL entries only for paths
        # present in BOTH snapshots (`_acls_changed`): added/removed paths are
        # ignored as churn, while a real ACL change on a persisting path is
        # still detected. Comparing actual before/after state, rather than
        # reconstructing the desired ACL, keeps this correct as the applied
        # spec evolves. getfacl uses check=False because a getfacl hiccup must
        # not mask the real SSHError a failing setfacl would surface.
        try:
            before = target.run(f"getfacl -R -n {quoted_ws}", sudo=True, check=False, timeout=120)
            apply_workspace_acls(target, ws.workspace_path)
            after = target.run(f"getfacl -R -n {quoted_ws}", sudo=True, check=False, timeout=120)
            if _acls_changed(before.stdout, after.stdout):
                output.detail("Fixed: ACLs")
                fixes += 1
            else:
                output.detail("OK: ACLs")
        except SSHError as e:
            output.warn(f"ACL fix failed: {e}")

        # 5. Fix parent directory traversal. Each ancestor's chmod carries
        # -c, so the loop's aggregated stdout is non-empty exactly when we
        # opened traversal that was missing (Fixed) and empty when every
        # ancestor was already a+x (OK). The path is passed as a positional
        # arg ($1) so shlex.quote handles spaces / metacharacters cleanly
        # rather than interpolating a raw path into the sh -c script.
        try:
            traversal_cmd = (
                f'sh -c \'p="$1"; while [ "$p" != "/" ]; do chmod -c a+x "$p"; p=$(dirname "$p"); done\' _ {quoted_ws}'
            )
            traversal = target.run(traversal_cmd, sudo=True)
            if traversal.stdout.strip():
                output.detail("Fixed: parent traversal")
                fixes += 1
            else:
                output.detail("OK: parent traversal")
        except SSHError as e:
            output.warn(f"parent traversal fix failed: {e}")

        # 5b. Converge the checkout's git identity. Repo-local config is
        # actor-agnostic and idempotent, so identity joins the repair
        # convergence set: an identity added or changed on the template
        # after create lands here (detection-based, so an unchanged value
        # reports OK). Only meaningful when the workspace is a git repo; a
        # declared identity on a repo-less workspace is a no-op.
        fixes += _repair_git_identity(target, registry, ws)

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
            output.result(f"\nRepaired {fixes} issue(s)")
        else:
            output.result("\nNo issues found")


def _parse_getfacl(text: str) -> dict[str, frozenset[str]]:
    """Parse ``getfacl -R -n`` output into ``{path: acl-entry-set}``.

    Each record starts with a ``# file: <path>`` line followed by that
    path's ACL entries (``user::rwx``, ``group::rwx``, ``mask::rwx``,
    ``default:...``). Comment lines (``# owner:``, ``# group:``,
    ``# flags:``) are not ACL entries and are dropped, so ownership churn
    never registers as an ACL change. Entries go into a set so the compare
    is order-independent (getfacl's traversal order between two adjacent
    snapshots is stable, but the set makes the compare robust regardless).
    """
    blocks: dict[str, frozenset[str]] = {}
    path: str | None = None
    entries: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# file:"):
            if path is not None:  # flush the record we just finished
                blocks[path] = frozenset(entries)
            path = line[len("# file:") :].strip()
            entries = set()
        elif line and not line.startswith("#") and path is not None:
            entries.add(line)
    if path is not None:
        blocks[path] = frozenset(entries)
    return blocks


def _acls_changed(before: str, after: str) -> bool:
    """True when any path present in BOTH getfacl snapshots has different
    ACL entries.

    Paths that appear in only one snapshot are ignored: files created,
    renamed, or deleted by an active session between the two ``getfacl``
    runs are ordinary churn, not an ACL repair. A path that persists across
    both snapshots with genuinely re-ACLed entries is still detected.
    """
    b = _parse_getfacl(before)
    a = _parse_getfacl(after)
    return any(b[path] != a[path] for path in b.keys() & a.keys())


def _repair_git_identity(
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
