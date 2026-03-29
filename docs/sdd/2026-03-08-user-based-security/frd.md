# User-Based Security Model -- Functional Requirements

## Problem Statement

Agentworks provisions VMs where AI agents run inside workspaces. The security model needs to:

- Isolate agents from each other and from the admin user's environment
- Support multiple agents collaborating in a single workspace via shared file access
- Provide per-agent audit trails via standard Linux user accounting
- Allow full per-user configuration for agent users (shell, dotfiles, tools, etc.)

## Personas

### Operator (admin user)

The human who owns the VM. Logs in as the admin user (default: `agentworks`), manages VMs and
workspaces via the CLI. Has unrestricted sudo.

### AI agent

Operates inside a workspace via an AI coding tool (Claude Code, Cursor, Codex, etc.). Has full
read/write access to its workspace via group membership. Cannot sudo or access other workspaces.

## Requirements

### R1: Admin user

The VM has an admin user (configurable, default `agentworks`) that is the operator's identity. This
user has unrestricted sudo and owns all VM-level configuration and tooling.

### R2: Workspace groups

Each workspace has a corresponding Linux group (`ws-<workspace-name>`). The group is created when
the first agent is provisioned on the workspace. Agents in the same workspace share file access via
group membership.

### R3: Agent users

Each agent is a Linux user (`<workspace>--<agent>`) assigned to the workspace's group. Agent users:

- Have full read/write access within their workspace (via group membership)
- Cannot sudo or escalate privileges
- Have their own home directory (`/home/<workspace>--<agent>`)
- Are fully configurable via agent templates: shell, dotfiles, git credentials, user install
  commands, mise packages, etc.

### R4: Per-agent audit trail

All agent activity is attributable to a specific Linux user, enabling standard Linux audit
mechanisms (process accounting, log correlation) to track what each agent did.

## Current Implementation

- Admin user created during VM provisioning (Phase A bootstrap)
- Agent users created via `agent create` with workspace group membership
- Agent templates support full per-user configuration (shell, dotfiles, git credentials, mise, etc.)
- Workspace groups created idempotently during agent creation

## Planned

- **Agent workload identity**: identity framework (likely SPIFFE-based) for agent-level
  authentication to external services without shared credentials.

## Possible Future Directions

- **Secret injection via command shims**: controlled access to secrets through wrapper commands
  rather than direct credential access
- **Jails and resource controls**: cgroups, ulimits, or container-based isolation for stronger
  agent sandboxing
- **Network isolation**: per-agent or per-workspace network policies
