# User-Based Security Model -- High-Level Architecture

## Overview

The security model uses standard Linux users, groups, and file permissions to isolate agent
workspaces. Each agent is a Linux user with access scoped to its workspace via group membership.

## User and Group Topology

```text
Users:
  agentworks              (admin user, sudo)
  <workspace>--<agent>    (agent user, no sudo)

Groups:
  ws-<workspace>          (workspace file access)
```

### Admin user

The operator's identity on the VM. Created during Phase A (bootstrap). Has unrestricted sudo.

- Owns VM-level configuration and tooling
- Is the account the `agentworks` CLI connects as
- Configurable via `[admin.config]` (username, shell, dotfiles, git credentials, mise, etc.)

### Agent users

- Created via `agent create` with `useradd`
- Username: `<workspace>--<agent>` (double-hyphen separator)
- Home directory: `/home/<workspace>--<agent>/`
- No sudo, no password
- Member of: `ws-<workspace>`
- Fully configurable via agent templates (shell, dotfiles, git credentials, user install commands,
  mise packages)

### Workspace groups

- Group name: `ws-<workspace-name>`
- Created during workspace creation on a VM (and idempotently during agent creation for older
  workspaces)
- The admin user is added to every workspace group so they can read/write workspace files without
  sudo
- All agents in the workspace are members
- Workspace directory has setgid (mode 2775) so new files inherit the group
- Provides shared file access within the workspace directory

## Agent Provisioning Flow

When an agent is created for a workspace:

1. Ensure the workspace group exists: `groupadd ws-<workspace>` (idempotent)
2. Add admin to the workspace group (idempotent)
3. Repair workspace directory ownership if needed (`chgrp -R`, `chmod 2775`)
4. Create the agent user with the template's shell: `useradd -m -s <shell> <workspace>--<agent>`
5. Add to workspace group: `usermod -aG ws-<workspace> <workspace>--<agent>`
6. Write shell rc file (prompt, shell-appropriate syntax)
7. Configure git credentials (if specified in agent template)
8. Run user install commands (from agent template)
9. Sync dotfiles (from agent template, cloned as the agent user)
10. Configure mise (from agent template: shims PATH, activation, config, lockfile, install)
11. Record the agent in the database

Agent setup commands run as the agent user via `sudo su - <user> -c '...'`. File writes to the
agent's home use scp to `/tmp/` followed by `mv` + `chown`.

## Permissions Summary

| Resource        | Owner      | Group          | Effect                    |
| --------------- | ---------- | -------------- | ------------------------- |
| Workspace dir   | admin      | ws-WORKSPACE   | Agents read/write via group |
| Agent home      | agent-user | agent-user     | Agent-private             |
| VM tools        | admin      | admin          | Admin-only write          |

## What This Model Prevents

- Agents accessing other workspaces' files (no group membership)
- Agents escalating to root (no sudo)
- Agents killing other agents' processes (different UIDs)

## What This Model Does Not Prevent

- Agents reading system-wide readable files (`/etc/passwd`, installed packages, etc.)
- Agents making arbitrary network requests
- Agents consuming unbounded resources (no cgroups by default)

## Trust Boundary

The VM is the trust boundary. If stronger isolation is needed between agents, use separate VMs.
