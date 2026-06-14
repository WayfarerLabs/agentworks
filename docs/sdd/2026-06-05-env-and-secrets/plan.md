# Environment variables and secrets: plan

**Status:** Draft **Repo:** `agentworks`

The plan is phased. Each phase ends at a green CI and a usable intermediate state. Order matches the
HLA's phasing section; refer to FRD / HLA for the design and ADR for the trust-anchor rationale.

## Phase 1: secrets package foundations

Goal: stand up the `agentworks.secrets` package with the protocol, v1 sources, resolver, and config
types. No consumers yet. All unit-tested.

- [x] `cli/agentworks/errors.py`: add `SecretUnavailableError(AgentworksError)`.
- [x] `cli/agentworks/output.py`: add module-level `is_interactive()` helper. The
      `--non-interactive` flag still seeds via the existing Typer callback in `cli/_app.py`, but now
      writes into `output` rather than keeping a `cli/_app`-private module global.
      (`cli/_app.is_interactive` was dropped after grep confirmed no external callers; the lone
      internal user, `require_interactive`, now calls `output.is_interactive()` directly.)
- [x] `cli/agentworks/secrets/__init__.py`: package surface (re-exports).
- [x] `cli/agentworks/secrets/base.py`:
  - `SecretDecl` dataclass (name, description, hint, backend_mappings).
  - `SecretBackendConfig` dataclass (kind plus per-backend fields).
  - `SecretConfig` dataclass (backends list, implemented as `tuple[str, ...]` for hashable
    frozen-dataclass storage).
  - `SecretSource` Protocol (kind, would_attempt, get, batch_get) - pure structural type contract.
  - `SecretSourceBase` ABC with the default `batch_get` (loops `get`) for sharing between concrete
    sources. Concrete sources inherit from the ABC; the Protocol remains type-only.
- [x] `cli/agentworks/secrets/env_var.py`: `EnvVarSource` (default convention `AW_SECRET_<NAME>`,
      `backend_mappings.env_var` string-or-False override).
- [x] `cli/agentworks/secrets/prompt.py`: `PromptSource` (uses `output.is_interactive()` +
      `output.prompt_secret`, batch_get groups all prompts).
- [x] `cli/agentworks/secrets/resolver.py`: `SecretResolver` (chain iteration, batch_get per source,
      cache, `SecretUnavailableError` on no-source-resolved, per-secret hint listing only sources
      that actually `would_attempt`; `render` raises `ConfigError` on unknown-secret refs or
      malformed entries).
- [x] Tests: `cli/tests/test_secrets_base.py`, `test_secrets_env_var.py`, `test_secrets_prompt.py`,
      `test_secrets_resolver.py`. Coverage for resolution order, opt-out `false`, batch_get, cache,
      unreachable raises, would_attempt across sources, per-secret hint narrowing, mixed render
      shapes, render rejecting unknown-secret refs and malformed entries.

Definition of done: `from agentworks.secrets import SecretResolver, EnvVarSource, PromptSource`
works; tests pin every behavior in the FRD R4 / HLA Secret model sections.

## Phase 2: env package + EnvEntry + identity vars

Goal: the env model around the secrets foundation. Pure-data, no shell-opening sites changed yet.

- [x] `cli/agentworks/env/__init__.py`: package surface.
- [x] `cli/agentworks/env/entry.py`: `EnvEntry` dataclass (key, value-or-secret).
- [x] `cli/agentworks/env/merge.py`: `effective_env()` with the
      `session > (agent | admin) >     workspace > vm` ladder, including the
      `assert not (admin and agent)` invariant.
- [x] `cli/agentworks/env/identity.py`: `ResourceContext` dataclass and `agentworks_identity_env()`
      producer.
- [x] `cli/agentworks/env/exports.py`: `build_export_block()` and `build_prefixed_command()` (the
      latter handles empty-env case to keep call sites simple).
- [x] `cli/agentworks/config.py`:
  - Add `env: dict[str, EnvEntry]` to `AdminConfig`, `VMTemplate`, `WorkspaceTemplate`,
    `AgentTemplate`. Migrate `SessionTemplate.env` from `dict[str, str] | None` to
    `dict[str, EnvEntry]` (plaintext-compatible loader). Resolved\* templates also carry `env`,
    merged child-overrides-parent during inheritance resolution.
  - Add `[secrets.*]`, `[secret_backends.*]`, `[secret_config]` loaders.
  - Validate: env key regex, plaintext-vs-secret shape, unknown secret refs, AGENTWORKS\_\*
    overrides emit a load-time warning, backend kinds match a registered source, unreachable secrets
    raise. (`true` rejected in `backend_mappings`; only `false` opt-out is valid.)
- [x] Loader produces `SecretSource` instances per `[secret_config].backends` and assembles a
      `SecretResolver`. Returns `None` when neither secrets nor backends are configured (operators
      who don't opt in pay nothing).
- [x] Tests: env merge, identity producer, export-block formatting, loader for all the new shapes
      including the unreachable error and the AGENTWORKS\_\* override warning.
- [x] `cli/agentworks/sessions/manager.py._build_session_command`: minimal adapter to iterate
      `EnvEntry` (plaintext only; secret-ref entries raise `ConfigError` pointing at Phase 3
      wiring).

Definition of done: an operator can author config that mentions secrets and env without crashing;
`config.load_config()` returns populated structures; no shell-open sites use them yet.

## Phase 3: SSH SetEnv pivot + session / console wiring

Goal: env injection happens through the SSH layer's `SetEnv` mechanism, not through a CLI-composed
shell prelude. All shell-opening sites (sessions, consoles, exec, interactive shells) get the same
env via the same SSH-layer plumbing. Behavior is unchanged for operators with no env / secrets
configured.

### Pivot context

The first pass of Phase 3 (commits `91f32db` and `bb64136`) wired env into session create / restart
via a CLI-composed shell prelude (`build_export_block` + `build_prefixed_command`). Working through
the implementation surfaced enough friction (nested quoting through SSH → tmux → pane shell,
`sudo --login` env wipes, login-vs-non-login shell semantics, deferred console paths) that we
re-evaluated the transport mechanism itself. The SSH protocol already has `SetEnv` (client) /
`AcceptEnv` (server) for exactly this purpose. Adopting it removes the entire prelude composition
layer from the CLI and gives a uniform shape across every shell-opening site. See
`new-adrs/sshd-accept-env-wildcard.md` for the security-posture decision behind the wildcard
AcceptEnv.

This phase rewrites the earlier Phase 3 commits accordingly. The `compose_env` /
`per_context_identity_env` / SecretResolver-render machinery from those commits stays; what changes
is the consumption: instead of `build_export_block(env)` followed by glue, we pass the flat
`dict[str, str]` to SSH as `-o SetEnv=K=V` args.

### Phase 3 deliverables

- [x] Delete `cli/agentworks/env/exports.py` (`build_export_block`, `build_prefixed_command`).
      Remove from `agentworks.env` package surface and from tests. Env transport is now an SSH
      property, not a shell-command property.
- [x] `cli/agentworks/ssh.py`: thread an `env: dict[str, str] | None` kwarg through `run`,
      `interactive`, and `ExecTarget.run`. Each `K=V` pair becomes `-o SetEnv=K=V` on the SSH
      command line. Lima / RemoteLima / WSL2 transports embed env as scoped bash assignments at the
      head of the payload. The `RunCommand` Protocol in `agentworks.sessions.tmux` widens to match.
- [x] `cli/agentworks/sessions/tmux.py`: drop `_build_pane_command` and the prelude composition.
      `create_session` takes the env dict, the pane command becomes the simple inner shape
      (`$SHELL -lic 'cd <path> && exec <command>'` or just empty), env reaches the pane via
      `tmux new-session -e KEY=VAL` flags on the SSH-invoked tmux command. Tmux's session
      environment carries the vars to every pane in the session.
- [x] `cli/agentworks/sessions/manager.py`: `_resolve_session_env` stays (still composes the env
      dict). Call sites pass it via `create_tmux_session(..., env=...)` -> SSH layer.
- [x] **Admin sessions move to per-session sockets**, mirroring the agent-mode pattern. Each
      admin-mode `tmux new-session` starts a fresh server that inherits the SetEnv-delivered env
      from the SSH connection. Socket path lives under
      `/run/agentworks/admin-tmux-sockets/<admin-user>/<session>.sock`. `ensure_admin_socket_root`
      and `cleanup_stale_admin_sockets` helpers mirror the agent-mode plumbing.
- [x] **Drop the redundant `sudo su --login agentworks`** in the console admin-shell windows (legacy
      `sessions/console.py.create_console` and `multi_console._build_console_tmux`). Admin-shell
      window now `exec $SHELL -l` directly.
- [x] **Console add-shell panes** (`multi_console._split_shell_pane`): per-pane env injected via
      `tmux split-window -e KEY=VAL` plus belt-and-suspenders SetEnv on `target.run`. New
      `_resolve_pane_env` helper composes admin / agent scopes. For the agent-pane branch
      (`sudo --login -u <agent>`), the sudo IS legitimate (the tmux server runs as admin; the pane
      needs to run as the agent); sudoers env_keep deployed in Phase 4 keeps agentworks-managed vars
      surviving the sudo boundary. Until Phase 4 lands, env injection on the agent-pane branch is
      best-effort (vars cross into tmux's session env but get stripped at the sudo crossing).
- [x] `cli/agentworks/env/identity.py` retains the three identity-subset helpers
      (`vm_stable_identity_env`, `per_user_identity_env`, `per_context_identity_env`). With SetEnv
      as the transport, `compose_env` overlays per-context identity vars on top of the resolved user
      env (identity wins). VM-stable and per-user vars are NOT in the SetEnv payload; they come from
      the VM-side profile fragments in Phase 4.
- [x] Tests:
  - SSH-layer SetEnv arg construction (one `-o SetEnv=K=V` per dict entry, order preserved, values
    with special chars accepted).
  - `create_tmux_session` builds the `tmux new-session -e KEY=VAL` flag list correctly for admin and
    agent modes.
  - `_split_shell_pane` builds `tmux split-window -e KEY=VAL` for admin and agent panes.
  - Per-session socket plumbing for admin mode mirrors agent mode (socket-root setup + server-access
    grant).
  - Console admin-shell window no longer wraps in sudo.

Definition of done: a fresh `agw session create` produces a tmux server whose pane process
environment carries the resolved user env + per-context identity vars (verifiable from inside the
pane via `env | grep AGENTWORKS_`). Behavior is identical to pre-Phase-3 when no env / secrets are
configured (empty SetEnv list, default pane command shape). `agw vm shell` / `agw agent shell` work
the same way, via SSH SetEnv with no tmux involvement.

**Behavior changes to surface in the lockfile**:

- `_resolve_session_env` consults `VMRow.template` and `AgentRow.template` to resolve the actual VM
  / agent template, where the pre-Phase-3 `_build_session_command` only consulted
  `session.template`. Operators who populated `[vm_templates.<non-default>.env]` or
  `[agent_templates.<non-default>.env]` will see those vars in their next session create / restart;
  before this phase those tables were silently dead config.
- Admin sessions use per-session sockets after this phase. Raw `tmux ls` on a VM no longer shows all
  admin sessions on the user's default tmux server; operators should use `agw session list` /
  `agw session attach <name>`. Existing live admin tmux sessions from before the migration become
  orphaned (no agw-managed sockets backing them); on Phase 4 reinit they're cleared.

**Batch operations do not yet honor "prompt once up front"**: `restart_session` calls into
`_resolve_session_env` per session, so an `agw restart-all` (or similar) batch path will resolve
secrets incrementally rather than as a single eager pass. The resolver caches across calls within a
single CLI invocation, so the operator is still prompted at most once per secret, but the prompts
are interleaved with mutation work rather than happening up front. Phase 6 (eager prompting
orchestration) is the capstone that fixes this.

## Phase 4: VM-side identity + AcceptEnv / sudoers config

Goal: VM-stable identity vars and per-user identity vars land in profile fragments, sshd is
configured to accept SetEnv'd vars from agentworks, and sudoers is configured to keep
agentworks-managed vars across user switches. Provisioning shells and agent setup shells inject
env+secrets via the SSH SetEnv path established in Phase 3.

- [x] `cli/agentworks/vms/initializer.py`:
  - New helper `_write_agentworks_identity_profile(target, identity_env, logger)` writes
    `/etc/profile.d/agentworks-identity.sh` AND the matching block in `/etc/zsh/zprofile` (mirrors
    the existing `AGENTWORKS_NERF_HOME` install pattern). Contents are the VM-stable subset:
    `AGENTWORKS_VM`, `AGENTWORKS_VM_HOST` (when applicable), `AGENTWORKS_PLATFORM`. Reinit-safe
    (sed-strips the prior agentworks-identity block from `/etc/zsh/zprofile` before re-appending).
  - Extend `_write_agentworks_profile(target, ...)` (per-user fragment, existing) with an optional
    `identity_env` kwarg so `AGENTWORKS_USER` lands in the per-user profile
    `~/.agentworks-profile.sh`.
  - **New helper `_write_sshd_accept_env(target, logger)`** writes
    `/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf` with `AcceptEnv *`, validates with
    `sshd -t`, and reloads sshd (systemctl). Per the `sshd-accept-env-wildcard` ADR.
  - **New helper `_write_sudoers_env_keep(target, logger)`** writes
    `/etc/sudoers.d/50-agentworks-env-keep` with `Defaults env_keep += "AGENTWORKS_* AW_*"`,
    validated with `visudo -cf` against a staging file before promotion. Lets agentworks-managed
    vars survive the sudo boundary in console add-shell agent panes.
  - All four helpers wired into `_phase_b_setup` after `apply_vm_hardening`.
- [x] `cli/agentworks/agents/manager.py._create_agent_on_vm`:
  - Per-user identity (`AGENTWORKS_USER`) now lands in the agent's `~/.agentworks-profile.sh` via
    the rewritten `_run_agent_install_commands`. The profile fragment is written unconditionally so
    AGENTWORKS_USER lands even when an agent has no user install commands.
- [ ] **Follow-up in this phase**: thread env through provisioning / agent-setup SSH calls via
      `target.run(env=...)`. Per the SDD this should land in Phase 4. Splitting it off as a
      follow-up commit on top of this work so the four helpers + profile-fragment work can be
      reviewed independently from the per-call-site env threading. The follow-up will:
  - Compute admin env once at the head of `_phase_b_setup` via `compose_env` with the admin / vm
    scopes and thread to user-install-command runners, mise installers, claude plugin installer, and
    dotfiles installer.
  - Phase 1 of `_create_agent_on_vm` (admin bootstrap): SSH commands during admin bootstrap pass env
    via `target.run(env=...)` using the admin scope.
  - Phase 2 of `_create_agent_on_vm` (agent self-configure): every `agent_target.run(...)` call
    passes env using the agent scope.
- [x] Tests: tests/test_initializer_env_fragments.py (17 tests). Pin: identity-profile system-wide +
      zprofile mirror with reinit-safe sed-strip; sshd AcceptEnv validates before reload; sudoers
      env_keep validates with visudo and rolls back on failure; per-user profile carries
      AGENTWORKS_USER when identity_env is passed; backward-compat when identity_env is omitted.

Definition of done: a fresh `vm create` + `agent create` produces a VM where any shell on it (via
agentworks or via raw `ssh awvm--vm`) sees the expected `AGENTWORKS_*` identity vars, sshd accepts
SetEnv'd env vars from agentworks-issued commands, and sudo preserves `AGENTWORKS_*` / `AW_*` across
user switches for the console-pane paths. Agentworks-opened shells see the configured user env
merged correctly.

## Phase 5: CLI surface

Goal: operator-visible commands and docs.

- [x] `cli/agentworks/cli/commands/env.py`: new `agw env show` command with
      `--vm / --workspace / --agent / --session` flags (at least one required; auto-resolve the
      chain from the named entity's DB row) and `--reveal-secrets`. Output precedence-sorted,
      scope-annotated, secret entries redacted by default.
- [x] `cli/agentworks/doctor.py`: new Secrets and Env health groups per FRD R6.
- [x] `cli/agentworks/sample-config.toml`: add `[secret_backends.*]`, `[secret_config]`,
      `[secrets.*]` sections and the per-scope `env` subtable examples. Keep entries commented so a
      fresh `agw config init` does not enable backends an operator hasn't opted into.
- [x] `cli/README.md` + top-level `README.md`: short section on env + secrets, pointer to
      `agw env show`, the `AW_SECRET_<NAME>` env-var convention, the per-secret `backend_mappings`,
      and the `[secret_config].backends` precedence list.
- [x] Tests: command-shape tests for `env show` (context required, auto-resolution, redaction);
      doctor tests for each new finding type.

Definition of done: `agw env show --session s1` works end-to-end; `agw doctor` surfaces broken
secret refs, unused declarations, and the would-prompt preview.

## Phase 6: eager prompting orchestration

Goal: every secret-consuming command resolves all needed secrets up front (within first few
seconds), before any state mutation. Non-interactive failure is a clear actionable error.

- [x] `cli/agentworks/secrets/orchestration.py`:
      `compute_needed_secrets(targets, config, *, extra_decls=())` walks each target's env chain via
      `effective_env` and unions referenced `SecretDecl`s with first-encounter ordering.
      `resolve_for_command(...)` issues one batched `SecretResolver.resolve_all` and returns the
      resolved `{name: value}` map. `SecretTarget` mirrors the per-scope env shape; `extra_decls` is
      the hook for future Tailscale / git-cred migrations. The orchestrator stays generic (no DB /
      template coupling); static-vs-dynamic filter discipline lives at each manager call site (next
      bullet).
- [ ] Wire into the manager layer for: `vm create`, `vm reinit`, `agent create`, `agent reinit`,
      `session create`, `session restart`, `console create`, `console add-shell`, `vm exec`,
      `agent exec`, `vm shell`, `agent shell`. Each manager entry computes its candidate set from
      static filters only and calls `resolve_for_command` before any destructive work.
- [ ] Verify `session attach` / `session list` / `session describe` / `console attach` /
      `console add-sessions` do NOT consume secrets (per FRD R4 / R5).
- [x] Tests for the orchestration module (16 tests in `cli/tests/test_secrets_orchestration.py`):
      target unioning + dedup, cross-target ordering, substitution invariance, extra_decls hook,
      cache-wins-over-late-env-changes (cache contract), admin+agent mutex, label-not-in-equality,
      non-hashability.
- [ ] Tests for the manager wiring: spy on the resolver to confirm one resolve-all call per command,
      before any mutation; non-interactive mode raises `SecretUnavailableError` with the right hint.

Definition of done: an operator with no `AW_SECRET_<NAME>` in env who runs
`agw session create ... -t claude` gets one prompt up front for all needed secrets across the full
chain, then the command proceeds with no further interruptions.

## Sequencing notes

- Phases 1 and 2 are pure additive (new packages, new config types). No behavior change for
  operators with no env / secrets configured.
- Phase 3 pivots the env-transport mechanism to SSH SetEnv (vs the original shell-prelude design).
  Behavior preserved for empty env; new behavior for populated env. Admin sessions move to
  per-session sockets; raw `tmux ls` on a VM no longer shows them grouped. Console paths drop the
  redundant `sudo su --login admin`.
- Phase 4 changes VM-side state on init / reinit: profile fragments (`/etc/profile.d/`,
  `~/.agentworks-profile.sh`), sshd config (`/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf`),
  and sudoers config (`/etc/sudoers.d/50-agentworks-env-keep`). Idempotent. Until a VM is reinit'd
  to pick up the new sshd config, SetEnv'd vars from Phase 3 commands will be silently dropped at
  sshd; doctor (Phase 5) reports this state.
- Phase 5 is operator-visible; ships at the end of implementation so the docs match the surface.
- Phase 6 is the orchestration capstone: every command's secret needs computed up front, single
  resolve pass.

A reasonable PR shape: one PR per phase, OR one PR for the whole effort following the
direct-target-user-SSH SDD's model. Since this lands on the existing draft PR #115, intent is the
latter; each phase a separate commit (or commit cluster) for review legibility.
