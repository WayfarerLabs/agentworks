# User-Based Security Model -- High-Level Architecture

## Overview

The security model uses standard Linux users, groups, and file permissions to control agent
workspace access. Each agent is a VM-scoped Linux user with access to specific workspaces via a
grant system that translates to group membership.

## User and Group Topology

```text
Users:
  agentworks              (admin user, sudo)
  agt--<agent>            (agent user, no sudo)

Groups:
  ws--<workspace>         (workspace file access)
```

### Admin user

The operator's identity on the VM. Created during Phase A (bootstrap). Has unrestricted sudo. Added
to all workspace groups automatically.

### Agent users

- Created via `agent create` with `useradd`
- Username: `agt--<name>`
- Home directory: `/home/agt--<name>/`
- No sudo, no password
- Workspace access via group membership, managed by the grant system
- Fully configurable via agent templates

### Workspace groups

- Group name: `ws--<workspace-name>`
- Created during workspace creation on a VM
- Admin user added to every workspace group
- Workspace directory has setgid (mode 2770) so new files inherit the group
- Agent membership managed by the grant system

## Grant System

### Grant types

- **Explicit**: operator-managed via `agent grant-workspaces` / `agent deny-workspaces`
- **Implicit**: auto-created when a task is created for an agent, removed when the task is deleted
- **Grant-all**: flag on the agent that auto-grants access to all workspaces

### Grant storage

Grants are stored in the `agent_workspace_grants` table:

| Column         | Description                                           |
| -------------- | ----------------------------------------------------- |
| agent_name     | Agent name (FK to agents)                             |
| workspace_name | Workspace name (FK to workspaces, cascades on delete) |
| grant_type     | `explicit` or `implicit`                              |
| task_name      | NULL for explicit grants, task name for implicit      |

### Grant lifecycle

- **On grant**: add agent to workspace group via `usermod -aG`
- **On deny/task delete**: remove grant record, check if any remaining grants exist. If none, remove
  agent from workspace group via `gpasswd -d`.
- **On workspace create**: if agent has grant_all, auto-add to new workspace group
- **On workspace delete**: all grants for that workspace cascade (FK)
- **On agent delete**: all grants cascade (FK), agent removed from all workspace groups

## Agent Provisioning Flow

When an agent is created on a VM:

1. Create the Linux user with a private primary group: `useradd -m -U -s <shell> agt--<name>`. The
   `-U` (`--user-group`) is load-bearing, not cosmetic: it forces a per-user private group
   regardless of the image's `USERGROUPS_ENAB` setting, without which the 0750 home below could
   grant a shared primary group read access.
2. Tighten the home directory to mode 0750 (`useradd -m` honors the system umask, which leaves it
   world-readable at 0755), so the home is private to the agent user. A post-condition guard
   verifies the primary group really is private (`id -gn` equals the username) and warns if not, so
   drift on a reinit, a pre-existing agent, or an odd image is surfaced rather than silently
   defeating it.
3. Configure shell rc file (prompt), including `umask 027` in the agent's managed profile fragment
4. Configure git credentials (if specified in template)
5. Run user install commands (from template)
6. Sync dotfiles (from template)
7. Configure mise (from template)
8. Record in database

Workspace group membership is NOT set during creation. It is managed entirely by the grant system.

## Permissions Summary

| Resource      | Owner      | Group         | Effect                      |
| ------------- | ---------- | ------------- | --------------------------- |
| Workspace dir | admin      | ws--WORKSPACE | Agents read/write via grant |
| Agent home    | agent-user | agent-user    | Agent-private               |

Agent-private is enforced, not just conventional. Cross-agent isolation of the home comes from the
0750 mode plus the private primary group forced by `useradd -U`: with no other user in the group,
0750 is effectively owner-only, and because other agents cannot search the home, files inside it are
unreachable regardless of their own mode. A post-condition guard warns if the primary group is ever
found to be shared (drift from a reinit or an unexpected image), because 0750 over a shared group
would leak the home.

The agent's login shells also run with `umask 027`. This is defense-in-depth for artifacts the agent
writes _outside_ its home (in `/tmp`, `$TMPDIR`, or any world-traversable shared directory), where
the file's own mode is what protects it; it adds nothing to in-home protection, which the 0750 home
already provides. Its coverage is partial by design: the umask rides the login-shell profile chain,
so non-login `sh -c`, cron, systemd user units, and sftp/scp keep the default umask 022. The 0750
home is the boundary; the umask is a supplement. The umask does not reduce group access to files
created inside a workspace either: the workspace directory carries a POSIX default ACL
(`setfacl -d`) that makes new files inherit group rwx regardless of the process umask, so
cross-agent collaboration in workspaces is preserved.

## What This Model Prevents

- Agents accessing workspaces they have not been granted access to
- Agents escalating to root (no sudo)
- Agents killing other agents' processes (different UIDs)

## What This Model Does Not Prevent

- Agents reading system-wide readable files
- Agents making arbitrary network requests
- Agents consuming unbounded resources (no cgroups by default)

## Trust Boundary

The VM is the trust boundary. If stronger isolation is needed between agents, use separate VMs.
