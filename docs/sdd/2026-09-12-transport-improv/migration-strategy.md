# Transport Improvements: Migration Outline

- Status: Design baseline; contract proof, design reconciliation, parallel build, complete cutover
- Baseline: v0.19.0, `e440a28c49935df722e4e80685ef12f6d8247ff8`, inspected 2026-09-16
- Release delta reviewed: `7c744828..e440a28c`; file helpers, artifacts, harness facets and session
  lifecycle

## Inventory and destination

The current delivery implementations are SSH, Lima, remote Lima, WSL2, and Proxmox QGA. AWS, Azure,
and GCP reuse SSH for native access. The public abstraction has two tiers; `RunContext` currently
delivers the richer tier only.

SSH also reaches remote Lima placement hosts. `capabilities/vm_platform/lima.py:551` constructs this
target, `:616` starts detached VM provisioning on it before the guest exists, and `:704` cancels
that work during rollback. This host target participates in the shared execution/job migration
without being delivered as a guest target through `RunContext`; its macOS-compatible userspace must
be preserved.

The destination is reusable SSH-backed platform access. Remote Lima supplies its first management
commands and guest-hop integration; the shared SSH carrier and host target do not depend on Lima.
Another platform can compose the same host access without inheriting Lima behavior or introducing
another SSH runner. Existing host/guest identities and operation lifetimes remain distinct.

| Current owner                                                                        | Target change                                                                                                           |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `transports/base.py`, concrete transports, Proxmox transport                         | Common execution target above carrier delivery; explicit optional interaction.                                          |
| `transports/__init__.py`, VM platform native/provision results                       | Construct the new target under the same explicit route ownership.                                                       |
| `capabilities/base.py`, VM/agent/session context constructors                        | Deliver the new target type through existing identity accessors.                                                        |
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
core policy remains after cutover. Production continues using the shipped policy until the coherent
switch, never both policies for one mutation.

Retain domain code, not the execution facade: artifact capture, routing, native discovery rules,
generated sections, ownership records and application checkpoints remain in their existing owners.
Setup/readiness invocations carry RunContext alongside descriptive inputs instead of a raw runner;
resources use its bound command/file interfaces directly. Remove access through `files.runner`,
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

## Parallel build after the proof gate

The [design and delivery plan](plan.md) owns the mandatory sequence and proof matrix. First specify
and prove the transport-owned contract with SSH implementation input, then incorporate findings into
both efforts' artifacts. Broad parallel implementation starts only after that gate, not while stream
separation or shell bootstrap is being independently improvised. The OpenSSH 8.5 floor and its
applicable locations are recorded before proof acceptance. The proof is not full-platform acceptance
or a production cutover.

Build the destination execution stack in separate modules while the old production path remains
operational. New internal entry points and test composition roots exercise common requests/results,
shell policy, files/jobs, channel features, and context delivery. Production `RunContext` does not
gain an old/new target union, and plugin authors are not asked to choose a stack.

New contexts deliver the scoped target/access interfaces from the contract proposal, not the full
implementation object. Core composition explicitly supplies required operations and elevation; test
composition also supplies restricted views. The future third-party plugin policy evaluator is not a
dependency of this cutover, but the API must not require another redesign to withhold commands, file
directions, job actions or elevation later. File grants are bounded now by the core-owned mutation
allowlist, not deferred until that evaluator exists. Registration requests and user approval are
future selection mechanisms within the same ceiling.

Develop against the [proposed carrier contract and destination layout](execution-contract.md). The
proposed revised SSH assignment is a new carrier and connection/trust implementation under
`execution/carriers/ssh/`, not consolidation of the old runner. This effort builds shared semantics
and the other adapters, then composes the new SSH carrier. The independent-carrier design in #796 at
`2494f6e2` supersedes #757's legacy consolidation; both SDDs still need reconciliation after the
proof. No old execution code is called from the new stack, directly or indirectly. Copying useful
code and tests is permitted. Transport owns applying reusable SSH policy in platform-host access,
Lima adapters/provisioning and provider-inner paths; SSH owns the policy/guarantees and independent
connection/trust migration.

Old and new implementation code intentionally coexist during development; production continues using
only the old stack until the coherent cutover. Configuration and trust records are retained state,
not disposable implementation. The SSH effort specifies reuse or migration of those records without
resetting host trust, broadening identity selection, or importing the old runner. Resolve trust-file
writer ownership and rollback evidence before switching production; tests of conversion use isolated
copies, not a concurrent second writer against operator state.

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
4. Validate complete new-stack workflows through internal entry points: provisioning, native
   recovery with Tailscale unavailable, plugin operations, backup, and interactive attachment.
   Validate new context delivery independently while the production context still uses the old API.
5. Prepare and validate the complete caller cutover against the settled contract. Audit every call's
   invocation form, shell/startup policy, identity, environment, stdio, deadline, and job lifetime.
   Audit each consumer's needed action interfaces, file locations/metadata and elevation, not merely
   its admin/agent identity. Resolve surviving legacy work, SSH configuration/trust state, and the
   external plugin compatibility policy before switching.
6. Cut over factories, `RunContext` producers/consumers, plugins, and direct service entry points in
   one coherent production increment. Run the same workflow gates through real production entry
   points, physically remove the old stack and temporary test scaffolding, and update collateral.
   Prove production package import and workflows still work after removal, not only that factories
   prefer the new path.

The cutover gate requires all mandatory operations on supported targets, optional-feature
support/refusal, native bootstrap without a circular helper dependency, secret-handling evidence,
shell-policy coverage, and supported workstation/platform live evidence. Missing evidence needs
operator disposition; a successful SSH fixture alone cannot satisfy it.

Implementation can use successive commits and internal test harnesses on its feature branch. The
current delivery publishes the reviewed design baseline before proof, without claiming a proven
implementation boundary. The default implementation landing unit contains the new stack and complete
cutover together; splitting it later requires independently complete units and an explicit removal
point. Temporary coexistence during development is not a promise to release two public APIs or a
runtime selection flag.

## Existing jobs and compatibility

### File consumer migration

Refresh this release inventory again before implementation and cutover. Classify shell snippets that
only create/write/merge/chmod/chown/remove files and move those operations to FileAccess. Withhold
commands/jobs from resources whose remaining work needs only files; do not assume every existing
shell snippet requires an execution grant forever.

For each destination record the core entry, exact-file/subtree and root-creation scope, approved
actions and metadata, owning resource, and whether a consumer interprets its contents as commands.
Include session-scoped roots derived from trusted session identity, native recovery paths, helper
scratch and platform-host locations. Missing approval requires a core policy change before cutover,
not an automatic parent-wide grant, plugin override or fallback to public exec. This ceiling governs
the file API, not arbitrary commands that were separately authorized for real execution work.

Validate the plan's file-only slice before migrating the wider estate. Prove privileged placement,
JSON value preservation/conflicts and directory/stale-socket operations with execution interfaces
absent, then run the full harness/session workflows. Required file operations remain available on
native routes; registration consent and a general plugin permission evaluator are not cutover
prerequisites.

### Jobs and plugin compatibility

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
- Before cutover, the old production path remains the rollback point. After cutover, rollback must
  account for new jobs, retained artifacts, and plugin contract changes; changing only an import
  does not establish that old code can read new state. Prefer a forward repair when it cannot.
- A context target can outlive its route accidentally. Lifetime checks and later-observation tests
  must cover both normal exit and exceptions.
- In-flight SSH changes can move migration sites. Reconcile the artifacts after the shared proof;
  #796 records the intended split, not evidence that its runtime contract has been demonstrated.
- Contract versions and any job persistence changes require an explicit compatibility decision after
  the caller inventory. This draft does not assume that aliases or a database migration are
  necessary.
