# Idempotency

Agentworks init and reinit operations are designed to be safe to re-run. The general goal was to be
as idempotent as possible but that was not always achievable. This document describes what is and is
not idempotent across `vm reinit` and `agent reinit`.

## VM reinit

`vm reinit` re-runs Phase B (initialization) using the current config. All steps are non-fatal:
failures produce warnings and a `partial` status.

### Fully idempotent

| Step                  | Notes                                                                                                               |
| --------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Apt sources           | Key downloaded if missing, source list overwritten                                                                  |
| Shell                 | Overwritten from config                                                                                             |
| SSH authorized keys   | Overwritten from config                                                                                             |
| Git credentials       | Overwritten from config                                                                                             |
| Dotfiles (git source) | `git pull` if already cloned, fresh clone if not                                                                    |
| Mise packages         | Installed if missing, pruned if removed (when `mise_prune_on_reinit = true`)                                        |
| Mise activation       | Overwritten from config (disabled comment written when off)                                                         |
| PATH additions        | Overwritten from config                                                                                             |
| Tailscale DNS         | Drop-in rewritten only when content differs; `resolv.conf` symlink repaired only when not already the resolved stub |

### Additive only

These add things on reinit but do not remove them when removed from config:

| Step                    | Notes                                                                     |
| ----------------------- | ------------------------------------------------------------------------- |
| Apt packages            | Never removed. Transitive deps not cleaned up. Too risky for reinit.      |
| Snap packages           | Never removed.                                                            |
| System install commands | Not uninstalled when removed from config. Skipped if test passes.         |
| User install commands   | Same as system install commands.                                          |
| Mise packages           | When `mise_prune_on_reinit = false`, stale tool versions are not removed. |

### Other

| Step                    | Notes                                                                    |
| ----------------------- | ------------------------------------------------------------------------ |
| Dotfiles (local source) | Overwritten, not merged. Side effects from previous installs may linger. |

## Agent reinit

`agent reinit` re-runs the full agent setup using the stored template. The Linux user is not
recreated (skipped if exists), but the shell is updated if the template changed.

### Fully idempotent

| Step                  | Notes                                                                        |
| --------------------- | ---------------------------------------------------------------------------- |
| User creation         | Skipped if exists; shell updated if template changed                         |
| Workspace group       | Skipped if exists                                                            |
| Shell rc (prompt)     | Overwritten from template                                                    |
| Git credentials       | Overwritten from template                                                    |
| Dotfiles (git source) | `git pull` if already cloned                                                 |
| Mise packages         | Installed if missing, pruned if removed (when `mise_prune_on_reinit = true`) |
| Mise activation       | Overwritten from template                                                    |
| PATH additions        | Appended idempotently                                                        |

### Additive only

| Step                  | Notes                                                                     |
| --------------------- | ------------------------------------------------------------------------- |
| User install commands | Not uninstalled when removed from template.                               |
| Mise packages         | When `mise_prune_on_reinit = false`, stale tool versions are not removed. |

### Other

| Step                    | Notes                                                                    |
| ----------------------- | ------------------------------------------------------------------------ |
| Dotfiles (local source) | Overwritten, not merged. Side effects from previous installs may linger. |
