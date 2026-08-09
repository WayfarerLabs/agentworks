# Task brief: safer migrations (2026-08-09)

From the saga lead, via the branch-seeded brief mechanism. You own this branch from pickup; the saga
lead reviews; the operator merges. Delete this file before the PR goes ready.

## Charter

Operator ruling (2026-08-09): database migrations get a safety net as table-stakes UX, landing
before the 0.14.0 tag. This is a full SDD effort (FRD, HLA, plan, LLDs as needed) with phased
artifact review; the saga is `docs/sdd/2026-08-04-next-steps/`.

The feature, in operator terms:

1. **Pre-migration notice and backup offer.** When a command finds the schema below latest, it says
   a migration is about to happen and offers to back up first. Interactive: prompt. Non-interactive:
   back up automatically by default, with a config setting to opt out. Prompting is the interactive
   courtesy; the automatic backup is the safety mechanism.
2. **Backup on demand.** A CLI command that backs up the state database whenever the operator wants,
   not only at migration time.
3. **Restore.** A CLI command that restores from a chosen backup, and a migration-failure error
   message that prints the exact restore invocation — the failure path suggests its own remedy.

Design pointers (yours to confirm at HEAD, not to take on faith):

- Use SQLite's online backup API; it handles WAL correctly for free. Timestamped backups in the
  state directory with a small retention window.
- SQLite DDL is transactional, so a failed migration usually rolls itself back. Say this in the FRD:
  the backup's real value is successful-but-wrong migrations and **version rollback** (an operator
  stepping back from 0.14 to 0.13 needs a pre-migration backup to return to), not crash recovery.
- Doctor's non-migrating posture (PR #462) is adjacent: its below-latest message ("a normal
  Agentworks command that opens state will migrate it") is the surface your notice wraps, and its
  `db/schema.py` gate plus `Database(read_only=True)` open are the reading primitives you build on.
  Coordinate wording so doctor and the pre-migration notice teach the same model.

## Constraints

- **Scope discipline — read this twice.** Two efforts in this saga were scope-corrected on
  2026-08-09 for machinery no owner priced (doctor's hostile-filesystem snapshot subsystem; wave 3's
  traceback-elimination fences). The contract here is user-facing safety UX. Explicitly out of
  scope: hostile-filesystem defense, atomicity proofs beyond what the SQLite backup API provides,
  backup encryption, remote/off-host backup targets, and any state beyond the SQLite database. If
  you find yourself building ownership or verification machinery, stop and raise it to the saga lead
  before writing more.
- **CLI command home**: the resource-CLI grammar break (describe/explain/graph) is being settled
  with the operator concurrently, and where backup/restore live (a new noun group vs. an existing
  one) is part of that conversation. Propose a home in your FRD with rationale, and confirm it with
  the saga lead before implementation. Completions must follow whatever lands (standing rule).
- **Timing**: lands before the 0.14.0 tag (operator ruling). Coordinate with the pre-0.14 gate list
  in the saga's phasing ledger.
- **Config surface**: the opt-out setting (and any retention knob) goes in `sample-config.toml` with
  the standing comment/organization rules; the upgrade guide and guide topics that teach migration
  behavior (`concept-migration`) must reflect the new flow in the same PR that changes it.
- Saga vocabulary throughout; message-signatures and Agentworks-Session trailer rules apply; the
  always-consider rules (docs, sample config, completions, SDD artifacts) apply as ever.

## Definition of done

An operator upgrading across a schema change is told before migration happens, gets a backup by
default in automation and by choice interactively, can back up and restore on demand, and is handed
the exact restore command when a migration fails; doctor and the migration notice teach one model;
config, completions, docs, and guide are in lockstep; gates green; SDD locked truthfully.

-- agw-next-steps (saga lead session)
