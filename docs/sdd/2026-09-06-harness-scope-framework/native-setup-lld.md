# Native Harness Setup: Detailed Design

- Status: Implemented locally; integrated acceptance pending
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
prior applied-state record for this integration and a core-owned checkpoint callback. A present
integration activation has validated config; retirement has absent desired config plus prior
applied-state record. Retirement does not validate an empty pretend activation or derive new
defaults. Admin and agent are both user invocations, with actual username/home/transport;
integration code has no separate admin operation.

No-op defaults return successfully without native facts. Core may record a completed invocation, but
completion alone never proves a plugin, profile, settings file or executable exists.

## Operation sequence

The owning manager builds the effective activation map and validates every selected facet before
native setup. The same effective declarations supply capability references, eager secret needs,
config binding and freshness comparison. The integration lane runs after existing core setup and
uses a full canonical Transport, never the native ExecTransport bootstrap channel for file transfer.

For one owner:

1. Acquire the native mutation guard before reading prior applied-state records or observing native
   state.
2. Read prior applied-state records and bind the ordered desired activations. Reject an
   unimplemented desired facet before capture or native setup, including default-only activations.
   Include retired activations with remaining claims after desired entries, in stable prior order.
3. For each integration, prepare sources and native observations and reject conflicting desired
   plugin/settings changes before its first write.
4. Persist incomplete status before the first mutation, retaining existing claims needed for retry.
5. After each plugin or marketplace mutation, observe its authoritative result and checkpoint the
   resulting ownership before proceeding. Persistence failure aborts further writes. Settings
   publication creates no ownership claim and needs only the operation's completion record.
6. Record completion only after successful return. For a facet domain or transport error, preserve
   the confirmed prefix, persist the sanitized current-attempt reason, append an owner/facet/
   integration failure to the command's list, and continue the next independent facet. A same-input
   retry must not mistake the record for successful current setup.
7. Release the guard on every exit, including interruption. The command reports the collected
   failures with one typed error after its independent initialization and bookkeeping complete.

The collector is operation-local and does not replace the returned applied state. Each command
creates it explicitly; shared realization helpers append to it without raising the aggregate inside
rollback spans. Persistence callback failures propagate their original exception and abort further
writes, even when the exception type would otherwise describe a recoverable facet error. Shared
state reads, binding preflight, cancellation and unexpected internal exceptions are not collected.
Soft entry skips remain incomplete reports and do not become command errors.

For VM init, preserve the existing final authorized-key reconciliation and terminal checkpoint
ordering. Harness setup occurs before that terminal key write. Collected facet errors do not skip
the write or its checkpoint: the VM becomes partial, then the command reports the failures with exit
status 1. VM and admin records are distinct components of the same VM-owned native slice; neither
overwrites the existing SSH/hardware slices.

New agents and workspaces have no database owner until native creation succeeds. Their initial
applied-state records are buffered with the creation operation and inserted atomically with the
owner and desired overlay. Workspace creation retains its existing refusal to adopt unexplained
native residue. Fresh agent creation refuses an existing unowned Linux user or home before mutation
or rollback is armed. After that check, core creation failure cleans up only the native owner
created by this operation and drops its applied-state record buffer. A collected facet error instead
commits the new owner and its partial records, completes independent grants and SSH configuration,
and reports the failures at the outer command boundary. An explicit required session prerequisite
retains already-created ancestor owners while blocking session launch. A process crash leaves
residue that a new create refuses to adopt. Reinit of a database-owned agent retains convergence and
checkpoints directly into its existing instance-state row. This does not add partial-create rows or
a workspace repair/reinit command.

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
settings bytes in applied-state records.

## Applied state

Add a closed harness-native-setup applied key for VM, agent and workspace owners, using the current
instance-state table. The domain codec carries a version, owning native identity, and integration
records. VM records distinguish VM and user components. Agent/workspace records carry only their own
facet. Each integration record contains:

- Its name and component, unresolved effective config, env secret-reference names and hashes of
  literal env values needed for freshness. Neither literal env values nor resolved secrets are
  stored.
- Completion state, current-attempt incomplete reasons, and the last confirmed successful mutation
  prefix.
- Native plugin and marketplace claims identified by integration-owned role, native identifier,
  destination and source identity. A coincidentally matching name or cached file does not establish
  native association ownership.
- Pending-cleanup disposition where a prior native association remains relevant. Settings mappings
  retain their documents when removed and do not create ownership claims.

Use typed domain structures and a versioned codec, following vms/applied_state.py. Reject malformed
known payloads without echoing raw contents. Preserve unknown versions and unrelated integrations;
do not replace an unknown record with empty defaults. Core persists only what the integration
reports as native evidence. Source snapshot contents and resolved secrets never enter the codec.
Writer version 3 adds credential-free `incomplete_reasons` and distinguishes confirmed routing from
a partial file checkpoint. A nullable `artifact_inputs` means the full routing input set is unknown.
An incomplete record may retain known partial deferrals alongside that null value; those routes
preserve obligations without confirming the full set. A complete record cannot contain partial
deferrals with unknown inputs. Non-null tables prove routing only for current input identities they
cover, including explicit incoming routes whose payload is unavailable. Older incomplete records
with no deferrals are ambiguous because earlier writers stored input IDs before successful
application; readers normalize those records to unconfirmed. Complete or explicit-route older
records remain usable. Readers accept versions 1 through 3 and canonical export preserves this
normalization. Missing `incomplete_reasons` defaults to empty for earlier records. The incomplete
callback persists the current attempt's reasons through the existing record, and a new attempt
clears prior reasons. A malformed driver association string cannot prove ownership; retain and
diagnose that entry rather than treating it as removable.

Copy/rehome/restored applied-state records must match actual destination identity before satisfying
readiness. An unknown version or different destination cannot bless setup. Backup export reads
native slices for the VM owner tree, canonicalizes known domain payloads, and preserves unknown
records according to the instance-state forward-compatibility contract. Existing database restore
stays a separate workflow; no VM import/restore command is added.

## Serialization and deletion

A process-level native mutation guard provides serialization; Database.transaction and VM activation
gates do not provide it. Use operating-system file locking associated with the local state database
and VM identity. Initially serialize native setup mutations within one VM family, which includes
VM/admin setup, agent/workspace create and reinit, and deletion. Different VMs remain independent.
This deliberately avoids a multi-lock cascade and locks held in different orders while an agent or
workspace deletion invokes existing nested cleanup. Nested operations share the guard through the
owning operation, rather than reacquiring it.

The guard refuses contention immediately and reports a typed retryable contention error. It holds no
transaction on the state database across remote calls, releases automatically on process exit, and
does not use PID-file staleness as proof that another process has stopped. A lock file is
coordination metadata; unlinking it during concurrent use would allow a second inode to evade the
lock, so normal owner deletion does not unlink it. The lock implementation must exercise native
Windows and POSIX paths.

Parent deletion follows the existing VM, agent and workspace lifecycle and its existing failure
policies. It removes contained native effects with their parent, without first retiring plugins or
reading applied-state records. Recorded claims guide reconciliation while an owner remains; they do
not veto parent deletion or workspace rehome. Destination fingerprints determine readiness after a
move, not permission to move. Settings mapping removal retains the document without ownership
bookkeeping. Native reconciliation must not force-remove another integration's or an operator's
material.

## Native settings and plugins

Claude and Codex share native implementation helpers in `agentworks.plugins._harness_native`. This
is an internal plugin package, not a registered capability. Generic setup dispatch, applied-state
records and settings-source facilities remain outside it and do not depend on the native tool
branches.

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
ownership. Adopt an unclaimed registration only when its native source equals the declaration. For
an installed user plugin, compare its observed marketplace identity with the selected parent, then
retain the stable association identity in the claim. Native installed-version changes do not change
ownership; older claims that included a version remain readable. Record and report adoption; retries
can therefore heal mutation-before-checkpoint interruption without provisional claims. Differing
unowned entries are skipped individually and reported through the invocation's incomplete callback.
Core leaves `complete=False` while allowing independent native entries, artifact publication and
later integrations to proceed. A settings mapping must preserve skipped native associations,
including under replace policies. Query again after every command before recording its actual
result. A command's success alone does not establish scope or identity.

Claude git/GitHub inventory without its user settings declaration has unobservable full source
identity. Preserve it and protect its settings association. Check that scoped removal is available
before retiring any marketplace dependents: an absent user declaration retains both marketplace and
plugins, without broadening removal to all scopes or guessing replacement settings. An ambiguous
unqualified plugin selection reports qualification advice and retains its prior associations;
nonexistent selections remain invalid configuration.

Isolated local fixtures pin concrete native query payload fields, removal behavior and
command/settings ordering; see [native research](prior-art-research.md). Commands resolve the
executable and login PATH once under the actual user before switching to an isolated native home for
source discovery. Prepared env remains authoritative. Marketplace source comparison includes
optional identity fields. A simple location fast path is valid only when no additional source fields
need resolution; otherwise isolated native discovery establishes the requested full identity. The
native-reported association is authoritative, without independent cache-origin verification. A
settings overlay can materialize an authorized association key missing from the native document
after inventory confirmed the association. User-scoped plugin setup only is in scope. Workspace
facets publish project settings only.

## Readiness

The session integration receives applicable applied-state records and performs inexpensive native
probes at the existing readiness boundary, before runtime teardown. It returns typed gaps with owner
remediation, reason and required/recommended severity. Required gaps refuse launch; recommended gaps
warn and permit it if other checks pass. Default no-gaps behavior preserves session-only use.

Core validates the returned gap objects and severity at this plugin boundary. An unknown severity
cannot silently become a recommendation. Applied-state freshness probes use the actual VM, runner
and optional user or workspace destination directly; they do not construct setup invocations.

Use the actual bound user's applied-state record; admin or another agent cannot satisfy it. Missing,
incomplete, stale or failed setup is not successful current setup. Keep pending-target readiness
after explicitly requested creation. The check is read-only and does not invoke ancestor setup.
Cache only inside the same operation and evaluated setup generation.

Failure diagnostics match known secret patterns both before and after terminal normalization, before
a 2 KiB UTF-8 bound. Secret patterns use the same unbounded normalization as the text, including
values known only to the logger, so control sequences cannot reconstruct a secret after matching.
The same rendering boundary protects older persisted reasons without rewriting their state. A pure
summary formatter supplies both the terminal aggregate and explicit required-prerequisite hints;
formatting does not raise and catch an error merely to obtain its text. VM user-facet diagnostics
identify the admin scope while retaining the facet name `user`.
