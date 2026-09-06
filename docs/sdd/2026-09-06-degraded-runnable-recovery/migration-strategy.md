# Degraded Runnable Recovery: Migration Strategy

- Status: Design
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Current to target behavior

| Surface                     | Current                                              | Target                                 |
| --------------------------- | ---------------------------------------------------- | -------------------------------------- |
| plain session list          | any missing workspace or VM fails selection          | every stored selected row projects     |
| session list status         | structural failure aborts before healthy observation | orphan unknown, healthy peers observed |
| console list status         | missing VM aborts before healthy observation         | orphan unknown, healthy peers observed |
| focused operations          | strict relationship validation                       | unchanged                              |
| migration foreign-key check | raw SQLite error possible at direct boundary         | typed Agentworks state error           |

## Delivery sequence

This is an in-place code transition on one PR; it has no stored-data migration.

1. Lock the recovery contract and JSON v1 limit in reviewed SDD artifacts.
2. Add focused failing tests for incomplete current-schema state and stale-schema migration refusal.
3. Make session list projection nullable only at the internal human-list boundary and retain strict
   JSON v1 projection before observation.
4. Partition session and console rows in their list services before calling unchanged strict
   observers.
5. Translate post-step foreign-key check failures at the database boundary without weakening safe
   open recovery handling.
6. Update permanent docs and the dated locked-SDD correction.
7. Run private reviews, full local gates, isolated shipped-CLI validation, hosted CI, and authorized
   published feedback rounds before readiness.

## Compatibility

No CLI spelling changes. No deprecation window is needed. No schema or capability version changes.

The machine contract does not change. Existing `sessions[]` items retain `vm_name: string`, the
collection retains every selected session, and every other field meaning and type stays fixed. A
missing workspace still makes JSON v1 fail atomically because no truthful string VM name can be
emitted. A missing VM row remains representable when the workspace preserves its VM name.

## Rollback

The code can be rolled back without transforming the database. Orphan rows will again make some list
operations fail, but no new persisted shape is introduced. Any operator database used for live
testing is backed up first; tests do not repair or delete production state.

## Risks and safeguards

- **Risk: tolerance leaks into mutation.** Partition only in list services; keep shared observers
  strict and verify one focused path per resource.
- **Risk: unknown is mistaken for stopped.** Initialize orphans to the existing `UNKNOWN` domain
  value and never invoke absence classifiers without guest evidence.
- **Risk: JSON sentinel ambiguity.** Keep missing VM names out of the existing string field; do not
  serialize `-` or another sentinel.
- **Risk: migration safety weakens.** Preserve foreign-key checks and checkpoint ordering; change
  only the exception boundary.
- **Risk: machine recovery breaks v1 consumers.** Retain strict JSON projection and validate it
  before optional live observation.
- **Risk: recovery reads mutate state.** Retain write-seam tests around both plain and status lists.

-- agw-ns-onboard-disco
