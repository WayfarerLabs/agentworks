# VM-Scoped Agents - Functional Requirements

## Problem Statement

Agents are currently scoped to workspaces: each agent is created within a specific workspace and can
only access that workspace. This prevents "personal assistant" style agents that work across
multiple workspaces on the same VM. An agent helping with a refactoring that spans two repos needs
to be duplicated across both workspaces with separate tool installations, credentials, and dotfiles.

## Decision

Agents move from workspace-scoped to VM-scoped. An agent is created on a VM and granted access to
specific workspaces through explicit and implicit permission grants.

This supersedes ADR-0006 (agents are scoped to workspaces).

## Requirements

### R1: VM-scoped agent identity

Agents are Linux users on a VM, not tied to any single workspace. The username is `agt--<name>`.

- `agent create` takes `--vm` instead of `--workspace`
- Agent templates, shell, dotfiles, git credentials, mise, etc. work exactly as before
- Agent home directory is `/home/agt--<name>/`

### R2: Explicit workspace grants

Operators can explicitly grant or deny workspace access for agents.

- `agent grant-workspaces <agent> <workspace>[,<workspace>,...]` grants access to specific
  workspaces
- `agent grant-workspaces <agent> --all` grants access to all current and future workspaces
- `agent deny-workspaces <agent> <workspace>[,<workspace>,...]` removes explicit grants for specific
  workspaces
- `agent deny-workspaces <agent> --all` removes all explicit grants
- Grants are stored in the database, not in config (they reference runtime workspace names)
- Granting workspace access adds the agent to the `ws--<workspace>` Linux group
- Denying workspace access removes the agent from the group (unless an implicit grant remains)

### R3: Implicit grants via tasks

When a task is created for an agent in a workspace the agent doesn't have explicit access to, an
implicit grant is automatically created.

- Implicit grants add the agent to the workspace group, same as explicit grants
- The union of explicit and implicit grants determines the agent's effective workspace access
- Stopped tasks retain their implicit grants. Only task deletion removes the implicit grant.
- When all implicit grants for a workspace are removed (all tasks deleted) and no explicit grant
  exists, the agent loses access to that workspace (removed from group)

### R4: Grant-all behavior

The `--all` flag on `grant-workspaces` (and `--grant-all-workspaces` on `agent create`) means:

- The agent is added to all existing workspace groups immediately
- When new workspaces are created in the future, the agent is automatically added
- This is stored as a flag on the agent record, not as individual grants per workspace
- `deny-workspaces --all` clears this flag and all explicit grants

### R5: Username convention

Agent Linux usernames are `agt--<name>`. The double-hyphen prefix is consistent with the existing
naming convention and avoids collision with system users.

### R6: Agent shell behavior

`agent shell` drops into the agent's home directory by default. An optional `--workspace` flag
changes to a specific workspace directory (if the agent has access).

## Out of Scope

- Agent-to-agent communication or collaboration within a workspace (they share files via group
  permissions, which is sufficient)
- Workspace-level agent templates (different templates per workspace for the same agent)
