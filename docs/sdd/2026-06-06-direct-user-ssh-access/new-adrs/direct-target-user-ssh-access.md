# N. Direct target-user SSH as the access model

Date: 2026-06-10

## Status

Draft (will be numbered and moved to `docs/adrs/` when SDD `2026-06-06-direct-user-ssh-access`
merges).

## Context

Operations on a VM run as different Linux users: admin for provisioning and admin-side maintenance,
an agent user for each agent's sessions and shells. Until this SDD, every "do work as the agent"
path went through admin SSH followed by `sudo --login -u <agent>`. The detour worked, but it kept
costing us complexity at the sudo boundary (env stripping, command quoting, two-step error surfaces,
a steady stream of small bugs) and it gave every agent operation root in its call stack even when
the operation itself didn't need root.

## Decision

Operations whose target user is the agent open SSH directly as the agent's Linux user. Admin SSH
stays in scope only for operations that fundamentally need root or that the agent cannot do for
itself: `useradd`, the agent's tmux socket directory under `/var/lib/`, the initial
`authorized_keys` install, and bulk maintenance ops (`stop_all_sessions`, `delete_agent` killing the
agent's sessions) where iterating per-agent SSH probes would penalize performance for no benefit.

The agent's `~/.ssh/authorized_keys` is reconciled at agent create / reinit via a stage-and-install
pattern (admin `scp`s content to a private `mktemp` path, then `sudo install` atomically places it
owned by the agent). After that point, direct agent SSH is possible.

The operator's local SSH config gets a parallel alias surface: each agent gets a
`Host awagent--<agent>` block alongside the existing `Host awvm--<vm>` blocks, keyed on the
operator-facing agent name rather than the on-VM Linux user.

## Consequences

- Less code runs as root on the agent's behalf. Reduced attack surface; agent shells no longer carry
  an admin-privileged process anywhere in their call stack.
- The recurring sudo-boundary issues (env stripping, login-shell quoting, opaque errors) stop
  happening because the boundary is gone.
- The operator's SSH public key is now installed in N+1 places per VM (admin's `authorized_keys`
  plus each agent's). Key rotation requires `vm reinit` for admin plus `agent reinit` for each
  agent. The declarative reconciliation makes it mechanical, but it is no longer a single-place
  change.
- Audit trails distribute across Linux users instead of centralizing on admin. Operators who relied
  on the admin-centric view need to aggregate across per-user logs.
- Tooling that targets SSH host aliases (VS Code Remote-SSH, `scp`, manual `ssh`) now reaches agents
  directly via `ssh awagent--<name>`, the same way it already reached VMs.
- Pre-rollout agents (created before this SDD landed) have no `authorized_keys` for the operator.
  Surfaced as an actionable `StateError` at first contact, pointing to `agw agent reinit`. Not a
  permanent state; one reinit converts the agent.
