# 4. Config-driven initialization over imperative provisioning

Date: 2026-03-05

## Status

Accepted

## Context

VM setup involves two categories of work: one-time platform operations (create the VM, allocate
resources, join Tailscale) and repeatable configuration (install packages, configure shell, sync
dotfiles, set up tools). These have different lifecycles: provisioning happens once, but
configuration needs to evolve as the operator's needs change.

An imperative approach (bake everything into the provisioner) means changes require destroying and
recreating VMs. A declarative approach (drive configuration from a config file) means changes can be
applied in place.

## Decision

VM lifecycle is split into two phases with separate status tracking:

- Phase A (provisioning): one-time, platform-specific, pass/fail. Creates the VM, installs base
  packages, joins Tailscale.
- Phase B (initialization): repeatable via `vm reinit`, driven entirely by config. Installs user
  packages, configures shell, syncs dotfiles, runs install commands, sets up mise, etc.

While Phase A remains a one-time effort, all Phase B steps are idempotent, non-fatal, and retryable
(failures produce warnings, not aborts). This can be used both to recover from transient errors and
to apply config changes without reprovisioning.

## Consequences

- `vm reinit` applies config changes without reprovisioning. Change a package list, add a tool,
  update dotfiles -- just reinit.
- Config is the source of truth for what a VM looks like. Templates extend this by providing named,
  inheritable configurations.
- Platform portability: the same config produces the same environment on Lima, Azure, and WSL2. Only
  Phase A differs per platform.
- Tradeoff: non-fatal failures mean a VM can end up in a partial state. This is surfaced clearly via
  `partial` init status and log files.
- Tradeoff: idempotency requires careful design of each init step. Install commands must be safe to
  re-run.
