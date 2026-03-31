# 6. Agents are scoped to workspaces

Date: 2026-03-08

## Status

Superseded by [10. VM-scoped agents with workspace grants](0010-vm-scoped-agents-with-workspace-grants.md)

## Context

Agents are AI coding agents that operate inside workspaces on VMs. Each agent needs a Linux user for
process isolation, file access control, and audit attribution. The question is how to scope agent
identity: per-VM (agents can access any workspace), per-workspace (agents are bound to one
workspace), or floating (agents are created independently and attached to workspaces).

## Decision

Agents are scoped to workspaces. Each agent is created within a specific workspace and can only
access that workspace's files. The Linux username encodes this relationship: `<workspace>--<agent>`.

Agent file access is enforced via workspace groups (`ws-<workspace>`). Agents in the same workspace
share file access through group membership. Agents in different workspaces have no access to each
other's files.

## Reasoning

This one was honestly a close call. At the end of the day, though, users are cheap and the template
mechanism allows for easy replication of agent configurations across workspaces.

This could change in the future based on experience and feedback, though. If we do want agents at
the VM level, we should absolutely use the floating model where agents only have access to
workspaces they are explicitly added to.

## Consequences

- Clear security boundary: an agent in workspace A cannot read or write files in workspace B. This
  is enforced by Linux file permissions, not application-level checks.
- Multi-agent collaboration within a workspace works naturally through shared group ownership
  (setgid on the workspace directory).
- The username convention (`<workspace>--<agent>`) makes it immediately obvious in `ps`, logs, and
  audit trails which workspace an agent belongs to.
- Tradeoff: an agent cannot be "moved" or "shared" between workspaces. This is intentional. If you
  need the same tool configuration in a different workspace, use agent templates.
  same VM resources. For stronger isolation, use separate VMs.
