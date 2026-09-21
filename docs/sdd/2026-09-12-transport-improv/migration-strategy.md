# Transport Improvements: Migration Outline

- Status: Buffered proof accepted; additive delivery, consumer migration, then legacy removal
- Baseline: v0.19.0, `e440a28c49935df722e4e80685ef12f6d8247ff8`, inspected 2026-09-16
- Release delta reviewed: `7c744828..e440a28c`; file helpers, artifacts, harness facets and session
  lifecycle

## Inventory and destination

### Additive implementation inventory, 2026-09-19

At merged #830 (`cea5e852`), the independent package contains only the accepted buffered proof: 11
Python modules, with no target/access/file/job/profile implementation or production use. There are
24 production `RunContext(` construction sites and no new execution-target accessors. These counts
came from a source search, not runtime coverage.

The additive composition work owns `capabilities/base.py` and these context-producing families: VM
manager boundaries/lifecycle/nodes, agent lifecycle, session create/roll/lifecycle/scope, workspace
create, VM inspection/power/status and doctor. They continue passing through the old targets
unchanged while gaining separately supplied new targets. Composition binds the new targets from
resolved connection/identity facts, never by wrapping a legacy target. Absent targets remain absent
at preflight; adding accessors cannot activate a route or discover authority.

The adapter inventory is local Lima, remote Lima's guest hop, WSL2, and Proxmox QGA, plus the
separately owned SSH carrier used for canonical access, cloud-native access and placement hosts. The
existing WSL2 platform hold (`capabilities/vm_platform/wsl2.py`) remains distinct from guest jobs.
The QGA proof currently lives in `execution/carriers/proxmox.py`; moving it to the final plugin
location first requires removing the plugin package's eager legacy-import dependency. Bypassing
plugin initialization in a test is not proof of that independence.

This refresh supplements the release-behavior baseline below. It does not mark any consumer migrated
or establish the new lifecycle/file guarantees.

### Owned-boundary integration inventory, 2026-09-21

The read-only audit at `faf99365` locates admission before effects without moving legacy commands
onto the new stack. `vms/manager/boundary.py` already has the database and canonical VM before
`_gated_vm_boundary` enters activation, but builds its ordinary RunContext only afterward. The new
owned boundary belongs around that sequence, not inside a passive accessor or the shared activation
engine. Existing gated and live-only boundary calls remain unchanged until their consumer migrates.

Propagation has three distinct seams:

- `LiveVMNode._gate_ops_ctx` constructs the activation context independently. Bind the original
  owner into the new workflow's node before activation, then preserve it through `_platform_ops_ctx`
  and subsequent explicit context reconstruction.
- Agent create/reinit, workspace create, session create/start/restart, VM reinit and VM rekey have
  activation paths outside the common gated boundary. Their eventual migration must adopt the owned
  boundary before the gate; adding one common wrapper does not cover those paths automatically.
- VM creation starts without an activation gate. Ownership must precede platform creation and cover
  bootstrap, the platform power hold, initialization and rollback. `RealizationLog` invokes retained
  nodes' no-argument teardown; pending VM/agent/workspace nodes must carry the original owner into
  nested deletion rather than acquire a second claim.

The descriptive capability `OperationScope` is not the database claim's `OperationScope`. In
particular, a batch's SYSTEM description cannot become a system-wide claim in the current exact-VM
repository. Explicit resource admission and later hierarchy remain distinct from context display
scope. Finalization also remains explicit: returning from a legacy helper or leaving an ExitStack
does not prove that remote effects stopped. This inventory locates the integration work, not
implemented production ownership or accepted recovery.

### Remaining-native-platform inventory, 2026-09-21

The read-only inventory at `bf819094` covers Lima, AWS EC2, Azure VM and GCP GCE. The cloud native
paths use provider APIs for identity, live endpoint and route management, then SSH to the guest;
they are not API-based guest command channels. AWS describes its current instance IP, Azure walks
the VM/NIC public-IP state, and GCP verifies the owned instance/network before reading its external
IP. Stop/start can change these addresses. The platform must perform that resolution explicitly
under the core operation's preparation deadline, not inside a passive RunContext accessor.

Each cloud create path already holds the endpoint, admin account and operator key before building
its legacy SSH transport. `ProvisionResult.native_transport` still carries that old object into
Debian attestation and Phase A. New create-time result composition must carry an independent binding
from those facts; adding an existing-VM resolver alone does not cover provisioning. Explicit
known-hosts and connection-isolation inputs remain SSH-lane dependencies, not ambient trust that
transport may infer from the operator's key path.

Lima still needs an independent guest carrier. Local delivery uses `limactl shell`, but the current
template and invocation do not explicitly select its actual guest delivery account. That account
must be observed or explicitly established, not assumed equal to the separately created VM admin.
Remote Lima additionally needs explicit placement-host endpoint/account/trust/OS facts and separate
inner-guest completion evidence. Its host create/rollback currently uses legacy detached execution;
replacing only the returned guest transport leaves those required workflows unmigrated.

This inventory is source evidence, not native feasibility or acceptance. Its concrete seams are
`plugins/aws/platform.py`, `plugins/azure/platform.py`, `plugins/gcp/platform.py`,
`capabilities/vm_platform/lima.py`, `capabilities/vm_platform/base.py` (`ProvisionResult`),
`vms/manager/lifecycle.py` (attestation/Phase A), and the current native factory in
`transports/__init__.py`. Provider identities, route policy and opaque metadata decoding stay inside
their platforms. The explicit resolver and complete create-time/factory integration remain required
before native production cutover.

### New-target composition inventory, 2026-09-21

Read-only inspection at `449b297e` found that `VMPlatform.native_transport` and
`ProvisionResult.native_transport` still expose only legacy targets. An additive new-stack
composition seam must retain platform ownership of `VMRow.platform_metadata`; core must not decode
provider keys itself or obtain new connection facts by constructing an old transport. Preserve the
old hooks and callers during coexistence. New construction must state its actual delivery identity
so account observation and DIRECT, root-entry or demotion plans do not guess from the requested
recipient. Route and platform holds remain owned outside the target.

- Proxmox owns node/VM-ID extraction and API-secret resolution. The new QGA carrier delivers as
  root; an ordinary guest target needs observed account identity and an explicit demotion plan.
  System-trust verification maps to the new carrier, but the old `verify_ssl=False` choice does not.
  Add a supported explicit CA-bundle composition path and migration diagnostics rather than
  weakening the new carrier's TLS checks. Remove the plugin's eager legacy-transport import before
  claiming independent platform composition.
- WSL2 owns distribution metadata and the explicit delivery user. The existing new carrier can
  consume those facts, but the platform does not yet expose them through an independent hook.
  Distribution lifetime remains separate from guest execution lifetime.
- Local and remote Lima still lack independent carriers. Preserve platform-owned instance lookup;
  replace legacy command-string execution with prepared invocation delivery and distinguish host
  completion from guest evidence across the extra hop. Remote placement currently accepts one host
  string, including `user@host` or ambient aliases. SSH's inspected `34a4eb71` connection contract
  instead requires explicit endpoint/account/trust facts. Transport owns that placement migration
  and explicit host OS/runtime binding, consuming SSH-owned policy rather than inventing another SSH
  configuration parser or runner.

These are implementation and compatibility gates, not new supported configuration or production
factories. Exact additive hook types and provisioning-result composition still require their
implementation review. Existing legacy trust behavior is unchanged by this inventory; required
new-stack workflows must resolve incompatible settings before acceptance, without an insecure
fallback or a dependency on an old target.

### Release baseline

The current delivery implementations are SSH, Lima, remote Lima, WSL2, and Proxmox QGA. AWS, Azure,
and GCP reuse SSH for native access. The public abstraction has two tiers; `RunContext` currently
delivers the richer tier only.

SSH also reaches remote Lima placement hosts. `capabilities/vm_platform/lima.py:551` constructs this
target, `:616` starts detached VM provisioning on it before the guest exists, and `:704` cancels
that work during rollback. This host target participates in the shared execution migration without
being delivered as a guest target through `RunContext`; its macOS-compatible userspace must be
preserved. The platform retains VM-resource lifecycle ownership rather than being required to
replace its runtime with a generic host MANAGED job. It participates in core operation coordination
for conflicting resources; this is not isolation from a malicious platform.

The destination is reusable SSH-backed platform access. Remote Lima supplies its first management
commands and guest-hop integration; the shared SSH carrier and host target do not depend on Lima.
Another platform can compose the same host access without inheriting Lima behavior or introducing
another SSH runner. Existing host/guest identities and operation lifetimes remain distinct.

| Current owner                                                                        | Target change                                                                                                           |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `transports/base.py`, concrete transports, Proxmox transport                         | Common execution target above carrier delivery; explicit optional interaction.                                          |
| `transports/__init__.py`, VM platform native/provision results                       | Construct the new target under the same explicit route ownership.                                                       |
| `capabilities/base.py`, VM/agent/session context constructors                        | Add stable new identity accessors alongside legacy; migrate consumers, then delete legacy accessors.                    |
| Capability readiness and operations, setup invocation runners                        | Consume the common type without rebuilding transports or context authority.                                             |
| `harness_setup/runner.py`                                                            | Move prepared environment policy into shared target defaults; retire forwarding implementation.                         |
| `remote_exec.py`, backup, remote Lima host provisioning/rollback, native logout      | Use managed job start/observe/wait/dispose with explicit retention and actual execution-host identity.                  |
| VM/agent exec and shell, sessions/consoles                                           | Preserve command/stdin and terminal behavior while using shared execution and feature checks.                           |
| SSH-named shared result/error/logger types                                           | Move generic execution facts into transport-neutral vocabulary.                                                         |
| `native_files.py`, including `NativeFiles` and `ROOT_FILE_DIRECTORIES`               | Replace mechanisms/policy in the independent execution package; migrate callers and delete the old module, not wrap it. |
| `artifacts/publication.py`, `_harness_native/native.py`, setup/readiness invocations | Consume RunContext file access; retain artifact ownership, settings policy and confirmed-effect checkpoints.            |
| `artifacts/native/probe.py`, `native_cli.py`, session home/destination discovery     | Separate bounded file observation and core identity discovery from genuinely authorized harness CLI execution.          |
| `sessions/tmux.py`, session lifecycle cleanup                                        | Move socket-directory metadata and confirmed stale-socket removal to files; tmux still creates and owns sockets.        |

### The 0.19.0 file substrate is a replacement input, not a second stack

PR #825 shipped `native_files.py`. Its root-mode ceiling is `/etc/claude-code`, `/etc/codex` and
`/opt/agentworks/artifacts` (`:192-238`); ordinary mode has normalized-path validation, not that
allowlist (`:304-305`). Its guest helper already walks directory descriptors without following
symlinks, refuses special files, checks expected content, applies metadata before replacement, and
can preserve extended attributes. This is useful engineering to copy and test, not code the new
stack may call: `NativeFiles` holds a legacy `Transport`, stages through it and exposes its runner.

Decision: transport owns the successor allowlist, file mechanics, all consumer migration and
physical deletion of `agentworks.native_files` at cutover. Copy/adapt proven algorithms and useful
tests into `execution/files.py` and `execution/file_policy.py` without importing the old module. Its
elevated directories seed the catalog; ordinary user/workspace roots and narrowly approved tmux
directories must be inventoried explicitly, not enabled by a global root or home wildcard. Only one
core policy remains after removal. Legacy calls keep the shipped checks; new calls do not enforce
the successor permission policy during coexistence. The successor catalog and recipient intents are
reviewed before activation at removal, never composed with old policy for one mutation.

Retain domain code, not the execution facade: artifact capture, routing, native discovery rules,
generated sections, ownership records and application checkpoints remain in their existing owners.
Setup/readiness invocations carry RunContext alongside descriptive inputs instead of a raw runner;
resources use its bound execution/file interfaces directly. Remove access through `files.runner`,
public staging slots and alternate file wrappers. Actual marketplace/plugin installation may still
require command access; file-only must describe the operation's authority honestly.

### Release behaviors that the cutover must preserve

| Source at the baseline                                                           | Required migration behavior                                                                                                                                                                                                          |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `capabilities/harness_integration/settings.py:78-91,166-175`                     | Preserve replace, merge-overwrite, merge-preserve and skip-existing, JSON literal null, whole-array replacement and TOML values. Do not reinterpret null as deletion.                                                                |
| `artifacts/publication.py:106-217`, `artifacts/sections.py:15-39`                | Preserve unowned/modified files, generated-section surroundings, malformed-marker refusal, metadata/ACLs and per-confirmed-effect checkpoints; retain cleanup evidence after interruptions.                                          |
| `harness_setup/dispatch.py:41-56`, `artifacts/session.py:72-79`                  | Supply trusted destination/home identity through composition and bounded file observations, not arbitrary commands granted solely to discover paths.                                                                                 |
| `artifacts/native/probe.py:370-437`                                              | Replace raw Transport and implicit login-shell discovery with bounded inventory/read primitives and explicitly prepared native environment; retain collision checks and redaction without exposing a script hook through FileAccess. |
| `sessions/manager/_create_roll.py:374-375`, `db/models.py:177-180`               | Retain durable session UUID and per-launch run ID, private artifact ownership and unknown-runtime cleanup evidence. Names alone must not authorize deletion of a replacement session.                                                |
| `sessions/manager/_lifecycle.py:46-88,704-712`                                   | Preserve running-restart confirmation and selected-session identity checks; transport migration cannot broaden lifecycle consent.                                                                                                    |
| `vms/initializer/driver.py:550-551`, `capabilities/vm_platform/cloud_init.py:27` | Python3 is guaranteed by Phase B initialization, not every base image, early recovery or platform host. Prove helper prerequisites without circular bootstrap dependence.                                                            |

The new FileAccess needs bounded directory inventory and conditional writes/removal, not just
upload/download. Generated-section transforms stay local in the artifact domain, using authorized
read and conditional publication; no caller-supplied remote transformation callback is introduced.
Copy the behavioral expectations in `tests/test_native_file_boundaries.py`,
`test_harness_settings.py`, `artifacts/test_publication.py`, `test_generated_sections.py` and
native-probe tests, adapting their delivery to the new stack. The old helper's expected-hash check
followed by rename is not atomic compare-and-swap; its workstation-local mutation guard does not
serialize every remote writer. Preserve conflict refusals while proving the stronger
cooperating-writer protocol separately.

Current `/run` objects are tmux sockets under `/run/agentworks/agent-tmux-sockets` and
`/run/agentworks/admin-tmux-sockets` (`sessions/tmux.py:35-66`). Their parent directories and
confirmed stale-socket cleanup are file consumers; creating a socket remains tmux's job. There is no
current FIFO consumer; the operator removed FIFO creation from the initial contract on 2026-09-16.
Tests use the real tmux paths and never substitute an invented event-pipe workflow.

## Incident-derived behavior inventory

This inventory is a required migration deliverable. Refresh it while auditing every old entry point;
the seed cases below are not exhaustive. Deletion is blocked until each behavior has a new-stack
owner, concrete replacement regression, workstation/platform evidence, and a disposition of
preserved, deliberately replaced, or retired with rationale. Copying an old test is not evidence
that its relevant behavior is exercised through the new stack.

| Behavior and existing evidence                                                                                                                                                               | New-stack owner and replacement evidence required                                                                                                                                                                                                                                                                                                                  | Current disposition                                                                                                                                               |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Forced TTY versus closed stdin: [ADR 0020](../../adrs/0020-close-ssh-stdin-instead-of-forcing-a-tty.md), `tests/test_ssh_input.py`, `tests/transports/test_ssh.py`                           | SSH carrier plus shared preparation. Prove bounded repeated short commands on a Windows workstation with no inherited console input, no forced terminal, distinct guest streams, byte-exact output and application EOF. The prepared envelope itself needs finite carrier stdin even when application input is EOF; do not mechanically add `ssh -n` to that path. | Preserve the behavior, not the old argv recipe. Live Windows evidence remains open.                                                                               |
| Windows text-mode stdin rewrites LF: `subprocess_io.py`, `tests/test_subprocess_io.py`                                                                                                       | Every workstation subprocess delivery path. Exercise finite binary input, NUL, CR/LF and all byte values through new carriers on Windows and a POSIX workstation. New reports retain raw bytes; legacy output newline normalization is deliberately not the new contract.                                                                                          | Preserve byte-exact input. Retire implicit output normalization only after callers choose any needed text decoding explicitly. Replacement evidence remains open. |
| Git-for-Windows tools are not a Linux guest: `tests/conftest.py:requires_posix_shell`, `tests/test_bootstrap_script.py`, `tests/test_codex_integration.py`, `tests/test_session_liveness.py` | Shared preparation and platform adapters. Run guest shell mechanics on actual supported targets; separately exercise native Windows path/argv/pipe handling with its actual SSH client. Record Bash/coreutils prerequisites and any shell-test skip by axis, not as a guest compatibility pass.                                                                    | Retain the distinction. A local Git Bash fixture cannot establish Linux guest behavior; platform-host macOS commands also need their own userspace evidence.      |

Transport owns this list and final deletion. SSH and other migration owners supply their assigned
regression/evidence entries; completion is assessed on the integrated tree, including imports with
retired modules unavailable. The broader release behavior table above and every newly discovered
incident follow the same disposition rule.

## Parallel build after the proof gate

The [design and delivery plan](plan.md) owns the mandatory sequence and proof matrix. First specify
and prove the transport-owned contract with SSH implementation input, then incorporate findings into
both efforts' artifacts. Broad parallel implementation starts only after that gate, not while stream
separation or shell bootstrap is being independently improvised. The OpenSSH 8.5 floor and its
applicable locations are recorded before proof acceptance. The proof is not full-platform acceptance
or a production cutover.

Build the destination stack independently, then add the complete new surface to production
`RunContext`. Permanent new accessors `admin_execution_target()` and `agent_execution_target()`
return `ExecutionTarget | None`; existing `admin_target()` and `agent_target()` retain their legacy
types and behavior until deletion. No union type, runtime stack selector, fallback wrapper or second
context object is introduced. Core composition owns both sets of passive handles during coexistence
without routing a call from one implementation through the other.

Database operation ownership is explicit at the new workflow's core entry boundary, before
activation, not a side effect of first accessing a target. At `d07fcbc2`, the ordinary VM boundary
already has the database and canonical VM name before entering activation; VM creation and retained
rollback nodes have separate roots that must carry the same ownership. Adding unconditional claims
to those existing roots would change legacy commands immediately. The additive PR instead supplies
the new operation boundary and ownership carriage, proves new-only workflows through it, and leaves
unmigrated calls unchanged. First adoption of a production consumer opts into that boundary as part
of its migration batch. This adds no runtime stack selector or second RunContext type. Passive
accessors alone cannot satisfy the pre-activation gate, and a legacy runner's successful return is
not generic evidence that uncertain remote effects have stopped.

The [2026-09-19 ruling](frd.md#operator-rulings-2026-09-19) defers new recipient permission
enforcement, including the successor core file ceiling, until legacy is physically removed. Record
consumer intent in the migration inventory and prepare small grant values and isolated denial tests,
but do not restrict production recipients or claim file-only/profile-required isolation during
coexistence. Keep existing legacy checks unchanged. No runtime bypass toggle, policy engine or
shadow-decision service is needed. Registration requests and user consent remain future work.

Deferral is not a safety waiver: bound identity, explicit elevation/shell/profile, no-staging
readiness, sensitive-data handling, actual guest permissions, SSH trust, safe path/object handling,
metadata preservation, and truthful lifecycle/outcome semantics apply immediately. A file operation
acts only on its explicit destination and cannot escape it through links or helper injection, even
before the catalog limits which destinations recipients may choose. Requested profiles must supply
their guarantees; coexistence prevents treating their selection as mandatory for the recipient.

Develop against the [proposed carrier contract and destination layout](execution-contract.md). The
proposed revised SSH assignment is a new carrier and connection/trust implementation under
`execution/carriers/ssh/`, not consolidation of the old runner. This effort builds shared semantics
and the other adapters, then composes the new SSH carrier. The
[current SSH reference](prior-art-research.md#ssh-coordination-reference) supersedes #757's legacy
consolidation; broader contract reconciliation remains after the accepted buffered proof. No old
execution code is called from the new stack, directly or indirectly. Copying useful code and tests
is permitted. Transport owns applying reusable SSH policy in platform-host access, Lima
adapters/provisioning and provider-inner paths; SSH owns the policy/guarantees and independent
connection/trust migration.

Old and new code intentionally coexist in production during the bounded migration sequence. Each
consumer operation selects exactly one API, never dual dispatch. Configuration and trust records are
retained state, not disposable implementation. The SSH effort specifies reuse or migration of those
records without resetting host trust, broadening identity selection, or importing the old runner.
Resolve trust-file writer ownership and rollback evidence before the first production use of the new
carrier; tests of conversion use isolated copies, not a concurrent second writer against operator
state.

Existing callers are migration evidence, not the new API's limit. Design and test the core/plugin
scenarios in FRD R11 even where the old stack has no equivalent operation. Test new mutating
workflows on isolated resources, never by sending one production request down both stacks.

## Sequence and cutover gates

1. Pass the plan's small-contract proof gate under its own authorized charter. No legacy
   consolidation is a prerequisite, and an unresolved shell/input/output boundary blocks expansion.
2. Reconcile both SDDs against the proof and publish the agreed implementation boundary. Finalize
   execution/context and file/job LLDs and remaining platform feasibility before broad development.
3. Build in parallel and integrate the independent SSH carrier, shared execution, and all supported
   adapters. Verify single-attempt delivery and truthful status-255 handling; apply preparation
   exactly once. Run new-stack tests with legacy modules unavailable, and test reusable
   platform-host composition independently from Lima-specific management commands.
4. **Additive implementation PR:** implement database-level resource-operation coordination before
   exposing conflicting new-stack operations. Nested file requests share operation ownership;
   uncertain remote effects retain it across disconnects and process death. Remove the private
   destination-lock implementation and privileged setup rather than maintaining both mechanisms.
   Validate complete new-stack workflows before exposing the new RunContext accessors: provisioning,
   native recovery without Tailscale, files, jobs, backup, host provisioning/rollback and
   interactive attachment. Existing callers remain unchanged. Prove their behavior stays unchanged
   and constructing/accessing either surface adds no I/O, route activation or mutations. The new
   surface is usable independently, not a facade calling legacy.
5. **Consumer migration PR(s):** assign non-overlapping workflow batches with an owner and tests.
   Audit each operation's command versus file API, shell/startup, bound identity, elevation,
   environment, stdio, deadline, profile, lifetime and exact filesystem effects. Record intended
   grants and core catalog entries for later enforcement. Migrate direct calls as well as context
   consumers. Each batch uses only the new API for its migrated operations and proves its complete
   workflows through production entry points; unmigrated operations continue unchanged. Resolve
   job/state compatibility before switching their owners, not after deleting their reader.
6. **Removal and permission-activation PR:** require an empty legacy-consumer inventory and all
   migration evidence. Delete legacy accessors, factories, packages, tests superseded by proven
   replacements and temporary scaffolding. Prove installed-package startup and all workflows with
   those files absent, then enable the reviewed recipient grants and core file allowlist in that
   same final increment. Prove allowed workflows and denied actions/paths/profile choices through
   real context entry points, with no public or indirect legacy bypass. Only this state may claim
   restricted RunContext authority. A failed gate blocks activation/completion, not permission to
   ship enforced restrictions while leaving a legacy escape.

Each migration batch records old entry points, new owner/accessor, deliberate operation choices,
intended grants/paths, regression and live evidence, surviving state disposition and removal status.
Transport owns this ledger and the final removal PR; delegates cannot leave deletion unassigned. No
automatic fallback or duplicate production mutation is permitted at any stage.

The additive-surface gate requires all mandatory operations on supported targets, optional-feature
support/refusal, native bootstrap without a circular helper dependency, secret-handling evidence,
shell-policy coverage, and supported workstation/platform live evidence. Missing evidence needs
operator disposition; a successful SSH fixture alone cannot satisfy it.

PR #830 publishes this design before implementation so both lanes can build against main. The
implementation, migration batches and removal are separately reviewed, independently green delivery
units. Temporary released coexistence is intentional, not a permanent compatibility promise.
Permanent docs describe the actual stage in the PR that ships it; do not claim enforced permissions
in additive or migration releases. Broader lifecycle/file acceptance remains open.

## Existing jobs and compatibility

### File consumer migration

Refresh this release inventory again before implementation and cutover. Classify shell snippets that
only create/write/merge/chmod/chown/remove files and move those operations to FileAccess. Record
file-only intent for those resources; withhold commands/jobs only at post-removal activation. Do not
assume every existing shell snippet requires an execution grant forever.

For each destination record the core entry, exact-file/subtree and root-creation scope, approved
actions and metadata, owning resource, and whether a consumer interprets its contents as commands.
Include session-scoped roots derived from trusted session identity, native recovery paths, helper
scratch and platform-host locations. Missing approval requires a core policy change before
activation, not an automatic parent-wide grant, plugin override or fallback to public exec. This
ceiling governs the file API, not arbitrary commands that were separately authorized for real
execution work.

Validate the plan's file-only slice before migrating the wider estate. Prove privileged placement,
JSON value preservation/conflicts and directory/stale-socket operations without caller execution
calls, then run the full harness/session workflows. Isolated tests also exercise execution
interfaces absent, but production withholding waits for removal. Required file operations remain
available on native routes; registration consent and a general plugin permission evaluator are not
cutover prerequisites.

### Jobs and plugin compatibility

Sessions adopt the [shared supervisor and profile design](execution-lifecycle-lld.md), not a second
cgroup implementation. Preserve session UUID/run identity and resource-domain readiness while moving
launch, observation and stop below it. The operator assigned this implementation to transport and
closed #770 on 2026-09-19; its preserved requirements still need complete reconciliation. Audit each
consumer's required profile, action, identity, lifetime and I/O grants, including read-only job
observers and file-only resources. Guest containment is not an in-process plugin sandbox.

Existing sessions do not gain containment by moving their parent into a unit. Untracked detached
descendants require an explicit legacy-run disposition: authorized shutdown/recreation or retained
uncertainty, never a false clean certification. Preserve restart consent and do not broaden an
operation's cleanup targets to unrelated same-user work. Non-systemd placement-host jobs and WSL2
power lifetime must pass their own gates before production cutover.

The two in-tree production `run_detached` callers do not implement intentional cross-invocation
reuse of a completed result: Lima provisioning passes `reuse_completed=False`
(`capabilities/vm_platform/lima.py:623`), and backup creates a fresh directory for each launch
(`vms/backup.py:348`, launch at `:360`). This is a source-level finding about current consumers, not
proof that no external script calls the exported helper. Explicit new job references and later
observation remain required by FRD R6/R11.

Before deleting the old helper, inventory surviving jobs and artifacts. The migration must choose
how an owning operation drains or explicitly adopts its in-progress work, and how owned obsolete
records are disposed. It must not infer new job authority from arbitrary old PID files, kill
unrelated work, or auto-reuse completed results. Any required transition reader has a bounded
purpose and removal point; it cannot become a second execution stack.

Inventory external plugin entry points and contract versions before cutover. Select and document the
version/refusal or migration policy rather than silently accepting incompatible callers. A temporary
internal adapter must not become a separately supported plugin API.

## Worked example: native recovery command

The historical PR #789 proposal, now closed without merge, routed native exec through a separate
manager function calling the limited `run` contract. In the destination, the operation boundary
selects native access and minimal recovery environment, opens the platform route/hold, and creates a
context carrying a native admin target. The common execution service interprets the command,
dispatches it, and renders the same result/error contract as ordinary execution. An optional shell
request can be refused independently.

If the repair needs a script, file, or detached process, the same target supplies it. Nothing at the
caller switches on Proxmox, assumes SCP, or embeds a `nohup` sequence. The route can close after
acknowledged detached launch; later observation opens a fresh authorized context for its job.

## Risks and safeguards

- Existing shell strings may depend on expansion or login profiles. Classify every caller before
  moving it to literal arguments or scripts; do not mechanically split shell source into argv. Fixed
  interpreter, user-default shell, and login/interactive startup are explicit choices. A transport
  change must not select one accidentally.
- Environment and elevation changes can alter guest authority or expose secrets. Preserve scoped
  resolution and prove whole-operation identity and secret absence across adapters.
- Coexistence is not automatic rollback. Reverting a consumer migration must account for new jobs,
  retained artifacts, trust writers and plugin contracts; changing an import does not establish that
  old code can read new state. Never replay an uncertain mutation on legacy. After removal, prefer
  forward repair when an older release cannot interpret the state. A rollback restoring legacy also
  invalidates the new permission-boundary claim.
- A context target can outlive its route accidentally. Lifetime checks and later-observation tests
  must cover both normal exit and exceptions.
- In-flight SSH changes can move migration sites. Reconcile the artifacts after the shared proof;
  #796 records the intended split, not evidence that its runtime contract has been demonstrated.
- Contract versions and any job persistence changes require an explicit compatibility decision after
  the caller inventory. This draft does not assume that aliases or a database migration are
  necessary.
