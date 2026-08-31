# Idempotency

Agentworks init, reinit, and repair operations are safe to re-run where listed below. This document
states the guarantees and limitations for `vm reinit`, `agent reinit`, and `workspace repair`.

## Install commands

An install-command is one logical shell invocation written as a single-line YAML scalar, either
plain or quoted. Prefer the template's `apt`, `apt_packages`, `snap`, or `mise_packages` fields,
then a maintained package-manager or vendor entry point. Embedded scripts, block scalars,
here-documents, multi-step installers, state machines, signature pipelines, and cleanup routines do
not belong in an install-command manifest.

Init and reinit may both reach the resource. Its invocation must be repeat-safe itself or declare
`test_exec`, `test_file`, or `test_dir` completion checks that reliably skip it after success. When
at least one check is declared, Agentworks skips the command only when every declared check passes.
With no checks, the command always runs.

## Re-pointing the bound template

`agent reinit` accepts `--update-template <name>`. The DB declares the desired state and reinit
converges live state to it, so re-pointing is a two-part operation: the flag changes _which_
template the agent is bound to (validated against the resource registry, then persisted to the DB),
and the existing reinit run then applies it exactly as it applies any template change. Plain reinit
already re-reads the current content of the stored template each run; the flag only changes the
binding.

An undeclared template name is rejected up front, before any prompt or on-VM work, and leaves the
stored binding untouched.

The re-point is persisted before the on-VM convergence, so it is deliberately non-atomic: if setup
fails partway through, the agent stays bound to the new template and a plain `agent reinit <name>`
(no flag) re-converges toward it.

The same boundary accepts an inline `--spec` as the final instance-specific layer. Omitting the
option retains the prior layer, supplying a JSON object replaces it, and `--spec '{}'` or
`--spec ''` clears it. The empty-value shorthand is specific to `agent reinit`; create commands
still require a JSON object.

When `--update-template` and `--spec` are supplied together, Agentworks validates and stores the new
template binding and instance layer as one desired-state change before convergence. A later plain
reinit reads both stored inputs again.

## VM reinit

`vm reinit` re-runs Phase B (initialization) using the current config. All steps are non-fatal:
failures produce warnings and a `partial` status.

If the VM was created with `--spec` or `--admin-spec`, reinit consumes both stored final layers
after their selected VM and admin templates. It does not accept a flag to change or clear either
layer.

### Fully idempotent

| Step                   | Notes                                                                                  |
| ---------------------- | -------------------------------------------------------------------------------------- |
| Apt sources            | Key downloaded if missing, source list overwritten                                     |
| SSH host key preserve  | cloud-init drop-in written so host keys survive stop/start                             |
| Shell                  | Overwritten from config                                                                |
| SSH authorized keys    | Overwritten from config                                                                |
| Git credentials        | Complete desired list rebuilt; empty removes Agentworks-owned credential/routing state |
| Dotfiles (git source)  | `git pull` if already cloned, fresh clone if not                                       |
| Mise packages          | Installed if missing, pruned if removed (when `mise_prune_on_reinit = true`)           |
| Mise activation        | Overwritten from config (disabled comment written when off)                            |
| PATH additions         | Overwritten from config                                                                |
| Tailscale DNS          | Startup-ordering drop-in rewritten only when content differs                           |
| sshd AcceptEnv         | Drop-in overwritten; sshd reloaded                                                     |
| sudoers env_keep       | Fragment overwritten, staged and `visudo -cf` validated before promotion               |
| sudoers console setenv | Same; scoped to the VM's admin user                                                    |

### Additive only

These add things on reinit but do not remove them when removed from config:

| Step                    | Notes                                                                                                                                          |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Apt packages            | Never removed. Transitive deps not cleaned up. Too risky for reinit.                                                                           |
| Snap packages           | Never removed.                                                                                                                                 |
| System install commands | Not uninstalled when removed from config. Commands must be repeat-safe intrinsically or use reliable completion checks to skip completed work. |
| User install commands   | Same as system install commands.                                                                                                               |
| Mise packages           | When `mise_prune_on_reinit = false`, stale tool versions are not removed.                                                                      |

### Other

| Step                    | Notes                                                                    |
| ----------------------- | ------------------------------------------------------------------------ |
| Dotfiles (local source) | Overwritten, not merged. Side effects from previous installs may linger. |

## Agent reinit

`agent reinit` re-runs the full agent setup using the stored template. The user step converges
rather than blindly creating: an existing user is left in place (with its shell corrected if the
template changed), and a missing user is created.

The user step is detection-based and reports the true outcome (it is verifying, not blindly
creating): it probes the agent user with `getent passwd` first, then reports `Created agent user`
when the user was absent, `Agent user '<name>' already exists` when it converged as a no-op, or
`Fixed agent user '<name>': shell <old> -> <new>` when an existing user's login shell diverged from
the template. Because the report is driven by detected state rather than by the calling command, a
truly-gone agent user recreated on reinit is honestly reported as created, not silently
"reinitialized" into existence.

After the user step, reinit reconciles the agent's workspace group memberships against its recorded
grants. This matters when the user was recreated: a fresh Linux user lands with no group
memberships, while the DB grant rows survive the reinit untouched, so without this pass the agent
would hold grants in the database but no actual on-VM workspace access. The reconcile is idempotent
(each group add is a no-op when membership already holds), so it runs on every reinit, and it
touches on-VM state only: the grant rows are left as they are. A grant row pointing at a
since-deleted workspace is skipped with a warning rather than failing the repair. On success reinit
reports how many grants it reconciled, so an operator recovering a gone user gets confirmation the
access was restored.

### Fully idempotent

| Step                       | Notes                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| User creation              | Detection-based: created if absent, shell corrected if diverged, else no-op                                                                                              |
| Workspace group            | Skipped if exists                                                                                                                                                        |
| Workspace grant membership | Reconciled from the agent's recorded grants: idempotent group add per granted workspace, no-op if already a member. Repairs a recreated user whose memberships were lost |
| Shell rc (prompt)          | Overwritten from template                                                                                                                                                |
| Git credentials            | Complete desired list rebuilt; empty removes Agentworks-owned credential/routing state                                                                                   |
| Dotfiles (git source)      | `git pull` if already cloned                                                                                                                                             |
| Mise packages              | Installed if missing, pruned if removed (when `mise_prune_on_reinit = true`)                                                                                             |
| Mise activation            | Overwritten from template                                                                                                                                                |
| PATH additions             | Appended idempotently                                                                                                                                                    |

### Additive only

| Step                  | Notes                                                                     |
| --------------------- | ------------------------------------------------------------------------- |
| User install commands | Not uninstalled when removed from template.                               |
| Mise packages         | When `mise_prune_on_reinit = false`, stale tool versions are not removed. |

### Other

| Step                    | Notes                                                                    |
| ----------------------- | ------------------------------------------------------------------------ |
| Dotfiles (local source) | Overwritten, not merged. Side effects from previous installs may linger. |

## Workspace repair

`workspace repair` converges the on-VM workspace to the DB and template. It never re-clones the repo
(the checkout is preserved) and never removes on-VM state.

### Fully idempotent

| Step                         | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Workspace group + membership | Group created if missing; admin and granted agents reconciled against the grants. Reports `Fixed:` per change, `OK:` when already correct.                                                                                                                                                                                                                                                                                                                               |
| Ownership, permissions, SGID | Canonical chown/chmod re-run every time (no-ops on already-correct state), but carry `-c` so the step observes whether anything changed and reports `Fixed:` vs `OK:` accordingly.                                                                                                                                                                                                                                                                                       |
| ACLs                         | Canonical setfacl (the same recursive spec `workspace create`, `copy`, `rehome`, and VM init apply, via one shared helper) re-run every time; the step snapshots the tree's ACLs with `getfacl` before and after and compares, reporting `Fixed:` only on a real change and `OK:` otherwise. Because all workspace ACL paths share the spec, a first repair of a freshly created, copied, or rehomed workspace is a no-op.                                               |
| Parent traversal             | `chmod a+x` re-applied up each ancestor, seeded from the workspace's PARENT so the walk never touches the workspace dir's own canonical `2770` (which carries no world bits by design). Carries `-c`, so the step reports `Fixed:` when it opened a missing traversal bit and `OK:` when every ancestor was already traversable. A freshly created workspace whose ancestors are already traversable reports `OK:` here, so a first repair after create is a true no-op. |
| Git identity                 | Template `git_user_name` / `git_user_email` stamped into the checkout's repo-local config; detection-based, so an already-correct value is left as-is. No-op when no identity is declared or the workspace has no repo.                                                                                                                                                                                                                                                  |
