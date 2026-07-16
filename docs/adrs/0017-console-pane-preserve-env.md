# 17. Preserve Composed Env Across the Console Agent-Pane Sudo Boundary

Date: 2026-07-15

## Status

Accepted. Refines the sudoers portion of ADR 0014 (which narrowed the `AcceptEnv *` wildcard down to
`AGENTWORKS_* AW_*` at the sudo boundary). This ADR carves out the console add-shell agent-pane case
so that arbitrarily-named operator env and secrets reach that specific pane.

## Context

A console is a named tmux session on a VM that aggregates sessions as windows and can host extra
shell panes ("companion" or "add-shell" panes) beside them. The console's tmux server runs as the VM
**admin**. When an operator adds a shell pane to an **agent** session's window, that pane must run as
the **agent** user, so the pane bootstrap does `exec sudo --login -u <agent> ...`.

Env reaches the pane as `tmux split-window -e KEY=VAL` flags, which tmux sets on the pane process
before it execs. But `sudo --login` resets the environment. Per ADR 0014, VM init deploys
`Defaults env_keep += "AGENTWORKS_* AW_*"`, so only agentworks-managed vars survive that crossing.
Everything else, including operator-defined env and resolved secrets composed at the `agent` scope,
is stripped.

The observable result: a secret mapped to a session at the `agent` scope is present in the agent's
main session (created over the agent's own SSH login, no sudo) but absent in an agent companion shell
in a console. ADR 0014 called this stripping "intended" on the theory that such vars are "scoped to
the SSH session, not to processes the user delegates from there." That reasoning fits a user
delegating a fresh privileged process, but it does not fit the console agent pane: the pane is not a
delegation, it is agentworks reconstructing the agent's own working shell for the operator to observe
and drive. A shell that claims to be the agent's should carry the agent's environment.

`env_keep` cannot solve this on its own: secret and operator env-var names are arbitrary and defined
in operator config, not known at VM-init time, so they cannot be enumerated into a static allowlist.
We need a mechanism that names the vars to preserve **dynamically**, at pane-spawn time, when the
composed key set is known.

Scope note: this ADR covers `agent`-scope (plus `vm` / `workspace`) operator env and secrets, which
`_resolve_pane_env` already composes for the agent pane. It deliberately does **not** extend the pane
to full `session` scope; a companion shell remains "an admin or agent shell rooted in a workspace,"
not part of the session itself (see `_resolve_pane_env`). Session scope for companion shells is a
separate identity-model question left for future work.

## Decision

1. At pane-spawn time, pass the composed env keys explicitly on the sudo invocation:
   `sudo --login --preserve-env=<K1,K2,...> -u <agent> ...`. The **values** continue to ride the
   `tmux -e` channel; only the **names** appear on the argv, so no secret value is exposed in the
   process table. The flag is omitted entirely when there is no composed env.

2. Permit that on the VM with a new, user-scoped sudoers fragment deployed at init:
   `/etc/sudoers.d/51-agentworks-console-setenv` containing `Defaults:<admin> setenv`. Without
   `setenv`, sudo refuses `--preserve-env` for any var outside the `env_keep` allowlist. The
   directive is scoped to the admin user (`Defaults:<user>`), not enabled globally. It is validated
   with `visudo -cf` on a staging path before promotion, identical to the `env_keep` fragment.

The `env_keep` fragment from ADR 0014 stays. It still carries `AGENTWORKS_* AW_*` unconditionally
(including for admin panes, which never sudo and so never consult it), and it is the belt to
`--preserve-env`'s suspenders for the managed vars.

## Positives

- **Fixes the reported gap:** agent-scope secrets and operator env now reach agent companion shells,
  matching the agent's main session.
- **No secrets at rest and no secret values on the argv.** Values stay on the tmux `-e` channel that
  already carried them; `--preserve-env` lists names only. This is why we chose it over a
  write-env-to-a-file handoff (which would leave secret material on disk to manage and clean up) and
  over passing `VAR=value` pairs to sudo (which would put values in the process table).
- **No privilege change.** The admin already holds `ALL=(ALL) NOPASSWD:ALL`; granting it `setenv`
  only permits command-line env preservation it could already achieve as root. Scoping the directive
  to the admin user keeps the surface off every other account.
- **Name-agnostic and dynamic.** Works for any operator/secret var name without an sshd or sudoers
  change per name; the preserved set is computed from the composed env at spawn time.

## Negatives

- **A few reserved names still do not survive.** sudo's env sanitization (`env_delete` / `env_check`)
  drops names like `LD_PRELOAD`, `LD_LIBRARY_PATH`, `IFS`, `BASH_ENV`, and friends even when listed
  in `--preserve-env`. An operator who named a secret after one of those would not see it in the
  pane. This is a non-issue in practice and arguably desirable; flagged here for completeness.
- **Broader sudoers posture than a bare `NOPASSWD:ALL`.** An auditor sees a `setenv` directive for
  the admin user. Mitigated by the scoping (admin only) and by this ADR: the admin is already root,
  so `setenv` grants nothing new.
- **Requires reinit.** A VM initialized before this fragment landed will not have the `setenv`
  directive, so `--preserve-env` is refused and the pane silently falls back to the `env_keep`
  behavior (only `AGENTWORKS_* AW_*`). Same reinit-to-adopt story as ADR 0014; the operator cue is a
  composed var not appearing in an agent companion shell.

## Alternatives considered

- **Broaden `env_keep` to more patterns.** Rejected: secret and operator var names are arbitrary and
  operator-defined, so they cannot be enumerated into a static allowlist at VM-init time.
- **Write the composed env to an agent-owned `0600` file and source it post-sudo.** Robust and fully
  name-agnostic, but leaves secret material at rest with a file lifecycle (create, chown, clean up on
  pane death) to get right. Rejected in favor of keeping secrets off disk.
- **Pass `VAR=value` pairs to sudo directly** (also requires `setenv`). Rejected: puts secret values
  in the process table.
- **Enable `setenv` globally (`Defaults setenv`).** Rejected: broader than needed. Only the admin
  user spawns these panes, so the directive is scoped to it.
- **Extend the pane to full `session` scope.** Out of scope here; a companion shell is not part of
  the session under the current identity model. Tracked as separate future work.

## Consequences

- VM init grows one more sudoers.d/ deployment step
  (`_write_sudoers_console_setenv`), reusing the shared stage -> `visudo -cf` -> promote helper.
- `_split_shell_pane`'s agent-pane branch adds `--preserve-env=<keys>` built from the composed
  `pane_env`.
- Existing VMs need `agw vm reinit` to deploy the `51-agentworks-console-setenv` fragment before
  agent companion shells carry non-`AGENTWORKS_*` composed env.
