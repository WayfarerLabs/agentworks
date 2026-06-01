# 10. VM-scoped agents with workspace grants

Date: 2026-03-30

## Status

Accepted

Supersedes [6. Agents are scoped to workspaces](0006-agents-are-scoped-to-workspaces.md)

## Context

ADR-0006 scoped agents to workspaces: each agent was created within a specific workspace and could
only access that workspace. This prevented "personal assistant" style agents that work across
multiple workspaces on the same VM. An agent helping with work that spans two repos needed to be
duplicated with separate tool installations, credentials, and dotfiles.

## Decision

Agents are now VM-scoped. An agent is created on a VM (`agt--<name>`) and granted access to specific
workspaces through a permission grant system.

- **Explicit grants**: operator manages via `agent grant-workspaces` and `agent deny-workspaces`
- **Implicit grants**: automatically created when a task is created for an agent in a workspace,
  removed when the task is deleted
- **Grant-all**: a flag that automatically grants access to all workspaces (existing and future)
- The union of explicit and implicit grants determines the agent's effective workspace access
- Linux group membership (`ws--<workspace>`) enforces file access at the OS level

## Consequences

- An agent can work across multiple workspaces without duplication. Tools, credentials, dotfiles,
  and mise packages are configured once.
- Agent templates are reusable across workspaces (same template, different access).
- The grant system provides clear visibility into which agents can access which workspaces.
- Implicit grants via tasks mean the common case ("start a task for this agent in this workspace")
  works without explicit grant management.
- Workspace group naming changed from `ws-<name>` to `ws--<name>` for consistency with the
  `agt--<name>` convention.
- Tradeoff: more complex permission model than workspace-scoped agents. Mitigated by implicit grants
  handling the common case automatically.
