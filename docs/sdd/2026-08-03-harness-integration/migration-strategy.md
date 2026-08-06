# Migration strategy: harness to harness-integration

This strategy fixes the 0.13.0 compatibility boundary for the capability rename. It is the
implementation contract for plan Phase 1a through 1c, not a second plan. The rename is
behavior-preserving: the session workload, its implementation-owned configuration, and its JSON
state values do not change. Only the generic capability name changes.

> Correction (2026-08-04): the later session-resume SDD changes the shell lifecycle vocabulary in
> the same compatibility release. `resume_command` is now canonical and migration emits it.
> References to `restart_command` below describe accepted deprecated input only; it warns in 0.13.0
> and is removed in 0.14.0.

## 1. Decisions and invariants

- The only canonical kind is `harness-integration`. `harness` receives no registry alias, so
  `--kind harness` and `harness/<name>` are unknown-kind errors in 0.13.0.
- The only canonical YAML selector is `harness_integration:` as a tagged table. The internal pair is
  `harness_integration` / `harness_integration_config`.
- The compatibility promise accepts only declarations that were valid before 0.13.0: the old TOML
  pair, the old YAML tagged table, and the old YAML sibling pair. The renamed TOML pair is also the
  canonical spelling in the still-supported TOML resource path. This is not a new compatibility
  form, and it receives no rename warning. No hybrid shape or precedence rule exists.
- Old accepted input normalizes to the canonical internal pair before inheritance, default-shell
  resolution, dependency discovery, and validation. Those stages see no old-name branch.
- All emitted YAML, samples, listings, diagnostics, completions, and documentation use the new name.
  Compatibility is input-only and ends in 0.14.0.

## 2. Current-state inventory (snapshot: 2026-08-03)

| Surface                             | Current state                                                                                                                                                                                                                                                                                                                                                       | Required 0.13.0 change                                                                                                                     |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Schema                              | Migration v29 adds `sessions.harness_state`; v30 is the current head ([migrations.py](../../../cli/agentworks/db/migrations.py:581), [migrations.py](../../../cli/agentworks/db/migrations.py:589)).                                                                                                                                                                | Add v31, leaving v29 and v30 unchanged.                                                                                                    |
| Session state I/O                   | `SessionRow`, row conversion, insert, and update all use `harness_state` ([models.py](../../../cli/agentworks/db/models.py:158), [converters.py](../../../cli/agentworks/db/converters.py:122), [database.py](../../../cli/agentworks/db/database.py:541), [database.py](../../../cli/agentworks/db/database.py:647)).                                              | Rename the model member, conversion helper, and SQL column references together.                                                            |
| Session-template model and resolver | The declaration and resolved pair is `harness` / `harness_config`; the resolver merges that pair before validation ([template.py](../../../cli/agentworks/sessions/template.py:45), [templates.py](../../../cli/agentworks/sessions/templates.py:73)).                                                                                                              | Rename the pair throughout. The normalized value must reach the existing merge behavior unchanged.                                         |
| TOML loader                         | `_session_harness_pair` accepts the old pair and hoists legacy shell fields ([loaders_sessions.py](../../../cli/agentworks/config/loaders_sessions.py:42), [loaders_sessions.py](../../../cli/agentworks/config/loaders_sessions.py:95)).                                                                                                                           | Make the new pair canonical and recognize the old pair only in the 0.13.0 shim.                                                            |
| YAML decode                         | `CAPABILITY_FIELDS` names the old pair, and `_normalize_capability_field` already folds a tagged table into a sibling pair and rejects table-plus-sibling ambiguity ([decode.py](../../../cli/agentworks/manifests/decode.py:73), [decode.py](../../../cli/agentworks/manifests/decode.py:80)).                                                                     | Extend this boundary to map old selector forms to the new pair and record old-selector use for aggregation.                                |
| Warnings                            | Manifest old-shape warnings already aggregate once across a load ([loader.py](../../../cli/agentworks/manifests/loader.py:232), [decode.py](../../../cli/agentworks/manifests/decode.py:116)). TOML resource-section warnings are a separate aggregate ([loaders_secrets.py](../../../cli/agentworks/config/loaders_secrets.py:128)).                               | Reuse the deprecation channel and suppress flag. Do not emit a warning per template.                                                       |
| Resource migration                  | `plan_migration` discovers TOML sections only and `_emit_document` currently emits `spec.harness`; execution appends YAML and atomically rewrites only `config.toml` ([planning.py](../../../cli/agentworks/migrate/planning.py:106), [planning.py](../../../cli/agentworks/migrate/planning.py:528), [execute.py](../../../cli/agentworks/migrate/execute.py:47)). | Update TOML emission and add a narrowly scoped YAML rewrite path for old `session-template` selectors. This capability does not exist yet. |
| Existing DB guardrail               | The v29 test builds a prior schema, opens it through `Database`, and exercises insert/read/update ([test_db_migration_harness_state.py](../../../cli/tests/test_db_migration_harness_state.py:30)).                                                                                                                                                                 | Add the analogous v31 rename test, seeded at v30 with a non-empty blob.                                                                    |

The schema runner executes each migration version, checks foreign keys, records the version, and
commits it ([database.py](../../../cli/agentworks/db/database.py:106)). Its documented failure mode
is a crash after DDL but before the version record, which retries that migration. v31 must therefore
be a Python step that recognizes the interrupted schema rather than a bare string migration.

## 3. Database migration

The next free migration is **v31**. Add a Python migration after v30. It inspects
`PRAGMA table_info(sessions)` and has exactly these branches:

```sql
ALTER TABLE sessions RENAME COLUMN harness_state TO harness_integration_state;
```

When only `harness_state` exists, it executes that exact statement. When only
`harness_integration_state` exists, it returns successfully, allowing the runner to record v31 after
a crash in the DDL-to-version-record window. If both columns exist or neither exists, it raises a
clear `sqlite3` migration error naming the unexpected column set. It must not add, copy, drop, or
rebuild a table.

SQLite preserves the values, constraints, and defaults. In particular, the JSON text is opaque to
the schema migration: its top-level keys remain the implementation names `shell`, `claude-code`, or
`codex`, so no JSON transform is permitted.

The migration test starts from a v30 database, creates the required VM, workspace, and session row,
and seeds a non-empty blob such as `{"claude-code": {"session_id": "abc-123"}}`. Opening it at v31
must prove all of the following:

1. `PRAGMA table_info(sessions)` contains `harness_integration_state` and no `harness_state` column.
2. The raw SQL value selected from `harness_integration_state` is byte-for-byte equal to the raw
   value selected from `harness_state` before migration, including deliberately non-canonical JSON
   whitespace. Its decoded `SessionRow` value is also equal.
3. A fresh insert, read, and state update use the renamed column.
4. The migration completes the runner's foreign-key check.

A separate interrupted-state test first executes the exact rename against a v30 fixture, leaves
`schema_version` at 30, then opens `Database`. The rerun must record v31 without reissuing the DDL,
preserve raw and decoded state, and leave only the new column.

Migration v29 is historical schema and must not be edited. The identifier sweep renames the field
and helpers around the migrated column, including malformed-blob warnings, but does not alter their
degrade-to-empty behavior.

## 4. 0.13.0 input contract

The compatibility shim lives only at the source boundaries. The matrix below is the complete
accepted surface. "Selector warning" means the rename-specific aggregate; the existing general
TOML-resource deprecation remains independent.

| Source and spelling                                                                   | 0.13.0 load | Selector warning                                                | Normalized internal pair                              | `agw resource migrate` result                                             |
| ------------------------------------------------------------------------------------- | ----------- | --------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------- |
| TOML: `harness = "shell"` with optional `[...harness_config]`                         | Accept.     | Yes.                                                            | `harness_integration` / `harness_integration_config`. | Emits canonical YAML tagged table.                                        |
| TOML: `harness_integration = "shell"` with optional `[...harness_integration_config]` | Accept.     | No rename-specific warning.                                     | Already canonical.                                    | Emits canonical YAML tagged table.                                        |
| TOML legacy shell fields: `command`, `restart_command`, and `required_commands`       | Accept.     | Old selector warning, plus field warning for `restart_command`. | Canonical pair with `shell`.                          | Emits canonical YAML with `resume_command`.                               |
| YAML tagged old: `harness: {name: shell, command: htop}`                              | Accept.     | Yes.                                                            | Canonical pair.                                       | Rewrites in place to `harness_integration: {name: shell, command: htop}`. |
| YAML sibling old: `harness: shell` plus `harness_config: {...}`                       | Accept.     | Yes.                                                            | Canonical pair.                                       | Rewrites in place to one canonical tagged table.                          |
| YAML tagged new: `harness_integration: {name: shell, command: htop}`                  | Accept.     | No.                                                             | Canonical pair.                                       | No content change.                                                        |

The new YAML selector has exactly one authoring form: the tagged table. A newly invented new-name
sibling pair (`harness_integration: shell` plus `harness_integration_config`) is not accepted. It
was never a valid input and would teach an immediately non-canonical shape.

### TOML canonical and legacy-shell behavior

`_SESSION_TEMPLATE_KEYS` gains `harness_integration` and `harness_integration_config`; those are the
canonical TOML discriminator pair. The loader validates the canonical selector as a string and
canonical config as a table, rejects a config without its selector, and normalizes the pair directly
to the new internal fields. The old pair stays in the accepted-key set only for the 0.13.0 shim,
maps to the same new fields, and contributes the rename-warning token.

The existing flat shell fields (`command`, `restart_command`, `required_commands`) stay supported
with their hoist semantics: when present, they construct the canonical
`harness_integration = "shell"` plus `harness_integration_config` blob before inheritance. The
resume rename then normalizes `restart_command` to `resume_command` and emits its field deprecation
warning. They may coexist only with an explicit old or canonical selector that is `"shell"`; an old
selector adds the rename token and a canonical selector does not. They cannot coexist with a
non-shell selector or with either old or canonical explicit config table. The error remains a hard
load error naming the flat fields and the conflicting selector/config field. Mixed old/new fields
are hard errors before this hoist, so flat fields never create an exception to the no-precedence
rule. Tests retain each current flat-shell success and conflict case, then add old-selector,
canonical-selector, old/new-mixed, and canonical-config conflict coverage.

### Mixed-style hard errors

Before normalizing either old form, reject every declaration containing a field from both
vocabularies. This includes `harness` with `harness_integration`, `harness_config` with
`harness_integration_config`, and either selector paired with the other vocabulary's config field.
The error must name the conflicting fields, state that old and new selector/config fields cannot be
mixed, and direct the operator to use only `harness_integration: {name: ..., <config keys...>}`.
There is no precedence rule.

Preserve the existing hard errors for a tagged table plus its own sibling config, for a tagged table
missing a non-empty string `name`, and for an ownerless config blob. These are invalid
configurations, not deprecations. A mixed declaration must fail before inheritance so no parent or
child can accidentally decide precedence.

### Aggregated warning

Use exactly one rename warning for an ordinary combined TOML-plus-YAML registry load, in
deterministic source order, rather than one warning per template or source type. The exact message
is:

```text
deprecated session-template selector in: session-template/<name>[, ...]. `harness` is deprecated; use `harness_integration` instead. It will be removed in 0.14.0. Run `agw resource migrate` to rewrite these declarations. Silence this warning with --no-deprecations.
```

`load_config` records old TOML tokens as facts but does not print this selector warning.
`load_manifests` similarly records old YAML tokens, including the old sibling shape, without
printing the selector warning or its existing generic shape warning for that document.

Add a pure `harness_selector_deprecation(config, manifests)` helper that returns the one message or
`None`, combining TOML declaration order with manifest path/document order. `build_registry` remains
pure: it emits neither this warning nor the current auto-loaded-manifest warnings, and it changes no
behavior when handed an explicit `ManifestSet`. A new request-level `load_request_registry` boundary
explicitly loads the manifest set, calls `build_registry(config, manifests)`, then emits manifest
issues, existing manifest deprecations, and the helper's result once through `output.warn`, gated by
`--no-deprecations` where appropriate. This boundary is used for both the normal auto-discovered
manifest path and callers that supply an explicit `ManifestSet`; tests spy on `output.warn` to prove
direct `build_registry` calls emit nothing and each request emits at most one rename warning.
`load_request_registry` accepts the caller's warning policy. `agw resource migrate` preserves its
existing `warn_deprecations=False` remediation-command exemption and emits no rename warning; cover
that path explicitly in its warning tests. `doctor` calls the same pure helper with the `Config` and
`ManifestSet` it already loads, rendering one structured warning row rather than emitting ambient
output. Existing generic TOML-resource deprecation output may still appear, but it is distinct from
this rename warning.

Inventory every production `build_registry` call site while introducing the request boundary. Route
request-serving callers through `load_request_registry` and add a static drift test that allowlists
the few direct calls intentionally kept pure, so a future or missed caller cannot silently suppress
the selector warning.

## 5. `agw resource migrate` behavior

The command must become the remediation named by the warning for both source forms. Keep its
existing TOML-to-YAML behavior and verification model, but make these session-template-specific
changes:

1. In TOML emission, consume either accepted discriminator pair and always emit
   `spec.harness_integration` as a tagged table. Legacy shell flat fields retain their present shell
   hoist, then emit under that table. The emitted document must never contain `harness` or either
   `*_config` sibling.
2. Discover existing operator YAML documents whose `kind` is `session-template` and whose `spec`
   uses an old `harness` selector. New-name YAML is not rewritten. Other manifest kinds are out of
   scope.
3. Rewrite only the affected YAML document(s), preserving their envelope, metadata, unrelated `spec`
   keys, document ordering, and multi-document files. The old tagged shape changes its key only; the
   old sibling shape becomes the canonical tagged table and removes `harness_config`. Use
   `ruamel.yaml`'s round-trip loader/dumper for this path, not PyYAML: comments, quote style,
   mapping order, document separators, and comments attached to the moved selector/config keys must
   survive. This requires adding `ruamel.yaml` as a pinned direct Python dependency. Its version is
   an implementation dependency decision and is deliberately not guessed here. Tests cover leading,
   inline, and nested comments in both old YAML shapes and a multi-document file.
4. Validate the reconstructed canonical capability config before writing, as the current TOML
   emitter does. Refuse a mixed or otherwise invalid document before any file changes.
5. Extend `migrate/planning.py`, `migrate/execute.py`, `migrate/render.py`, the migration result
   model, and `cli/commands/resource.py` rather than adding a second command. The CLI help, preview,
   dry-run summary, full dry-run output, and final result summary must distinguish TOML emissions
   from in-place YAML rewrites.
6. A YAML rewrite is a planned file replacement. The plan records each target's original bytes and
   SHA-256 digest, plus the proposed bytes and their digest. Execution takes the existing backup and
   additionally snapshots every selected YAML file. It re-reads each target immediately before
   replacement and requires the planned old digest before using an atomic replacement. A digest
   mismatch is a `StateError`: do not write that file, report that it changed after planning, retain
   the backup, and tell the operator to reconcile the edit and rerun. The same compare-and-swap rule
   applies to `config.toml`.
7. Rollback restores a changed file only when its current digest still equals this run's recorded
   new digest. If it does not, another writer changed it after this run; do not overwrite that edit.
   Leave the backup and report the path, expected digest, observed digest, and manual recovery
   steps. These guards detect edits before replacement and prevent rollback from overwriting a later
   edit; no filesystem-level check-and-replace primitive can make an arbitrary external writer
   cooperate after the final digest check.
8. After both TOML emission and YAML rewriting, rebuild the registry and compare normalized rows
   with the pre-run registry, following the existing `execute_plan` verification. A mismatch rolls
   back TOML, created/appended YAML, and every replaced YAML file subject to the digest guard. This
   is required because the present implementation only appends YAML and cannot safely claim to
   rewrite existing YAML yet.

Tests must pin both old YAML shapes and the old TOML pair, exercise a multi-document YAML file,
assert canonical output, assert no write for canonical YAML, and force both a later YAML replacement
failure in a multi-file run and a post-write verification failure. Each failure proves all eligible
original files are restored, while a simulated concurrent edit proves the digest guard leaves that
edit intact and reports recovery. The existing TOML comment/delete modes continue to apply only to
TOML sections.

### Selector, preview, and no-op semantics

The existing selector grammar stays unchanged, but its source set expands only for old YAML
session-templates:

| Invocation                                   | Selected work                                                                                                                                                                   | Result when no selected legacy work exists                                            |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `agw resource migrate session-template/name` | The one TOML declaration, or the one old YAML `session-template/name`. A same-name TOML/YAML pair is already a cross-source duplicate and fails registry build before planning. | `ValidationError` naming `session-template/name`.                                     |
| `agw resource migrate session-template`      | Every TOML session-template plus every old YAML session-template, in TOML declaration order then manifest path/document order.                                                  | `ValidationError` naming the kind.                                                    |
| `agw resource migrate <other-kind>[/name]`   | Existing TOML-only behavior.                                                                                                                                                    | Existing unmatched-selector `ValidationError`.                                        |
| `agw resource migrate --all`                 | Every existing TOML migration unit plus every old YAML session-template.                                                                                                        | A successful no-op plan when neither set has work, matching today's `--all` behavior. |
| `agw resource migrate`                       | Nothing.                                                                                                                                                                        | Existing error: selectors or `--all` are required.                                    |

Canonical YAML-only resources do not count as migration work: an explicit selector for one fails as
unmatched, and `--all` leaves it untouched. An old YAML-only session-template is a valid selected
unit, including when there is no TOML work. TOML and YAML work with different names combines in one
plan. Each replacement is atomic and caught failures roll back subject to the digest safeguards
above. A process or host crash can leave a recoverable partial run; the backup and planned/current
digests provide the information needed to resume or recover it safely.

`render_preview` lists TOML emission and YAML replacement as separate actions, including every
`kind/name` and YAML path. `--dry-run` takes no backup or write and shows that same summary;
`--dry-run --full` includes the emitted YAML documents, the `config.toml` diff, and a unified diff
for each YAML replacement. The final result model and CLI output separately report created YAML,
appended YAML, replaced YAML, commented/deleted TOML sections, and the backup location. Tests cover
kind/name, kind, `--all`, unmatched, YAML-only, mixed distinct TOML/YAML, canonical-YAML no-op,
preview, summary dry-run, full dry-run, and zero-write dry-run behavior.

## 6. Delivery sequence and safeguards

1. Add focused failing tests for the v31 schema upgrade, the full input matrix, mixed-field errors,
   exact warning text, old/new normalization before inheritance, and TOML/YAML migration rollback.
2. Land v31 and rename the DB field accesses together. Do not alter v29.
3. Rename the canonical selector and internal pair. Add the old-name shim at the TOML and YAML
   boundaries, then prove the resolver receives only the new pair.
4. Change the migration planner/emitter/executor in the same PR as the shim, so every warning points
   to a working remediation.
5. Perform the kind cutover with no alias, then the package, identifier, output, sample, completion,
   and documentation sweep. First-party manifests must use the canonical selector and therefore
   remain warning-free.
6. Run the plan's full gate and a residual `rg -in harness` review. Remaining mechanism-sense uses
   must be restricted to compatibility input code until 0.14.0; historical changelog, industry-sense
   prose, and this temporary SDD are separately justified.

The main risks are loss of session-state data, ambiguous hybrid declarations, and a half-applied
YAML rewrite. The v31 round-trip test protects the first; fail-fast mixed-field validation protects
the second; planned atomic replacements, backups, and normalized-registry rollback protect the
third.

## 7. 0.14.0 removal checklist

- [x] Delete all recognition of `harness` and `harness_config` as session-template input, including
      TOML discriminator support and old YAML tagged/sibling normalization.
- [x] Delete the old-selector aggregation data, pure request-warning helper, and exact deprecation
      warning; old input now fails with a clear canonical-selector error. Keep the request
      boundary's unrelated manifest-warning handling and `build_registry` purity.
- [x] Remove YAML rewrite discovery and rewrite paths that exist solely for the old selector. Keep
      the general TOML-to-YAML migration facility. Remove the `ruamel.yaml` direct dependency if it
      has no remaining use.
- [x] Remove old-only migration tests and replace them with clear-rejection tests; retain canonical
      YAML/TOML migration and inheritance coverage.
- [x] Confirm `harness_integration` / `harness_integration_config` canonical behavior is unchanged
      and the renamed DB column remains the only column.
- [x] Hand the generated 0.14.0 release record to the release-spanning deprecation-removal effort,
      complete the full gate, perform the residual sweep with no compatibility-code exception, then
      leave the SDD open only for Phase 3 closeout.
