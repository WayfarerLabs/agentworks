# N. Direct target-user SSH as the access model

Date: 2026-06-10

## Status

Draft (will be numbered and moved to `docs/adrs/` when SDD `2026-06-06-direct-user-ssh-access`
merges).

## Context

Agentworks runs commands on VMs as multiple Linux users -- admin for provisioning and admin-side
maintenance, agents for sessions, shells, and ad-hoc execution. The original access model used a
single SSH transport: connect as admin, then `sudo --login -u <agent>` whenever the actual subject
was an agent.

That detour caused a slow accumulation of friction:

- `sudo` strips most env variables. Anything that needed the agent's env (PATH for user-installed
  tools, profile-exported config) had to be wrapped or worked around.
- Cross-uid `/proc` visibility was unconstrained; an agent shell launched via `sudo` could read
  unrelated process metadata that admin had access to.
- Several code paths had to thread "what user am I really running as" through helpers that otherwise
  didn't care.
- The in-flight env-and-secrets initiative needs per-workload env injection (agent and VM-level).
  Injecting through a sudo boundary is brittle and doesn't compose with the standard SSH conventions
  (`SetEnv` / `SendEnv` / `AcceptEnv`) the env-and-secrets work plans to lean on.

A separate motivation: VS Code Remote-SSH and other operator-side tooling that targets SSH host
aliases couldn't reach agents directly. The only operator-facing surface was admin.

## Decision

Operations whose target user is the agent open SSH directly as the agent's Linux user. Admin SSH
stays in scope only for steps that fundamentally need root or that the agent cannot do for itself.

The model has three access modes:

1. **Provisioning script** -- admin only. Cloud-init, apt, package install, sysctl, fstab.
2. **Interactive shell** -- admin or agent. `vm shell`, `agent shell`. Opens a TTY login shell
   directly as the requested user.
3. **Tmux session creation** -- admin or agent. Agent-mode session creation, restart, kill, and
   delete all run over the agent's own SSH session. Admin-mode sessions run over admin's.

A small carve-out keeps admin-side paths where they make sense (FRD R1):

- Admin's reads / attaches / maintenance into agent tmux servers via the shared socket and
  `server-access` ACL (the 2026-04-10 agent-tmux-sockets infrastructure).
- Batch operations across many agents (`stop_all_sessions`, `delete_agent` killing the agent's
  sessions). Iterating per-agent SSH probes is wasteful here; admin's existing socket access does
  the job.
- The agent bootstrap itself: `useradd`, tmux socket infrastructure under `/var/lib/`, and the
  initial `authorized_keys` install. Until those exist, direct agent SSH isn't possible.

The bootstrap uses a stage-and-install pattern: admin scp's the new `authorized_keys` content into a
`mktemp` staging path with 0600 perms, then `sudo install -o <agent> -g <agent> -m 0600` atomically
places it into the agent's home. No window where the file is readable by other uids.

The operator-side SSH config gets a parallel alias surface: each agent gets a top-level
`Host awagent--<agent>` block (configurable via `operator.ssh_agent_host_prefix`, default
`awagent--`), keyed on the operator-facing agent name rather than the on-VM Linux user (which is an
implementation detail).

## Consequences

- Simpler call-site contract: `admin_exec_target(vm, config)` vs.
  `agent_exec_target(vm, config, agent)`. Downstream code is identical regardless of which builder
  produced the `ExecTarget`. No more wrapping `tmux ...` commands in `sudo --login -u`.
- Cleaner phase split in `_create_agent_on_vm`: admin does the bootstrap (useradd, sockets,
  authorized_keys); everything after that runs over agent SSH. The only admin writes into the
  agent's home are the authorized_keys file itself.
- Compatibility with env-and-secrets: once agents are reached via their own SSH sessions, env var
  injection follows the standard SSH conventions. Detail is owned by the env-and-secrets SDD; this
  ADR only commits to the access model that makes it possible.
- New pre-rollout failure mode: agents created before this SDD landed have no `authorized_keys` for
  the operator. Direct agent SSH gets rejected with exit 255. Handled by `_assert_agent_ssh_works`:
  a one-line probe before destructive actions converts the opaque transport failure into a clear
  "run `agw agent reinit <name>`" instruction.
- Env-sensitive commands need login-shell wrapping. A plain non-interactive SSH command runs in a
  non-interactive non-login shell that sources nothing. Any agent-side step that invokes a
  user-installed binary (claude, mise, the user's dotfiles install command) or relies on
  profile-exported env wraps in `<shell> -lc <cmd>`. POSIX builtins and system-PATH tools (`git`,
  `grep`, `mkdir`, `printf`, `test`, `rm`) don't.
- Operator-facing alias surface: `ssh awagent--<name>` lands the operator directly in the agent's
  shell. VS Code Remote-SSH, ad-hoc `scp`, and any tool that targets SSH host aliases now work for
  agents the same way they already worked for VMs.
- Tradeoff: agent creation / reinit now has two sequential SSH transports (admin for the bootstrap,
  then agent for self-configuration). One extra connection handshake per agent operation. The cost
  is negligible against the simplification it enables.
