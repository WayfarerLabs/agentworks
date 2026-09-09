# Degraded Runnable Recovery: Functional Requirements

- Status: Implemented; locked on merge of PR #764
- Date: 2026-09-06
- Tracks: #763
- Source baseline: `b22cc49c9827cb38fb7d8fc77522b436b318ac03`

## Why this exists

Agentworks stores sessions, workspaces, VMs, and consoles as related records. Normal writes preserve
those relationships, but an operator can still encounter incomplete state after restoring or
repairing a database, importing older state, or interrupting work outside Agentworks. Recovery is
when local inventory is most valuable, yet one incomplete relationship currently prevents plain
`session list` from showing any selected session. The same orphan prevents a status request from
reporting healthy rows. Migration correctly refuses inconsistent state, but its direct database
boundary uses a raw SQLite exception instead of the project error taxonomy.

The desired contract separates three jobs:

1. inventory reports the local records that still exist;
2. optional status enriches every structurally observable record and marks only unobservable rows
   unknown; and
3. focused and mutating operations stay strict because they need a complete target boundary.

This is recovery tolerance, not permission to create or preserve broken relationships during normal
writes.

## Personas and outcomes

- **Operator recovering state.** Can see every stored session and console, identify the broken
  relationship, and continue observing healthy resources.
- **Automation consuming JSON v1.** Retains the existing session row type, collection meaning, and
  failure boundary. It can recover a session whose workspace preserves a VM name, but receives a
  typed failure when no VM name can be represented.
- **Maintainer diagnosing migration.** Receives an Agentworks state error with recovery guidance
  instead of an implementation exception, while the migration continues to refuse foreign-key
  violations.
- **Operator restoring damaged state.** Is protected from unintentionally replacing the live
  database with a referentially inconsistent backup, while retaining an explicit, visibly unsafe
  recovery path when that backup is the best available source.

## Functional requirements

- **R1.** Plain human `agw session list` and `--names-only` shall be local recovery inventories. A
  selected session whose workspace row is missing, or whose workspace references a missing VM row,
  shall not prevent any selected row from being returned.
- **R2.** A session row shall preserve its stored workspace name. When its workspace exists, the row
  shall preserve that workspace's stored VM name even when the VM row is missing. When the workspace
  is missing and no VM name can be derived, human output shall show `-`.
- **R3.** Plain `agw console list` shall continue to preserve every selected console and its
  directly stored VM name even when the referenced VM row is missing.
- **R4.** `session list --status` and `console list --status` shall isolate structural failures per
  row. Structurally observable rows shall still be observed; rows missing a required workspace or VM
  shall carry `unknown`. One orphan shall not suppress healthy status results on the same or another
  VM.
- **R5.** Status lists shall perform no guest call for a row without a complete backing boundary.
  They shall retain the established bounded, read-only observation policy for healthy targets.
- **R6.** Human status output shall identify unknown rows without inventing a VM grouping for a
  session whose VM is not known. Existing grouped warnings shall remain concise for rows with a
  known VM name, and the no-VM rows shall appear in a separate diagnostic clause.
- **R7.** Plain inventory shall not warn merely because a relationship is incomplete. The visible
  placeholder or preserved stale name is the local fact. Requested status shall warn because
  `unknown` is an observation result that may require action.
- **R8.** List ordering, filtering, and names-only behavior shall remain stable. Supplied unknown
  filter names shall retain their typed validation errors. Relationship-based filters select only
  rows reachable through the requested existing relationship; unfiltered inventory is the recovery
  view for otherwise unreachable orphans.
- **R9.** JSON v1 shall retain the existing `{sessions}` collection meaning and every item field
  name, meaning, and type. A selected session with a known stored VM name shall remain
  representable, including when that VM row is missing. A selection containing a session whose VM
  name cannot be derived shall retain the existing typed failure instead of emitting an incomplete
  collection, widening `vm_name`, or inventing a sentinel. This validation shall happen before
  requested live observation. Representable plain items shall carry status `unavailable`; requested
  structurally unobservable items shall carry `unknown`.
- **R10.** Named describe commands and every lifecycle mutation shall continue to require complete
  structural relationships and raise typed errors when those relationships are missing. Batch list
  tolerance shall not weaken those boundaries.
- **R11.** Database migration shall continue checking foreign-key consistency after every migration
  step and shall refuse to advance the schema-version checkpoint when violations exist.
- **R12.** A migration-time foreign-key refusal shall cross the direct database boundary as an
  `AgentworksError` subtype with database identity and actionable backup, restore, inspection, or
  repair guidance. The production safe opener shall retain its existing typed partial-migration
  wrapper and recovery command.
- **R13.** `agw database restore` shall check the candidate backup for foreign-key violations before
  replacing the live database. It shall refuse a violating source by default through a typed
  Agentworks error. `--force` shall explicitly permit only that referential-integrity bypass;
  malformed SQLite, failed quick checks, unsupported versions, and invalid schema shapes or
  foreign-key declarations shall remain unconditionally rejected.
- **R14.** A forced restore of a backup with foreign-key violations shall warn before confirmation
  and again after successful replacement. The warning shall state that the source is structurally
  inconsistent and that some resources may be unavailable until repaired. `--force` shall not imply
  `--yes`: interactive restore still requires confirmation, and non-interactive restore still
  requires `--yes`. `--yes` shall suppress only the prompt, never either warning.
- **R15.** This change shall not repair or delete orphan rows, mutate the database during list or
  status operations, add a database migration, or change a capability contract version.
- **R16.** Permanent command and recovery guidance, machine-output documentation, the locked
  runnable-status record, and executable behavior shall change together.

## Non-goals

- An automatic database repair command or inferred ownership for orphan rows.
- Making `database restore --force` bypass file-integrity, Agentworks-schema, or version checks.
- Refusing or warning on `database backup` when the live database has broken relationships. Backup
  remains an unconditional evidence-preservation primitive; restore owns the later import decision.
- Treating a missing parent as proof that a guest runtime is stopped.
- Making describe or lifecycle operations best-effort.
- Changing VM inventory or VM status behavior, which already observes independent VM rows.
- Adding a JSON schema version, diagnostic-reason field, or changing an existing JSON v1 field type
  or collection meaning. Human warnings and typed focused errors remain the diagnostic surfaces for
  this effort.

## Acceptance criteria

1. A current-schema database containing healthy sessions plus sessions missing either a workspace or
   VM can be listed locally in human and names-only modes without external work or database writes;
   JSON v1 recovers only rows whose VM name remains representable.
2. Requested status returns healthy session and console results alongside `unknown` orphan results,
   with no transport construction for the orphan boundary.
3. Human placeholders and warnings distinguish an unknown VM name from a known but missing VM row,
   while JSON v1 rejects the former before live observation and preserves the latter.
4. Named describe and lifecycle operations still reject the same incomplete relationships through
   typed errors.
5. An older schema with a foreign-key violation refuses migration without advancing its version;
   direct construction and the production safe opener both surface truthful typed Agentworks errors
   at their respective boundaries.
6. Restore rejects a backup with foreign-key violations before replacement unless `--force` is
   explicit; the forced path retains confirmation semantics and emits both pre-restore and
   post-restore warnings without weakening any other validation.
7. Filters, names-only output, empty selections, JSON v1 shapes, row ordering, table alignment,
   timeouts, and no-write guarantees retain focused coverage.
