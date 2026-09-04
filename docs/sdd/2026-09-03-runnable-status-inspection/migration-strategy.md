# Runnable Status Inspection: Migration Strategy

- Status: Design
- Date: 2026-09-03
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [status-observation-lld.md](./status-observation-lld.md)

## Migration objective

Move from one default-live list and two local lists to one consistent opt-in grammar without
carrying dual manager semantics. The 0.18.0 implementation changes the session default, adds VM and
console status enrichment, repairs describe observation safety, and completes the internal
`OK`-to-`RUNNING` rename atomically.

There is no database migration, data rewrite, capability-version bump, or persisted status cache.

## Current-state inventory

Snapshot at `3f97ea3cd357582ceb71c2c065ad23db8d08379d` on 2026-09-03:

| Surface              | Current state                                                                 |
| -------------------- | ----------------------------------------------------------------------------- |
| VM list              | Local DB projection; no live flag or live status field                        |
| Session list         | Live by default; visible `--no-status`; activation-gated; unbounded SSH probe |
| Console list         | Local DB projection; no live flag or live status field                        |
| VM describe          | Live provider status without activation                                       |
| Session describe     | Live tmux status through an activation gate                                   |
| Console describe     | Configured DB state only                                                      |
| Session domain enum  | `OK`, `STOPPED`, `RESIDUAL`, `BROKEN`, `UNKNOWN`                              |
| VM platform contract | Version 1 with required read-only `status(vm, ctx)`                           |
| Harness contract     | Version 1; not involved in runtime observation                                |

Static scope at the snapshot:

- 15 production/test files reference `SessionStatus.OK`.
- 6 production/test files reference the Python name `no_status`.
- 15 code/doc files reference the CLI spelling `--no-status`.
- Existing focused list coverage spans at least 8 VM, 12 session, and 8 console test files.

The counts are planning evidence, not a permanent guard. Implementation performs a fresh residual
scan before handoff.

## Before and after

### Human commands

```text
0.17 and earlier                    0.18
----------------                   ----
agw vm list                        agw vm list
                                   agw vm list --status

agw session list                   agw session list
agw session list --no-status       agw session list --status

agw console list                   agw console list
                                   agw console list --status
```

Plain list becomes the inventory question everywhere. `--status` becomes the live question
everywhere.

### Machine output

```text
session.list row before:
  ... status: running|stopped|residual|broken|unknown|unavailable

session.list row after:
  same field and vocabulary; plain list emits unavailable,
  --status emits a live state

vm.list row after:
  existing fields ..., observed_status: null|running|stopped|deallocated|unknown,
  status_disposition: null|manual|idle

console.list row after:
  existing fields ..., status: unavailable|running|stopped|residual|unknown
```

New VM and console fields are appended to their row projections. No existing key changes name or
type.

## Transition mechanics

### 1. Add positive status selection at the CLI boundary

Add `--status` to all three list commands and thread `include_status` through services. Change
completions, help, and examples in the same commit. Reject status with names-only before service
work.

### 2. Isolate and replace session live-list orchestration

Build the non-activating bounded observer first. Move session list and describe to it, then remove
list from the activation-boundary documentation and tests. This sequence keeps one live-status
implementation at every commit.

The low-level session batch probe remains reusable by console `--all-running`; only its timeout,
TTY, result completeness, and enum naming change.

### 3. Add console observation

Extract the canonical/staging classifier from lifecycle probing, add the resource-local batch
observer, and join it into list and describe. Existing lifecycle calls reuse the pure classifier but
retain their strict gated and mutating policy.

### 4. Add VM list observation

Reuse the existing vm-platform version-1 status operation through one non-gated multi-VM
composition. Extract shared VM status/disposition projection so list and describe cannot drift.

### 5. Complete nomenclature cutover

Rename `SessionStatus.OK` to `RUNNING` across production code, tests, console `--all-running`, help,
comments, and docs. This is an in-place source change with no alias member. An alias would keep the
false vocabulary available to new code and teach two names for one state.

### 6. Keep one bounded CLI compatibility shim

In 0.18.0 only, hidden `session list --no-status`:

- conflicts with `--status`;
- emits the existing suppressible deprecation notice;
- dispatches plain local session list; and
- is absent from canonical help and completion.

No manager argument, service branch, or status policy is preserved for it. The option is removed in
0.19.0. The lifecycle release-sequencing cleanup establishes the same rule for 0.17 compatibility
grammar: 0.18 accepts and warns, 0.19 retires.

### 7. Update permanent collateral with behavior

Implementation updates or creates the 0.18 upgrade guide, active command reference, CLI overview,
session-status guide, lifecycle guidance, completion metadata, and capability docs whose examples or
safety statements change. Historical locked SDD bodies remain unchanged.

## Worked example: session automation

An automation currently finds running sessions with:

```console
agw session list --output json
```

Under 0.18.0 this returns the same rows and field shape, but every row has `"status":"unavailable"`
because no live observation was requested. The migrated command is:

```console
agw session list --status --output json
```

This makes the cost and remote dependency explicit. If one VM is unreachable, its affected rows are
`unknown`; other VMs retain their observed states. The command does not start the unreachable VM.

An automation already using `--no-status` can remove the flag. In 0.18.0 it still runs with a
deprecation notice unless globally suppressed. In 0.19.0 the stale option is an ordinary usage
error.

## Compatibility matrix

| Invocation                 | 0.17        | 0.18                          | 0.19                 |
| -------------------------- | ----------- | ----------------------------- | -------------------- |
| `session list`             | live        | local                         | local                |
| `session list --status`    | usage error | live, non-activating          | live, non-activating |
| `session list --no-status` | local       | hidden deprecated local alias | usage error          |
| `vm list --status`         | usage error | live, non-activating          | live, non-activating |
| `console list --status`    | usage error | live, non-activating          | live, non-activating |

No compatibility promise allows installed old completion scripts to trigger live status. Their
names-only calls remain recognized and local. If an old script includes `--no-status`, the 0.18
parser accepts it; the canonical generated scripts no longer emit it.

## Risks and safeguards

### Default behavior breaks status-dependent scripts

Safeguard: preserve the JSON field and unavailable meaning, document the required `--status`
addition in the 0.18 upgrade guide and release notes, and keep the compatibility flag for scripts
that already requested the cheap path.

### Observation accidentally activates a VM

Safeguard: introduce dedicated non-gated observers, delete list/describe references from activation
boundaries, and instrument activation, platform start, repair, and DB-write seams in tests.

### One unreachable guest or dispatched provider stalls the fleet

Safeguard: emit progress before dispatch, use a 10-second one-attempt guest probe, retain finite
parallelism, initialize requested rows to unknown, and isolate failures by VM or provider site.

Shared VM registry, preflight, and credential resolution remain one all-or-nothing setup boundary.
Their expected failure leaves all selected VM observations unknown rather than introducing repeated
prompt sessions. Inventory rows remain available.

### Provider clients are not thread-safe

Safeguard: share one credential resolution but serialize status calls within each bound site;
parallelize only independent site groups after setup. Do not add a speculative batch capability.

### Console absence is inferred from a transport error

Safeguard: retain the existing tmux diagnostic classifier, enumerate one complete session-name
snapshot per VM, and compare validated canonical and staging names by exact equality. Only an
authoritative server result establishes absence.

### JSON v1 silently changes meaning

Safeguard: retain the broad JSON v1 consumer meaning of session `unavailable`, establish only a
narrower 0.18 producer invariant, use existing VM describe names and null carrier, keep console
fields additive, and test explicit requested-unknown projection and ordering.

### Release chronology drifts again

Safeguard: every artifact names 0.18.0 introduction and 0.19.0 compatibility removal. The plan has a
dedicated residual scan for stale 0.19-introduction or 0.20-removal language before handoff.

## Rollback

Before merge, ordinary git reversion restores the old behavior because no data is migrated. After a
0.18 release, rolling back only the CLI behavior would reintroduce implicit remote side effects and
would change automation semantics again. Prefer a corrective forward release that retains the
canonical `--status` grammar and fixes the observer.

No database restore is required for either path.
