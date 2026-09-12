# Transport Improvements: Migration Outline

- Status: Revised draft; contract proof, design reconciliation, parallel build, complete cutover
- Baseline: `7c744828184ccb0ad9ffd90a8a02226384fb384e`, inspected 2026-09-12

## Inventory and destination

The current delivery implementations are SSH, Lima, remote Lima, WSL2, and Proxmox QGA. AWS, Azure,
and GCP reuse SSH for native access. The public abstraction has two tiers; `RunContext` currently
delivers the richer tier only.

SSH also reaches remote Lima placement hosts. `lima.py:551` constructs this target, `:616` starts
detached VM provisioning on it before the guest exists, and `:704` cancels that work during
rollback. This host target participates in the shared execution/job migration without being
delivered as a guest target through `RunContext`; its macOS-compatible userspace must be preserved.

The destination is reusable SSH-backed platform access. Remote Lima supplies its first management
commands and guest-hop integration; the shared SSH carrier and host target do not depend on Lima.
Another platform can compose the same host access without inheriting Lima behavior or introducing
another SSH runner. Existing host/guest identities and operation lifetimes remain distinct.

| Current owner                                                                   | Target change                                                                                          |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `transports/base.py`, concrete transports, Proxmox transport                    | Common execution target above carrier delivery; explicit optional interaction.                         |
| `transports/__init__.py`, VM platform native/provision results                  | Construct the new target under the same explicit route ownership.                                      |
| `capabilities/base.py`, VM/agent/session context constructors                   | Deliver the new target type through existing identity accessors.                                       |
| Capability readiness and operations, setup invocation runners                   | Consume the common type without rebuilding transports or context authority.                            |
| `harness_setup/runner.py`                                                       | Move prepared environment policy into shared target defaults; retire forwarding implementation.        |
| `remote_exec.py`, backup, remote Lima host provisioning/rollback, native logout | Use managed job start/observe/wait/dispose with explicit retention and actual execution-host identity. |
| VM/agent exec and shell, sessions/consoles                                      | Preserve command/stdin and terminal behavior while using shared execution and feature checks.          |
| SSH-named shared result/error/logger types                                      | Move generic execution facts into transport-neutral vocabulary.                                        |

## Parallel build after the proof gate

The [design and delivery plan](plan.md) owns the mandatory sequence and proof matrix. First agree
and prove the small shared contract, then incorporate findings into both efforts' artifacts. Broad
parallel implementation starts only after that gate, not while stream separation or shell bootstrap
is being independently improvised. The OpenSSH 8.5 floor and its applicable locations are recorded
before proof acceptance. The proof is not full-platform acceptance or a production cutover.

Build the destination execution stack in separate modules while the old production path remains
operational. New internal entry points and test composition roots exercise common requests/results,
shell policy, files/jobs, channel features, and context delivery. Production `RunContext` does not
gain an old/new target union, and plugin authors are not asked to choose a stack.

New contexts deliver the scoped target/access interfaces from the contract proposal, not the full
implementation object. Core composition explicitly supplies required operations and elevation; test
composition also supplies restricted views. The future third-party plugin policy evaluator is not a
dependency of this cutover, but the API must not require another redesign to withhold commands, file
directions, job actions or elevation later.

Develop against the [proposed carrier contract and destination layout](execution-contract.md). The
proposed revised SSH assignment is a new carrier and connection/trust implementation under
`execution/carriers/ssh/`, not consolidation of the old runner. This effort builds shared semantics
and the other adapters, then composes the new SSH carrier. This differs from #757's currently
published plan; the SSH developer supports the revised assignment in feedback relayed by the
operator, and their artifacts still need reconciliation after the proof. No old execution code is
called from the new stack, directly or indirectly. Copying useful code and tests is permitted.
Transport owns applying reusable SSH policy in platform-host access, Lima adapters/provisioning and
provider-inner paths; SSH owns the policy/guarantees and independent connection/trust migration.

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
   Audit each consumer's needed action interfaces and elevation, not merely its admin/agent
   identity. Resolve surviving legacy work, SSH configuration/trust state, and the external plugin
   compatibility policy before switching.
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
current delivery remains a draft design PR. The default implementation landing unit contains the new
stack and complete cutover together; splitting it later requires independently complete units and an
explicit removal point. Temporary coexistence during development is not a promise to release two
public APIs or a runtime selection flag.

## Existing jobs and compatibility

The two in-tree production `run_detached` callers do not implement intentional cross-invocation
reuse of a completed result: Lima provisioning passes `reuse_completed=False` (`lima.py:616`), and
backup creates a fresh directory for each launch (`vms/backup.py:345`). This is a source-level
finding about current consumers, not proof that no external script calls the exported helper.
Explicit new job references and later observation remain required by FRD R6/R11.

Before deleting the old helper, inventory surviving jobs and artifacts. The migration must choose
how an owning operation drains or explicitly adopts its in-progress work, and how owned obsolete
records are disposed. It must not infer new job authority from arbitrary old PID files, kill
unrelated work, or auto-reuse completed results. Any required transition reader has a bounded
purpose and removal point; it cannot become a second execution stack.

Inventory external plugin entry points and contract versions before cutover. Select and document the
version/refusal or migration policy rather than silently accepting incompatible callers. A temporary
internal adapter must not become a separately supported plugin API.

## Worked example: native recovery command

The current PR #789 proposal routes native exec through a separate manager function calling the
limited `run` contract. In the destination, the operation boundary selects native access and minimal
recovery environment, opens the platform route/hold, and creates a context carrying a native admin
target. The common execution service interprets the command, dispatches it, and renders the same
result/error contract as ordinary execution. An optional shell request can be refused independently.

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
  developer support for the direction does not mean #757 already records the proven contract.
- Contract versions and any job persistence changes require an explicit compatibility decision after
  the caller inventory. This draft does not assume that aliases or a database migration are
  necessary.
