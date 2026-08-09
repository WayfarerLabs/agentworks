# Low-Level Design: Machine-Readable Operational Output

- Status: Implemented in Phase 2
- Parent design: frd.md R7 and AC4, hla.md machine-readable output contract, and plan Phase 2
- Baseline: main at 0cabf37b

## Scope

This LLD adds JSON v1 to selected operational list and describe commands. It makes the facts their
human renderers already present available without parsing terminal tables, display sentinels, or
prose. JSON adds no new mutation. It preserves a status-enabled session list's existing PID and
boot-ID repair behavior, which can update stored session state before rendering.

| CLI command                                     | Contract command                   |
| ----------------------------------------------- | ---------------------------------- |
| agw resource list                               | resource.list                      |
| agw resource kinds                              | resource.kinds                     |
| agw resource describe KIND/NAME                 | resource.describe                  |
| agw vm list, agw vm describe NAME               | vm.list, vm.describe               |
| agw workspace list, agw workspace describe NAME | workspace.list, workspace.describe |
| agw agent list, agw agent describe NAME         | agent.list, agent.describe         |
| agw session list, agw session describe NAME     | session.list, session.describe     |
| agw console list, agw console describe NAME     | console.list, console.describe     |
| agw secret list, agw secret describe NAME       | secret.list, secret.describe       |
| agw doctor                                      | doctor                             |

Guide is Markdown-only. Resource field-reference and sample commands, schema emission, verification
commands, logs, interactive attachment, and every mutation command are outside v1. The reusable
option must not suggest that an unsupported command has a JSON contract.

The implementation does not redesign agentworks.output.OutputHandler, replace the Typer handler, or
add a global output-format renderer or process-global output setting. A narrow request-scoped
ContextVar may carry parsed machine mode for plain error styling and may suppress ordinary
presentation while a command collects facts; local JSON projections and the direct envelope writer
remain the only JSON renderer. It must not run remote work solely because JSON was requested. It
must not expose raw configuration, secret values or resolver results, session harness state, session
socket paths, boot identifiers, or opaque VM platform metadata.

## Current paths and extraction seams

The command module parses options and chooses a renderer. The service constructs an immutable fact
record. Human and JSON renderers consume that one record and never parse one another's output.

| Surface                        | CLI path                                 | Current service path                                            | Phase 2 seam                                                              |
| ------------------------------ | ---------------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Resource list, kinds, describe | cli/agentworks/cli/commands/resource.py  | resources.inspect.list_resources, list_kinds, describe_resource | Reuse ResourceListing, ResourceSummary, KindRow, and ResourceDescription. |
| Secret list, describe          | cli/agentworks/cli/commands/secret.py    | secrets.inspect.build_secret_table, describe_secret             | Reuse SecretTable, SecretRow, SecretCell, and SecretDescription.          |
| VM list, describe              | cli/agentworks/cli/commands/vm.py        | vms.manager.power.list_vms, describe_vm                         | Extract DB and existing bounded live-read facts before rendering.         |
| Workspace list, describe       | cli/agentworks/cli/commands/workspace.py | workspaces.manager.create.list_workspaces, describe_workspace   | Extract facts from existing rows, sessions, and grants.                   |
| Agent list, describe           | cli/agentworks/cli/commands/agent.py     | agents.manager.inspect.list_agents, describe_agent              | Extract facts from existing rows, grants, and sessions.                   |
| Session list, describe         | cli/agentworks/cli/commands/session.py   | sessions.manager.\_queries.list_sessions, describe_session      | Extract facts while preserving current bounded status work.               |
| Console list, describe         | cli/agentworks/cli/commands/console.py   | sessions.multi_console.attach.list_consoles, describe_console   | Extract DB facts for counts, members, and shells.                         |
| Doctor                         | cli/agentworks/cli/commands/doctor.py    | doctor.run_checks                                               | Reuse HealthReport, HealthGroup, and HealthCheck.                         |

A small module below Typer, such as agentworks.machine_output, owns the closed output enum, envelope
writer, and safe projections. It is not a global output framework. Domain records remain
domain-owned.

## Option and envelope

Each covered command has a local --output human|json option, defaulting to human. The output enum is
closed and has exactly human and json. Unknown formats are usage errors before config, registry,
database, network, or service work starts.

Every successful JSON response is exactly one UTF-8 document followed by one line feed, without BOM,
ANSI, table padding, headings, warnings, progress lines, or friendly empty-state prose. The encoder
uses explicit projections, never a generic dataclass or **dict** dump. It retains Unicode rather
than escaping it and writes object members in their declared order.

Example envelope:

    {
      "schema_version": 1,
      "command": "resource.list",
      "data": {}
    }

Envelope order is always schema_version, command, data. schema_version is integer 1, command is one
of the exact strings in the scope table, and data is always an object. Object fields use the order
listed below. Arrays follow their stated ordering rule. Timestamps retain stored ISO 8601 text.
Missing facts are JSON null, never a display sentinel. Counts are integers and flags are booleans.

## Shared JSON records

All fields below are always present and in listed order.

    origin = { variant, file, line, source, source_resource, plugin }
    reference = { source_kind, source_name, usage, declared_by_kind, declared_by_name }
    instance_reference = { kind, name }
    vm_issue = { source, code }

origin is nullable. When present, variant is exactly operator-declared, auto-declared, built-in, or
system-plugin. file, line, source, source_resource, and plugin are null when inapplicable.
source_resource is null or {kind, name} and represents an auto-declared tuple source. source is a
code-source string. This keeps its type stable across origin variants.

declared_by_kind and declared_by_name are nullable strings. They are the ReferenceEntry declarer
when the graph entry records one, not a display-derived source. References preserve every original
graph entry and its graph order. They are never deduplicated, regrouped, or sorted. Instance
references are the current InstanceRef values. JSON never groups them for display.

vm_issue appears only in vm.describe. Its source is exactly site_lookup, preflight,
secret_resolution, or platform_status. Its code is exactly unavailable. The fact builder records an
issue at the named stage only: site lookup failures map to site_lookup; a failure raised by the
preflight sweep maps to preflight; the resolver's ordered boundary pass maps to secret_resolution;
display-backend-name or platform-status failures map to platform_status. It must split the current
broad VM describe catch at those boundaries. Source and code are closed enums in the fact type, and
the projection rejects any non-enum value rather than echoing it. It never serializes
str(exception), an exception class, a hint, or backend-authored text. The same fact record carries a
renderer-private typed diagnostic for the human renderer, preserving today's stderr warning and hint
transcript. JSON writes only the safe issue on stdout. The existing live-resource helper already
returns null for every failed or malformed live read and therefore creates no issue.

Secret resolution is an output-neutral boundary operation. The ordered resolver accepts the existing
ResolutionReporter protocol from its caller rather than constructing an output reporter internally.
Resolver.resolve, the VM no-gate boundary, the session batch boundary, and its late repair-key
resolution thread that reporter through. The singular session path also threads it unchanged:
\_prepare_vm receives the reporter, passes it to gated_vm_boundary, and that boundary passes it to
gate_secret_resolver and its Resolver.resolve call. Human rendering passes the existing output
reporter and therefore preserves its resolution and skipped-backend transcript. JSON passes a quiet
reporter to the same ordered resolver, active backend chain, interactive policy, preflight, and
value cache. It does not use the verification-only quiet resolver, which has deliberately different
backend wrapping and interactivity rules. Thus JSON suppression changes presentation only, never
secret resolution semantics or whether a successful VM describe, session describe, or status-enabled
session list can prompt. Request-local presentation suppression is a ContextVar. Session status
workers receive an explicit, separate `copy_context()` for each ThreadPoolExecutor task, so
suppression follows the request without leaking to overlapping human invocations.

## Data schemas

### Resources and kinds

resource.list.data is {resources, counts}.

    resources[] = {
      kind, name, origin, reference_count, used_by_count, description,
      not_ready_reason, disabled
    }
    counts = { operator_declared, auto_declared, built_in, system_plugin }

origin, used_by_count, and not_ready_reason may be null. disabled is boolean. Rows retain
ResourceListing order: explicit kinds retain request order, otherwise kinds sort lexically, then
names sort within kind. Counts are post-filter values. Current kind, origin, and disabled filters
retain their exact behavior.

resource.kinds.data is {kinds}, where each item is {kind, category, resource_count, description}.
category is exactly declarable or capability. Kinds sort lexically.

resource.describe.data is {resource}, where resource is {kind, name, origin, description,
references, used_by, not_ready_reason, disabled_reason}. references uses reference; used_by is an
array of instance_reference or null when no instance concept exists. Both reason fields are
nullable. Explicit disabled-resource lookup remains supported.

### Secrets

secret.list.data is {backends, secrets, counts}. backends preserves active backend precedence. Each
secret is {name, description, backends}, with backends[] equal to {backend, would_attempt,
identifier, not_ready_reason} in that precedence order. identifier and not_ready_reason are
nullable. counts is {operator_declared, auto_declared}. Secrets sort by name. This reports lookup
prediction only, never a secret value.

secret.describe.data is {secret}. secret is {name, kind, origin, description, hint, references,
used_by, backend_mappings, resolution}. backend_mappings[] is {backend, would_attempt, identifier,
not_ready_reason}. resolution is {resolved_by, available, skipped_not_ready}, where
skipped_not_ready[] is {backend, reason}. hint, used_by, identifier, not_ready_reason, and
resolved_by use null as appropriate. available equals whether resolved_by is non-null. Backend
collections retain chain order.

### VMs

vm.list.data is {vms}. Each VM is {name, site, template, provisioning_status, initialization_status,
workspace_count, agent_count, session_count, tailscale_host, created_at}. template and
tailscale_host are nullable. Provisioning is one of pending, in_progress, complete, failed;
initialization is one of pending, in_progress, complete, partial, failed. VMs sort by name.

vm.describe.data is {vm, issues}. vm field order is:

    {
      name, created_at, site, platform, backend, observed_status, status_disposition,
      operator_stopped, hostname, system_slug, system_slug_state, template,
      admin_template, admin_username, provisioning_status, initialization_status,
      tailscale_host, last_seen_at, provisioned_resources, live_resources,
      agents, workspaces, events
    }

platform, backend, observed_status, status_disposition, system_slug, template, admin_template,
tailscale_host, last_seen_at, and live_resources are nullable. observed_status is running, stopped,
deallocated, or unknown. status_disposition is manual or idle only for stopped or deallocated VMs.
system_slug_state is set, declined, or unset, retaining the current empty-versus-absent distinction
without a display sentinel.

provisioned_resources is {cpus, memory_gib, disk_gib, swap_gib} with nullable integers.
live_resources is null or {cpus, load_average, memory_total, memory_used, memory_percent,
swap_total, swap_used, swap_percent, disk_total, disk_used, disk_percent}. These retain current
bounded live-read text and units. agents[] is {name, linux_user, grant_all, grant_count}.
workspaces[] is {name, path, sessions}, and sessions[] is {name, template, mode, agent_name}.
events[] is {created_at, event, detail}. event is exactly provisioning_started,
provisioning_complete, provisioning_failed, init_started, init_complete, init_partial, init_failed,
backup_started, backup_completed, backup_failed, rekey, or unknown. Unknown or historical raw names
project to the stable unknown sentinel and never echo stored text. detail is reserved and ALWAYS
null in JSON v1: persisted event detail is unbounded historical diagnostic text, and v1 defines no
safe non-null grammar. mode is admin or agent; agent_name is nullable. These arrays retain current
DB ordering. issues[] uses vm_issue in encounter order.

### Workspaces and agents

workspace.list.data is {workspaces} with {name, vm_name, template, created_at} entries. template is
nullable. It preserves current post-filter workspace-name order.

workspace.describe.data is {workspace}. workspace is {name, vm_name, template, path, created_at,
sessions, agents}. sessions[] is {name, template, mode, agent_name}; agents[] is {name, linux_user}.
template and agent_name are nullable; mode is admin or agent. Collections retain current DB order.

agent.list.data is {agents} with {name, vm_name, template, grant_all, grants} entries. template is
nullable and grant_all is boolean. grants[] is {workspace_name, grant_type}, where grant_type is
explicit, implicit, or both. It replaces the human-only comma string with the same grant facts.
Agent ordering remains VM name then agent name.

agent.describe.data is {agent}. agent is {name, vm_name, linux_user, template, grant_all,
created_at, explicit_grants, sessions}. template is nullable. explicit_grants is a string array, and
sessions[] is {name, template, workspace_name}. Both retain current service order.

### Sessions and consoles

session.list.data is {sessions}. Each session is {name, workspace_name, vm_name, template,
harness_integration, mode, agent_name, status}. harness_integration is a nullable string. mode is
admin or agent; agent_name is nullable. A broken config or unresolvable template produces null for
harness_integration, matching the current human display fallback without exposing an error string.
status is exactly running, stopped, broken, unknown, or unavailable. unavailable represents current
--no-status behavior or a current status probe without a result, rather than a human display
sentinel. Ordering remains workspace name then session name. Existing human warnings for broken and
unknown state stay on stderr only.

session.describe.data is {session}. session is {name, workspace_name, vm_name, template,
harness_integration, mode, agent_name, status, pid, created_at, updated_at}. harness_integration,
agent_name, and pid are nullable. pid is a positive integer or null only. The stored PID_STOPPED
sentinel is rendered as null, never as a negative number. Opaque harness state, boot identifier, and
socket path are excluded.

console.list.data is {consoles}, where entries are {name, vm_name, session_count} in current name
order after filter validation. console.describe.data is {console}. console is {name, vm_name,
admin_shell, created_at, updated_at, sessions}. sessions[] is {position, session_name, shells}, and
shells[] is {cwd, admin}. The booleans remain booleans and cwd is nullable. Members keep ascending
position and shell order. This is configured DB state, never live tmux state.

### Doctor

doctor.data is {groups, counts}. groups[] is {name, checks}; checks[] is {name, status, message,
hint}; counts is {ok, info, unavailable, warn, fail}. status is exactly ok, info, unavailable, warn,
or fail; message and hint are nullable. Group and check order is HealthReport construction order.
Counts are integers from the complete report. JSON is emitted for a failing report, then doctor
exits 1. Reports with no failed checks exit 0; unavailable checks do not increment warn or fail.

## Ordering, error, and terminal behavior

The service alone selects collection order. Serializers never re-sort an ordered collection and
renderers never mutate fact records. No JSON field comes from a table cell, formatted origin, or
terminal label.

On a domain failure, no envelope is written. Existing entry handling writes the framed error and
optional hint to stderr and exits nonzero. This includes bad resource references, unknown names or
filters, configuration errors, and failed dependencies. Invalid --output follows normal usage
handling before work begins.

Doctor is the only report-with-failure exception. A successful VM inspection that currently degrades
after a bounded attempted read uses null for unavailable facts and a closed vm_issue; it is not a
business-error envelope. JSON contains no ANSI on either output stream. Request-local machine stderr
rendering removes C0 controls other than ordinary line feeds and tabs, DEL, and C1 controls from
every prompt form's prose/default/options, exception messages, hints, and native Click/Typer usage
that may repeat argv; machine prompts emit no terminal-mode reset sequences. Machine debug formats
its full traceback through this sanitizer, while human debug keeps its raw re-raise. Human rendering
otherwise remains unchanged. Covered callbacks record their parsed output mode in request-local
state before mutex, config, database, or service work, and that state remains active while errors
unwind. A closed pre-parsing detector recognizes only the 16 supported command paths, including
after leading root option tokens, and the two JSON option spellings before a literal `--`; it
disables and sanitizes native Click/Typer usage without selecting machine mode or interpreting
mutation and passthrough arguments. The serializer writes stdout directly, rather than output.info,
so the ambient handler cannot add presentation. --output human executes the exact current human
renderer path.

Doctor is inspection-only at both schema states. One report-scoped collection supplies the System,
VM sites, and Database groups, so all three project facts from one verified database generation. A
stale schema yields their pending-migration rows without opening the original database through
SQLite. A current, existing database is resolved to its real identity, then doctor stream-copies the
main file and any resolved WAL/SHM sidecars into a private writable directory while recording file
identity, size, modification time, and content fingerprints. Source entries are acquired by
non-blocking, no-follow descriptors and accepted only when the descriptor identifies a regular file.
Reads are bounded to the acquired size. Broken or looping symlinks, FIFOs, devices, directories,
sockets, and other unsupported entries fail closed with a path-free inspection-unavailable error.
The complete acquisition protocol requires non-blocking and no-follow flags plus directory-relative
open and directory-only support. A host lacking any required primitive raises a distinct typed
protocol-unavailable result before inspecting a database or sidecar entry; it never substitutes a
check-then-open sequence. Doctor projects that result as fixed, path-free `unavailable` rows in the
System, applicable VM sites, and Database groups. It is non-failing and an otherwise healthy report
exits 0. Unsupported source entries, copy or retry failures, and malformed schema versions remain
the path-free database-inspection failure path and make the Database row fail. After resolving
supported requested-path symlinks, doctor opens the filesystem anchor, then walks each parent
component with directory-only, no-follow opens relative to the previously pinned fd. The final
parent fd supplies every main, WAL, and SHM open. The same bounded protocol handles main-only and
active sets. A main-only candidate requires sidecar absence to remain stable throughout copying and
verification. Doctor reopens and re-fingerprints the complete source set and accepts only an exact
match, then validates and opens only the disposable copy through SQLite. A concurrent
clean-to-active transition, checkpoint, replacement, or sidecar transition discards the candidate
and retries a small bounded number of times; exhaustion is the same path-free error. The
report-scoped snapshot is cleaned up after all database facts are collected; doctor neither migrates
nor creates or changes the original database, WAL, or SHM files. The schema-version boundary accepts
only SQLite null as version 0 or an exact nonnegative integer. Text, bytes, floating-point, boolean,
and negative values are malformed state and fail closed before any version comparison or public
diagnostic can expose the raw value.

--names-only remains completion plumbing and is mutually exclusive with --output json on every
covered list or kinds command that already has it. Validate that conflict before service work. It
has no JSON interpretation. Existing completion snippets continue to call only --names-only.

## Compatibility, tests, docs, and completions

JSON v1 is additive. Optional fields may be added only when documented as optional and existing
values retain type and meaning. Removing a field, requiring an optional field, changing type,
meaning, collection ordering, or enum spelling requires a new schema version and explicit
compatibility period. A new command requires its own documented data schema.

Implementation must add:

1. Parse-and-assert fixtures for every command string and schema, including empty lists, nulls,
   disabled resources, unready backend cells, unavailable sessions, and a complete failing doctor
   report.
2. Repeat-run raw-byte tests that prove deterministic array and key order.
3. A human byte-compatibility fixture for every covered command: no option and explicit --output
   human both match pre-Phase-2 stdout and stderr in a non-interactive no-color fixture. VM and
   session fixtures cover existing bounded status and degraded paths, including the existing session
   PID and boot-ID repair write.
4. Mutual exclusion, no-ANSI, stderr-routing, error-empty-stdout, and doctor-report-before-exit
   tests.
5. Safety tests proving JSON excludes secret values, raw config, opaque platform metadata, harness
   state, sockets, and boot identifiers.
6. An end-to-end guide-action fixture that parses each applicable v1 document rather than human
   output.
7. Secret-boundary parity fixtures. A successful VM describe whose site requires a resolvable secret
   proves JSON stdout is exactly one parseable envelope with no resolver transcript. The equivalent
   human fixture preserves the current resolved and skipped-backend transcript. A resolvable-secret
   session-describe fixture proves that JSON passes the quiet reporter through \_prepare_vm,
   gated_vm_boundary, and gate_secret_resolver: stdout is exactly one parseable envelope, while the
   equivalent human invocation preserves its current transcript. The status-enabled session-list
   fixture exercises the same quiet reporter through the batch boundary and its late repair-key
   resolution, and proves it retains the existing status and PID-repair outcome.
8. Reference fixtures with repeated entries and inheritance declarers. They assert the exact graph
   sequence, nullable declared_by fields, and no JSON-side deduplication. Session detail fixtures
   assert a positive live PID and a stopped PID_STOPPED row rendered as null. Harness-integration
   degradation fixtures assert null in list and describe JSON.

cli/command-reference.md owns the permanent JSON v1 contract: envelope, supported commands, resource
and doctor examples, null and ordering rules, errors, doctor exit behavior, --names-only exclusion,
and compatibility policy. Related command guidance links to it. Applicable guide action records use
--output json and require the envelope schema_version, command, and data checks.

The new public option requires generated Typer help and Bash, Zsh, and PowerShell completion
expectations in the same commits. Dynamic completion maps remain names-only contracts. Sample config
has no new setting and is recorded as unaffected in the Phase 2 handoff.

## Implementation sequence

1. Add the local output enum, explicit envelope writer, safe shared projections, and serializer
   tests. Keep JSON projection and writing local; do not replace OutputHandler or add a global
   output-format renderer. Narrow request-state and presentation-suppression ContextVars are allowed
   only for presentation isolation and plain machine-error styling.
2. Wire resource, kinds, secret, and doctor first, reusing existing fact records to establish the
   renderer, null, enum, error, and human-fixture patterns.
3. Extract read facts for VM, workspace, agent, console, and session. Make the resolver reporter
   caller-owned before wiring JSON VM describe, session describe, and status-enabled session list.
   Preserve existing queries, secret-resolution behavior, and session PID-repair behavior, but make
   fact construction independently testable.
4. Wire command options, permanent docs, completion expectations, and guide-action consumption, then
   run focused and full gates. This LLD changes no plan checkbox.

## Coordination note: declarative-schema PR #455

PR #455 changes schema structural unions and related model sources, but does not touch covered
operational command modules, resource inspection services, doctor, or database rows. Phase 2 should
not wait. Rebase after it merges and rerun focused JSON fixtures. Adapt only if Phase 2 deliberately
projects a changed model shape. The safe projections above expose no raw manifest or opaque
capability model, so no adaptation is expected from the current PR scope.
