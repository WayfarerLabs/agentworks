# 14. AcceptEnv Wildcard on Agentworks-Managed VMs

Date: 2026-06-13

## Status

Accepted. Where this document describes how the CLI sources secret values, that mechanism is
superseded by ADR 0016's backend-chain model; the transport decision itself stands. The sudoers
`env_keep` decision below (only `AGENTWORKS_* AW_*` survive a sudo boundary) is refined by ADR 0017
for the console agent-pane case, where composed operator env and secrets also cross via
`--preserve-env`.

## Context

The env-and-secrets SDD propagates user-defined env vars (plaintext and secret-resolved) plus
agentworks-managed `AGENTWORKS_*` identity vars into every shell agentworks opens on a managed VM.
These vars vary per-shell-open: per session, per pane, per execution context.

The initial design carried env into the remote shell by composing a shell prelude
(`export K1=v1 && export K2=v2 && …`) and prepending it to the SSH command. Working through the
implementation surfaced friction: nested quoting through SSH → tmux → pane shell, divergent behavior
across `sudo --login` user switches, mismatched login-vs-non-login shell semantics, and a nontrivial
`build_export_block` / `build_prefixed_command` composition layer in the CLI. Closer examination
showed SSH has a native mechanism for the same job:

- `ssh -o SetEnv=KEY=VALUE user@host` (client side): hands the var directly to the SSH protocol's
  environment-passing channel. Available since OpenSSH 7.8 (every distro we target).
- `AcceptEnv` (server side): names which env-var patterns sshd is willing to accept from the client
  and inject into the user's shell environment (sshd's session-spawn code path places the matched
  vars into the child environment before exec; this is independent of the `pam_env` module). Most
  distros ship a conservative default (`AcceptEnv LANG LC_*`) for historical compatibility reasons.

Adopting SetEnv on the client requires the server to accept the vars. The question is: **what should
`AcceptEnv` allow?**

Three candidate patterns:

1. **Curated allowlist**: `AcceptEnv AGENTWORKS_* AW_*` (plus whatever other prefixes we adopt over
   time). Pro: minimum-surface, easy to audit. Con: every new agentworks-managed prefix (e.g. for
   future per-agent metadata) needs a sshd config update and a VM reinit. Composability with
   operator-defined env (R2) requires deciding on naming conventions and possibly extending the list
   per-template.
2. **Operator-extensible allowlist**: `AcceptEnv AGENTWORKS_* AW_*` plus an operator-provided
   patterns config option that gets folded into sshd_config at init time. Pro: predictable surface.
   Con: more config sprawl; operators have to think about pattern lists; failure mode is "I set a
   var, it didn't reach the VM" which is hard to debug.
3. **Wildcard**: `AcceptEnv *`. sshd accepts whatever the client asks to send. Pro: zero config
   sprawl, no failure mode where "I set a var and it didn't get through," uniform behavior across
   the whole agentworks env surface. Con: broader posture than typical defaults.

The historical reason `AcceptEnv` defaults to conservative is that env vars can change shell
behavior in unexpected ways: `LD_PRELOAD`, `LD_LIBRARY_PATH`, `IFS`, `PS4`, `BASH_ENV` and friends
can be weaponized when an attacker can inject vars into a shell they otherwise couldn't reach. That
threat applies in environments where SSH access is granted to many parties with varying trust
levels, or where the SSH-using identity is more privileged than the credential holder.

Agentworks's threat model doesn't match that profile. Anyone who can SSH to an agentworks-managed VM
has authenticated against keys in the agentworks operator's possession; if they have those keys,
they already have shell access to the VM and don't need env-var tricks to influence what their own
shell does. There is no scenario in which an attacker can inject env vars without also holding the
credentials needed to log in as the target user. The "untrusted SSH party" leg of the threat model
is absent.

## Decision

Set `AcceptEnv *` in sshd_config on every agentworks-managed VM. The same VM-init code path that
already deploys VM-stable identity fragments and the agentworks tmux-restricted config writes the
sshd config snippet, restarts sshd (the existing init flow already manages sshd lifecycle), and
verifies the directive is active.

All SSH commands that agentworks issues coalesce every env pair into one
`-o SetEnv="K1=V1" "K2=V2" ...` argument; no shell-prelude composition lives in the CLI. The
single-argument shape matters: `ssh_config(5)` says "for each parameter, the first obtained value
will be used," so repeating `-o SetEnv=` per pair silently drops every pair after the first. Values
are always double-quoted with `\` and `"` escaped so spaces, empty values, and embedded quotes pass
through cleanly. The `agentworks.env` package's `compose_env` produces the flat `dict[str, str]`
that drives the SetEnv arg, but no longer feeds `build_export_block`. Both `build_export_block` and
`build_prefixed_command` are removed; env transport is a property of the SSH connection, not a
property of the shell command.

For sudo-to-agent paths (console add-shell panes that switch user to an agent for a pane shell), the
corresponding sudoers config grows an `env_keep += "AGENTWORKS_* AW_*"` directive so that sudo
doesn't strip agentworks-managed vars across the user switch. This narrows the wildcard at the
sudoers layer: only the agentworks-prefixed vars survive sudo, not arbitrary client-supplied env.
Operators that put non-agentworks vars into their config tables (R2) get those vars stripped when
crossing into a sudo'd shell; this is intended (the var was scoped to the SSH session, not to
processes the user delegates from there).

## Positives

- **Removes a layer of complexity from the CLI.** No prelude composition, no quote-escape reasoning
  through nested shells, no "outer shell vs login shell" question. The SetEnv path is one well-known
  SSH protocol feature; the existing OpenSSH client and sshd handle the transport.
- **Uniform across all SSH command paths the design targets**: sessions, consoles, exec, interactive
  shells, provisioning, agent setup. Every site composes the same env dict and passes it to the SSH
  layer. (Phase 3 wires sessions / consoles / exec / interactive paths; Phase 4 deploys the VM-side
  acceptance plus initial profile fragments; threading the `env=` kwarg through the per-step
  provisioning and agent-setup SSH calls is the remaining Phase 4 follow-up, tracked in the plan.)
- **Survives the `sudo --login` boundary** cleanly via the targeted sudoers `env_keep` directive,
  without the CLI having to invent a per-user-switch injection mechanism.
- **Operator-extensible without sshd_config changes**: an operator who adds a new env-var pattern to
  their config gets it propagated immediately. No sshd restart, no reinit.
- **No env-var-naming dependency between client and server.** A future agentworks feature that wants
  to ship a new var prefix doesn't have to land an sshd_config update.
- **Composable with `agw env show`**: the CLI knows exactly what it would SetEnv, so the inspection
  surface is exact, not "approximately what the shell will see after dotfiles run."

## Negatives

- **Broader sshd posture than typical defaults.** Operators auditing their VM's `sshd_config` will
  see the wildcard and may need to be reassured about the threat-model alignment above. Mitigated by
  this ADR existing as the answer.
- **`AcceptEnv` is implemented inside sshd itself.** The matched vars are placed in the session env
  by sshd's `session.c` before it `exec`s the user's shell (distinct from the `pam_env` module,
  which sources vars from `pam_env.conf` / `environment` files). Its precise behavior across distros
  is conventionally well-defined but not centrally guaranteed; a future minor sshd change could in
  principle alter the surface. Validated on Debian 12 (the agentworks base, per ADR 0002), whose
  openssh-server has carried this code path stably for many years. Non-Debian VM bases are out of
  scope per ADR 0002. We treat `AcceptEnv` as a stable OpenSSH contract for the foreseeable future;
  if a regression appears, this ADR is the place to revisit.
- **Values containing newlines** are not reliably transportable via SetEnv (the SSH protocol encodes
  env strings without escaping mechanisms for control chars). Agentworks secrets are expected to be
  opaque tokens; an operator who tries to set a multiline value in `[admin.env]` may see truncation.
  Surfaced as a config-load warning when a plaintext value contains a newline; resolved secret
  values are also checked at `SecretResolver.resolve_all` time and raise `ConfigError` rather than
  silently corrupting the SSH argument. The env-var source additionally strips trailing newlines
  (the common copy-paste artifact).
- **The wildcard is a one-time decision per VM.** Operators who want a curated allowlist for their
  own audit reasons would need to override agentworks's init template. This is supported (init
  writes a single file under `/etc/ssh/sshd_config.d/`; operators can replace it) but adds one more
  "you can override this" surface to the docs.

## Alternatives considered

- **Curated allowlist** (option 1 above). Rejected on operator-friction grounds: every new var
  pattern would need an sshd config change and a VM reinit. The friction is recurring; the posture
  benefit is small in a model where the SSH credential holder already has shell access.
- **Operator-extensible allowlist** (option 2). Rejected as the worst of both worlds: requires
  pattern config in agentworks config, doesn't materially improve the posture vs wildcard given the
  threat-model alignment, and introduces a "I set a var, it didn't reach the VM" failure mode that's
  hard to debug.
- **Shell-prelude composition** (the original design). Rejected after working through the
  implementation: the nested-quote handling, login-vs-non-login boundary semantics, and
  `sudo --login` env-wipe interactions add complexity that SetEnv eliminates. See the SDD plan's
  "Phase 3 (SetEnv pivot)" section for the trace of why.
- **Brokered on-VM secret service** (one of the original SDD alternatives). Rejected in the
  cli-side-secret-injection ADR; the env transport question is downstream of that decision and
  doesn't reopen it.

## Consequences

- VM init grows a small sshd_config.d/ deployment step (mirrors the existing /etc/profile.d/
  fragment install).
- `agentworks.env.exports` (which currently houses `build_export_block` / `build_prefixed_command`)
  is deleted. The env package surface narrows to: `EnvEntry`, `effective_env`, `ResourceContext` +
  identity producers, `compose_env`.
- The SSH layer (`agentworks.ssh`) gains an `env: dict[str, str] | None` kwarg on `run` and
  `interactive` plus on `ExecTarget.run`. The `RunCommand` protocol widens to match.
- VM-init also writes a sudoers fragment with `env_keep += "AGENTWORKS_* AW_*"`. The sudoers surface
  narrows the AcceptEnv wildcard, so only agentworks-managed vars survive a sudo boundary.
- An existing VM that predates this ADR cannot accept SetEnv'd env vars until it's reinit'd to pick
  up the new sshd config. Operators with pre-SDD VMs need to run `agw vm reinit` to deploy the sshd
  fragment. We considered a doctor probe that SSHs into every VM to check for the
  `50-agentworks-accept-env.conf` fragment, but rejected it: every `agw doctor` invocation would pay
  an SSH round-trip per VM, and the same shape of probe would need to grow for each future drift the
  operator cared about (a slippery slope). The principled fix is to version the VM definition in the
  database and detect drift cheaply against the recorded version; that's tracked as future work. In
  the meantime, operator-visible failures (SetEnv'd vars not reaching the VM) are the cue to reinit.
