# User-Based Security Model -- Functional Requirements

## Problem Statement

Agentworks provisions VMs where AI agents run inside workspaces. The security model needs to:

- Isolate agents from each other and from the admin user's environment
- Support agents working across multiple workspaces via controlled access grants
- Support multiple agents collaborating in a single workspace via shared file access
- Provide per-agent audit trails via standard Linux user accounting
- Allow full per-user configuration for agent users (shell, dotfiles, tools, etc.)

## Personas

### Operator (admin user)

The human who owns the VM. Logs in as the admin user (default: `agentworks`), manages VMs and
workspaces via the CLI. Has unrestricted sudo.

### AI agent

Operates inside workspaces via an AI coding tool (Claude Code, Cursor, Codex, etc.). Has access
to specific workspaces via grant-based permissions. Cannot sudo or access workspaces it has not
been granted access to.

## Requirements

### R1: Admin user

The VM has an admin user (configurable, default `agentworks`) that is the operator's identity. This
user has unrestricted sudo and owns all VM-level configuration and tooling.

### R2: Workspace groups

Each workspace has a corresponding Linux group (`ws--<workspace-name>`). The group is created when
the workspace is created on a VM. The admin user is added to all workspace groups. Workspace
directories use setgid (mode 2770) so new files inherit the group.

### R3: VM-scoped agent users

Each agent is a Linux user (`agt--<name>`) on a VM. Agents are not tied to any single workspace.
Agent users:

- Have access to workspaces via explicit or implicit grants (group membership)
- Cannot sudo or escalate privileges
- Have their own home directory (`/home/agt--<name>`)
- Are fully configurable via agent templates

### R4: Workspace access grants

Agents access workspaces via a grant system:

- Explicit grants: managed by the operator via `agent grant-workspaces` and
  `agent deny-workspaces`
- Implicit grants: automatically created when a task is created for an agent in a workspace,
  removed when the task is deleted
- Grant-all flag: agent is automatically added to all workspaces
- Grants translate to Linux group membership for file access enforcement

### R5: Per-agent audit trail

All agent activity is attributable to a specific Linux user, enabling standard Linux audit
mechanisms (process accounting, log correlation) to track what each agent did.

## Current Implementation

- Admin user created during VM provisioning (Phase A bootstrap)
- Agent users created via `agent create` on a VM
- Workspace groups created during workspace creation with setgid
- Grant system manages workspace access via explicit and implicit grants
- Agent templates support full per-user configuration

## Planned

- **Agent workload identity**: identity framework (likely SPIFFE-based) for agent-level
  authentication to external services without shared credentials.

## Possible Future Directions

- **Secret injection via command shims**: controlled access to secrets through wrapper commands
- **Jails and resource controls**: cgroups, ulimits, or container-based isolation
- **Network isolation**: per-agent or per-workspace network policies
