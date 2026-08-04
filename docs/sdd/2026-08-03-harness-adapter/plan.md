# Plan: Harness Adapter (multi-scope tool integration and rename)

- Status: Draft
- Builds on: `frd.md`, `hla.md`, `codex-response.md`

Definitions of done are objective and testable. Completed checkboxes are an immutable record: once
checked, do not edit, move, or uncheck them (SDD rule); if the plan changes, add new checkboxes.
LLDs and per-step implementation are delegated to `agentworks-dev`; the lead owns this plan and the
upstream docs. Every step is reviewed by `agentworks-reviewer` before merge.

Suggested PR split (dev-process "split only when phases carry independent standalone value"):

- PR A = Phase 1 (the rename): standalone value, ships alone.
- PR B = Phases 2-3 (the multi-scope expansion): the feature.
- PR C = Phase 4 (closeout: docs promotion, deprecation removal, lock): after the ramp.

Each phase's gate is the full local suite:
`cd cli && uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest -q`,
plus `./scripts/lint-files.sh` and `./scripts/check-locked-sdds.sh`.

## Phase 0: Design closeout (no code)

- [ ] Author `harness-adapter-api-lld.md`: the target `HarnessAdapter` interface, the new optional
      `provision_user`/`provision_workspace` hook signatures, the scope-aware `RunContext`/readiness
      objects, how a hook declares per-scope dependencies and readiness, and the scope-aware
      generalization of `_check_identity`.
- [ ] Author `migration-strategy.md`: the current-state inventory (from the rename survey), the DB
      migration number and exact `RENAME COLUMN` statement, the kind-alias vs hard-cutover decision
      (D5), the selector-field shim design (modeled on the legacy-flat-field hoist), the
      `agw resource migrate` changes, and the deprecation-removal schedule.
- [ ] Author `user-workspace-hooks-lld.md`: the new `agent-template` and `workspace-template`
      declaration fields (D6), the two net-new invocation sites, and the workspace-hook privilege
      confirmation (D7) and user-scope auth contract (D8).
- [ ] Resolve open decisions D5-D8 in the LLDs; update `frd.md`/`hla.md` if any decision changes a
      requirement.
- DoD: the three LLDs and `migration-strategy.md` exist, are reviewed, and every D5-D8 has a
  recorded decision; no `[ ]` decision remains.

## Phase 1: The rename (`harness` -> `harness-adapter`), one PR

Rename the existing session-scoped capability with no behavior change. Landing this half-done has no
value, so all sub-steps ship together.

- [ ] **1a Persisted state.** New numbered DB migration (after v29):
      `ALTER TABLE sessions RENAME COLUMN harness_state TO harness_adapter_state`. Update
      `db/models.py:158`, the INSERT/UPDATE SQL in `db/database.py`, and
      `db/converters.py:_parse_harness_state`/`_warn_bad_harness_state`. Do not edit migration v29.
      DoD: a migration test analogous to `cli/tests/test_db_migration_harness_state.py` proves an
      old-schema DB upgrades and its session rows keep their (name-namespaced, unchanged) blob
      values; round-trip insert/read works on the new column.
- [ ] **1b Kind slug + alias.** Set the kind to `harness-adapter` in `capabilities/harness/kinds.py`
      and `plugins/adapters.py`, add it to the capability-kind set in `resources/graph.py:369`, and
      add the deprecated `harness` kind alias per D5. DoD:
      `agw resource list --kind harness-adapter` works; `--kind harness` and `harness/<name>`
      resolve with a deprecation warning (or error with hint, per D5); registry tests updated.
- [ ] **1c Selector field + shim.** Rename the `session-template` tagged-table selector to
      `harness_adapter` and the field pair to `harness_adapter`/`harness_adapter_config`
      (`sessions/templates.py`, `sessions/template.py`, `manifests/decode.py:76`,
      `config/loaders_sessions.py`). Load the old `harness:`/`harness_config:` shape as a deprecated
      alias with a warning; teach `migrate/planning.py` to rewrite it. DoD: a manifest using
      `harness_adapter:` validates; one using `harness:` loads with a deprecation warning and
      `agw resource migrate` rewrites it; parity tests updated.
- [ ] **1d Identifier sweep.** Rename package `capabilities/harness/` ->
      `capabilities/harness_adapter/`, the plugin `harness.py` modules, classes (`Harness` ->
      `HarnessAdapter`, `ShellHarness`, `ClaudeCodeHarness`, `CodexHarness`, `HarnessEntry`,
      `_HarnessKind`, `_HarnessAdapter`), `HARNESS_REGISTRY` + accessors (`harness_for` ->
      `harness_adapter_for`, `ensure_harness_enabled` -> `ensure_harness_adapter_enabled`), and the
      threaded variables/params/comments (heaviest in `sessions/nodes.py`, `templates.py`,
      `template.py`, `manager/*`). DoD: `ruff`, `mypy`, and the full suite pass; no import of the
      old module path remains.
- [ ] **1e CLI-visible text.** Update the `"HARNESS"` list column and `"Harness:"` describe label
      (`sessions/manager/_queries.py:352,481`, `_env.py:_display_harness`) and the operator-facing
      error/hint strings (`capabilities/harness/__init__.py:74,105-109`). DoD:
      `test_session_list_harness.py` and describe tests assert the new header/label; no operator
      string says "harness" except in the industry sense.
- [ ] **1f Built-in manifests.** Update the shipped claude/codex `session-templates.yaml` and the
      example `agent-templates.yaml` to the new `harness_adapter:` selector. DoD: all six example
      session-templates still load present-but-disabled and validate; live parity test passes.
- [ ] **1g Docs.** Rename `capabilities/harness/README.md` under the new dir and update it,
      `capabilities/README.md`, `cli/README.md`, `docs/guides/resources.md`, ADR 0020, and the
      sample manifests to the new name and the per-hook framing. Leave historical `CHANGELOG.md`
      entries. Do not touch the operator's separately-authored target-state README section beyond
      the mechanism-sense/YAML-example updates. DoD: `lint-files.sh` clean; docs describe reality at
      HEAD (rename done, expansion not yet).
- DoD (phase): full gate green; `agw` create/list/restart a session round-trips on the renamed
  column and selector; `agw resource migrate` moves a legacy `harness:` template; a fresh grep for
  `harness_state`/`--kind harness`/`spec.harness:` finds only alias/compat/deprecation code and
  justified prose.

## Phase 2: User-scope hook

- [ ] Add `provision_user(ctx)` to the adapter interface (default no-op) per the API LLD, with
      scope-aware readiness.
- [ ] Add the `agent-template` (and `admin-config`) declaration field for adapters to provision at
      user scope (D6).
- [ ] Invoke enabled, declared adapters' `provision_user` in the agent self-configure phase
      (`agents/initializer.py`) and the admin analog (`vms/initializer/driver.py:_phase_b_setup`),
      after existing install-commands, gated by `ensure_harness_adapter_enabled`.
- [ ] Port the ad-hoc Claude debt (`claude_marketplaces`/`claude_plugins`, `install_claude_plugins`)
      into the `claude-code` adapter's `provision_user`; deprecate the old fields with a migration,
      per `plugins/claude/__init__.py:1-33`.
- DoD: enabling the `claude` plugin and referencing its adapter from an agent-template installs and
  configures Claude Code for that agent user on a real VM (verified via the `verify`/tester flow,
  not just unit tests); the old `claude_plugins` path still works via deprecation shim; provisioning
  against a disabled adapter is refused with the enable hint; effects stay within the user (R2).

## Phase 3: Workspace-scope hook (net-new)

- [ ] Add `provision_workspace(ctx)` to the adapter interface (default no-op) per the API LLD.
- [ ] Add the `workspace-template` declaration field for adapters to provision at workspace scope
      (D6), the first tool-aware field on `WorkspaceTemplate`.
- [ ] Invoke enabled, declared adapters' `provision_workspace` from `realize_workspace`
      (`workspaces/realize.py`) after `create_vm_workspace` returns and the directory/ACLs exist,
      running as admin with group-readable perms (D7).
- DoD: a workspace-template naming an adapter publishes that adapter's workspace material under the
  workspace path, group-readable under the setgid dir, verified on a real VM; a granted agent can
  read it; failure self-cleans with the existing workspace teardown; effects stay within the
  workspace (R2).

## Phase 4: Closeout

- [ ] Update `capabilities/README.md` "Planned Future Capabilities": mark `harness-user-provisioner`
      superseded (FRD R9), keeping the orthogonal account-strategy note as the part that may still
      warrant its own capability.
- [ ] Promote any load-bearing concepts out of this SDD into permanent docs (the per-hook scope
      contract into the capability README; the three-layer model where it belongs) so the SDD is
      deletable (SDD-not-permanent rule).
- [ ] Execute the deprecation-removal schedule from `migration-strategy.md` when its release lands
      (remove the `harness` kind alias and the `harness:` selector shim), or record the removal
      release if it postdates this SDD.
- [ ] Residual sweep: `rg -in harness` and justify every remaining hit (industry-sense prose,
      historical changelog, ephemeral SDD dirs); no live mechanism-sense `harness` remains.
- [ ] Write `locked.md` summarizing the final state; land it with the last edits.
- DoD: full gate green; the "Planned Future Capabilities" note is updated; permanent docs reflect
  reality at HEAD; `locked.md` present.

## Traceability (FRD -> plan)

- R1 (multi-scope invocable): Phases 2, 3.
- R2 (per-hook scope containment): Phase 2/3 DoD (effects stay within scope); HLA section 4.
- R3 (optional stages): default-no-op hooks (Phase 2, 3).
- R4 (account strategy orthogonal): HLA section 9; enforced in review, not built here.
- R5 (cohesion): one adapter unit contributes all three scopes.
- R6 (selector names the adapter): Phase 1c.
- R7 (rename the kind): Phase 1b.
- R8 (adapter identity vs harness attribute): API LLD (Phase 0), permitted not built (D3).
- R9 (supersede `harness-user-provisioner`): Phase 4.
- R10 (migration): `migration-strategy.md` (Phase 0), Phases 1a/1b/1c.
