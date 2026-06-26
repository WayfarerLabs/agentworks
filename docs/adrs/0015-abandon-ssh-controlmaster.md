# 15. Abandon SSH ControlMaster

Date: 2026-06-26

## Status

Accepted. Supersedes the per-VM ControlMaster blocks added in PR #134 (issue #113).

## Context

PR #134 added IP-keyed `ControlMaster auto` / `ControlPath` / `ControlPersist 60s` blocks to the
managed ssh config to multiplex the dozens of sequential SSH calls each `vm create/reinit` and
`agent create/reinit` issues, dropping per-call latency from a fresh-handshake ~150-300ms to
~20-50ms once the master was warm.

The mechanism caches PAM/NSS-resolved state at master-open time and reuses it for every subsequent
command on the same multiplexed connection: supplementary groups (sshd's NSS lookup at auth),
`pam_env` vars, `pam_limits` RLIMITs, systemd-logind cgroup placement. Anything sshd reads once when
setting up the session inherits to every channel that follows, regardless of how `/etc/group` or
`/etc/security/*.conf` change in between.

Agentworks routinely mutates state of that kind between calls. The triggering example:
`agw session create` for an agent that doesn't yet have a workspace grant. The implicit-grant flow
runs `usermod -aG ws-<ws> agt--<agent>` via the admin connection _between_ the agent SSH master
opening (during `_assert_agent_ssh_works`) and the tmux launch reusing that same master. The pane
inherits the master's cached groups (without `ws-<ws>`), `cd /opt/agentworks/workspaces/<ws>`
returns EACCES, and tmux dies before the per-session socket is created. The operator sees a cryptic
"no server running on socket".

The class is broader than supplementary groups. Any feature that needs to change PAM/NSS-resolved
state mid-flight has the same shape of bug latent against the multiplexed connection. We can
band-aid individual symptoms (reorder + `ssh -O exit`, or `-o ControlMaster=no` on the affected
call), but the surface keeps growing and each instance is silent until it fires.

## Decision

Remove ControlMaster from the agentworks-managed ssh config. Every SSH call agentworks issues pays a
fresh handshake. The managed config emits only the alias blocks (`Host awvm--<name>`,
`Host awagent--<name>`); no `Host <tailscale_ip>` / `ControlMaster` block, no
`_controlmaster_supported` platform gate, no Windows-specific carve-out.

## Positives

- **Correctness across the surface.** Anything that depends on PAM/NSS state being read at command
  time gets that contract uniformly. No future feature has to know multiplexing exists to avoid this
  class of bug.
- **One less cross-platform branch.** The `_controlmaster_supported` Windows skip and its associated
  tests/docs are gone; Linux/macOS/Windows now behave identically here.
- **Smaller blast radius for operator customization.** No agentworks-emitted `Host <ip>` block to
  conflict with an operator's own ssh_config; the file contains only the per-resource aliases.

## Negatives

- **Per-call handshake comes back.** `vm create/reinit` and `agent create/reinit` regress by several
  seconds each (30+ sequential SSH calls × the saved handshake cost). Day-to-day `session create`,
  `agent list`, etc., are barely affected (handful of calls). The regression is acceptable because
  the affected operations are operator-rare and the correctness gain is surface-wide.

## Alternatives considered

- **Reorder the implicit-grant flow + tear down the master between the group add and the tmux
  launch.** Band-aid for this specific symptom; doesn't address the `pam_env` / `pam_limits` /
  cgroup analogs.
- **Use `-o ControlMaster=no -o ControlPath=none` on the tmux-launching call only.** Targeted, but
  exports the same "remember this special case" tax to every future flow that mutates PAM/NSS state.
- **Reduce `ControlPersist` to 0 (or drop it).** Doesn't help: the master persists for the duration
  of a single `agw` invocation regardless, and the failure occurs within one invocation.
- **Bundle related commands into fewer SSH calls.** Worth doing independently to recover some of the
  perf the optimization targeted, but a separate effort that doesn't share the correctness risk.
  Tracked as future work.

## Consequences

- `ssh_config.sync_ssh_config` no longer emits `Host <ip>` / `ControlMaster` blocks. On upgrade, any
  in-flight master sockets time out on their own `ControlPersist 60s` and OpenSSH silently falls
  back to fresh handshakes; no operator action required.
- Env var injection is unaffected. `-o SetEnv=K=V` (per ADR 14) is sent as an SSH2 channel `env`
  request per command, not part of the master's negotiated state, and was never multiplexed.
- If we want a portion of the perf back later, the lever is "issue fewer SSH calls" (bundle,
  parallelize where safe), not "cache the connection."
