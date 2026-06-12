# N. VM hardening at init (hidepid=1 + sysctl baseline)

Date: 2026-06-10

## Status

Draft (will be numbered and moved to `docs/adrs/` when SDD `2026-06-06-direct-user-ssh-access`
merges).

## Context

With multiple Linux users on the same VM, default Debian leaves cross-uid metadata leakage open at
the kernel layer: `/proc/<pid>/cmdline` exposes process arguments (often containing secrets passed
via CLI flags), `/proc/<pid>/environ` exposes env vars, and the default sysctl profile leaves
several other cross-uid surfaces readable. We need a security baseline applied automatically and
idempotently to every VM, not something operators have to remember.

## Decision

VM init applies two hardening steps, at both `vm create` and every `vm reinit`:

1. Mount `/proc` with `hidepid=1`. Restricts the contents of `/proc/<pid>/` to processes owned by
   the same uid or root.
2. Write `/etc/sysctl.d/99-agentworks.conf` with a baseline set: `dmesg_restrict`, `kptr_restrict`,
   `yama.ptrace_scope`, `fs.protected_*` family, `unprivileged_bpf_disabled`. Each closes a class of
   cross-uid surface that has no place on a multi-user VM.

`hidepid=1` rather than `hidepid=2`: mode 2 hides pid existence from non-owners, which would force
admin's liveness checks (`test -d /proc/<agent-pid>`) through a sudo path. Mode 1 keeps directory
entries visible cross-uid while restricting file contents. That's the exact threshold we need.

The fstab edit is a semantic parse-and-edit of the existing `/proc` row (find the `proc` fstype
line, edit its options field in place) rather than a sentinel-line marker. The semantic approach
avoids a duplication foot-gun: an admin editing the line by hand strips any sentinel comment, the
next reinit sees no sentinel and appends a duplicate.

## Consequences

- Cross-uid metadata leakage closed at the kernel layer. No per-call-site mitigation needed.
- Agentworks owns this layer of VM configuration. Manual operator edits to `/etc/fstab`'s `/proc`
  options or to the agentworks sysctl file are not recommended and can break agentworks (e.g.
  `hidepid=2` would break our liveness checks). The fstab editor is tolerant where it safely can be:
  it preserves `hidepid=2` if it sees an admin set it, and warns on lines it can't parse rather than
  rewriting blindly. The contract, though, is that agentworks owns this configuration.
- Idempotent reapply: a steady-state VM produces no observable side effects on `vm reinit`. The
  sysctl path content-compares before writing; the fstab editor returns `no-op` when the line
  already meets the bar; the live remount is a kernel no-op when options match.
- Verified on lima (2026-06-10). Remaining platforms (azure, wsl2, proxmox) tracked in the SDD plan;
  mainline-kernel platforms are expected to behave identically, WSL2 (Microsoft-patched kernel) is
  the one worth a smoke check.
