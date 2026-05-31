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

1. Create the Linux user: `useradd -m -s <shell> agt--<name>`
2. Configure shell rc file (prompt)
3. Configure git credentials (if specified in template)
4. Run user install commands (from template)
5. Sync dotfiles (from template)
6. Configure mise (from template)
7. Record in database

Workspace group membership is NOT set during creation. It is managed entirely by the grant system.

## Permissions Summary

| Resource      | Owner      | Group         | Effect                      |
| ------------- | ---------- | ------------- | --------------------------- |
| Workspace dir | admin      | ws--WORKSPACE | Agents read/write via grant |
| Agent home    | agent-user | agent-user    | Agent-private               |

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
