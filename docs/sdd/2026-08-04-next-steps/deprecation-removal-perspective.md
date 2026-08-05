# Deprecated Functionality Removal Perspective

- Status: Initial perspective
- Date: 2026-08-04
- Baseline: Agentworks 0.13.0 (`v0.13.0`)

## Purpose

This document records a perspective on removing expired compatibility surfaces after the Agentworks
0.13.0 release. It is an input to later requirements and implementation planning in this SDD, not a
functional specification or implementation plan.

The immediate goal is to remove bounded, already-deprecated functionality before the larger
declarative-schema effort begins. Doing that first gives the schema work fewer aliases, warning
paths, migration exceptions, and tests to carry while avoiding an overlapping redesign of resource
modeling.

## Executive Assessment

Agentworks 0.13.0 intentionally carried several one-release compatibility paths. Those paths have
now served their purpose and should be removed together in 0.14.0. Several older aliases and unused
compatibility APIs can be removed in the same focused effort.

The cleanup should not absorb the removal of TOML resource declarations or the generic legacy
capability discriminator shape. Those changes are coupled to the planned declarative-schema work,
which will replace the resource modeling and validation boundaries they currently depend on.

The generic deprecation mechanism should remain. Per-command suppression, aggregated notices, and
doctor reporting are useful reusable infrastructure for future transitions. This effort should
delete expired registrations and special-purpose plumbing, not eliminate the ability to deprecate a
surface cleanly.

| Area                                        | Direction                                     |
| ------------------------------------------- | --------------------------------------------- |
| Expired 0.13 compatibility                  | Remove in 0.14.0                              |
| Old, unused aliases and wrappers            | Remove directly                               |
| TOML resource declarations                  | Defer to declarative-schema work              |
| Generic capability discriminator shape      | Defer to declarative-schema work              |
| Generic deprecation framework               | Retain                                        |
| Historical database migrations              | Retain                                        |
| Persisted live-session compatibility        | Decide under its own operational cutoff       |
| Open SDDs whose deferred removal has landed | Finish and lock after implementation closeout |

## Removal Scope

### Session resume vocabulary

Version 0.13.0 made `agw session resume` canonical and retained `agw session restart` for one
release. Version 0.14.0 should remove:

- The `session restart` command and its duplicated option signature.
- Its per-command warning and completion-tree entry.
- Parity and suppression tests that exist only for the alias.
- Current documentation that presents the alias as accepted input.

The old command should become an ordinary unknown-command error. Historical changelog entries remain
unchanged because they accurately describe the release in which they were written.

The shell harness integration similarly made `resume_command` canonical while accepting
`restart_command` for 0.13.0. The cleanup should remove:

- `restart_command` recognition in TOML session templates and YAML manifests.
- Normalization from `restart_command` to `resume_command`.
- Warning aggregation and template provenance used only by that normalization.
- Migration rewrites and fixtures that exist only for this one-release field alias.
- Inheritance conflict rules whose only purpose is distinguishing the two spellings.

After removal, `restart_command` should fail as an unknown or unsupported field at the existing
validation boundary. `resume_command` behavior remains unchanged.

### Harness-integration naming compatibility

The pre-0.13 session-template selector names `harness` and `harness_config` should be removed. The
only accepted current selector is `harness_integration`, using the canonical tagged-table YAML form
and the existing canonical TOML form while TOML resources remain supported.

This is a deletion from the current decoder, not a redesign of resource modeling. It should remove:

- Old-selector normalization in manifest and TOML session-template loading.
- Mixed old-and-new selector conflict branches.
- Aggregated old-selector facts and request warnings.
- Doctor reporting specific to the old selector.
- Old-selector migration rewrites and compatibility fixtures.

The `harness` resource-kind slug is already not an alias and needs no new cutover behavior.

### Older configuration aliases

Several compatibility spellings predate the 0.13 transition and no longer justify permanent parser
branches:

- `[defaults].platform` in favor of `[defaults].site`.
- `[operator]`'s old top-level section name `[user]`.
- `[paths].code_workspaces` in favor of `[paths].vscode_workspaces`.
- `agw vm shell --provisioner` in favor of `--platform`.

`code_workspaces` is not currently accompanied by a formal deprecation warning, but it is a very old
compatibility spelling and is deliberately included for direct removal. Canonical settings keep
their current behavior and defaults.

Retired settings should fail clearly rather than silently falling back to defaults. In particular,
an old `code_workspaces` key must not be ignored in a way that unexpectedly writes VS Code workspace
files to the default directory.

### Deprecated command and Python API surfaces

The following bounded surfaces should also be removed:

- `agw vm console`, whose replacement is the current `agw console` command family.
- `UserConfig`, the unused Python alias for `OperatorConfig`.
- `output.phase()`, the unused deprecated wrapper superseded by scoped output sections.
- `env_compat.py`, which has no production callers and is retained only by its own tests.

Removing `agw vm console` should not mechanically delete shared console implementation used by the
canonical console commands. Call-graph analysis should distinguish the deprecated CLI entry point
from still-live console construction and attachment behavior.

## Explicitly Deferred Work

### TOML resource declarations

Agentworks should continue using TOML for settings such as operator identity, paths, defaults,
plugins, and secret resolution policy. The deprecated surface is declaring resources inside
`config.toml`, not TOML as a settings format.

Removing TOML resources is intentionally outside this cleanup. YAML manifest decoding currently
reuses resource loaders originally written for TOML, and `agw resource migrate` depends on both the
legacy input model and registry-equivalence verification. Removing those loaders safely requires the
native declarative models and validation boundaries being designed in the declarative-schema effort.

That workstream should own:

- Native declarative models for every resource kind.
- Removal of TOML resource loading and publication from ordinary runtime configuration.
- The future of `agw resource migrate` and its independent legacy reader.
- Removal of TOML-only resource aliases such as git-credential `type`.
- Rejection and remediation behavior for old TOML resource sections.

### Generic capability discriminator compatibility

The old YAML sibling shape, such as a scalar `platform` plus `platform_config` or a scalar
`provider` plus `provider_config`, is also deferred. Its removal changes the shared manifest
modeling boundary and should land with the declarative schemas that make tagged capability tables
authoritative.

The rename-specific `harness` selector is still in this cleanup because deleting that localized
normalization reduces inputs without designing a replacement model. The generic discriminator
normalizer remains until the schema work removes it.

### Persisted data and live runtime recovery

Historical database migrations are upgrade contracts, not deprecated input surfaces. They should not
be deleted merely because current configuration vocabulary has changed.

The pre-namespaced harness-state hoist and legacy tmux-socket recovery also require a separate
decision. They can affect existing live session rows and external tmux processes. Their removal
needs a supported-upgrade policy, an eager data or runtime migration, or an explicit operational
cutoff. A syntax cleanup is not sufficient authorization to remove them.

## Deprecation Framework Direction

The current generic deprecation mechanism is worth preserving if it continues to provide a single
consistent path for:

- Aggregating repeated configuration notices.
- Emitting a warning once per command rather than once per resource.
- Suppressing ambient notices with `--no-deprecations`.
- Reporting deprecation health through `agw doctor` even when ambient notices are suppressed.
- Keeping deprecation notices separate from correctness and readiness issues.

This cleanup should simplify that framework by deleting expired producers and fact fields that no
remaining deprecation uses. It should not remove `--no-deprecations` or the underlying output and
doctor concepts merely because the present registrations become fewer.

Any framework field left after cleanup should have a real current consumer. Rename-specific fields
such as `deprecated_harness_selectors` should disappear even though the generic mechanism remains.

## Recommended Work Shape

The removal effort is broad enough for its own SDD and reviewed implementation, but it should remain
architecturally modest. A practical sequence is:

1. Record a residual inventory of every active warning, alias, compatibility branch, completion,
   test, and current documentation reference.
2. Remove the session command and shell-field aliases, then finish the session-resume SDD.
3. Remove the harness selector aliases, then finish the harness-integration SDD.
4. Remove the older settings, option, command, and Python aliases.
5. Delete dead compatibility modules and narrow the generic deprecation bookkeeping to live uses.
6. Update canonical docs, samples, completions, and release notes in the same commits as behavior.
7. Run a final source and installed-CLI residual sweep, review, and full gate.
8. Reconcile and lock all completed open SDDs whose implementation is already present.

The implementation should use always-green slices. The session-resume and harness-integration
removals overlap in session-template loading, migration code, docs, and tests, so they belong in one
coordinated effort even if they remain separate commits and retain their own historical SDDs.

## SDD Closeout

Two open SDDs deliberately span the 0.13 compatibility release and the 0.14 removal:

- `2026-08-03-harness-integration`
- `2026-08-04-session-resume`

Their pending removal and closeout phases should be completed by this effort, their load-bearing
current behavior promoted to permanent documentation, and `locked.md` added after the final state is
verified.

Three older open SDDs also appear operationally complete but need administrative reconciliation:

- `2026-03-29-proxmox-provider` has a complete checklist and needs a final-state review and lock.
- `2026-05-03-session-enhancements` has a complete checklist and needs permanent-doc verification
  and a lock.
- `2026-03-26-mise-integration` has an unchecked historical plan despite substantial implementation;
  each item needs evidence or a recorded deviation before the SDD can be locked.

Completed checkboxes remain immutable. Unchecked historical items may be checked only after current
code, tests, or recorded verification establish that the promised result was delivered.

## Success Criteria for Later Requirements Work

A later FRD and implementation plan should be able to make the following outcomes objective:

- Every in-scope retired CLI token and configuration field is rejected.
- Canonical replacements behave exactly as they did in 0.13.0.
- No current docs, samples, or completions teach removed inputs.
- Historical changelog and migration records remain truthful.
- No TOML resource or generic discriminator redesign has leaked into this effort.
- Database upgrades and existing live-session recovery remain supported.
- The generic deprecation mechanism remains usable and contains no rename-specific residue.
- The two release-spanning SDDs and the completed older SDDs are accurately closed and locked.
