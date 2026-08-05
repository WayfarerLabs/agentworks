# HLA: 0.14 Deprecation Removal

- Status: Approved
- Start date: 2026-08-05
- Builds on: `frd.md`
- Baseline: `main` after declarative-schema phase 1 (PR #316)

## 1. Architecture summary

This effort removes compatibility inputs at the boundaries where Agentworks accepts them. It does
not add a replacement subsystem or change the topology of canonical session, manifest, console,
configuration, or deprecation flows.

The target architecture has three rules:

1. Canonical inputs continue through their existing loaders and managers unchanged.
2. Ordinary validation rejects retired inputs before warn-mode unknown-key handling or default
   selection can make them appear successful.
3. Rename-specific compatibility facts disappear, while the generic deprecation channel remains
   available to its live consumers.

```text
CLI argv --------------------------> Typer command tree ----------> existing managers
  retired command/option                rejected by Typer

config.toml ----------------------> strict settings validation --> existing settings loaders
  retired settings                    ordinary ConfigError

YAML session-template envelope ---> strict kind validation -----> canonical normalization/decode
  retired selector/field              ordinary ConfigError         (surviving shapes only)

Config / ManifestSet notices -----> bootstrap aggregation -------> one ambient command warning
                                 +-> doctor ----------------------> deprecation health
  expired producers deleted; generic capability-shape and secret-backend producers remain
```

No database schema or persisted session-state migration is involved. Historical database migrations
and live-session recovery paths remain intact.

## 2. Validation boundaries

### 2.1 Retired settings

The settings loader stops treating unknown keys as soft issues at the boundaries needed by this
removal:

| Boundary         | Strictness needed for removal                                      |
| ---------------- | ------------------------------------------------------------------ |
| config top level | Unknown sections fail, which rejects `[user]`                      |
| `[defaults]`     | Unknown keys fail after `platform` leaves the known-key set        |
| `[paths]`        | A declared known-key set rejects `code_workspaces` and other typos |

The errors use the existing generic unexpected-key or required-section vocabulary. There is no
retired-key table and no replacement hint. The strict `[paths]` check runs before defaults are
selected, guaranteeing that `code_workspaces` cannot fall through to the default VS Code workspace
directory. Alias branches disappear from `_load_operator`, `_load_paths`, and `_load_defaults`.
Canonical defaults do not change.

Strictness is local to the config top-level, `[defaults]`, and `[paths]` call sites. The shared
`_warn_unexpected_keys()` helper does not change globally; unrelated settings sections and manifest
kinds retain their existing warn-mode contracts. Existing targeted handling in
`_LEGACY_SINGLETON_HINTS` remains at the top-level boundary and becomes a hard error with its
current message, while other unknown top-level keys use the generic unexpected-key error.

`agw vm shell --provisioner` is removed from Typer's option declaration. `--platform` continues to
select the same native platform transport. The old flag receives Typer's ordinary unknown-option
error; current help, tests, and release notes name `--platform`.

### 2.2 Retired session-template fields

Session-template decoding currently uses warn-mode unknown-key handling, so deleting normalization
alone would silently accept several retired fields. The session-template decoder instead makes its
ordinary top-level unknown-key check strict. Once the alias fields leave the known-key set,
`harness`, `harness_config`, and top-level `restart_command` fail generically.

The public canonical YAML shape is the tagged table
`spec.harness_integration: {name: shell, resume_command: ...}`. The decoder normalizes that table to
the internal sibling pair `harness_integration` / `harness_integration_config`; that sibling pair is
not accepted as a public YAML shape. Nested `restart_command` continues to fail through the shell
integration's ordinary unknown-field validation once normalization stops recognizing it.

The migrator's independent legacy TOML reader is a second surviving ingress. Its session-template
loader likewise makes unknown keys strict before compatibility rewrite code is deleted. Canonical
`harness_integration` and `resume_command` continue through the existing normalization, inheritance,
migration, and validation paths.

The expiring `manifests/deprecated_fields.py` shim is deleted. It has no warn-level producer and
exists only for fields being removed here. No retired-field table replaces it.

Manifest and migrator strictness is likewise local to their session-template call sites. Other
manifest kinds and legacy TOML resource readers keep their current unknown-key behavior.

## 3. Removal slices

### 3.1 Session restart command

Delete the `session restart` Typer command, its per-command warning, dynamic completion mappings,
and alias-only parity and suppression coverage. `session resume` retains its current handler,
options, manager calls, and tests. The removed token is asserted absent from command discovery and
the built CLI.

### 3.2 Session-template compatibility

In one atomic behavior-and-documentation slice, establish strict ordinary validation and delete:

- old harness-selector normalization and mixed-form conflict branches;
- `restart_command` normalization, provenance, inheritance conflicts, and warning aggregation;
- `deprecated_harness_selectors` and `deprecated_restart_commands` fact plumbing;
- selector-specific bootstrap and doctor reporting;
- migrator rewrites and compatibility fixtures for both old spellings.

R2 and R3 form one implementation slice because they share the same decoder, manifest aggregation,
migrator, and fixture families. Canonical fixture conversions happen in the same slice so the tree
remains green.

The generic capability discriminator normalizer remains for `vm-site` and `git-credential`. Its
`deprecated_shape_resources` facts, aggregated warning, suppression behavior, and doctor reporting
remain live until declarative-schema phase 2 removes that shape.

### 3.3 Older CLI and Python surfaces

Delete the legacy `vm console` command and its standalone `sessions/console.py` implementation.
Delete the session-create roll-forward hook that adds a new session to a running legacy VM console,
but preserve the separate tmuxinator regeneration in the same flow. The top-level `console` command
family and `sessions/multi_console` implementation are not touched.

Delete `UserConfig`, `output.phase()`, and `env_compat.py` with their exports and surface-only
tests. These have no production callers.

## 4. Deprecation framework after cleanup

The framework remains a reusable transport, not a registry of expired names:

- `Config.deprecation_issues` continues to carry `[secret_backends.*]` no-op notices.
- `ManifestSet.deprecation_issues` and `deprecated_shape_resources` continue to carry the generic
  capability sibling-shape notice.
- bootstrap continues to emit ambient notices once per command.
- `--no-deprecations` continues to suppress only ambient teaching notices.
- doctor continues to report live deprecation health independently of ambient suppression.
- correctness and readiness findings remain separate from deprecations.

Rename-specific fact fields, formatters, scanners, and tests are removed. A final consumer audit
must justify every field left on `Config` and `ManifestSet`.

## 5. Completions, documentation, and release communication

The completion tree is derived from Typer. Manual mappings exist only for dynamic arguments, so the
`session.restart` and `vm.console` mappings are deleted in the same commits as their commands. Tests
assert the retired commands are absent and canonical commands remain discoverable. No generated
completion scripts are committed.

Permanent docs and shipped samples change with the behavior they describe:

- `cli/README.md`, `docs/guides/resources.md`, and the harness-integration capability README teach
  only `resume`, `resume_command`, and `harness_integration`.
- ADR 0020 preserves necessary historical rationale but its current-state language records that the
  compatibility selectors were removed in 0.14.0.
- session-template and named-console samples stop advertising retired compatibility.
- sample configuration is verified for canonical settings even where no edit is necessary.

Historical changelog entries and locked SDDs are not edited. Release Please builds the 0.14.0
changelog from conventional commits. The release-facing breaking-change record must present phase
1's TOML sunset and this cleanup as one upgrade story, explicitly name the aliases that never
warned, and direct operators through 0.13.0 warnings and migration tooling before upgrading.

## 6. SDD reconciliation and permanent documentation

The release-spanning session-resume and harness-integration SDDs close after their removal items,
canonical permanent docs, residual inventories, and gates are complete.

The Proxmox-provider and session-enhancements SDDs require evidence-based closeout, not new feature
work. Completed checkboxes stay immutable. The session health model needs a concise permanent
maintainer-facing description of its persisted PID and boot ID, status derivation, auto-repair, and
force semantics before that SDD locks.

The mise-integration SDD requires reconciliation against current code:

- check an old item only when code, tests, or recorded verification proves it;
- record the declarative-manifest and always-installed-mise outcomes as later design deviations;
- add focused source-reference, early validation, and mise installation-flow tests where the audit
  found small coverage and validation gaps;
- correct stale permanent guidance that still presents TOML resource declarations as accepted; and
- leave unperformed live-verification items unchecked and record that limitation in closeout.

The absent `install_mise` toggle is not added. Reintroducing that choice would be new feature work,
not a small reconciliation fix.

Only the deprecation-removal SDD and the five SDDs explicitly assigned for closeout are modified.
The parent roadmap remains owned by its roadmap lead.

## 7. Verification strategy

Each removal slice pins both rejection and canonical preservation:

- retired settings fail before managers can create files or apply defaults;
- retired manifest fields fail at the load boundary in top-level and nested forms;
- removed CLI commands/options are absent from help, completion extraction, and invocation;
- canonical session resume, shell integration, settings, platform shell, and console coverage stays
  green;
- fixture families contain only canonical forms except ordinary unknown-input rejection fixtures;
- the deprecation framework's remaining producers are exercised through ambient and doctor paths.

The final gate includes ruff, formatting, strict mypy, the non-integration pytest suite, file
linters, Rulesync drift, locked-SDD enforcement, and an isolated built-wheel smoke test. The wheel
test exercises help and bash, zsh, and PowerShell completion output without mutating a globally
installed Agentworks tool.

The residual sweep classifies every remaining in-scope token. Expected exceptions are limited to
historical changelog text, historical database migrations, active/closing SDD records, ordinary
unknown-input tests, and unrelated uses of words such as `restart`, `harness`, or `provisioner` in
their canonical technical senses.

## 8. Risks and safeguards

- **Silent retired-input acceptance.** Warn-mode decoders and defaulting settings loaders can hide a
  deletion. Safeguard: make the owning unknown-key checks strict before removing compatibility
  branches, with no-write and nested-field regressions.
- **Overly broad token deletion.** `restart`, `harness`, `console`, and `provisioner` remain valid
  in other domains. Safeguard: path- and contract-specific inventories, plus canonical regression
  suites.
- **Breaking canonical console creation.** The legacy and canonical console implementations have
  adjacent terminology. Safeguard: delete only `sessions/console.py`, the VM command, and its one
  create hook; retain and run all `multi_console` coverage.
- **Weak release communication for never-warned aliases.** Safeguard: make `code_workspaces` and
  `--provisioner` explicit in the breaking-change record and test their ordinary failures and
  canonical help.
- **False historical closeout.** Old plans may describe superseded shapes or unrecorded manual
  checks. Safeguard: never rewrite completed boxes, check stale boxes only with evidence, and record
  deviations or unverified work plainly in the lockfile.
