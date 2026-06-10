# N. VM hardening at provisioning (hidepid=1 + sysctl baseline)

Date: 2026-06-10

## Status

Draft (will be numbered and moved to `docs/adrs/` when SDD `2026-06-06-direct-user-ssh-access`
merges).

## Context

The direct target-user SSH access model (see the companion ADR) lets multiple Linux users coexist on
the same VM with their own login surface. Cross-uid leakage that didn't matter when only admin ever
held a shell now does: `/proc/<pid>/cmdline` exposes process arguments (often containing secrets
passed via CLI flags), `/proc/<pid>/environ` exposes env vars, and the default sysctl profile on a
fresh VM leaves several other classes of cross-uid information readable.

We want a security baseline applied at every VM, idempotently, without each operator having to
remember to harden manually. The baseline must not break agentworks's own pid checks (admin reads
`/proc/<agent-pid>` directory entries to verify tmux server liveness) and must respect operators who
have hardened further on their own.

## Decision

VM init applies two hardening steps, both at `vm create` and at every `vm reinit`:

1. Mount `/proc` with `hidepid=1`. Restricts the contents of `/proc/<pid>/` (cmdline, environ,
   status, etc.) to processes owned by the same uid or root. Crucially, **directory entries remain
   visible cross-uid** -- `test -d /proc/<pid>` and other "does this pid still exist" checks
   continue to work for any uid. This is the difference between `hidepid=1` (restrict file contents)
   and `hidepid=2` (also hide pid existence).
2. Write `/etc/sysctl.d/99-agentworks.conf` with a baseline set: `kernel.dmesg_restrict=1`,
   `kernel.kptr_restrict=1`, `kernel.yama.ptrace_scope=1`, `fs.protected_*` family,
   `kernel.unprivileged_bpf_disabled=1`. Each closes a class of cross-uid surface that has no place
   in a multi-user VM.

`hidepid=1` over `=2` is a deliberate choice. Mode 2 would have forced agentworks's pid checks
(`sessions/manager.py:_pid_alive` and the `force_kill_tmux_server` guards) through a sudo path on
the cross-uid case, since admin would no longer see the agent's pid directory at all. Mode 1 keeps
the existence check working while restricting everything that actually leaks information. Plan phase
1 verified this empirically on lima before any code committed to the assumption.

The fstab edit uses a semantic parse-and-edit, not a sentinel-line marker.
`_ensure_proc_hidepid_in_fstab` locates the `/proc` row by fstype field, parses its options, and
chooses one of six actions: `no-op` / `appended` / `added-option` / `upgraded` /
`preserved-stricter` / `malformed`. An admin who has set `hidepid=2` keeps it (preserved-stricter).
An admin who has manually edited the line in unexpected ways gets a warning, no rewrite. This
replaces the originally-specified `# agentworks: hidepid` sentinel approach, which had a foot-gun:
an admin editing the line by hand strips the comment, the next reinit sees no sentinel and appends a
duplicate.

Both writes use a stage-and-install pattern (`mktemp` + `install -m 0644 -o root -g root` +
`finally rm`) so a partial failure can't leave a half-written file or leak the staging file.

A live remount (`mount -o remount,hidepid=<effective> /proc`) runs unconditionally so the kernel
mount picks up the new value without reboot. Naturally idempotent.

## Consequences

- Closes cross-uid metadata leakage at the kernel layer. No per-call-site mitigation needed; every
  process running on the VM gets the same protection regardless of what code spawned it.
- Idempotent reapply: a steady-state VM produces no observable side effects on `vm reinit`. The
  sysctl path content-compares before writing; the fstab editor returns `no-op` when the line
  already has `hidepid>=1`; the live remount is a kernel no-op when options match.
- Compatible with stricter operator overrides: an operator who set `hidepid=2` manually keeps it.
- The semantic editor is more code than a sentinel approach (~70 lines plus its test suite) but the
  action codes are exhaustively unit-tested and the design eliminates the duplication foot-gun the
  sentinel had.
- Empirically verified on lima (2026-06-10, Debian 12 / kernel 6.1.0-49-arm64). Plan phase 1 tracks
  the remaining platforms (azure, wsl2, proxmox); azure and proxmox are expected to behave
  identically (mainline kernel), wsl2 is the one worth a paranoid check (Microsoft-patched kernel).
- Tradeoff: hardening at init means a freshly-provisioned VM is not the same as a stock Debian VM in
  observable kernel behavior. Operators inspecting `/proc` for diagnostic reasons may find things
  missing they expected. The sysctl file is named with the `99-agentworks` prefix so it loads last
  and is easy to find.
