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
2. Retired inputs fail before warn-mode unknown-key handling or default selection can make them
   appear successful.
3. Rename-specific compatibility facts disappear, while the generic deprecation channel remains
   available to its live consumers.

```text
CLI argv --------------------------> Typer command tree ----------> existing managers
  retired command/option                rejected by Typer

config.toml ----------------------> retired-setting guard -------> existing settings loaders
  retired settings                    actionable ConfigError

YAML session-template envelope ---> retired-field guard --------> canonical normalization/decode
  retired selector/field              actionable ConfigError       (surviving shapes only)

Config / ManifestSet notices -----> bootstrap aggregation -------> one ambient command warning
                                 +-> doctor ----------------------> deprecation health
  expired producers deleted; generic capability-shape and secret-backend producers remain
```

No database schema or persisted session-state migration is involved. Historical database migrations
and live-session recovery paths remain intact.

## 2. Validation boundaries

### 2.1 Retired settings

`load_config` gains a small, explicit retired-setting guard that runs after TOML parsing and before
section loaders consume values or apply defaults. The guard recognizes these exact paths:

| Retired path              | Replacement                 |
| ------------------------- | --------------------------- |
| `[defaults].platform`     | `[defaults].site`           |
| top-level `[user]`        | `[operator]`                |
| `[paths].code_workspaces` | `[paths].vscode_workspaces` |

Any match raises `ConfigError` naming the retired path and its replacement. The guard is narrow by
design. This effort does not turn every settings-section unknown key into a hard error, which would
be a broader validation-contract change. Running the guard before loading also guarantees that
`code_workspaces` cannot fall through to the default VS Code workspace directory.

After the guard is established, the alias branches disappear from `_load_operator`, `_load_paths`,
and `_load_defaults`. Canonical defaults and issue handling do not change.

`agw vm shell --provisioner` is removed from Typer's option declaration. `--platform` continues to
select the same native platform transport. The old flag receives Typer's ordinary unknown-option
error; current help, tests, and release notes name `--platform`.

### 2.2 Retired session-template fields

Session-template decoding currently uses warn-mode unknown-key handling, so deleting normalization
alone would silently accept several retired fields. Narrow guards therefore run on the decoder's
local `spec` copy around capability normalization and before per-kind delegation.

For `session-template`, the guard hard-rejects:

- top-level `harness` and `harness_config`, naming `harness_integration`;
- top-level `restart_command`, naming the shell integration's nested `resume_command` location; and
- `restart_command` in the normalized shell integration config, naming `resume_command`.

The public canonical YAML shape is the tagged table
`spec.harness_integration: {name: shell, resume_command: ...}`. The decoder normalizes that table to
the internal sibling pair `harness_integration` / `harness_integration_config`; that sibling pair is
not accepted as a public YAML shape. Selector rejection runs before normalization, while the nested
`restart_command` check runs within tagged-table normalization or immediately after it on the
internal copy. The nested field is also rejected by shell integration validation today; the shared
guard makes the retirement contract explicit and produces consistent replacement guidance at the
manifest boundary.

The migrator's independent legacy TOML reader is a second surviving ingress. Its session-template
loader gets the same explicit `restart_command` rejection before compatibility rewrite code is
deleted. Canonical `harness_integration` and `resume_command` continue through the existing
normalization, inheritance, migration, and validation paths.

This guard replaces the expiring generic-looking `manifests/deprecated_fields.py` shim. That module
has no warn-level producer and exists only for fields being removed here. Keeping the rejection
table close to session-template decoding avoids preserving a dead doctor scanner or suggesting a
second permanent schema system.

## 3. Removal slices

### 3.1 Session restart command

Delete the `session restart` Typer command, its per-command warning, dynamic completion mappings,
and alias-only parity and suppression coverage. `session resume` retains its current handler,
options, manager calls, and tests. The removed token is asserted absent from command discovery and
the built CLI.

### 3.2 Session-template compatibility

In one atomic behavior-and-documentation slice, establish the retired-field guards and delete:

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
- fixture families contain only canonical forms except explicit rejection fixtures;
- the deprecation framework's remaining producers are exercised through ambient and doctor paths.

The final gate includes ruff, formatting, strict mypy, the non-integration pytest suite, file
linters, Rulesync drift, locked-SDD enforcement, and an isolated built-wheel smoke test. The wheel
test exercises help and bash, zsh, and PowerShell completion output without mutating a globally
installed Agentworks tool.

The residual sweep classifies every remaining in-scope token. Expected exceptions are limited to
historical changelog text, historical database migrations, active/closing SDD records, explicit
rejection messages and tests, and unrelated uses of words such as `restart`, `harness`, or
`provisioner` in their canonical technical senses.

## 8. Risks and safeguards

- **Silent retired-input acceptance.** Warn-mode decoders and defaulting settings loaders can hide a
  deletion. Safeguard: establish explicit rejection guards before removing compatibility branches,
  with no-write and nested-field regressions.
- **Overly broad token deletion.** `restart`, `harness`, `console`, and `provisioner` remain valid
  in other domains. Safeguard: path- and contract-specific inventories, plus canonical regression
  suites.
- **Breaking canonical console creation.** The legacy and canonical console implementations have
  adjacent terminology. Safeguard: delete only `sessions/console.py`, the VM command, and its one
  create hook; retain and run all `multi_console` coverage.
- **Weak release communication for never-warned aliases.** Safeguard: make `code_workspaces` and
  `--provisioner` explicit in the breaking-change record and test their replacement errors/help.
- **False historical closeout.** Old plans may describe superseded shapes or unrecorded manual
  checks. Safeguard: never rewrite completed boxes, check stale boxes only with evidence, and record
  deviations or unverified work plainly in the lockfile.
