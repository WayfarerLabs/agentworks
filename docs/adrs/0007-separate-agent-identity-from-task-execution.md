# 7. Separate agent identity from task execution

Date: 2026-03-23

## Status

Accepted

## Context

Agentworks needs to support AI agents running work inside workspaces. Two concepts are involved: the
identity under which work runs (Linux user, permissions, credentials, tools) and the work itself (a
running process in a tmux session, likely with an associated tool session). These could be combined
into a single entity ("an agent is a running process") or separated ("an agent is an identity, a
task is a process").

## Decision

Agents and tasks are separate entities:

- An **agent** is a security identity: a Linux user with a specific shell, dotfiles, git
  credentials, tools (mise), and install commands. Agents are created via templates and persist
  across task restarts.
- A **task** is a running workload: a named tmux session executing a command (from a task template).
  Tasks run as either the admin user or an agent user.

A task references an agent via `--agent`, running the task command as that agent's Linux user.
Multiple tasks can run under the same agent, allowing for easy sharing of the agent's
permissions/setup while helping to organize and track the different workstreams. An agent can exist
without any tasks running.

## Consequences

- Agent identity is reusable. Create an agent once with the right tools and credentials, then start
  multiple tasks under it without re-provisioning.
- `agent reinit` updates an agent's configuration (new tools, updated dotfiles) without affecting
  running tasks.
- Task lifecycle (start, stop, restart, delete) is independent of agent lifecycle (create, reinit,
  delete).
- The admin user can also run tasks directly (no agent required), which is useful for operator tasks
  that do not need agent isolation.
- Tradeoff: two concepts to learn and manage instead of one. The mental model is: agent = who, task
  = what.
