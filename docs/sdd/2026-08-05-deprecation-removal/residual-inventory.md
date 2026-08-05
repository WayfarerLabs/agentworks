# Pre-implementation Residual Inventory

- Snapshot: 2026-08-05
- Baseline: `main` at `a5ae6ec7`
- Purpose: Phase 0 inventory for the removals and final classified sweep required by R11

## Baseline verification

- Ruff check: passed
- Ruff format check: passed (506 files)
- Strict mypy: passed (506 source files)
- Pytest excluding integration: passed (3,422 passed, 3 deselected)
- File lint (Prettier, markdownlint, cspell): passed
- Current CLI help: exposes deprecated `session restart` and `vm console`; `vm shell` help exposes
  canonical `--platform`
- Current bash completion: exposes `session restart` and `vm console`; canonical `session resume`
  and top-level `console` are present

The first sandboxed pytest attempt failed because 135 tests could not create their ordinary log
directory under the read-only user config path. The same suite passed outside that filesystem
sandbox; this was an execution-environment failure, not a repository baseline failure.

This is a scoped inventory, not a claim that every occurrence of a generic word such as `restart`,
`harness`, `console`, or `provisioner` is deprecated. The final sweep updates the disposition of
each group after implementation.

## R1: `agw session restart`

- Command and warning: `cli/agentworks/cli/commands/session.py`
- Dynamic completion mappings: `cli/agentworks/completions/spec.py`
- Alias parity, warning, suppression, and discovery tests: `cli/tests/test_session_resume_cli.py`,
  `cli/tests/test_completions.py`
- Current teaching: `cli/README.md`
- Canonical control: `agw session resume` command, manager, completion, and lifecycle coverage

## R2: `restart_command`

- Template provenance and inheritance: `cli/agentworks/sessions/template.py`,
  `cli/agentworks/sessions/templates.py`
- YAML normalization and warning facts: `cli/agentworks/manifests/decode.py`,
  `cli/agentworks/manifests/loader.py`, `cli/agentworks/manifests/deprecated_fields.py`
- Independent legacy TOML migration reader and rewrite: `cli/agentworks/migrate/toml_resources.py`,
  `planning.py`, `verify.py`
- Tests and fixtures: session-template surface, shell integration, manifest deprecated-field,
  capability-shape, decode-parity, and resource-migrate suites
- Current teaching: CLI and resources guides, capability README, session-template samples
- Canonical control: `resume_command` decode, inheritance, shell validation, migration, and runtime

## R3: `harness` / `harness_config` selectors

- YAML selector normalization: `cli/agentworks/manifests/decode.py`
- Fact aggregation: `cli/agentworks/manifests/loader.py`
- Request and doctor reporting: `cli/agentworks/bootstrap.py`, `cli/agentworks/doctor.py`
- Migrator compatibility: `cli/agentworks/migrate/`
- Tests and fixtures: capability-shape, registry-warning-boundary, decode-parity, and
  resource-migrate suites
- Current teaching: resources guide, harness-integration capability README, ADR 0020 transition
  status, session-template samples
- Canonical control: tagged `spec.harness_integration` YAML and internal normalized fields

The canonical `harness` resource-kind slug and vendor-harness terminology are not aliases and stay.

## R4: older configuration aliases

- `[defaults].platform`: `cli/agentworks/config/loaders_core.py` and config deprecation tests
- `[user]`: `cli/agentworks/config/loaders_core.py` and `cli/tests/test_config.py`
- `[paths].code_workspaces`: `cli/agentworks/config/loaders_core.py`; no focused baseline regression
- `agw vm shell --provisioner`: `cli/agentworks/cli/commands/vm.py`, VM CLI surface tests, and
  current CLI documentation
- Canonical controls: `[defaults].site`, `[operator]`, `[paths].vscode_workspaces`, and
  `agw vm shell --platform`

## R5: legacy VM console

- Command: `cli/agentworks/cli/commands/vm.py`
- Standalone implementation: `cli/agentworks/sessions/console.py`
- Session-create hook: `cli/agentworks/sessions/manager/_create_roll.py`
- Dynamic completion mapping: `cli/agentworks/completions/spec.py`
- Tests: legacy block in `cli/tests/sessions/test_console_attach_orchestrated.py` and the
  create-hook assertion in `test_create_resume_orchestrated.py`
- Current teaching: `cli/README.md`, named-console sample, and comparison comments near canonical
  multi-console and tmuxinator code
- Canonical control: top-level `agw console` and `cli/agentworks/sessions/multi_console/`

## R6: dead Python surfaces

- `UserConfig`: alias/export in `cli/agentworks/config/models.py` and `config/__init__.py`
- `output.phase()`: wrapper and wrapper-only output test
- `env_compat.py`: module and self-contained test module
- Production call-site search: zero live callers at this snapshot

## R7: deprecation framework

Expired producers and facts:

- command-local session-restart warning;
- restart-command manifest aggregation and provenance;
- `deprecated_harness_selectors` fact field and selector-specific formatter/reporting; and
- deprecated-field doctor scanner with no warn-level table entry.

Live consumers that remain:

- `[secret_backends.*]` no-op notices through `Config.deprecation_issues`;
- generic capability sibling-shape notices through `ManifestSet.deprecation_issues` and
  `deprecated_shape_resources`;
- bootstrap once-per-command aggregation, `--no-deprecations`, and doctor health reporting.

## R8-R9: teaching and release surfaces

- Permanent docs: root and CLI READMEs, `docs/guides/resources.md`, `docs/guides/mise.md`,
  `docs/guides/proxmox.md`, ADR 0020, and capability READMEs
- Samples: sample config plus session-template and named-console YAML samples
- Completions: live Typer extraction plus dynamic mappings; no generated scripts are committed
- Release record: Release Please consumes conventional commits and `BREAKING CHANGE:` metadata;
  historical `cli/CHANGELOG.md` entries stay unchanged

## R10: closeout baseline

- Session-resume and harness-integration: removal and closeout items genuinely pending
- Proxmox-provider: checklist complete; permanent docs and implementation present; verification and
  lock pending
- Session-enhancements: checklist complete; implementation present; permanent maintainer description
  of the PID/boot-ID status model and lock pending
- Mise-integration: stale unchecked plan; most code exists, while focused tests, early validation,
  stale TOML guidance, deviations, and evidence-based reconciliation remain

## Final-sweep classification rules

Remaining hits are allowed only when classified as one of:

1. an explicit 0.14 rejection message or rejection test;
2. a historical changelog entry or historical database migration;
3. an active or closing SDD record;
4. canonical technical vocabulary unrelated to the retired input; or
5. a live generic-deprecation consumer named above.

Current production code, help, completion output, permanent instructional docs, and shipped samples
must not accept, complete, or teach an in-scope retired input.
