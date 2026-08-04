# HLA: Harness Adapter (multi-scope tool integration and rename)

- Status: Draft
- Start date: 2026-08-03
- Builds on: `frd.md` (R1-R10, D1-D4), `codex-response.md`
- Companion artifacts to be authored per the plan: `migration-strategy.md`, and the LLDs listed in
  the plan.

## 1. Scope and shape of the change

Two things happen together, and the plan sequences them so they do not happen in the same edit:

1. **Rename** the existing session-scoped capability from `harness` to `harness-adapter`, everywhere
   (kind slug, selector field, persisted DB column, code identifiers, files and directories, CLI
   text, docs), with a migration path for the operator-facing and persisted surfaces.
2. **Expand** the capability from one session-scope pair of ops (`start`/`restart`) into a
   multi-scope adapter that also contributes user-scope and workspace-scope provisioning, under a
   per-hook scope and privilege contract.

The rename is a contained, wide, mostly mechanical migration of what exists. The expansion is
net-new architecture on top of the renamed base. Doing the rename first (its own vertical slice and
PR) keeps the team from renaming and redesigning the same lines at once.

## 2. Target model

Three layers, named honestly (FRD three-layer model, D1 settled on `harness-adapter`):

1. **model** (Claude): weights.
2. **harness** (Claude Code, Codex): the agent runtime around the model. Agentworks does not
   implement this.
3. **harness-adapter** (Agentworks): the integration that provisions and drives a harness inside
   Agentworks. This is the capability. Today it drives the harness only at the session; the
   expansion lets it also provision at the user and workspace scopes.

A harness-adapter is a capability resource with its own identity that declares the harness it drives
(FRD R8). For the current 1:1 reality, an adapter keeps the tool's name as its default identity
(`harness-adapter/claude-code`); the model permits, but does not build, multiple adapters per
harness (FRD D3).

## 3. The capability interface (target)

Today `Harness(Capability)` (`capabilities/harness/base.py:120`, `owner_kind="session-template"`)
provides session-scope ops plus shared readiness. The target `HarnessAdapter` capability keeps that
and adds optional provisioning hooks at two more scopes. Every new hook is **optional** with a no-op
default (FRD R3), so `shell` overrides none of them and existing adapters keep working untouched.

Existing (unchanged in behavior, renamed in identifier):

- `start(ctx) -> str`, `restart(ctx) -> str` (session-scope ops; `base.py:246-257`).
- `_probe_target(transport)` (session readiness; `base.py:259-264`).
- `dependencies`, `validate`, `merge_config`, `hoist_legacy_state`, `launch_note` (construct-time
  and helpers).

New (all optional, default no-op):

- **User scope:** `provision_user(ctx) -> None`. Installs, authenticates, and applies user-level
  configuration for the tool, for one agent (target) user. Idempotent (re-runnable by `reinit`).
- **Workspace scope:** `provision_workspace(ctx) -> None`. Publishes the tool's workspace-level
  material (rules, skills, hooks, workspace config) under the workspace path. Idempotent.
- **Per-scope readiness:** the shared `_run_readiness`/`_probe_target` mechanism generalizes so each
  scope can declare the executables/state it needs before its hook runs. The current session-only
  `_check_identity` guard (`base.py:332-370`) becomes scope-aware rather than SESSION-hardcoded.

The exact method signatures, the `RunContext`/scope objects each hook receives, and how a hook
declares its per-scope dependencies and readiness are LLD work (harness-adapter-api-lld).

## 4. Per-hook scope and privilege contract (FRD R2)

The blanket "a harness stays session-scoped" rule (`capabilities/harness/README.md:40-56`) is
replaced by a per-hook version. Each hook may act only within its stage's scope, and runs at that
stage's privilege:

| Hook                  | Runs during                                                                          | As user                                               | May touch                                                                       |
| --------------------- | ------------------------------------------------------------------------------------ | ----------------------------------------------------- | ------------------------------------------------------------------------------- |
| `provision_user`      | agent create/reinit self-configure phase (`agents/initializer.py:181+`)              | the agent (target) user, no sudo                      | that user's home and user-level tool state only                                 |
| `provision_workspace` | workspace create (`workspaces/backends/vm.py:31-170`)                                | the admin user (workspace is admin-provisioned today) | the workspace directory (group-owned, setgid `2770`), with group-readable perms |
| `start` / `restart`   | session create/restart (`sessions/manager/_create_roll.py:249`, `_lifecycle.py:544`) | the session's target user                             | session-local state only (unchanged v1 contract)                                |

Rationale for the workspace hook running as admin: workspace creation is 100% admin/root today
(`workspaces/backends/vm.py` runs entirely over the admin transport), and workspace material must
land group-readable under the setgid directory so granted agents can read it. Introducing an
agent-user transport mid-workspace-create is a larger change and is out of scope here; the workspace
hook therefore writes as admin with group perms. A hook MUST NOT reach a wider scope than its stage
(no machine-wide effects, no cross-user writes).

## 5. Where each hook is invoked (the seams)

The adapter is referenced from whichever template drives each scope, and invoked at that scope's
existing provisioning body:

- **Session (exists):** `session-template` selects the adapter; `_harness_for_template`
  (`sessions/nodes.py:305-372`) constructs it; `session_node.harness.start/restart` runs it, gated
  by `ensure_harness_enabled` (`sessions/manager/_create_build.py:178`). No new seam.
- **User (generalize existing debt):** `agent-template` (and the admin analog, `admin-config`)
  declares which adapters to provision. In `agents/initializer.py` (agent path) and
  `vms/initializer/driver.py:_phase_b_setup` (admin path), after the existing install-commands, the
  flow calls `provision_user` for each declared, enabled adapter. This **subsumes** the current
  ad-hoc, Claude-specific `claude_marketplaces`/`claude_plugins` fields (`agents/template.py:44-45`,
  `vms/initializer/driver.py:846-895`) that `plugins/claude/__init__.py:1-33` flags as
  not-yet-migrated debt: those become the `claude-code` adapter's `provision_user`.
- **Workspace (net-new):** `workspace-template` gains a field naming which adapters to provision.
  `realize_workspace` (`workspaces/realize.py`) calls `provision_workspace` for each declared,
  enabled adapter after `create_vm_workspace` returns and the directory and ACLs exist. There is no
  existing seam here at all today; both the `WorkspaceTemplate` field and the invocation site are
  net-new.

This gives one coherent story: the same `harness-adapter/claude-code` resource is referenced from an
agent-template (user hook), a workspace-template (workspace hook), and a session-template (session
op), and the framework invokes each at the right stage with the right scope contract.

## 6. Resource-model and selector decisions

- **Kind slug (D1, settled):** `harness-adapter`. Registered in `capabilities/harness/kinds.py` and
  `plugins/adapters.py`, and added to the hardcoded capability-kind set in `resources/graph.py:369`.
- **Session selector field (D2):** the tagged-table selector on `session-template` becomes
  `harness_adapter` (snake case, matching the internal field pair and the other capability selectors
  `platform`/`provider`), selecting an adapter by name. Recommendation:

  ```yaml
  spec:
    harness_adapter: { name: claude-code, permission_mode: default }
  ```

  The old `harness:` key is retained as a **deprecated alias** that loads with a warning and is
  rewritten by `agw resource migrate`, modeled on the existing legacy-flat-field hoist
  (`manifests/decode.py:76`, `config/loaders_sessions.py`, `migrate/planning.py`). This satisfies
  FRD R6 (selector names the adapter) while giving operators a ramp, and keeps the canonical emitted
  form on the new name. The ergonomic cost of the longer key is real; it is accepted for consistency
  with the kind and the internal vocabulary.

- **User/workspace selector fields (net-new):** `agent-template` and `workspace-template` each gain
  a field listing the adapters to provision at that scope (working name
  `harness_adapters: [claude-code]`). Exact names and whether they are lists of adapter names or
  richer references are LLD work.
- **Config field pair:** `harness`/`harness_config` become
  `harness_adapter`/`harness_adapter_config` on `SessionTemplate` and `SessionTemplateSpec`
  (`sessions/templates.py:36-37`, `sessions/template.py:63-64`), with `CAPABILITY_FIELDS`
  (`manifests/decode.py:76`) updated.

## 7. Migration architecture

Detailed mechanics and sequencing go in `migration-strategy.md`; the architecture is:

- **DB column (highest risk):** a new numbered migration after v29 does
  `ALTER TABLE sessions RENAME COLUMN harness_state TO harness_adapter_state` (SQLite supports
  `RENAME COLUMN`). No data transform: the JSON blob's inner keys are namespaced by adapter name
  (`shell`/`claude-code`/`codex`), not the word "harness", so they are untouched. The shipped v29
  migration is never edited. `db/models.py:158`, `db/database.py` SQL, and `db/converters.py` update
  in lockstep with the migration.
- **Kind slug alias:** `--kind harness` and `harness/<name>` addressing are things operators have
  typed and scripted. The registry gains a deprecated kind alias `harness -> harness-adapter` for
  one release (resolve with a warning), then removal. Kind aliasing is net-new registry behavior and
  is its own LLD decision (versus a hard cutover with a `doctor` hint); recommend the alias for a
  softer ramp.
- **Selector field shim:** `harness:` on session-templates loads as a deprecated alias of
  `harness_adapter:`, reusing the legacy-field-hoist machinery. `agw resource migrate` rewrites
  operator manifests; the built-in shipped manifests (claude/codex session-templates, plus the
  example agent-templates) are updated in-repo directly.
- **Identifier sweep (soft):** rename the package `capabilities/harness/` ->
  `capabilities/harness_adapter/`, classes (`Harness` -> `HarnessAdapter`, `ShellHarness` ->
  `ShellHarnessAdapter`, `ClaudeCodeHarness` -> `ClaudeCodeAdapter`, `CodexHarness` ->
  `CodexAdapter`, `HarnessEntry`, `_HarnessKind`, `_HarnessAdapter` the plugin adapter),
  `HARNESS_REGISTRY` and its accessors, the plugin `harness.py` modules, and the threaded
  variables/params. Test files and function names follow.
- **CLI-visible text:** the `"HARNESS"` list column and `"Harness:"` describe label
  (`sessions/manager/_queries.py:352,481`), plus the operator-facing error/hint strings
  (`capabilities/harness/__init__.py:74,105-109`), update to the new name.
- **Docs:** the capability README (renamed dir), `capabilities/README.md`, `cli/README.md`,
  `docs/guides/resources.md`, ADR 0020, and the sample manifests update. Historical `CHANGELOG.md`
  entries are left as-is (immutable record); new entries use the new name. The README's "Agentworks
  Is Not a Harness" prose uses "harness" in the industry sense and stays; only its mechanism-sense
  usages and the YAML example change (the operator is authoring the target-state README separately).

## 8. Enablement and readiness across scopes

The present-but-disabled model (`plugins/enablement.py`, `ensure_harness_enabled`) carries over
unchanged in mechanism, renamed to `ensure_harness_adapter_enabled`. It must now gate at the new
invocation sites too: provisioning a user or workspace against a disabled adapter is refused with
the same "enable plugin `<name>`" hint used at session create, rather than silently skipped. The
gate stays at the call sites (which hold the `Registry`), never inside the node/template factories,
per the existing drift-guard discipline (`cli/tests/sessions/test_harness_gate_drift.py`).

## 9. Account strategy stays orthogonal (FRD R4)

The per-agent Linux user decision lives in `create_agent_on_vm` (`agents/initializer.py:108-122`,
`useradd -U`) and is deliberately not touched. A harness-adapter's `provision_user` runs against
whatever user the account strategy produced; it MUST NOT create users, decide isolation, or own the
account shape. If a pluggable account strategy is ever wanted, it is a separate capability, not this
one.

## 10. Open decisions for the plan and LLDs

- **D5 (kind alias vs hard cutover).** Recommend a deprecated `harness` kind alias for one release.
  Alternative: hard cutover plus a `doctor`/error hint. Decide in `migration-strategy.md`.
- **D6 (user/workspace declaration shape).** How `agent-template` and `workspace-template` name the
  adapters to provision (bare name list vs richer reference with per-scope config). LLD.
- **D7 (workspace-hook privilege).** Confirm admin-run-with-group-perms is acceptable for all
  foreseeable workspace material, or whether any tool needs an agent-user write at workspace scope
  (which today's flow cannot provide). LLD/HLA revisit if a real case appears.
- **D8 (auth at user scope).** The FRD allows user-level auth; the per-hook contract must state
  exactly what "authenticate at user scope" may persist (an injected env var vs a written login) and
  that it stays within the user, consistent with the session-scope auth discussion already in the
  harness README.

## 11. Risks

- **Persisted-column rename** is the only at-rest data risk; a botched migration corrupts live
  session state. Mitigation: `RENAME COLUMN` is lossless, gated by a migration test analogous to
  `test_db_migration_harness_state.py`.
- **Operator-script breakage** on `--kind harness` / `harness/<name>` / `harness:` selector.
  Mitigation: the kind alias and selector shim, plus `agw resource migrate`.
- **Sweep miss** leaving a stale identifier or CLI string. Mitigation: the plan's final phase greps
  for residual `harness` (case-insensitive) and justifies every remaining hit (industry-sense prose,
  historical changelog, the ephemeral SDD dirs).
- **Scope-creep into account strategy.** Mitigation: R4 is a hard boundary in the contract and the
  reviews.
