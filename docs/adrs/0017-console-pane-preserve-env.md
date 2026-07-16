# 17. Preserve Composed Env Across the Console Agent-Pane Sudo Boundary

Date: 2026-07-15

## Status

Accepted. Refines the sudoers portion of ADR 0014 (which narrowed the `AcceptEnv *` wildcard down to
`AGENTWORKS_* AW_*` at the sudo boundary). This ADR carves out the console add-shell agent-pane case
so that arbitrarily-named operator env and secrets reach that specific pane.

## Context

A console is a named tmux session on a VM that aggregates sessions as windows and can host extra
shell panes ("companion" or "add-shell" panes) beside them. The console's tmux server runs as the VM
**admin**. When an operator adds a shell pane to an **agent** session's window, that pane must run
as the **agent** user, so the pane bootstrap does `exec sudo --login -u <agent> ...`.

Env reaches the pane as `tmux split-window -e KEY=VAL` flags, which tmux sets on the pane process
before it execs. But `sudo --login` resets the environment. Per ADR 0014, VM init deploys
`Defaults env_keep += "AGENTWORKS_* AW_*"`, so only agentworks-managed vars survive that crossing.
Everything else, including operator-defined env and resolved secrets composed at the `agent` scope,
is stripped.

The observable result: a secret mapped to a session at the `agent` scope is present in the agent's
main session (created over the agent's own SSH login, no sudo) but absent in an agent companion
shell in a console. ADR 0014 called this stripping "intended" on the theory that such vars are
"scoped to the SSH session, not to processes the user delegates from there." That reasoning fits a
user delegating a fresh privileged process, but it does not fit the console agent pane: the pane is
not a delegation, it is agentworks reconstructing the agent's own working shell for the operator to
observe and drive. A shell that claims to be the agent's should carry the agent's environment.

`env_keep` cannot solve this on its own: secret and operator env-var names are arbitrary and defined
in operator config, not known at VM-init time, so they cannot be enumerated into a static allowlist.
We need a mechanism that names the vars to preserve **dynamically**, at pane-spawn time, when the
composed key set is known.

Scope note: this ADR covers `agent`-scope (plus `vm` / `workspace`) operator env and secrets, which
`_resolve_pane_env` already composes for the agent pane. It deliberately does **not** extend the
pane to full `session` scope; a companion shell remains "an admin or agent shell rooted in a
workspace," not part of the session itself (see `_resolve_pane_env`). Session scope for companion
shells is a separate identity-model question left for future work.

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

3. Ask sudo before committing to the flag in 1, because a VM missing the fragment from 2 refuses the
   whole command rather than dropping the un-preservable vars. `_split_shell_pane` probes once as
   the admin (`AWPROBE=1 sudo -n --preserve-env=AWPROBE -u <agent> true`) and, if refused, omits the
   flag and warns. `AWPROBE` deliberately matches neither `env_keep` glob, so it isolates the
   `setenv` grant; a covered name would pass even without the fragment. The probe sets the var
   itself rather than reusing a composed key, so it does not depend on the composed env having
   reached the CLI (it has not, on non-SSH transports). See the third Negative for why this is
   load-bearing rather than defensive padding.

The `env_keep` fragment from ADR 0014 stays, for two concrete reasons rather than as
belt-and-braces. It is what makes the degraded pane in 3 useful rather than empty: on a VM without
the `setenv` fragment, `AGENTWORKS_*` / `AW_*` are the only vars that survive, and they are the ones
that carry workspace identity. It also covers vars inherited from the console tmux server's own
environment, which are never in `pane_env` and so are never named on `--preserve-env`. For keys that
_are_ in `pane_env`, `--preserve-env` names them explicitly and `env_keep` is redundant; if the
fallback in 3 is ever retired, that redundancy is what retires with it.

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

- **Dynamic-linker names still do not survive.** `LD_PRELOAD`, `LD_LIBRARY_PATH`, and friends are
  stripped by the dynamic linker before sudo begins execution (sudo is setuid), so `--preserve-env`
  never sees them: `parse_env_list` looks each name up with `getenv` and silently skips the misses.
  An operator who named a secret after one of those would not see it in the pane. This is a
  non-issue in practice and arguably desirable; flagged here for completeness. Note that `setenv`
  exempts command-line vars from `env_check` / `env_delete`, so names like `IFS` and `BASH_ENV` _do_
  reach the pane. They are operator-authored config landing in the operator's own agent shell, so
  this crosses no privilege boundary.
- **Broader sudoers posture than a bare `NOPASSWD:ALL`.** An auditor sees a `setenv` directive for
  the admin user. Mitigated by the scoping (admin only) and by this ADR: the admin is already root,
  so `setenv` grants nothing new.
- **Requires reinit, and the fallback must be explicit.** A VM initialized before this fragment
  landed will not have the `setenv` directive, and neither will one whose fragment install failed
  (that step warns and carries on). Sudo does not merely drop the un-preservable vars in that case:
  `--preserve-env=<list>` is passed to the policy as command-line `env_add` vars (it does not set
  `MODE_PRESERVE_ENV`), and with `setenv` off, `validate_env_vars` rejects every one outside
  `env_keep` and aborts the command. A naive `sudo --login --preserve-env=... -u <agent>` would
  therefore _fail to start the pane at all_ on such a VM, which is strictly worse than the pre-ADR
  behavior, and it would fail precisely for the operators this ADR exists to serve (the flag is only
  populated when there is agent-scope operator env or secrets to carry). Hence the probe in
  decision 3. Same reinit-to-adopt story as ADR 0014.
- **A degraded pane is still a real outcome, not just a warning.** On the fallback path the operator
  gets a working shell that is missing its agent-scope env and secrets. We surface that with an
  `output.warn` naming the missing directive, the fragment path, and `agw vm reinit <vm>`, rather
  than only inside the pane, because `tmux split-window -P` returns a pane id whether or not the
  command inside it survives: the service layer cannot infer the degradation from the split's exit
  status, and an operator who never opens the pane would otherwise never learn of it.
- **One extra round trip per agent-pane split.** The probe is a real `sudo` call on the VM, so a
  `restore_session` repairing several agent panes pays it per pane and repeats the warning per pane.
  Accepted for now: the alternative is caching capability state per VM, which is stale-prone against
  out-of-band sudoers edits and buys little on a path already dominated by tmux round trips.

## Alternatives considered

- **Broaden `env_keep` to more patterns.** Rejected: secret and operator var names are arbitrary and
  operator-defined, so they cannot be enumerated into a static allowlist at VM-init time.
- **Write the composed env to an agent-owned `0600` file and source it post-sudo.** Robust and fully
  name-agnostic, but leaves secret material at rest with a file lifecycle (create, chown, clean up
  on pane death) to get right. Rejected in favor of keeping secrets off disk.
- **Pass `VAR=value` pairs to sudo directly** (also requires `setenv`). Rejected: puts secret values
  in the process table.
- **Enable `setenv` globally (`Defaults setenv`).** Rejected: broader than needed. Only the admin
  user spawns these panes, so the directive is scoped to it.
- **Extend the pane to full `session` scope.** Out of scope here; a companion shell is not part of
  the session under the current identity model. Tracked as separate future work.
- **Detect the missing fragment and refuse** (raise a `StateError` with a `reinit` hint instead of
  degrading). Tempting for an opinionated framework: arguably we should converge the VM and then
  give the operator the pane they asked for, rather than one that looks right but is missing its
  secrets. Rejected because `add_shell` is a best-effort path (`_split_shell_pane` already warns and
  returns `None` on a failed split rather than raising), and because it would make a companion shell
  on a not-yet-reinitialized VM strictly less useful than it is today, for env the operator may not
  even be relying on. The `output.warn` is the compromise: the operator is told, and still gets a
  shell. If the degraded pane proves to be a footgun in practice, refusing is the natural next step.
- **Record the fragment's deployment on the VM row and skip the probe when it is known present.**
  The DB is the source of truth for what init has done, so this would retire the per-split round
  trip. Rejected for now: it adds schema and a second source of truth for a VM-side fact that can
  drift (an operator editing sudoers out of band), and the probe is authoritative by construction.
  Worth revisiting if the round trip shows up in practice.

## Consequences

- VM init grows one more sudoers.d/ deployment step (`_write_sudoers_console_setenv`), reusing the
  shared stage -> `visudo -cf` -> promote helper.
- `_split_shell_pane`'s agent-pane branch adds `--preserve-env=<keys>` built from the composed
  `pane_env`, behind a `_sudo_can_preserve_env` capability probe that warns and falls back to a
  plain `sudo --login` when the VM lacks the `setenv` fragment.
- The probe is permanent, not a migration window: nothing records per-VM that the fragment landed,
  so every agent-pane split asks. That is a deliberate trade (authoritative over cached, one `sudo`
  call per split); see the last two Alternatives for what retiring it would take.
- Existing VMs need `agw vm reinit` to deploy the `51-agentworks-console-setenv` fragment before
  agent companion shells carry non-`AGENTWORKS_*` composed env.
