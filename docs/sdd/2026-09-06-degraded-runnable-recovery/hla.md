# Degraded Runnable Recovery: High-Level Architecture

- Status: Design
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)

## Architectural stance

Recovery tolerance belongs in list services, not in the database model, shared observers, or
lifecycle helpers. Database rows remain truthful records of what is stored. List projection joins
them when possible, represents an unavailable derived fact explicitly, and partitions observable
from unobservable rows before calling a strict observer. Focused commands continue through strict
`_require_*` helpers.

```text
persisted rows
    |
    +-- list projection ---- best-effort structural join ---- output facts
    |                                                   \
    |                                                    +-- optional healthy-only observation
    |
    +-- describe/lifecycle -- strict structural join -------- typed failure or operation

stale schema
    +-- migration step -- foreign_key_check -- checkpoint OR typed refusal
```

The design does not introduce a generic runnable abstraction. Sessions and consoles share a failure
policy, but their stored relationships and guest evidence remain resource-owned.

## Components and responsibilities

### Session inventory projection

`session_listing` performs a best-effort local join for each selected `SessionRow`:

| Stored relationship          | Output workspace | Output VM         | Status eligibility |
| ---------------------------- | ---------------- | ----------------- | ------------------ |
| workspace and VM exist       | stored name      | workspace VM name | eligible           |
| workspace exists, VM missing | stored name      | workspace VM name | unknown            |
| workspace missing            | stored name      | null / human `-`  | unknown            |

The internal list projection permits `vm_name: str | None`; `SessionDescription.vm_name` stays `str`
because the focused description is strict. This avoids contaminating lifecycle APIs with
recovery-only nullability.

The human projection uses `-` only as display. JSON v1 does not widen the existing string
`sessions[].vm_name` field or change the meaning of `{sessions}`. Rows with a known stored VM name
stay representable, including when the VM row is missing. A selection containing a row with no
derivable VM retains a typed failure and emits no JSON document. The JSON path requests this strict
local projection before any optional live observation.

### Session status partition

`session_listing` owns the forgiving partition:

1. initialize every selected session to `UNKNOWN`;
2. resolve its workspace and VM with non-raising lookups;
3. leave structurally incomplete rows unknown and exclude them from guest grouping;
4. call the unchanged strict `observe_session_statuses` with only complete rows; and
5. merge those returned statuses into the initialized map.

The shared observer remains strict for lifecycle, console-mutation, workspace-rehome, and other
non-list callers. Recovery tolerance cannot silently leak into future mutation code.

### Console status partition

Console rows store `vm_name` directly. `console_listing` initializes every selected status to
`UNKNOWN`, partitions rows by whether `db.get_vm` succeeds, sends only complete rows to the
unchanged strict `observe_console_statuses`, and merges the result. `console_description` and
lifecycle therefore retain their current strict behavior without compensating checks.

### Human projection

The shared table renderer receives `session.vm_name or "-"`; alignment remains centralized there.
Status warnings group unknown rows with known VM names as today. Sessions without any derivable VM
stay in a separate sequence and render in a distinct clause outside the VM-group map. This avoids
colliding with any valid VM name. The warning does not claim connectivity failure; `unknown` covers
both structural and operational inability to observe.

Progress announces the number of selected resources and the number of complete VM boundaries that
may receive live work. This keeps the message honest when some selected sessions have no VM.

### Filter boundaries

Name-filter validation remains unchanged. Workspace and VM filters are relational queries and do not
select rows disconnected from that relationship. Agent/admin filters can still select orphaned
sessions using fields stored on the session. Plain unfiltered list and names-only remain the broad
recovery inventory.

The architecture does not add special filter syntax for orphans. That would expand grammar and query
semantics beyond the recovery defect.

### Migration error boundary

`Database._migrate` retains `PRAGMA foreign_key_check` after each step. If rows are returned, it
raises a typed `StateError` carrying `entity_kind="database"`, the failed target version, and a hint
to restore an automatic or manual backup or repair the inconsistent relationship before retrying.

`MigrationBlockedError` is not used for this branch. That subtype promises a precondition failure
before schema or data changes, while the post-step foreign-key check can occur after SQLite DDL has
changed the live file. Through the safe opener, the existing migration-failure wrapper may replace
the detail with its stronger partial-migration warning and exact backup recovery command. Direct
`Database` construction still receives an Agentworks state error. Both paths remain typed and
truthful about possible partial change.

The schema-version row is inserted only after a clean check, exactly as today. No violation is
ignored, deleted, or auto-repaired.

## Data and machine contracts

No stored schema, capability contract, or JSON v1 shape changes.

The ordinary session list JSON v1 item remains unchanged:

```json
{
  "name": "example",
  "workspace_name": "example-workspace",
  "vm_name": "example-vm",
  "template": "claude-auto",
  "harness_integration": "claude-code",
  "mode": "agent",
  "agent_name": "example-agent",
  "status": "unavailable"
}
```

When the workspace exists but its VM does not, the stored VM name remains a string and requested
status becomes `unknown`. When the workspace itself is missing, the command returns a typed error
before observation because no truthful string VM name exists. Human and names-only output remain the
complete recovery inventory for that case. Healthy row vocabulary and console JSON remain unchanged.

## Failure matrix

| Condition                  | Plain list          | List `--status`        | Describe/lifecycle      | Stale migration                  |
| -------------------------- | ------------------- | ---------------------- | ----------------------- | -------------------------------- |
| session workspace missing  | row, VM `-`         | row unknown            | typed failure           | typed refusal if checked         |
| session VM missing         | row, stored VM name | row unknown            | typed failure           | typed refusal if checked         |
| console VM missing         | row, stored VM name | row unknown            | typed failure           | typed refusal if checked         |
| healthy peer beside orphan | row                 | independently observed | unchanged               | n/a                              |
| transport unavailable      | row                 | row unknown            | existing focused policy | n/a                              |
| migration FK violation     | n/a                 | n/a                    | n/a                     | no checkpoint, typed state error |

The plain-list column describes human and names-only recovery. JSON v1 additionally requires a
derivable string VM name and fails atomically when a selected session lacks one.

## Security and safety

- Recovery projection does not infer a target, username, transport, or authority from stale names.
- Orphans never trigger guest work.
- Status remains read-only and does not reconcile persisted runtime evidence.
- Migration validation remains fail-closed.
- Error text may report table and relationship metadata but not row payloads or secrets.

## Permanent homes

Implementation behavior lives in the session and console query/status modules and migration opener.
The operator contract lives in `cli/command-reference.md` and `docs/guides/runnable-status.md`. The
JSON recovery limit lives in the command reference. The prior runnable-status SDD receives a dated
correction in its lockfile because its current post-lock limit is superseded.

-- agw-ns-onboard-disco
