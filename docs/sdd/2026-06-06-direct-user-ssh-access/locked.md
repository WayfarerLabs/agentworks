# Direct target-user SSH access: lockfile

**Date:** 2026-06-12 **Status:** Locked

Implementation complete and shipped via PR #108. SDD artifacts (FRD, HLA, plan, ADRs) are settled as
of this date; further changes go through a new SDD or a follow-up PR that updates this lockfile with
a dated entry.

## What shipped

- **Direct target-user SSH access model** (FRD R1). Agent operations open SSH directly as the
  agent's Linux user; admin SSH keeps the bootstrap, the bulk-maintenance carve-outs, and the steps
  that fundamentally need root. The `sudo --login -u <agent>` detour is gone from the hot paths.
- **Agent lifecycle integration** (FRD R3). `_reconcile_authorized_keys` extended with an `owner=`
  stage-and-install path. Agent create / reinit / delete each call `sync_ssh_config(config, db)` so
  the operator-side SSH config tracks DB state declaratively.
- **Per-agent SSH alias surface** (FRD R7). `Host awagent--<agent>` blocks alongside the existing
  `Host awvm--<vm>` blocks. Configurable via `operator.ssh_agent_host_prefix` (default `awagent--`).
  Keyed on the operator-facing agent name, not the on-VM Linux user.
- **VM hardening at init** (FRD R4a/b). `hidepid=1` on `/proc` plus the sysctl baseline at
  `/etc/sysctl.d/99-agentworks.conf`, applied at both `vm create` and every `vm reinit`. Semantic
  fstab parse-and-edit (not a sentinel marker).
- **Pre-rollout safety.** `_assert_agent_ssh_works` probes agent SSH before destructive actions and
  surfaces pre-rollout agents as actionable `StateError` with an `agw agent reinit` hint.
- **Login-shell wrapping rule.** Agent-side commands that invoke user-installed binaries (claude,
  mise, dotfiles install, user install commands) wrap in `<shell> -lc`; POSIX builtins and
  system-PATH tools don't.

## ADRs

- [`docs/adrs/0011-direct-target-user-ssh-access.md`](../../adrs/0011-direct-target-user-ssh-access.md)
- [`docs/adrs/0012-vm-hardening-at-init.md`](../../adrs/0012-vm-hardening-at-init.md)

## Deferred at lock

- **Per-platform hidepid verification** (plan Phase 1). Lima verified 2026-06-10. Azure and WSL2
  verification planned in operator's environment; proxmox not currently testable. If any platform
  deviates from the lima result, the four pid-check call sites in `sessions/manager.py` and
  `sessions/tmux.py` route through sudo for that platform; design accommodates this without code
  shape changes.
- **Manual UX smokes** (plan Phases 2 / 3 / 5 / 6). `vm reinit` on a live VM, raw
  `ssh awagent--<agent>` landing in the agent's shell, pre-conversion session restart picking up the
  new code path, `agent shell` and `agent exec` clean env. Happen naturally during ongoing use; if
  any surface a problem, file a bug and update this lockfile with the date and remediation.

## Follow-ups (not part of this SDD)

- [#113](https://github.com/WayfarerLabs/agentworks/issues/113): add SSH ControlMaster to the
  managed SSH config blocks. Performance optimization on top of the access model that landed here.
  Separate PR, no SDD needed.
