# Plan: 0.14 Deprecation Removal

- Status: Approved
- Start date: 2026-08-05
- Requirements: `frd.md`
- Architecture: `hla.md`

## How to work this plan

Each implementation phase is an always-green vertical slice. Production behavior, tests,
completions, samples, and permanent documentation move together. Completed checkboxes are immutable
records and must not be edited, moved, removed, or unchecked.

This is a bounded deletion effort with validation changes at existing boundaries. No separate LLD is
required before implementation: the exact validation placement, retained consumers, removal slices,
and verification contracts are specified in `hla.md`. If implementation reveals a new subsystem or a
boundary change broader than the HLA, stop and add an LLD or revise the upstream artifacts before
continuing.

Implementation phases are delegated to `agentworks-dev` agents and reviewed step by step with
`agentworks-reviewer`. Code-heavy slices also receive a fresh-eyes correctness review. The lead owns
this plan, cross-slice invariants, SDD reconciliation, and escalation decisions.

## Phase 0: Artifact review and baseline

- [x] Review and approve `hla.md`, including the narrow retired-setting and retired-manifest-field
      rejection boundaries, the retained deprecation consumers, and the no-roadmap-edit ownership
      constraint.
- [x] Review and approve this plan's phase boundaries, definitions of done, SDD closeout policy, and
      decision not to add the absent mise install toggle.
- [x] Record a pre-implementation baseline: full gate status, exact residual inventory grouped by
      FRD requirement, and installed/current CLI help and completion surfaces.
- [x] If artifact review materially changes requirements or architecture, revise `frd.md`, `hla.md`,
      and this plan before implementation.

Definition of done: the artifacts are approved through the SDD pre-implementation review process,
the baseline is recorded, and no implementation work has begun against an unsettled contract.

### Material error-policy revision (2026-08-05)

- [x] Review the operator-directed shift from bespoke retired-name hints to ordinary validation,
      including call-site-local strictness and preservation of unrelated existing targeted errors.
- [x] Re-approve `frd.md`, `hla.md`, and this plan after resolving review findings; implementation
      must not be committed or Phase 1 marked complete before this item is checked.

## Phase 1: Session and harness compatibility removal

- [x] Remove `agw session restart`, its warning, duplicate option signature, dynamic completion
      mappings, and alias-only parity/suppression tests. Assert ordinary unknown-command behavior
      and preserve all `session resume` behavior.
- [x] Make the session-template decoder's ordinary unknown-key validation strict before removing
      `harness`, `harness_config`, and top-level `restart_command` from its accepted shape. Pin
      generic rejection with file/location context; do not add retired-name hints or expose the
      decoder's internal sibling representation as a public shape.
- [x] Make the migrator's independent legacy TOML session-template reader reject ordinary unknown
      keys before deleting its compatibility rewrites. Pin generic `restart_command` rejection.
- [x] Remove `restart_command` normalization, template provenance, inheritance conflicts, warning
      aggregation, migrator rewrites, compatibility fixtures, and alias-only tests.
- [x] Remove old `harness`/`harness_config` selector normalization, mixed-form conflicts,
      selector-specific facts, bootstrap/doctor reporting, migrator rewrites, compatibility
      fixtures, and alias-only tests.
- [x] Remove the expiring deprecated-field scanner after strict session-template validation covers
      its correctness responsibility; do not replace it with a retired-field table.
- [x] Convert shared fixture families to canonical spellings without weakening ordinary
      unknown-input rejection tests.
- [x] Update `cli/README.md`, `docs/guides/resources.md`, the harness-integration capability README,
      ADR 0020 current-state language, session-template samples, CLI help, and completion assertions
      in the same slice.
- [x] Verify the generic capability sibling-shape warning remains aggregated, suppressible, and
      reported by doctor; verify every remaining `Config` and `ManifestSet` deprecation field has a
      live consumer.
- [x] Review the slice, resolve every valid finding, and run session lifecycle, manifest decode,
      migration, registry warning, doctor, docs/sample, and completion tests plus the full gate.

Definition of done: R1-R3 and the rename-specific portion of R7 are implemented; retired tokens are
rejected rather than normalized or warned; canonical session and harness behavior is unchanged; the
generic deprecation framework remains live and tested.

## Phase 2: Older settings, option, and dead Python surfaces

- [x] Make unknown config top-level sections, `[defaults]` keys, and `[paths]` keys fail through
      ordinary validation, then delete alias consumption for `[defaults].platform`, `[user]`, and
      `[paths].code_workspaces` in the same behavior-and-documentation slice. Do not add a
      retired-key hint table.
- [x] Pin the `code_workspaces` safety contract with a regression proving configuration fails before
      any VS Code workspace file can be written to the default directory.
- [x] Remove `agw vm shell --provisioner` while preserving `--platform`, native transport routing,
      platform-specific remediation, and route cleanup behavior.
- [x] Remove `UserConfig` and its export, `output.phase()` and its wrapper-only test, and
      `env_compat.py` with its self-contained test module.
- [x] Update current CLI documentation, help assertions, sample config verification, and any
      comments that teach the retired aliases. Add release-facing text that explicitly names the two
      aliases that never warned.
- [x] Review the slice, resolve every valid finding, and run config, VM shell, output,
      sample-config, docs, and full-gate verification.

Definition of done: R4 and R6 are complete; old settings and option names fail clearly; canonical
settings and platform shell behavior are unchanged; no dead Python compatibility surface remains.

## Phase 3: Legacy VM console deletion

- [x] Delete `agw vm console`, its dynamic completion mapping, and its dedicated
      `sessions/console.py` implementation.
- [x] Delete session create's best-effort legacy-console window hook and its tests while preserving
      tmuxinator regeneration and all other roll-forward behavior.
- [x] Remove legacy VM-console-only tests and imports without weakening canonical named-console
      creation, attachment, mutation, layout, or recovery coverage.
- [x] Update `cli/README.md`, the named-console sample, current code comments, help, and completion
      assertions to describe only the canonical top-level `console` family.
- [x] Review the slice, resolve every valid finding, and run session create/resume, canonical
      console, orchestrated attach, sample, completion, docs, and full-gate verification.

Definition of done: R5 is complete; the old VM-wide console command and implementation have no
caller or teaching surface; canonical consoles and unrelated tmuxinator behavior remain green.

## Phase 4: Mise reconciliation and small gap closure

- [x] Audit every unchecked item in `2026-03-26-mise-integration/plan.md` against current code,
      tests, permanent docs, and recorded verification. Check only items with evidence and leave all
      completed historical records untouched.
- [x] Add focused unit coverage for source-reference parsing/fetch behavior and mise configuration
      and install-flow branches that lack evidence today.
- [x] Add early validation for malformed mise package/source reference and ordering inputs where
      current decoding defers errors until operation time, without redesigning the resource schema.
- [x] Record later design deviations: YAML declarative resources superseded classic TOML resource
      configuration, mise is always installed, catalog abstractions were removed, and the absent
      `install_mise` toggle is not introduced by this effort.
- [x] Correct stale `docs/guides/mise.md` guidance that presents TOML resource declarations as an
      accepted or deprecated runtime path.
- [x] Record any live verification that is actually performed. Leave unperformed manual matrix items
      unchecked and state that limitation in closeout rather than inferring success.
- [x] Review the slice, resolve every valid finding, and run focused mise/source-reference tests,
      manifest and initializer tests, docs lint, and the full gate.

Definition of done: the stale mise plan tells the truth, small validation and test gaps are closed,
permanent docs match HEAD, substantial feature work has not been smuggled into reconciliation, and
the SDD is ready to lock.

## Phase 5: Release-spanning and completed-SDD closeout

- [x] Complete the still-unchecked 0.14 removal and closeout items in `2026-08-04-session-resume`,
      using the Phase 1 implementation and canonical permanent docs as evidence; add its dated
      `locked.md`.
- [x] Complete the still-unchecked removal, migration-strategy, and closeout items in
      `2026-08-03-harness-integration`, using the Phase 1 implementation, residual sweep, and
      permanent docs as evidence; add its dated `locked.md`.
- [x] Verify every completed checklist item in `2026-03-29-proxmox-provider` against current code,
      tests, scripts, samples, and `docs/guides/proxmox.md`; record later evolution without changing
      completed boxes, then add its dated `locked.md`.
- [x] Promote a concise permanent maintainer description of the session PID/boot-ID status model,
      auto-repair, and force semantics; verify `2026-05-03-session-enhancements` against it and the
      current tests, then add its dated `locked.md` without altering completed boxes.
- [x] Finish the evidence-based mise reconciliation from Phase 4 and add
      `2026-03-26-mise-integration/locked.md`, explicitly recording deviations and any unverified
      manual checks.
- [x] Review all five lockfiles and plan edits for historical accuracy, permanent-doc sufficiency,
      and completed-checkbox immutability; run locked-SDD enforcement.

Definition of done: R10 is complete; all five named SDDs are truthful, non-load-bearing, and locked;
no roadmap artifact has been modified.

## Phase 6: Release record, residual sweep, and final verification

- [ ] Ensure the removal commit consumed by Release Please uses a breaking conventional-commit
      marker and a `BREAKING CHANGE:` footer containing the coherent 0.14.0 story: phase 1 TOML
      sunset plus all removals, explicit `code_workspaces` and `--provisioner` callouts, canonical
      replacements, and the instruction to resolve warnings and migrate on 0.13.0 before upgrading.
      Verify Release Please interprets the metadata as the intended generated release-note content.
- [ ] Run classified residual searches for every retired token across production code, tests,
      completions, samples, permanent docs, and SDDs. Record each remaining hit as an explicit
      rejection, historical record, database migration, active closeout record, or unrelated
      canonical technical use.
- [ ] Build the wheel and exercise it through an isolated tool environment. Verify retired commands
      and options fail, canonical commands remain, help teaches only canonical surfaces, and bash,
      zsh, and PowerShell completions contain no retired command entry.
- [ ] Run ruff check and format verification, strict mypy, the complete non-integration pytest
      suite, `./scripts/lint-files.sh`, Rulesync drift checking, and locked-SDD enforcement.
- [ ] Run final `agentworks-reviewer` and fresh-eyes reviews over the complete diff, resolve every
      valid finding, and repeat affected gates.
- [ ] Update this plan with the residual-sweep record, final gate evidence, and any deviations; add
      this effort's dated `locked.md` only after every requirement and acceptance criterion is met.

Definition of done: R8, R9, and R11 are complete; all FRD acceptance criteria have objective
evidence; the built 0.14 CLI accepts, completes, and teaches only canonical in-scope surfaces; every
SDD named by this effort is locked and the full repository gate is green.

## Traceability

| FRD requirement | Plan phases      |
| --------------- | ---------------- |
| R1              | 1, 6             |
| R2              | 1, 6             |
| R3              | 1, 6             |
| R4              | 2, 6             |
| R5              | 3, 6             |
| R6              | 2, 6             |
| R7              | 1, 6             |
| R8              | 1, 2, 3, 4, 5, 6 |
| R9              | 2, 6             |
| R10             | 4, 5, 6          |
| R11             | 0, 6             |
