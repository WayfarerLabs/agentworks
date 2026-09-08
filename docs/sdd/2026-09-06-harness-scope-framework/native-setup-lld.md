# Native Harness Setup: Detailed Design

- Status: Implementation design in progress
- Governing design: [HLA](hla.md), especially invocation, convergence and readiness
- Baseline: `3641ea8c`

This document specifies current native setup. The
[FRD successor direction](frd.md#future-artifact-context-not-this-efforts-contract) remains the home
for future artifact routing; none of its payloads or routing interfaces are added.

## Configuration and construction

Use the fixed capability facet vocabulary vm, user, workspace and session. Common construction binds
a declared config owner and one facet using the registration-selected cached model. Session binding
is a typed object containing the existing session name, VM/workspace identities and path, target,
admin mode and conversation-state namespace. Setup construction requires none of those session
fields. Session-only properties and readiness guards require a session binding; they cannot infer
one from a setup target. Setup methods receive typed invocations for their owning facet.

Each invocation supplies the actual owning instance, native destination identity, prepared runner,
prior receipt for this integration and a core-owned checkpoint callback. An active attachment has
validated config; retirement has absent desired config plus prior receipt. Retirement does not
validate an empty pretend attachment or derive new defaults. Admin and agent are both user
invocations, with actual username/home/transport; integration code has no separate admin operation.

No-op defaults return successfully without native facts. Core may record a completed invocation, but
completion alone never proves a plugin, profile, settings file or executable exists.

## Operation sequence

The owning manager builds the effective attachment list and validates every selected facet before
native setup. The same effective declarations supply capability references, eager secret needs,
config binding and freshness comparison. The integration lane runs after existing core setup and
uses a full canonical Transport, never the native ExecTransport bootstrap channel for file transfer.

For one owner:

1. Acquire the native mutation guard before reading prior receipts or observing native state.
2. Read prior receipts and bind the ordered desired attachments. Include retired attachments with
   remaining claims after desired entries, in stable prior order.
3. For each integration, prepare sources and native observations and reject conflicting desired
   plugin/settings changes before its first write.
4. Persist incomplete status before the first mutation, retaining existing claims needed for retry.
5. Perform one native mutation, observe its authoritative result, and checkpoint the resulting
   ownership before proceeding to the next mutation. Persistence failure aborts further writes.
6. Record completion only after successful return. Keep incomplete or pending cleanup evidence on
   failure; a same-input retry must not mistake it for a successful current setup.
7. Release the guard on every exit, including interruption.

For VM init, preserve the existing final authorized-key reconciliation and terminal checkpoint
ordering. Harness setup occurs before that terminal key write. VM and admin records are distinct
components of the same VM-owned native slice; neither overwrites the existing SSH/hardware slices.

New agents and workspaces have no database owner until native creation succeeds. Their initial
receipts are buffered with the creation operation and inserted atomically with the owner and desired
overlay. Workspace creation retains its existing refusal to adopt unexplained native residue. Fresh
agent creation must first refuse an existing unowned Linux user or home, before mutation or rollback
is armed; the current initializer instead converges such a user, so this boundary requires an
implementation change. After that check, handled creation failure cleans up only the native owner
created by this operation and drops its receipt buffer. A process crash leaves residue that a new
create refuses to adopt. Reinit of a database-owned agent retains convergence and checkpoints
directly into its existing instance-state row. This does not add partial-create rows or a workspace
repair/reinit command.

## Env and secrets

Construct setup SecretTarget declarations from the same scope dictionaries used for execution: VM
alone; VM plus actual admin or agent; VM plus workspace. Register them with the operation's existing
resolver before state mutation or log construction. Include selected integration config secret
references in that boundary. Secret redaction must be available when the SSH logger is created,
because its redaction set is fixed.

Use compose_env and its reserved-identity behavior for the integration runner. The runner delegates
file movement to the underlying Transport and adds prepared env to command execution; command-local
env may add values but cannot replace core identity variables. Existing bootstrap and
install-command runners remain hermetic. A session continues to compose its actual
VM/workspace/user/session env. Do not store resolved env, secret-derived hashes, or workstation
settings bytes in receipts.

## Metadata receipts

Add a closed harness-native-setup applied key for VM, agent and workspace owners, using the current
instance-state table. The domain codec carries a version, owning native identity, and integration
records. VM records distinguish VM and user components. Agent/workspace records carry only their own
facet. Each integration record contains:

- Its name and facet, effective non-secret config and declared env references needed for freshness.
- Completion state and the last confirmed successful mutation prefix.
- Native claims identified by integration-owned role, native identifier, destination and relevant
  comparison hash/strategy. A matching filename or matching bytes are not proof of ownership.
- Explicit retained settings or pending-cleanup disposition where a prior claim remains relevant.

Use typed domain structures and a versioned codec, following vms/applied_state.py. Reject malformed
known payloads without echoing raw contents. Preserve unknown versions and unrelated integrations;
do not replace an unknown record with empty defaults. Core persists only what the integration
reports as native evidence. Source snapshot contents and resolved secrets never enter the codec.

Copy/rehome/restored receipts must match actual destination identity before satisfying readiness. An
unknown version or different destination cannot bless setup. Backup export reads native slices for
the VM owner tree, canonicalizes known domain payloads, and preserves unknown records according to
the instance-state forward-compatibility contract. Existing database restore stays a separate
workflow; no VM import/restore command is added.

## Serialization and deletion

The implementation must add a process-level native mutation guard; Database.transaction and VM
activation gates do not provide it. Use operating-system file locking associated with the local
state database and VM identity. Initially serialize native setup mutations within one VM family,
which includes VM/admin setup, agent/workspace create and reinit, and deletion. Different VMs remain
independent. This deliberately avoids a multi-lock cascade and locks held in different orders while
an agent or workspace deletion invokes existing nested cleanup. Nested operations share the guard
through the owning operation, rather than reacquiring it.

The guard refuses contention immediately and reports a typed retryable contention error. It holds no
transaction on the state database across remote calls, releases automatically on process exit, and
does not use PID-file staleness as proof that another process has stopped. A lock file is
coordination metadata; unlinking it during concurrent use would allow a second inode to evade the
lock, so normal owner deletion does not unlink it. The lock implementation must exercise native
Windows and POSIX paths.

Consume claims before deleting an owner whose native effects can survive deletion. For effects
removed by deleting the native user/workspace/VM itself, confirmed native deletion is their removal
evidence. If cleanup fails before deletion, retain owner records and the remaining claims. Settings
mapping removal is deliberate retention and relinquishes mapping claims; it is not cleanup failure.
Do not force-remove another integration's or an operator's material.

## Native settings and plugins

SettingsMapping requires source and one of the four approved policies. The shared local-source
helper accepts SourceRef local spellings and captures bytes once on the workstation. Native JSON and
TOML parsing and semantic merge are tested independently. The integration determines its native
user/project role and checks the destination is absent or a suitable regular file before applying a
prepared result. Never follow an unsuitable link or write to an arbitrary supplied guest path.

A skipped mapping validates its source but does not parse or rewrite an existing destination.
Replacement validates the source but need not parse old content. Merge parses both documents and
rejects duplicate keys; arrays remain atomic. Matching output bytes avoid unnecessary writes. Use a
temporary file in the destination directory, set intended ownership/mode, and publish with atomic
rename after validating the final document. Clean temporary files on handled failure.

Plugin commands and mapped settings share one planned native change set. Compute the mapping's
effective document first, compare its plugin/marketplace declarations with explicit lists, reject
inconsistency and reconcile matching declarations once. Query native installed state before claiming
ownership. Installation or registration that existed without an Agentworks receipt remains unowned;
the migration strategy explains explicit remediation. Query again after every command before
recording its actual result. A command's success alone does not establish scope or identity.

The concrete native query payload fields, supported removal behavior, and command/settings ordering
must be pinned by isolated local fixture observations before this part is wired into setup. CLI help
confirms JSON query and add/remove commands for both tools, but is not proof of idempotency or side
effects. User-scoped plugin setup only is in scope. Workspace facets publish project settings only.

## Readiness

The session integration receives applicable receipts and performs inexpensive native probes at the
existing readiness boundary, before runtime teardown. It returns typed gaps with owner remediation,
reason and required/recommended severity. Required gaps refuse launch; recommended gaps warn and
permit it if other checks pass. Default no-gaps behavior preserves session-only use.

Use the actual bound user's receipt; admin or another agent cannot satisfy it. Missing, incomplete,
stale or failed setup is not successful current setup. Keep pending-target readiness after
explicitly requested creation. The check is read-only and does not invoke ancestor setup. Cache only
inside the same operation and evaluated setup generation.
