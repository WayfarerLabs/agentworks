# User-Based Security Model -- Functional Requirements

## Problem Statement

Agentworks provisions VMs where AI agents run inside workspaces. Today, a single
user (`agentworks`) owns everything on the VM. There is no isolation between
workspaces, no separation between the management layer and agent activity, and
no mechanism to prevent agents from modifying the tools they use.

As we add agent tools (MCP servers, CLI utilities, etc.) to the VM layer, we
need a security model that:

- Prevents agents from tampering with their own tools
- Isolates workspaces from each other
- Supports multiple agents collaborating in a single workspace
- Provides per-agent audit trails and resource control
- Works regardless of which AI tool drives the agent

## Personas

### Platform operator

Manages VMs and workspaces via the `agentworks` CLI. Installs and updates agent
tools. Needs full control over the VM.

### AI agent

Operates inside a workspace via an AI coding tool (Claude Code, Cursor, Codex,
etc.). Needs full read/write access to its workspace and read/execute access to
tools. Should not be able to modify tools, access other workspaces, or interfere
with other agents.

### Human developer

May SSH into a VM to debug or inspect agent work. Needs the same access as
agents within a workspace, plus the ability to escalate to admin when needed.

## Requirements

### R1: Admin user

The VM has a privileged admin user (currently `agentworks`) with unrestricted
sudo. This user:

- Owns the tools directory and all agent tooling
- Manages VM-level configuration (Tailscale, packages, systemd services)
- Runs the SSH agent daemon that provides git credentials
- Is the user the `agentworks` CLI connects as

### R2: Workspace groups

Each workspace has a corresponding Linux group. The workspace directory uses the
setgid bit so that all files created inside automatically inherit the group,
enabling shared access.

### R3: Agent users

Each agent is a Linux user assigned to one workspace's group. Multiple agents
can share a workspace (and thus its group). Agent users:

- Have full read/write access within their workspace (via group membership)
- Have read/execute access to the tools directory
- Have no write access outside their workspace and home
- Cannot sudo or escalate privileges
- Cannot signal processes of other users

### R4: Git credential sharing

A single SSH keypair per VM (generated during VM init and registered with git
hosts) provides git access. The private key is managed by an SSH agent daemon
running as the admin user. The agent socket is group-readable, allowing all
agent users to perform git operations without direct access to the key material.

### R5: Per-agent resource control

Agent users can have resource limits applied (CPU, memory, process count) to
prevent a single agent from starving others on the same VM.

### R6: Audit trail

All agent activity is attributable to a specific Linux user, enabling standard
Linux audit mechanisms (process accounting, auditd, log correlation) to track
what each agent did.

### R7: Tool integrity

The tools directory is owned by the admin user and is read-only to agent users.
Agents can execute tools but cannot modify, delete, or replace them. Tool
updates are performed by the admin user via the `agentworks` CLI or management
tooling.

## Out of Scope

- **Network isolation**: restricting agent network access (e.g., per-user
  iptables, network namespaces) is a future enhancement, not part of this
  initial model.
- **Repo-level isolation**: all agents on a VM share the same git credentials.
  Repo-level access control requires separate VMs.
- **Container/sandbox isolation**: this model uses Linux users and file
  permissions. Stronger isolation (seccomp, namespaces, containers) is a
  possible future layer.
