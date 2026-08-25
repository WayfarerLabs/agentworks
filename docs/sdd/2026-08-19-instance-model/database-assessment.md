# Instance Model and State: Database Assessment

- Status: Revised for saga-lead re-review
- Date: 2026-08-21
- Last revised: 2026-08-23
- Requirement: R1 in `frd.md`
- Scope: the current persistence estate and the storage needs of R3 through R5
- Code basis: schema version 31

## Executive assessment

Agentworks has one SQLite state database and already keeps runtime SQL behind a typed facade. The
public `Database` class owns connection setup and all ordinary CRUD SQL. Row retrieval normally
returns dataclasses, while counts, existence checks, and scalar projections return typed primitives.
Four composite but positional returns are worth naming: `check_schema()` returns a three-element
status tuple, `list_granted_workspaces_with_types()` returns grant fact triples,
`list_consoles_with_counts()` returns console/count pairs, and `snapshot_vm_backup_data()` returns a
six-element backup snapshot tuple (`cli/agentworks/db/database.py:188-209`,
`cli/agentworks/db/database.py:609-627`, `cli/agentworks/db/database.py:848-904`,
`cli/agentworks/db/database.py:1051-1080`). Raw SQLite outside the class is confined to migration
and backup internals. These exceptions make parts of the facade less self-describing, but they do
not create SQL bypasses or justify an ORM. The smallest R2 is recognition of `Database` as the
existing repository boundary, plus one concrete, typed instance-state repository inside the
`agentworks.db` package for the new cross-kind record.

The new repository should expose consumer-shaped methods for desired instance overlays, applied
state, and drift-oriented batch reads. Its persisted carrier should identify an instance by kind and
name, discriminate the record slice, carry a versioned JSON payload, and record operation and time.
The public API should remain typed; callers should never read or write arbitrary records or SQL.
This gives wave 4 a stable place for integration applied-state without inventing integration fields
now, and it leaves room for later artifact-ownership records without designing those records here.

Four findings constrain the design:

1. The VM row already stores one applied slice: `insert_vm()` writes the resolved CPU, memory, disk,
   and swap values at create time, and no update method rewrites them
   (`cli/agentworks/vms/manager/lifecycle.py:169-181`,
   `cli/agentworks/vms/manager/lifecycle.py:341-359`, `cli/agentworks/db/database.py:292-374`).
   Those columns do not record when or by which operation the values were applied, and no current
   column covers the SSH identity or anything reinit reapplies. Under the no-backfill ruling,
   historic rows do not acquire synthesized provenance and remain not recorded for the new
   applied-state comparison.
2. Missing applied records are ordinary unknowns. They are not matches and not drift. Workspace
   repair is not full convergence, so it cannot create a complete applied record merely because it
   returned successfully.
3. `Database.transaction()` is not a general composition guarantee today. Only methods using
   `_commit_unless_in_tx()` defer commits, while many older methods call `commit()` directly
   (`cli/agentworks/db/database.py:134-186`, `cli/agentworks/db/database.py:292-393`). New state
   writes need explicit atomic repository operations, and any lifecycle write combined with them
   must be made transaction-aware deliberately.
4. Password protection is not a distinct identity and must not become an error condition. An
   encrypted `openssh-key-v1` file exposes its public blob outside the encrypted section, so it is
   fully verifiable without unlocking. Other encrypted formats, including legacy PEM, may not expose
   the public identity without a passphrase. The repository stores a public fingerprint or an
   explicit unknown result, never private key material. It must not use an adjacent public-key file
   as evidence. How an ssh-agent-held identity participates remains outside this SDD and open.

No storage implementation should begin until the saga lead accepts this assessment.

## Persistence topology

### Boundary and ownership

`Database` is the ordinary state API (`cli/agentworks/db/database.py:54-55`). Its row models live in
`cli/agentworks/db/models.py:1-184`, and SQLite-row conversion lives in
`cli/agentworks/db/converters.py:1-244`. A source scan finds runtime `sqlite3.connect()` and
`execute()` calls only in:

- `cli/agentworks/db/database.py`, for schema lifecycle and typed CRUD;
- `cli/agentworks/db/migrations.py`, for forward schema transitions; and
- `cli/agentworks/db/backup.py`, for classification, locking, backup, and restore.

The CLI's writable composition root is `get_db()` (`cli/agentworks/cli/_helpers.py:33-99`). The only
production construction site for a writable `Database` is `_construct_writable_database()`
(`cli/agentworks/db/backup.py:273-277`). Doctor opens current state read-only
(`cli/agentworks/doctor_state.py:18-29`), and resource show opens a lazy read-only snapshot
(`cli/agentworks/resources/graph_query.py:266-292`). Completion also uses an ordinary WAL-aware
read-only connection with a short timeout (`cli/agentworks/db/backup.py:280-315`).

This is already the important repository property: domain and command call sites name typed
operations, not tables or statements.

### Current table inventory

The current schema is version 31. Its ten tables and columns are reconstructed by the schema
sentinel history (`cli/agentworks/db/migrations.py:631-758`). Historic `vm_git_host_keys`, `tasks`,
and `vm_hosts` tables are removed (`cli/agentworks/db/migrations.py:722-726`) and are not part of
the current estate.

| Table                    | Current shape                                                                                                                                                                                                                                                                               | Readers and query shapes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Writers and mutation shapes                                                                                                                                                                                                                                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`         | `version`, `applied_at`                                                                                                                                                                                                                                                                     | Database open, schema inspection, backup qualification, and restore validation read `MAX(version)` (`cli/agentworks/db/database.py:87-114`, `cli/agentworks/db/backup.py:512-521`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | `_migrate()` creates the table and appends one row after each successful version (`cli/agentworks/db/database.py:232-286`).                                                                                                                                                                                                                        |
| `vms`                    | `name`, `site`, `template`, `extra_packages`, `provisioning_status`, `init_status`, `ssh_public_key`, `tailscale_host`, `cpus`, `memory_gib`, `disk_gib`, `swap_gib`, `admin_username`, `hostname`, `platform_metadata`, `operator_stopped`, `created_at`, `last_seen_at`, `admin_template` | Exact name and ordered list; resource usage filters list results by template, admin template, or site; managers join through workspace ownership; doctor lists all; VM inspection and backup read one VM and related rows (`cli/agentworks/db/database.py:328-334`, `cli/agentworks/vms/kinds.py:102-111`, `cli/agentworks/vms/kinds.py:158-168`, `cli/agentworks/vms/kinds.py:213-218`, `cli/agentworks/doctor_state.py:93-115`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | VM create inserts the as-provisioned row before backend creation; initializer and power operations update statuses, host, platform metadata, and stop state; delete manually removes dependents first (`cli/agentworks/vms/manager/lifecycle.py:341-366`, `cli/agentworks/db/database.py:292-393`).                                                |
| `workspaces`             | `name`, `vm_name`, `template`, `workspace_path`, `linux_group`, `created_at`                                                                                                                                                                                                                | Exact name; ordered list, optionally by VM; counts by VM and agent; resource usage filters the list by template; sessions and grants resolve workspace ownership (`cli/agentworks/db/database.py:433-478`, `cli/agentworks/workspaces/kinds.py:69-77`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Workspace create and copy insert after remote realization; rehome updates path; delete removes sessions and grants before the row (`cli/agentworks/workspaces/realize.py:114-126`, `cli/agentworks/db/database.py:416-460`).                                                                                                                       |
| `agents`                 | `name`, `vm_name`, `linux_user`, `created_at`, `template`, `grant_all`                                                                                                                                                                                                                      | Exact name; ordered list, optionally by VM; resource usage filters by template; grant and session flows look up agent ownership and use (`cli/agentworks/db/database.py:499-538`, `cli/agentworks/agents/kinds.py:80-88`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Agent create inserts after remote realization; reinit can change the selected template; grants toggle `grant_all`; delete removes the row after remote cleanup (`cli/agentworks/agents/realize.py:129-143`, `cli/agentworks/agents/manager/lifecycle.py:452-462`, `cli/agentworks/db/database.py:482-530`).                                        |
| `agent_workspace_grants` | `agent_name`, `workspace_name`, `grant_type`, `session_name`, `created_at`                                                                                                                                                                                                                  | Existence counts; lists by agent; distinct workspace names; grant-type aggregation; explicit-granter join to agents; counts by workspace (`cli/agentworks/db/database.py:586-660`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Grant commands insert and delete explicit rows; session create/delete manages implicit rows; agent and workspace deletion cascade or remove related rows (`cli/agentworks/db/database.py:542-584`, `cli/agentworks/agents/grants.py:81-192`).                                                                                                      |
| `sessions`               | `name`, `workspace_name`, `template`, `mode`, `created_at`, `updated_at`, `agent_name`, `created_workspace`, `socket_path`, `pid`, `boot_id`, `created_agent`, `harness_integration_state`                                                                                                  | Exact name; ordered list with optional workspace, agent, and VM filters; resource usage filters by template; console and secret projections traverse session relationships (`cli/agentworks/db/database.py:705-756`, `cli/agentworks/sessions/kinds.py:84-94`, `cli/agentworks/secrets/kinds.py:116-142`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Session create inserts the row, then records socket and PID; resume updates PID, boot ID, socket, and harness integration state; delete removes one row or all rows for a workspace (`cli/agentworks/sessions/manager/_create_roll.py:229-332`, `cli/agentworks/sessions/manager/_lifecycle.py:571-616`, `cli/agentworks/db/database.py:671-814`). |
| `consoles`               | `name`, `vm_name`, `created_at`, `updated_at`, `admin_shell`                                                                                                                                                                                                                                | Exact name; ordered list; one aggregate query returns a session count using a join plus a correlated `EXISTS`; resource usage treats every console as using the singleton named-console template (`cli/agentworks/db/database.py:818-904`, `cli/agentworks/sessions/kinds.py:144-165`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Console create/update/delete and membership mutations touch `updated_at`; delete cascades membership rows (`cli/agentworks/db/database.py:906-1033`).                                                                                                                                                                                              |
| `console_sessions`       | `console_name`, `session_name`, `position`, `shells`                                                                                                                                                                                                                                        | Ordered memberships by console; reverse lookup by session; console aggregate counts (`cli/agentworks/db/database.py:848-904`, `cli/agentworks/db/database.py:936-954`, `cli/agentworks/db/database.py:1001-1014`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | Add uses `MAX(position) + 1`; remove, reorder, and shell update mutate membership, with reorder explicitly transactional (`cli/agentworks/db/database.py:911-934`, `cli/agentworks/db/database.py:956-1000`, `cli/agentworks/db/database.py:1016-1027`).                                                                                           |
| `vm_events`              | `id`, `vm_name`, `event`, `detail`, `created_at`                                                                                                                                                                                                                                            | Ordered history for one VM, used by VM inspection and VM backup snapshot (`cli/agentworks/db/database.py:1044-1077`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Provisioning, initialization, backup, and rekey operations append events (`cli/agentworks/vms/initializer/driver.py:137-154`, `cli/agentworks/vms/initializer/driver.py:250-278`, `cli/agentworks/vms/backup.py:83-185`, `cli/agentworks/vms/manager/power.py:474-476`).                                                                           |
| `settings`               | `key`, `value`                                                                                                                                                                                                                                                                              | Exact key lookup. Doctor, SSH-config generation, VM inspection, and VM, workspace, agent, and session composition roots read the system slug (`cli/agentworks/db/database.py:397-404`, `cli/agentworks/doctor_state.py:32-59`, `cli/agentworks/ssh_config.py:138-140`, `cli/agentworks/vms/manager/inspect.py:528`, `cli/agentworks/vms/manager/_helpers.py:176`, `cli/agentworks/vms/manager/_helpers.py:386-398`, `cli/agentworks/vms/manager/lifecycle.py:667`, `cli/agentworks/workspaces/manager/_common.py:28-32`, `cli/agentworks/agents/manager/_common.py:58-62`, `cli/agentworks/agents/manager/lifecycle.py:116-152`, `cli/agentworks/agents/manager/lifecycle.py:488-512`, `cli/agentworks/sessions/manager/_create_build.py:93-235`, `cli/agentworks/sessions/manager/_lifecycle.py:287-345`, `cli/agentworks/sessions/manager/_scope.py:61-65`, `cli/agentworks/sessions/manager/_scope.py:276-294`). | Upsert by key. The first VM-create path records the operator's system-slug choice (`cli/agentworks/db/database.py:406-412`, `cli/agentworks/vms/manager/_helpers.py:163-208`).                                                                                                                                                                     |

Columns alone do not describe the deletion and lookup baseline. The current relational constraints
and explicit indexes are:

| Table                    | Keys, constraints, and indexes                                                                                                                                                                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`         | No primary key, uniqueness constraint, or explicit index. Migration code appends one row per completed version and consumers select `MAX(version)` (`cli/agentworks/db/database.py:232-240`).                                                                               |
| `vms`                    | Primary key `name`; no foreign keys or explicit indexes after the version-27 rebuild (`cli/agentworks/db/migrations.py:167-206`).                                                                                                                                           |
| `workspaces`             | Primary key `name`; `vm_name` references `vms(name)` without a delete action; no explicit index on `vm_name` (`cli/agentworks/db/migrations.py:586-601`).                                                                                                                   |
| `agents`                 | Primary key `name`; unique `linux_user`; `vm_name` references `vms(name) ON DELETE CASCADE`; no explicit index on `vm_name` (`cli/agentworks/db/migrations.py:383-398`).                                                                                                    |
| `agent_workspace_grants` | No primary or unique key and no explicit index, so duplicate grant rows are structurally possible. `agent_name` and `workspace_name` reference their owner tables with `ON DELETE CASCADE`; `session_name` has no foreign key (`cli/agentworks/db/migrations.py:452-469`).  |
| `sessions`               | Primary key `name`; `workspace_name` references `workspaces(name)` and `agent_name` references `agents(name)`, both without delete actions; agent-mode rows require a non-null `socket_path`; no explicit relationship indexes (`cli/agentworks/db/migrations.py:479-496`). |
| `consoles`               | Primary key `name`; `vm_name` references `vms(name) ON DELETE CASCADE`; no separate explicit index (`cli/agentworks/db/migrations.py:513-520`).                                                                                                                             |
| `console_sessions`       | Composite primary key `(console_name, session_name)`; unique `(console_name, position)`; both foreign keys cascade on delete. An explicit `idx_console_sessions_order` also covers `(console_name, position)` (`cli/agentworks/db/migrations.py:521-529`).                  |
| `vm_events`              | Autoincrement primary key `id`; `vm_name` references `vms(name)` without a delete action; explicit `idx_vm_events_vm_name` supports per-VM history (`cli/agentworks/db/migrations.py:339-348`).                                                                             |
| `settings`               | Primary key `key`; no other constraint or explicit index (`cli/agentworks/db/migrations.py:209`).                                                                                                                                                                           |

This asymmetry explains the manual delete order in `Database`: deleting a VM cascades agents and
consoles, but workspaces, sessions, events, and their dependent rows must be removed first.

### Query patterns already in use

The estate has six recurring query shapes:

1. Primary-key lookup for a named instance or setting.
2. Ordered whole-kind list, sometimes with one owner filter such as `vm_name`.
3. Relationship existence or count, especially grants and workspace ownership.
4. Reverse relationship lookup, such as consoles containing a session.
5. Small aggregate projections, such as console counts and grant types.
6. Consistent multi-table snapshots for resource show and VM backup.

Resource show's live-use projection intentionally holds a read transaction across its lazy queries
(`cli/agentworks/resources/graph_query.py:275-301`). Each kind currently loads a whole table and
filters it in Python (`cli/agentworks/vms/kinds.py:102-111`,
`cli/agentworks/workspaces/kinds.py:69-77`, `cli/agentworks/agents/kinds.py:80-88`). The VM backup
snapshot similarly loads all sessions, filters them in Python, and issues one grant query per agent
(`cli/agentworks/db/database.py:1051-1080`). These are acceptable at today's scale but are the wrong
shape for doctor to repeat per instance after applied-state arrives. The new repository needs batch
reads keyed by instance kind and record slice from the start.

## Concurrency and migration story

### Ordinary access

- Writable opens enable foreign keys and WAL, then migrate before returning
  (`cli/agentworks/db/database.py:119-129`). SQLite serializes writers; there is no
  application-level write queue.
- Read-only opens use `mode=ro`, validate the exact current version, enable foreign keys, and read
  the WAL rather than pretending the database is immutable (`cli/agentworks/db/database.py:79-118`).
- `read_transaction()` provides one SQLite snapshot for a read-only connection and refuses nesting
  (`cli/agentworks/db/database.py:160-181`). Resource show relies on this property.
- `transaction()` tracks nesting in Python, but its atomicity extends only across methods that call
  `_commit_unless_in_tx()` (`cli/agentworks/db/database.py:134-186`). The console methods use that
  helper; much of the older VM, workspace, agent, grant, session, and event code commits directly.
- `snapshot_vm_backup_data()` starts and commits a read transaction on a writable connection rather
  than using the read-only snapshot API (`cli/agentworks/db/database.py:1051-1080`). It is a special
  composition root, not a reusable repository pattern.

### Migration safety

Migrations are a forward-only map from integer version to SQL or a Python callable
(`cli/agentworks/db/migrations.py:245-625`). `_migrate()` disables foreign keys for a run, executes
one version at a time, checks `foreign_key_check`, records the version, and commits each version
(`cli/agentworks/db/database.py:249-288`). The per-version checkpoint makes retry resume correctly,
but a failure inside one version can leave partial DDL. This residual is documented in the code and
means migration safety cannot be described as fully transactional.

The operator-facing open path is materially safer than the migration primitive alone:

- `inspect_schema()` is the canonical absent/current/stale/future/busy/malformed classifier
  (`cli/agentworks/db/backup.py:114-171`).
- `prepare_database_open()` qualifies and snapshots a stale baseline under a separate migration lock
  (`cli/agentworks/db/backup.py:174-220`).
- `open_database_safely()` reacquires the lock, rechecks that baseline, optionally creates an online
  pre-migration backup, and associates recovery guidance with failures
  (`cli/agentworks/db/backup.py:222-270`).
- Restore validates the exact completed table and column shape before copying
  (`cli/agentworks/db/backup.py:442-545`). SQLite's backup API supplies consistent online copies
  with a fixed deadline (`cli/agentworks/db/backup.py:589-623`).

Any new operator-state table must use this path. The migration should be additive if possible, must
leave old rows absent rather than backfilled from declarations, and must update exact restore
validation in the same change. Because the migration runner disables foreign keys,
`ON DELETE CASCADE` does not clean referencing rows during a table rebuild. A rebuild that removes
parent keys must delete referencing rows explicitly before dropping the old table, as version 26
does; a rebuild that preserves every referenced key may rely on the final `foreign_key_check`, as
version 28 documents (`cli/agentworks/db/migrations.py:533-602`).

## Concrete pain points

### P1: the transaction contract is only partially true

The `transaction()` docstring promises that CRUD methods defer per-call commits
(`cli/agentworks/db/database.py:134-141`), but `_commit_unless_in_tx()` is not used by the older
CRUD families. For example, VM insert and update methods commit immediately
(`cli/agentworks/db/database.py:292-374`), as do event inserts
(`cli/agentworks/db/database.py:1037-1042`). Code that wrapped those calls and an applied-state
write in `transaction()` would still expose a torn result. This is the highest-risk issue for R3.

Recommendation: give each new lifecycle checkpoint one repository operation that writes all of its
state slices atomically. Convert an existing instance-row mutation to transaction-aware commit only
when that same checkpoint must compose with it. Do not claim that all existing CRUD is now
transactional without converting and testing every affected method. The broader contract mismatch is
tracked in [issue 635](https://github.com/WayfarerLabs/agentworks/issues/635) with its root cause
and representative call sites.

### P2: schema classification has several readers

The simplification assessment identified one canonical classifier and several partial re-readers
(`docs/sdd/2026-08-12-simplification-pass/findings.md:297-307`). HEAD has improved `check_schema()`
by delegating to `inspect_schema()` (`cli/agentworks/db/database.py:188-209`), but read-only open,
writable future-version rejection, migration, and backup-source validation still read and interpret
`schema_version` separately (`cli/agentworks/db/database.py:79-114`,
`cli/agentworks/db/database.py:211-246`, `cli/agentworks/db/backup.py:512-521`). Their contexts need
different error contracts, so one helper does not necessarily replace them all, but adding another
classifier would worsen the estate.

Recommendation: reuse `inspect_schema()` and the safe-open entry path. Consolidate only a duplicate
that the new migration actually touches. The known unclassified open seams remain tracked in
[issue 505](https://github.com/WayfarerLabs/agentworks/issues/505).

### P3: exact schema validation shadows the migration ladder

`SCHEMA_SENTINELS` is a second, hand-maintained history of every migration's table and column
effects (`cli/agentworks/db/migrations.py:628-758`). Restore correctness depends on it
(`cli/agentworks/db/backup.py:467-509`), and a parity test replays migrations to catch drift. This
is valuable validation backed by duplicate maintenance, not a reason to weaken restore checks.

Recommendation: if the new table makes sentinel maintenance materially worse, derive completed
shapes from migration replay and retain restore's exact-schema behavior. Otherwise, add the one
version entry. A broad migration rewrite is not required for R3; derivation is tracked in
[issue 633](https://github.com/WayfarerLabs/agentworks/issues/633) with the dependent restore and
test call sites.

### P4: one class contains unrelated repositories and mixed query quality

`Database` is 1,080 lines and owns connection lifecycle, migrations, ten tables, aggregate queries,
and a backup snapshot. Some query methods push selection to SQLite, while resource usage and VM
backup load broad sets and filter or fan out in Python. The problem is not lack of abstraction at
call sites; it is that another cross-kind subsystem would enlarge a mixed owner and encourage more
broad reads.

Recommendation: keep the connection owner and existing CRUD where they are. Put the new cross-kind
record and its codecs in one concrete `InstanceStateRepository` beside `Database`, with construction
and transaction ownership controlled by `Database`. Do not add a protocol, unit of work, generic
query builder, or second connection.

### P5: the VM row mixes applied hardware with legacy columns

`vms.cpus`, `memory_gib`, `disk_gib`, and `swap_gib` are the opposite of dead compatibility fields:
VM create writes the resolved provisioning values once, and no update path rewrites them
(`cli/agentworks/vms/manager/lifecycle.py:169-181`,
`cli/agentworks/vms/manager/lifecycle.py:341-359`, `cli/agentworks/db/database.py:292-374`). They
are the current authority for as-provisioned hardware. Copying those values into a second writable
applied-state payload would create two sources of truth whose disagreement would be
indistinguishable from the drift R3 exists to report.

`vms.extra_packages`, `vms.ssh_public_key`, and `vms.last_seen_at` remain in the version-31 shape
(`cli/agentworks/db/migrations.py:635-647`). `insert_vm()` writes none of them
(`cli/agentworks/db/database.py:292-326`). The converter still exposes `extra_packages` and
`last_seen_at`, while it drops `ssh_public_key` entirely (`cli/agentworks/db/converters.py:52-76`).
`update_vm_last_seen()` exists (`cli/agentworks/db/database.py:369-374`) but has no production
caller. The live VM inspector still renders `last_seen_at` when present
(`cli/agentworks/vms/manager/inspect.py:654-655`).

Recommendation: keep the VM row as the applied hardware-value authority. R3 records the missing
operation and time proof for new hardware application, and R5 composes that proof with the row
values. It does not duplicate the values. Historic rows receive no provenance backfill and remain
not recorded under the operator ruling. Never repurpose the legacy columns for R3. Removing
write-dead columns is reasonable only if the design phase prices the table rebuild and test trim as
cheaper than carrying them; it is not a prerequisite. The deferred rebuild is tracked in
[issue 634](https://github.com/WayfarerLabs/agentworks/issues/634) with each current reader and
writer.

### P6: relationship cleanup is inconsistent

Some child relations use `ON DELETE CASCADE`, while `delete_vm()` and `delete_workspace()` also
issue long manual delete sequences (`cli/agentworks/db/database.py:376-393`,
`cli/agentworks/db/database.py:455-460`). A generic cross-kind state table cannot express a normal
foreign key to four different owner tables.

Recommendation: the design must specify owner deletion and orphan handling explicitly. The smallest
viable model is a polymorphic `(instance_kind, instance_name)` key whose typed delete hooks remove
state in the same transaction as the owner row. That requires converting the four owner-deletion
paths to transaction-aware commit and is a justified R2 cleanup, not an unrelated transaction
rewrite. It should not introduce a synthetic universal instance table merely to obtain a foreign
key. Owner-scoped reads may remain resilient to unrelated corruption, but repository validation or
doctor must report every orphaned record as database damage. An absent applied record is a
legitimate unknown; a present record with no owner is an invariant violation.

### P7: the database test estate is costly and contains inherited prose pins

The eight direct database suites contain 3,369 lines and 117 tests:

- `cli/tests/test_db.py`
- `cli/tests/test_database_backup.py`
- `cli/tests/test_database_cli.py`
- `cli/tests/test_database_migration_safety.py`
- `cli/tests/test_db_migration_harness_integration_state.py`
- `cli/tests/test_db_migration_harness_state.py`
- `cli/tests/test_db_migration_vm_sites.py`
- `cli/tests/db/test_read_transaction.py`

The prior assessment records five real-process lock scenarios, long join bounds, one multi-second
sleep, and roughly thirty authored-message pins
(`docs/sdd/2026-08-12-simplification-pass/findings.md:311-315`). The simplification re-scope assigns
database/persistence, resource show/inspect, and resource-attributable doctor checks to this effort,
including trim-to-standard for the surfaces actually rewritten
(`docs/sdd/2026-08-12-simplification-pass/message-2026-08-19-sweep-rescope.md:12-26`).

The primary transferred test files are `cli/tests/test_resource_show.py`,
`cli/tests/test_resource_list.py`, `cli/tests/test_doctor.py`, `cli/tests/test_doctor_cli.py`,
`cli/tests/test_doctor_env_and_secrets.py`, and `cli/tests/test_doctor_schema.py`. Kind-specific
resource and doctor checks also live beside their domains. The design and plan must name the exact
tests for each show, inspect, and doctor surface they rewrite; this assessment does not pre-commit
untouched cases merely because they share one of those files.

Recommendation: preserve behavioral migration, backup, lock, and snapshot coverage. When R2 changes
a test surface, remove prose-only assertions and redundant path permutations, replace wall-clock
waiting with controlled synchronization where practical, and retain derivation parity until the
sentinel shadow is actually removed. Tests in disjoint sessions, workspaces, consoles, and transport
surfaces remain the simplification effort's responsibility unless this effort rewrites them.

## Storage needs of R3 through R5

### R3: applied instance state

The VM vertical slice needs:

- exact lookup by `(instance_kind, instance_name, record_type, record_key)`;
- atomic replacement of the slices proven by one successful operation;
- a normalized, versioned JSON value so the stored effective spec survives model evolution;
- `recorded_at` and an operation discriminator such as `vm-create` or `vm-reinit`;
- batch listing by instance kind and record type for doctor and show surfaces;
- typed decoding that distinguishes absent, present, and malformed records; and
- deletion tied to instance lifecycle without assuming that every historic row has state.

Applied state is sliced rather than one all-or-nothing document. A VM create can record operation
and time proof for row-backed hardware plus stored values for slices such as the trusted SSH
identity when those become facts. Reinit replaces only what it actually reapplies. Sessions can
later record a resume-established slice. Workspace repair records no complete applied spec unless
its contract grows into full convergence. Absence remains unknown.

The VM hardware slice is split deliberately across existing and new storage without duplicating its
values. The VM row remains the value authority; a new applied record supplies operation and time
proof for hardware established after R3 ships. R5 treats a historic row with no such proof as not
recorded rather than manufacturing provenance from `created_at` or the current template.

The existing `_parse_vm_json()` converter is the error-quality precedent for typed decoding: absent
or empty legacy JSON gets an explicit fallback, while malformed JSON or a wrong payload shape raises
a typed `StateError` with entity context (`cli/agentworks/db/converters.py:79-108`). The new
repository keeps record absence distinct from a present payload and applies the same typed malformed
boundary rather than exposing raw decoder failures.

The proving SSH slice stores the public fingerprint of the identity actually selected for transport,
plus non-secret diagnostic context such as the configured private-key reference. It stores neither
private key material nor a passphrase. For `openssh-key-v1`, the safe non-interactive mechanism is
to parse the private file's unencrypted public blob directly and fingerprint that blob. Do not use
`ssh-keygen -lf PRIVATE_PATH`: when an adjacent `.pub` exists, OpenSSH prefers it, so a stale or
mismatched public file produces a fingerprint for the wrong identity and can create the exact false
pass this slice exists to prevent. `ssh-keygen -y -f PRIVATE_PATH` reads the authoritative private
identity but cannot derive an encrypted key without unlocking it.

Encrypted formats such as legacy PEM do not expose the public identity outside their encrypted
payload. When no non-interactive authoritative derivation exists, the result is unknown rather than
mismatch, pass, or unsupported. Password protection by itself is never the diagnosis, and the
unresolved ssh-agent selection question must not be encoded into the repository contract.

### R4: desired per-instance overlay

The overlay needs exact read, validated upsert, and delete for one instance, plus a way to enumerate
overlays for current-spec resolution and diagnostics. It is desired declaration, not evidence that
anything was applied. The repository API and record discriminator must keep desired overlay separate
from applied state even if both share one physical table.

Storage is the cheap part of R4. The costly dependency is the absent general layer-stack merge and
the four current per-kind mergers named in the FRD. R1 finds no database reason to split R4, but it
also provides no evidence that the merge generalization is small. Design must price that separately
and must route R4 rather than add a fifth merge if it is too large.

### R5: current and applied resolved specs

R5 writes nothing new. It needs:

- current resolved values and per-value provenance from the registry and layer stack;
- one exact overlay read while resolving a live instance;
- one exact applied-state read for live-instance show;
- batch applied-state reads for doctor drift reporting; and
- stable tri-state comparison: not recorded, match, or drift.

`resource show` already holds one read snapshot and preserves absent live state as an explicit
source state (`cli/agentworks/resources/graph_query.py:266-301`). The repository should participate
in that same connection and snapshot. It must not open a sidecar or a second database. Both doctor
and live-instance show must render an absent applied record as not recorded; omitting the row would
quietly imply agreement where the system has no evidence.

## Recommended minimum repository shape

### Physical record

Add one additive table for cross-kind instance records. The design may settle names, constraints,
and JSON envelope details, but the minimum semantic fields are:

| Field                            | Purpose                                                                                                                                         |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `instance_kind`, `instance_name` | Stable polymorphic owner identity for VM, workspace, agent, or session.                                                                         |
| `record_type`, `record_key`      | Separate desired overlay, applied spec slices, future integration applied-state, and later artifact records without predefining their payloads. |
| `payload_version`                | Select the typed decoder for the payload without colliding with the database's `schema_version` table.                                          |
| `value_json`                     | Canonical JSON whose typed payload model and codec belong to the consuming domain.                                                              |
| `recorded_at`                    | When the record became authoritative.                                                                                                           |
| `operation`                      | Which lifecycle operation established applied state; nullable only where the record is desired declaration rather than an applied fact.         |

The natural key is `(instance_kind, instance_name, record_type, record_key)`. Add an index
supporting `(instance_kind, record_type)` batch reads. Do not add columns for integration or
artifact concepts whose requirements do not exist yet.

### Public API

Expose typed consumer methods, not a public generic blob API. The initial surface should be no
larger than:

- get, put, clear, and list desired overlays;
- get an instance's applied slices;
- replace the applied slices established by one operation atomically;
- list applied slices or summaries for one instance kind.

Owner deletion is `Database`-owned infrastructure rather than a consumer operation. Its typed delete
paths use a private repository helper so record cleanup stays in the same transaction without
exposing state-only deletion as a public capability.

The concrete repository shares `Database`'s connection, read snapshot, migration gate, and
transaction. The repository owns SQL, the persistence envelope, record-type registration, and
canonical storage of the encoded JSON value. Each consuming domain owns its typed payload model and
codec, and passes only a validated encoded value across the repository boundary. Wave 4 adds its
payload model and codec in its own domain and registers a typed record type; it does not reopen
connection or table design.

### Deliberate non-expansion

R2 should not:

- migrate all existing CRUD merely for naming consistency;
- add an ORM, repository protocol, generic query language, unit-of-work hierarchy, or independent
  connection pool;
- backfill applied state from current templates, row columns, or the legacy public-key column;
- create a universal instance parent table solely for relational purity;
- solve key-agent selection or change SSH transport policy; or
- claim workspace repair is full convergence.

The justified adjacent cleanups are transaction correctness for lifecycle checkpoints that write new
state, transaction-aware owner deletion that removes it, and test trim on surfaces this effort
rewrites. Schema-classifier or sentinel consolidation is welcome only when it makes the required
migration simpler and safer in the same change.

## Saga-lead checkpoint disposition

The saga lead accepted the assessment's three architectural recommendations at head `d9646fcb`,
subject to the corrections incorporated in this revision:

1. One physical typed-record store satisfies the wave-2 ruling only while its public consumer API
   remains closed and enumerated. A generic query, filter, or blob API would violate the ruling.
2. No-backfill applies to VMs and every later adopter. An absent record must render visibly as not
   recorded rather than disappear from output or imply a match.
3. This effort owns required transaction fixes and test trim for surfaces it rewrites. Classifier,
   sentinel, dead-column, and unrelated transaction work remains bounded by the assessment and is
   tracked in issues 505, 633, 634, and 635 rather than left only in prose.

At this revision, the reviewed stacked R2 checkpoint in PR #636 implements transaction-aware owner
deletion and atomic state cleanup. It remains a follow-on implementation rather than behavior in
this R1 code basis until that PR merges.

The authenticated operator channel accepted those decisions and directed the format-aware SSH
clarification in this revision. The ssh-agent question remains intentionally unresolved.

-- agw-ns-instance-model (instance-model effort lead)
