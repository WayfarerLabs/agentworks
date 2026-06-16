# Environment variables and secrets: lockfile

**Date:** 2026-06-15 **Status:** Locked

Implementation complete and shipped on branch `feat/env-and-secrets-sdd`. SDD artifacts (FRD, HLA,
plan, ADRs) are settled as of this date; further changes go through a new SDD or a follow-up PR that
updates this lockfile with a dated entry.

## What shipped

### Foundations

- **`agentworks.secrets` package** (FRD R3, R4). `SecretDecl`, `SecretConfig`, the `SecretSource`
  protocol, and the v1 sources (`EnvVarSource` reading `AW_SECRET_<NAME>`; `PromptSource` as the
  interactive last-resort). `SecretResolver` walks the configured backend chain in precedence order,
  caches values for the lifetime of one CLI invocation, and raises `SecretUnavailableError` with a
  per-secret backend-tried hint on resolution failure.
- **`agentworks.env` package** (FRD R1, R2). `EnvEntry` (plaintext-or-secret-ref), `effective_env`
  with the FRD R2 precedence ladder `session > (agent | admin) > workspace > vm`, `ResourceContext`
  with per-context / per-user / vm-stable identity producers, and `compose_env` that renders user
  env through the resolver and overlays identity vars.
- **TOML loader extensions** (FRD R2, R3). `[admin.env]`, `[vm_templates.*.env]`,
  `[workspace_templates.*.env]`, `[agent_templates.*.env]`, `[session_templates.*.env]`,
  `[secrets.<name>]`, `[secret_backends.<kind>]`, and `[secret_config]` parsed and validated at
  config load. Dangling `{ secret = "..." }` references fail at load time; `AGENTWORKS_*` overrides
  emit a load-time warning.

### Transport

- **SSH SetEnv** (FRD R5, ADR 0014). The `agentworks.ssh` layer's `run` / `interactive` /
  `ExecTarget.run` / `ExecTarget.call_streaming` all accept an `env: dict[str, str] | None` kwarg
  and coalesce every pair into a single `-o SetEnv="K1=V1" "K2=V2" ...` argument (values always
  double-quoted with `\` and `"` escaped). Repeating `-o SetEnv=` per pair would silently drop every
  pair after the first per ssh_config(5)'s "first obtained value wins" rule; see `ssh._set_env_args`
  and the regression coverage in `test_ssh_set_env.py`. The remote sshd accepts the vars under the
  `AcceptEnv *` directive deployed at VM init.
- **VM-side fragments** (Phase 4). `/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf`
  (`AcceptEnv *`, validated with `sshd -t` before reload), `/etc/sudoers.d/50-agentworks-env-keep`
  (`env_keep += "AGENTWORKS_* AW_*"`, validated with visudo via staging),
  `/etc/profile.d/agentworks-identity.sh` for VM-stable identity vars, `~/.agentworks-profile.sh`
  for per-user identity. Idempotent on reinit.

### Eager prompting (Phase 6)

- **Generic orchestrator** (`secrets/orchestration.py`). `SecretTarget` mirrors the per-scope env
  shape; `compute_needed_secrets` walks each target's env chain via `effective_env` and unions
  referenced `SecretDecl`s with first-encounter ordering; `resolve_for_command` issues one batched
  `SecretResolver.resolve_all` and returns the resolved map. The orchestrator stays generic (no DB /
  template coupling); each manager call site builds its candidate set from static filters only and
  calls the orchestrator before any state mutation.
- **Manager-entry eager-resolve** wired at every shell-opening command per FRD R4: `session create`,
  `session restart`, `console add-shell`, `vm create`, `vm reinit`, `agent create`, `agent reinit`,
  `vm shell`, `vm exec`, `agent shell`, `agent exec`, plus the console build path (`attach_console`
  on first attach or `--recreate`) and `restore_session`.
- **Env threading through provisioning + agent setup** (Phase 6.3b + 6.4b). `_phase_b_setup`
  composes `admin_env` once via `compose_env` (the manager-entry eager-resolve has already warmed
  the cache, so this hits cached values rather than re-prompting) and threads it into the
  user-facing install runners: user_install_commands, mise install/prune, nerf claude plugin, claude
  marketplaces / plugins, dotfiles install. `_create_agent_on_vm` Phase 2 does the same for
  `agent_env` against the agent-side runners. Phase 1 of `_create_agent_on_vm` (admin bootstrap:
  useradd, socket setup, authorized_keys) and the infrastructure setup steps in `_phase_b_setup`
  (apt, sshd config, sudoers, hardening, identity profile) deliberately don't take env; they're
  bootstrap actions that shouldn't observe operator scope.
- **No-shell-opening commands verified** to NOT call `resolve_for_command` per FRD R4 / R5:
  `session attach`, `session list`, `session describe`, `console attach` against an existing tmux
  session, `console add-sessions`. Pinned by tests in `test_secrets_eager_resolve.py`.

### Operator surface

- **`agw env show`** (FRD R6, Phase 5). Inspect the merged env at any scope (`--vm` / `--workspace`
  / `--agent` / `--session`) with auto-resolve from any single context. Secrets render as
  `<from secret: NAME>` by default; `--reveal-secrets` resolves through the active backend chain.
- **`agw doctor` Secrets + Env health groups** (FRD R6, Phase 5). For each declared secret, reports
  whether it would resolve silently or fall through to an interactive prompt at command time
  (would-prompt preview via `SecretResolver.preview_resolution`). Surfaces unused secret
  declarations, soft-skip findings, `backend_mappings.<kind>` pointing at undeclared / inactive
  backends, `AGENTWORKS_*` identity overrides, and cross-scope env-key conflicts.
- **`agw secret list`** (Phase 5 / FRD R6 discoverability). Static (secrets x backends) table. Rows
  are declared secrets; columns are the configured chain in precedence order; cells show each
  backend's lookup identifier (env var name, `op://` URI, ...) or `disabled` / `enabled`. Powered by
  a generic `SecretSource.describe_lookup` protocol method so env-var has no special treatment;
  future backends (1Password, Vault) plug in by overriding the method.
- **Sample config** (`cli/agentworks/sample-config.toml`). Adds env tables and secret-config
  sections with explanatory comments.
- **README**. `cli/README.md` documents the env-and-secrets surface, including the eager-prompting
  contract and the `AW_SECRET_*` env-var convention.

## ADRs

- [ADR 0013: CLI-side Secret Injection for VM Shells](../../adrs/0013-cli-side-secret-injection.md).
  Secret values live on the operator workstation and transit SSH per-shell-open; never written to VM
  disk.
- [ADR 0014: AcceptEnv Wildcard on Agentworks-Managed VMs](../../adrs/0014-sshd-accept-env-wildcard.md).
  sshd accepts arbitrary env keys via `AcceptEnv *`; the trust anchor is the operator's SSH
  authentication, not a per-key allowlist.

## Tests

- 16 tests in `cli/tests/test_secrets_orchestration.py` (orchestration module).
- 27 tests in `cli/tests/test_secrets_eager_resolve.py` (manager wiring + no-shell-opening
  verification + add-sessions+N + restore-session window-missing + session attach tripwire).
- 17 tests in `cli/tests/test_initializer_env_fragments.py` (Phase 4 VM-side fragments).
- 15 tests in `cli/tests/test_doctor_env_and_secrets.py` (FRD R6 health groups).
- 8 tests in `cli/tests/test_secrets_inspect.py` (`agw secret list` table builder).
- Comprehensive coverage in `test_secrets_base.py`, `test_secrets_env_var.py`,
  `test_secrets_prompt.py`, `test_secrets_resolver.py`, `test_config_env_and_secrets.py`,
  `test_env_show.py`.
- Total cli suite: 789 tests, all passing at lock.

## Deferred at lock

- **Deprecated `agw vm console` not wired into eager-prompting.** The legacy single-VM-tmux console
  (`cli/agentworks/sessions/console.py`, surfaced via `agw vm console`) opens an admin shell on
  first attach without going through `resolve_for_command`. Operators are directed at the
  named-console surface (`agw console attach`) instead, which IS wired. The deprecated command is
  staged for removal; we did not retrofit it.
- **Legacy secret-adjacent fields not yet migrated.** `tailscale_auth_key` and the
  `git_credentials.*` token resolution still go through their pre-SDD prompt-or-env-var paths. The
  orchestrator's `extra_decls` kwarg is the agreed migration hook (default secret name + default
  backend chain so zero-config operators see no behavior change). Out of scope for this SDD; future
  follow-up.
- **Admin-shell env injection at console build time.** `_build_console_tmux` creates the
  `console.admin_shell` window via `tmux new-session -d ... 'exec $SHELL -l'` with no SetEnv /
  `tmux new-session -e` flags. The eager-resolve produces the right operator-facing UX (prompted up
  front, before any tmux work) and warms the cache, but the resolved values don't yet reach the
  admin shell at the wire. Documented in `_admin_only_secret_target`'s docstring.
- **Pre-SDD VMs need manual `agw vm reinit`.** VMs that were provisioned before this SDD don't have
  the `50-agentworks-accept-env.conf` sshd fragment, so SSH SetEnv silently drops env vars at the
  remote sshd. An earlier iteration of Phase 5 added an `agw doctor` "VM env support" group that
  SSH-probed every VM for the fragment; we removed it because the per-VM SSH round-trip is too
  costly for an interactive doctor sweep and the same shape of probe would need to grow for each
  future drift (a slippery slope). The principled fix is to version the VM definition in the
  database and detect drift cheaply against the recorded version; see ADR 0014 "Consequences".
  Deferred to a future SDD.
- **Per-target diagnostic context on `SecretUnavailableError`.** The resolver's error carries a
  per-secret backend-tried hint; manager-layer call sites could wrap and add `entity_kind` /
  `entity_name` context. Deferred per the Phase 6.1 review's stance: try the bare error in 6.2+ and
  add augmentation only if the operator-facing message reads poorly in practice. No augmentation has
  been needed.

## Cross-references

- [FRD](frd.md): functional requirements (R1 through R7).
- [HLA](hla.md): high-level architecture, including the SetEnv-pivot rationale and the env-transport
  diagram.
- [plan](plan.md): phased implementation plan with all checkboxes ticked.
