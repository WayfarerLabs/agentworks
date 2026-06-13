# Environment variables and secrets: plan

**Status:** Draft **Repo:** `agentworks`

The plan is phased. Each phase ends at a green CI and a usable intermediate state. Order matches the
HLA's phasing section; refer to FRD / HLA for the design and ADR for the trust-anchor rationale.

## Phase 1: secrets package foundations

Goal: stand up the `agentworks.secrets` package with the protocol, v1 sources, resolver, and config
types. No consumers yet. All unit-tested.

- [ ] `cli/agentworks/errors.py`: add `SecretUnavailableError(AgentworksError)`.
- [ ] `cli/agentworks/output.py`: add module-level `is_interactive()` helper. The
      `--non-interactive` flag still seeds via the existing Typer callback in `cli/_app.py`, but now
      writes into `output` rather than keeping a `cli/_app`-private module global. The existing
      `cli/_app.is_interactive` becomes a thin re-export for back-compat.
- [ ] `cli/agentworks/secrets/__init__.py`: package surface (re-exports).
- [ ] `cli/agentworks/secrets/base.py`:
  - `SecretDecl` dataclass (name, description, hint, backend_mappings).
  - `SecretBackendConfig` dataclass (kind plus per-backend fields).
  - `SecretConfig` dataclass (backends list).
  - `SecretSource` Protocol (kind, would_attempt, get, batch_get default).
- [ ] `cli/agentworks/secrets/env_var.py`: `EnvVarSource` (default convention `AW_SECRET_<NAME>`,
      `backend_mappings.env_var` string-or-False override).
- [ ] `cli/agentworks/secrets/prompt.py`: `PromptSource` (uses `output.is_interactive()` +
      `output.prompt_secret`, batch_get groups all prompts).
- [ ] `cli/agentworks/secrets/resolver.py`: `SecretResolver` (chain iteration, batch_get per source,
      cache, `SecretUnavailableError` on no-source-resolved).
- [ ] Tests: `cli/tests/test_secrets_base.py`, `test_secrets_env_var.py`, `test_secrets_prompt.py`,
      `test_secrets_resolver.py`. Coverage for resolution order, opt-out `false`, batch_get, cache,
      unreachable raises, would_attempt across sources.

Definition of done: `from agentworks.secrets import SecretResolver, EnvVarSource, PromptSource`
works; tests pin every behavior in the FRD R4 / HLA Secret model sections.

## Phase 2: env package + EnvEntry + identity vars

Goal: the env model around the secrets foundation. Pure-data, no shell-opening sites changed yet.

- [ ] `cli/agentworks/env/__init__.py`: package surface.
- [ ] `cli/agentworks/env/entry.py`: `EnvEntry` dataclass (key, value-or-secret).
- [ ] `cli/agentworks/env/merge.py`: `effective_env()` with the
      `session > (agent | admin) >     workspace > vm` ladder, including the
      `assert not (admin and agent)` invariant.
- [ ] `cli/agentworks/env/identity.py`: `ResourceContext` dataclass and `agentworks_identity_env()`
      producer.
- [ ] `cli/agentworks/env/exports.py`: `build_export_block()` and `build_prefixed_command()` (the
      latter handles empty-env case to keep call sites simple).
- [ ] `cli/agentworks/config.py`:
  - Add `env: dict[str, EnvEntry]` to `AdminConfig`, `VMTemplate`, `WorkspaceTemplate`,
    `AgentTemplate`. Migrate `SessionTemplate.env` from `dict[str, str] | None` to
    `dict[str, EnvEntry]` (plaintext-compatible loader).
  - Add `[secrets.*]`, `[secret_backends.*]`, `[secret_config]` loaders.
  - Validate: env key regex, plaintext-vs-secret shape, unknown secret refs, AGENTWORKS\_\*
    overrides emit a load-time warning, backend kinds match a registered source, unreachable secrets
    raise.
- [ ] Loader produces `SecretSource` instances per `[secret_config].backends` and assembles a
      `SecretResolver`.
- [ ] Tests: env merge, identity producer, export-block formatting, loader for all the new shapes
      including the unreachable error and the AGENTWORKS\_\* override warning.

Definition of done: an operator can author config that mentions secrets and env without crashing;
`config.load_config()` returns populated structures; no shell-open sites use them yet.

## Phase 3: session + console wiring

Goal: existing shell-open sites in sessions / consoles use the env+secrets prelude. Behavior is
unchanged for operators with no env / secrets configured.

- [ ] `cli/agentworks/sessions/manager.py`: replace inline `export` in `_build_session_command` with
      `build_prefixed_command(...)`. Move from inner-shell placement to outer SSH-command-shell
      placement (per HLA "Prelude placement vs login shells").
- [ ] `cli/agentworks/sessions/tmux.py`: pass the env dict through `create_session` so the prelude
      lands on the right transport for agent-mode and admin-mode sessions.
- [ ] `cli/agentworks/sessions/console.py` / `multi_console.py`: same shape, per pane / per window.
- [ ] Tests: spy on the constructed command to confirm the prelude shape. Cover plaintext-only,
      secret-resolved, and mixed cases. Re-run existing session / console suites for regressions.

Definition of done: `agw session create` with no env config produces the same on-VM state as before.
With env config, the values are present in the shell's env.

## Phase 4: provisioning + agent setup wiring

Goal: provisioning shells (vm create / reinit) and agent setup shells (agent create / reinit
Phase 2) inject env+secrets. VM-stable identity vars land in profile fragments.

- [ ] `cli/agentworks/vms/initializer.py`:
  - New helper `_write_agentworks_identity_profile(target, ctx)` writes
    `/etc/profile.d/agentworks-identity.sh` AND the matching block in `/etc/zsh/zprofile` (mirrors
    the existing `AGENTWORKS_NERF_HOME` install pattern).
  - Extend `_write_agentworks_profile(target, ...)` to include `AGENTWORKS_USER`.
  - Provisioning shells in Phase A / B prepend the env+secrets prelude.
- [ ] `cli/agentworks/agents/manager.py._create_agent_on_vm`:
  - Phase 1 (admin bootstrap): `_write_agentworks_profile` for the new agent's user gets
    `AGENTWORKS_USER`. Prelude assembled with `admin=...`, no agent.
  - Phase 2 (agent self-configure): every `agent_target.run(...)` call gets the agent-side prelude
    prepended. `_run_agent_install_commands` and `_run_agent_mise_setup` accept and use a per-call
    env dict.
- [ ] Tests: prelude assembly per phase; idempotent rewrites of profile fragments; AGENTWORKS\_\*
      vars present on a fresh-VM shell.

Definition of done: a fresh `vm create` + `agent create` produces a VM where any shell on it (via
agentworks or via raw `ssh awvm--vm`) sees the expected `AGENTWORKS_*` identity vars, and
agentworks-opened shells see the configured user env merged correctly.

## Phase 5: CLI surface

Goal: operator-visible commands and docs.

- [ ] `cli/agentworks/cli/commands/env.py`: new `agw env show` command with
      `--vm / --workspace / --agent / --session` flags (at least one required; auto-resolve the
      chain from the named entity's DB row) and `--reveal-secrets`. Output precedence-sorted,
      scope-annotated, secret entries redacted by default.
- [ ] `cli/agentworks/doctor.py`: new Secrets and Env health groups per FRD R6.
- [ ] `cli/agentworks/sample-config.toml`: add `[secret_backends.*]`, `[secret_config]`,
      `[secrets.*]` sections and the per-scope `env` subtable examples. Keep entries commented so a
      fresh `agw config init` does not enable backends an operator hasn't opted into.
- [ ] `cli/README.md` + top-level `README.md`: short section on env + secrets, pointer to
      `agw env show`, the `AW_SECRET_<NAME>` env-var convention, the per-secret `backend_mappings`,
      and the `[secret_config].backends` precedence list.
- [ ] Tests: command-shape tests for `env show` (context required, auto-resolution, redaction);
      doctor tests for each new finding type.

Definition of done: `agw env show --session s1` works end-to-end; `agw doctor` surfaces broken
secret refs, unused declarations, and the would-prompt preview.

## Phase 6: eager prompting orchestration

Goal: every secret-consuming command resolves all needed secrets up front (within first few
seconds), before any state mutation. Non-interactive failure is a clear actionable error.

- [ ] `cli/agentworks/secrets/orchestration.py` (or equivalent):
      `compute_needed_secrets(targets,     config)` walks the env chain across all candidates.
      `resolve_for_command(...)` calls `SecretResolver.resolve_all`. Static-vs-dynamic filter
      handling lives here.
- [ ] Wire into the manager layer for: `vm create`, `vm reinit`, `agent create`, `agent reinit`,
      `session create`, `session restart`, `console create`, `console add-shell`, `vm exec`,
      `agent exec`, `vm shell`, `agent shell`. Each manager entry computes its candidate set from
      static filters only and calls `resolve_for_command` before any destructive work.
- [ ] Verify `session attach` / `session list` / `session describe` / `console attach` /
      `console add-sessions` do NOT consume secrets (per FRD R4 / R5).
- [ ] Tests: spy on the resolver to confirm one resolve-all call per command, before any mutation;
      non-interactive mode raises `SecretUnavailableError` with the right hint.

Definition of done: an operator with no `AW_SECRET_<NAME>` in env who runs
`agw session create ... -t claude` gets one prompt up front for all needed secrets across the full
chain, then the command proceeds with no further interruptions.

## Sequencing notes

- Phases 1 and 2 are pure additive (new packages, new config types). No behavior change for
  operators with no env / secrets configured.
- Phase 3 swaps inline-export call sites. Behavior preserved for empty env; new behavior for
  populated env.
- Phase 4 changes VM-side state on init / reinit (profile fragments). Idempotent.
- Phase 5 is operator-visible; ships at the end of implementation so the docs match the surface.
- Phase 6 is the orchestration capstone: every command's secret needs computed up front, single
  resolve pass.

A reasonable PR shape: one PR per phase, OR one PR for the whole effort following the
direct-target-user-SSH SDD's model. Since this lands on the existing draft PR #115, intent is the
latter; each phase a separate commit (or commit cluster) for review legibility.
