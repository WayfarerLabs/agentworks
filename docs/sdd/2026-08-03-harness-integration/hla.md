# HLA: Harness Integration (capability rename)

- Status: Draft
- Start date: 2026-08-03
- Builds on: `frd.md`. Detailed migration mechanics go in `migration-strategy.md`.

## 1. Shape of the change

A pure rename of the existing session-scoped capability from `harness` to `harness-integration`,
with no behavior change. Version 0.13.0 keeps previously valid persisted and configured objects
working while making the new name canonical. Version 0.14.0 removes the old selector and
discriminator compatibility. There is no new runtime component and no new interface; `dependencies`,
`validate`, readiness, `preflight`, `runup`, `start`, and `restart`, plus the three implementations,
are unchanged except for their names.

Target names (used consistently across all surfaces):

| Concept                   | Old                                                         | New                                                                                             |
| ------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Kind slug                 | `harness`                                                   | `harness-integration`                                                                           |
| Session-template selector | `harness:` (key)                                            | `harness_integration:` (key)                                                                    |
| Config field pair         | `harness` / `harness_config`                                | `harness_integration` / `harness_integration_config`                                            |
| DB column                 | `sessions.harness_state`                                    | `sessions.harness_integration_state`                                                            |
| Package                   | `capabilities/harness/`                                     | `capabilities/harness_integration/`                                                             |
| Base class                | `Harness`                                                   | `HarnessIntegration`                                                                            |
| Impl classes              | `ShellHarness`, `ClaudeCodeHarness`, `CodexHarness`         | `ShellIntegration`, `ClaudeCodeIntegration`, `CodexIntegration`                                 |
| Registry + accessors      | `HARNESS_REGISTRY`, `harness_for`, `ensure_harness_enabled` | `HARNESS_INTEGRATION_REGISTRY`, `harness_integration_for`, `ensure_harness_integration_enabled` |
| Plugin module             | `plugins/<p>/harness.py`                                    | `plugins/<p>/harness_integration.py`                                                            |

Exact impl-class and helper names are confirmable in the sweep; the table is the intended
convention.

**Naming rule (do not shorten).** The compound is never shortened to bare `harness` (it collides
with the vendors' meaning, the tool) or bare `integration` (too generic). Use the full
`harness_integration` / `HarnessIntegration` / `harness-integration` for the generic concept
everywhere: the kind, the package, the base class, the registry and accessors, the plugin module
(`harness_integration.py`, not `integration.py`), and the selector/config fields. The one exception
is an implementation class or resource, which names its **specific** harness in place of the generic
word: `ShellIntegration`, `ClaudeCodeIntegration`, `CodexIntegration` are each a form of
`HarnessIntegration` (shell, Claude Code, and Codex are the harnesses). That substitution is fine
because it names a real harness, not a shortening of the generic term.

## 2. The surfaces (from the rename survey) and how each changes

### 2a. Persisted state (highest risk)

`sessions.harness_state` is a real SQLite column added by migration v29 (`db/migrations.py:582`),
read/written in `db/database.py`, decoded in `db/converters.py`, and modeled at `db/models.py:158`.
A **new** idempotent migration v31 inspects the schema and runs
`ALTER TABLE sessions RENAME COLUMN harness_state TO harness_integration_state` only while the old
column remains. This lets the migration runner safely resume if the rename committed but its version
record did not. SQLite supports `RENAME COLUMN`; there is no data transform, because the JSON blob's
inner keys are namespaced by the implementation name (`shell`/`claude-code`/`codex`), not the word
"harness". Migration v29 is never edited. `models.py`, `database.py`, and `converters.py` change in
lockstep with the new migration.

### 2b. Kind slug and registry

The slug is registered in `capabilities/harness/kinds.py:53,71` and again in `plugins/adapters.py`
(the `_HarnessAdapter.kind`), and is a member of the hardcoded capability-kind set in
`resources/graph.py:369`. All three become `harness-integration`. This is a hard cutover: the
registry has no alias, old kind tokens fail as unknown, and kind listings and completions expose
only the new slug.

### 2c. Selector field and manifest compatibility

The `session-template` tagged-table selector key becomes `harness_integration`, and the internal
field pair becomes `harness_integration`/`harness_integration_config`
(`sessions/templates.py:36-37`, `sessions/template.py:63-64`, `manifests/decode.py:76`
`CAPABILITY_FIELDS`). Compatibility is layered on the existing legacy-field machinery
(`manifests/decode.py`, `config/loaders_sessions.py`, `migrate/planning.py`), which already hoists a
deprecated flat shape into the canonical pair. The input matrix:

| Input                                                               | Loads? | Warns?                                   | `resource migrate` output            |
| ------------------------------------------------------------------- | ------ | ---------------------------------------- | ------------------------------------ |
| TOML session-template (`harness`/`harness_config`)                  | yes    | yes: removed in 0.14.0                   | YAML with `harness_integration:`     |
| TOML canonical (`harness_integration`/`harness_integration_config`) | yes    | no                                       | YAML with `harness_integration:`     |
| YAML tagged, old key: `harness: { name: ... }`                      | yes    | yes: `harness` key removed in 0.14.0     | `harness_integration: { name: ... }` |
| YAML flat, old key: `harness: <name>` + `harness_config:`           | yes    | yes: same aggregated deprecation warning | `harness_integration: { name: ... }` |
| YAML tagged, new key: `harness_integration: { name: ... }`          | yes    | no                                       | unchanged                            |

The legacy TOML flat shell fields remain accepted with the same hoist and conflict behavior under
both discriminator spellings. The complete compatibility and error matrix is pinned in
`migration-strategy.md` section 4.

The deprecation warning is a single aggregated message (reusing the existing aggregation in
`decode_document`/`capability_shape_deprecation`), naming the `harness` key and pointing at
`harness_integration` and `agw resource migrate`. Canonical emitted form is always the new key (FRD
R7). Any combination of old and new selector or config fields is a hard error. The compatibility
layer recognizes only inputs that were valid before 0.13.0; it does not define hybrid forms or
precedence rules. After normalization, inheritance and default-shell behavior operate only on the
canonical internal pair.

### 2d. Code identifiers (soft)

Rename the package `capabilities/harness/` -> `capabilities/harness_integration/`, the plugin
modules `plugins/<p>/harness.py` -> `harness_integration.py`, the classes and registry/accessors per
the table in section 1, and the threaded variables/params/comments (heaviest in `sessions/nodes.py`,
`sessions/templates.py`, `sessions/template.py`, `sessions/manager/*`). No behavior change; `ruff`,
`mypy`, and the full suite are the guardrail.

### 2e. CLI-visible text

The `"HARNESS"` list column and `"Harness:"` describe label (`sessions/manager/_queries.py:352,481`,
`_env.py:_display_harness`) and the operator-facing error/hint strings
(`capabilities/harness/__init__.py:74,105-109`) become `HARNESS INTEGRATION` /
`Harness integration:` and the renamed error text. Operators may script against these, so they
change with the canonical output (FRD R7).

### 2f. Files, directories, and docs

Rename the package directory and the plugin modules; move and update the capability README under the
new package; update `capabilities/README.md`, `cli/README.md`, `docs/guides/resources.md`, ADR 0020,
the sample manifests (`manifests/samples/session-template.yaml`) and the shipped claude/codex
`session-templates.yaml`. The example `agent-templates.yaml` files contain no selector and need only
be verified. The root README's "Agentworks Is Not a Harness" prose uses "harness" in the industry
sense and stays; only its mechanism-sense usages and the YAML example change (the operator is
authoring the target-state README separately). Historical `CHANGELOG.md` entries are left as an
immutable record; new entries use the new name.

## 3. Enablement and readiness (unchanged mechanism, renamed)

The present-but-disabled flow (`plugins/enablement.py`, `ensure_harness_enabled`) is unchanged in
behavior and renamed to `ensure_harness_integration_enabled`. It keeps gating at the two existing
call sites (`sessions/manager/_create_build.py:178`, `_lifecycle.py:305-313`), never inside the node
factories, preserved by the existing drift guard (`cli/tests/sessions/test_harness_gate_drift.py`,
renamed).

## 4. Testing approach

- A new migration test analogous to `cli/tests/test_db_migration_harness_state.py`: an old-schema DB
  upgrades, the column is renamed, and session rows keep their (unchanged) blob values; a round-trip
  insert/read works on the new column.
- Manifest parity/decode tests updated to assert: the new `harness_integration:` key validates; both
  old `harness` shapes load with exactly one aggregated deprecation warning; `resource migrate`
  rewrites them to the new key.
- Registry/kind tests assert `--kind harness-integration` resolves, `--kind harness` and
  `harness/<name>` fail as unknown, and completions expose only the canonical slug.
- Compatibility tests assert every previously valid TOML and YAML form works in 0.13.0, mixed
  old/new forms hard-error, and old/new declarations normalize before inheritance.
- Session list/describe tests assert the new column header and label.
- The full existing suite passes after the identifier sweep (behavior unchanged).

## 5. Migration and deprecation summary

- **0.13.0:** new name is canonical everywhere; the old kind slug is gone; previously valid TOML and
  YAML session-template forms remain accepted with an aggregated warning; `agw resource migrate`
  rewrites them; the DB column is renamed by the new migration on first run.
- **0.14.0:** old selector and discriminator compatibility is removed. The SDD stays open across
  both releases and is closed and locked only after this removal lands.

## 6. Deferred (separate SDD)

The multi-scope expansion (user/workspace hooks, per-hook scope contract) is out of scope and gets
its own SDD. This rename deliberately touches no lifecycle wiring or interface beyond names, so it
does not constrain that design; the only thing it settles for the future work is the name.

## 7. Risks

- **Persisted-column rename** is the only at-rest data risk; a botched migration corrupts live
  session state. Mitigation: `RENAME COLUMN` is lossless and gated by the migration test.
- **Operator-script breakage** on `--kind harness` / `harness/<name>`. Mitigation: intentional hard
  cutover in 0.13.0; canonical completions and documentation expose only the new slug.
- **Operator-config breakage** on previously valid `harness:` declarations. Mitigation: 0.13.0
  accepts them with a warning and `agw resource migrate` rewrites them before removal in 0.14.0.
- **Sweep miss** leaving a stale identifier or CLI string. Mitigation: the plan's residual sweep
  greps case-insensitively for `harness` and justifies every remaining hit (industry-sense prose,
  historical changelog, the ephemeral SDD dirs).
