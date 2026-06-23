# Provisioner shell and exec model cleanup -- Lockfile

## 2026-06-19

All plan items are complete:

- `exec_target()` was renamed to `admin_exec_target()` across the codebase.
- A single `ExecTarget.run(command, *, sudo, tty, check, timeout)` method replaced the prior
  per-transport dispatch for `run` / `run_as_root` / `_run_logged`.
- The Proxmox `admin_exec_target()` stub raises a clear `NotImplementedError` pointing at this SDD
  (operator escape hatches are documented in `cli/README.md` and the issue #117 heal hint shipped in
  PR #118).
- `agentworks vm rekey` is implemented and used.

These specs are accurate as of this date but are now locked and will not be updated to reflect
further changes to the implementation.

## Follow-up

This SDD normalized the within-shape exec model. The architectural shift from `ExecTarget`'s union
dispatch to polymorphic transports (one class per platform, factory functions for the canonical and
provisioner-specific paths) is its own effort, captured in
[2026-06-19-polymorphic-transports](../2026-06-19-polymorphic-transports/) and GitHub issue #128.
