# Transport Improvements: Design and Delivery Sequence

- Status: Additive implementation started from merged #830; production remains unchanged
- Delivery vehicle: Design PR #830, then additive implementation, consumer migration PR(s), and
  final removal/activation PR; all labeled `sdd:transport-improv`
- Requirements: [FRD](frd.md)
- Architecture: [HLA](hla.md)
- Proposed interfaces and layout: [Execution contract](execution-contract.md)
- Active proof implementation: [Proof LLD and evidence](proof-lld.md)
- Proposed lifecycle: [Execution profiles and supervisor design](execution-lifecycle-lld.md)

The required order is: settle the transport-owned small contract, prove it, reconcile both SDDs,
build independently in parallel, validate complete workflows, add the new RunContext surface,
migrate consumers in separate PRs, then physically delete legacy and activate permissions. The proof
is a bounded joint slice, not permission to start the broad rebuild. This proof now has a
transport-side implementation, local tests and successive joint live reports. The
[proof evidence](proof-lld.md) records acceptance of the finite-input slice and its limits. No
broad-build or production-cutover gate is completed by those measurements.

The transport lead owns this entire sequence, not just the API design. The operator confirms the
mandate to build with the SSH developer, migrate all consumers and physically delete the old stack.
The [0.19.0 migration inventory](migration-strategy.md) is the current baseline. `NativeFiles` is
retired, while useful domain behavior and evidence are preserved through direct RunContext access.

The operator directs publication of this reviewed baseline to `main` before the proof. Publication
gives both efforts a common design reference; it does not pass the proof, complete an LLD or freeze
the SDD. Proof-informed amendments follow through the same artifact owners. The transport lead owns
the carrier contract and acceptance criteria; SSH supplies implementation and feasibility input, not
a separately owned copy of that contract. Requirement changes still return to the operator.

After #795 merged, the operator authorized transport-side PoC work in a new PR while the SSH owner
updates its SDD. Both efforts start from the published contract, without another prerequisite
design-only merge. Transport integrates one joint proof delivery with the SSH contribution; SSH's
complete carrier implementation follows proof acceptance as a separate code delivery. The
[proof LLD](proof-lld.md) records this first implementation's exact subset, placement and evidence
gaps. None of the joint proof checkboxes below is completed by starting that work.

On 2026-09-17 the operator accepted deferring guest cancellation from the buffered PoC. Deadlines
remain local observation bounds. Live tests demonstrated surviving guest process trees after expiry;
the PoC has neither a remote cancellation handle nor a reaper. Recording that limitation does not
waive the production workload-lifecycle gate below or permit automatic replay.

## Buffered PoC checkpoint record

- [x] Publish the finite-input transport candidate and local fault evidence at `e3d93736` in #826;
      production factories and RunContext remain unchanged.
- [x] Obtain the first joint live report on 2026-09-17 at SSH `1c32e415` containing transport
      `a570a2de`: all eight shared vectors passed on the measured SSH cells and native PVE 8/9. The
      [evidence record](proof-lld.md) preserves gaps and does not declare joint acceptance.
- [x] Close the authorized feedback rounds and retest affected behavior at the final pinned
      transport/SSH combination, with independent cleanup evidence, before proof merge readiness.
- [x] Obtain the second joint live report on 2026-09-17 at transport `d75c0bd3` and SSH `1c32e415`:
      native TLS retesting, the Bash 5.1 floor and cleanup addendum are measured. The Windows SSH
      failure remains unresolved; this is evidence collection, not proof acceptance.
- [x] Obtain explicit live destination-account default-shell evidence through both carriers, and the
      reviewed SSH candidate's Windows disposition and affected-case retest. The eight shared
      vectors alone do not exercise `Shell.user_default()` or establish Windows delivery.
- [x] Retest the strengthened sensitive-reflection vector through both carriers on the final
      integrated head, and obtain affected macOS drain evidence or a precise case-level
      justification. Raw completion alone must not establish bootstrap or application execution.

The final-candidate report at transport `6617f6e6` / SSH `1ccc304b` measures exit 37 with
suppression in all six cells and fresh macOS live/local-pipe evidence. At transport `e41a4482` / SSH
`bc2a0711`, the tester independently verified unchanged runtime and carried those live cells
forward, rather than claiming fresh measurements. The affected macOS socket-fixture retest passed
under normal and long temporary paths; its local execution suite now reports 255 passed, 45
accounted platform-scoped skips and zero failures. The lead independently verified ancestry and
runtime equivalence and ran the combined Linux suite, 296 passed and four skipped. The four
authorized feedback rounds are closed. The final transport delta at `931ad8ef` is documentation
only; its local combination with SSH `bc2a0711` at `2b1f1d39` has an identical `cli/` tree to the
tested SSH candidate. The tester explicitly carried forward native evidence and found no need for
another live retest. Transport accepts the joint buffered proof using those reports and verified
tree equivalence, not an additional tester acknowledgment. The
[proof evidence](proof-lld.md#final-macos-delta-and-unchanged-runtime-evidence-2026-09-17) records
exact pins, independent cleanup and unchanged broader limitations. The
[acceptance disposition](proof-lld.md#joint-buffered-proof-acceptance-2026-09-17) maps the proof
matrix to that evidence. Design reconciliation, the broader contract and production gates remain
open; the SDD is not complete and must not be locked.

## Parallel ownership without overlapping edits

### Active implementation, 2026-09-19

The operator directed implementation after merging #830 at `cea5e852`. The additive delivery branch
is `feat/transport-execution-stack`; it builds the complete new surface without migrating existing
production consumers. SSH proceeds in its own lane. Development delegates use isolated worktrees
from the same published baseline; the transport lead integrates their reviewed work.

The first bounded assignments complete the file-operation and invocation/result LLDs and refresh the
RunContext/platform adoption inventory. These close implementation details already called out below,
not another requirements phase or a new prerequisite design-only PR. The lead owns shared types,
carrier-contract changes, composition and the overall plan. SSH implementation files and its SDD
remain SSH-owned. Broad changes wait for their relevant detailed-design/proof gate; limited
experiments and implementation of settled pieces stay unwired until production acceptance.

The cgroup/session ownership disposition remains required before overlapping lifecycle
implementation. File, preparation and context work can proceed independently of that ruling.
Recipient permissions and the successor core file ceiling stay inactive until final legacy removal,
while operational safety and deliberately selected profile guarantees apply immediately.

No new public feedback/fix allowance is inferred from the completed #830 review. A coherent
checkpoint receives the normal private reviews and validation before a testing brief and
`review-requested`; the additive implementation is marked ready only when its own gates pass.

After the shared seam and LLD gates, the lead may charter bounded migration packages against one
pinned contract. The following is an assignment plan, not a claim that developers are allocated:

| Package                                       | Exclusive responsibility                                                                                                                                                            |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transport lead                                | Shared execution API/helpers/file policy, `capabilities/base.py`, composition boundaries, integration gates and final deletion.                                                     |
| SSH developer                                 | `execution/carriers/ssh/`, connection/trust migration and associated tests, per #796.                                                                                               |
| Optional harness/artifact migration developer | `harness_setup/`, harness setup/readiness invocation types, artifact publication/probes and harness plugin consumers; preserve domain behavior while replacing runner/file facades. |
| Optional session migration developer          | Session/tmux/console consumers and tests; preserve session/run identity, restart consent, runtime evidence and owned cleanup.                                                       |
| Optional platform/CLI migration developer     | Non-SSH adapters, VM exec/recovery, backup and workspace transfer consumers, with explicit per-file assignment before starting.                                                     |

Every charter names exact files and tests; overlapping files remain with the lead or are handed off
explicitly before another developer touches them. Separate working trees/branches share the pinned
contract, not a mutable working tree. Cross-package requests return to the lead; only the lead
integrates changes to common types and production composition. After additive delivery, migration
batches may land independently against its pinned contract. Temporary released coexistence is
explicitly authorized; final removal remains this effort's responsibility. Refresh the inventory at
each integration boundary and prohibit new legacy consumers.

## 1. Specify the small contract and proof charter

- [ ] Specify `PreparedInvocation`, the single input choice in `CarrierIO`, stream ownership,
      failure behavior and `CarrierReport`, with SSH implementation input. Done when transport
      publishes one candidate contract and acceptance matrix for both efforts to use, including
      single-attempt semantics and explicit treatment of unresolved feasibility questions.
- [ ] Record the OpenSSH 8.5 minimum's applicable binaries/locations and server compatibility in the
      SSH-owned design. Include workstation, platform-host and provider-inner invocation sites; no
      version requirement may be silently assumed from another hop's client.
- [ ] Confirm ownership: transport owns shared preparation/public outcomes and applying SSH policy
      in platform adapters and provisioning; SSH owns reusable connection/isolation policy, carrier
      delivery and trust/configuration migration. Remote Lima is the first consumer of SSH-backed
      platform access, not a concept inside SSH or a dependency for later platform consumers.
- [ ] Obtain a bounded proof/live-test charter naming isolated resources, tool versions, workload,
      cleanup and evidence. This artifact round does not run the proof or select live resources.

## 2. Prove the shared boundary before broad implementation

Transport and SSH contributors jointly produce one new-stack end-to-end buffered invocation slice.
Transport owns preparation, result interpretation and the proof harness; SSH owns its independent
connection/delivery portion. Useful existing code/tests may be copied, never called through legacy
execution modules. This is not a full file/job implementation or a preliminary legacy consolidation.

| Proof case                            | Evidence required to pass                                                                                                                                                                                                                                       |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Literal execution and shell bootstrap | Empty/quoted/special-character argv arrives intact; fixed interpreters and explicit user-shell selection, env/cwd and elevation have intentional behavior. Record supported account-shell combinations and startup-hook effects separately from payload policy. |
| Source and finite input               | Script source is not consumed as application stdin; finite bytes survive exactly and absent input is EOF. Sensitive-input cases prove suppression in results, diagnostics and owned artifacts.                                                                  |
| Guest streams                         | Ordinary-input cases preserve binary stdout/stderr, separately from carrier diagnostics, including NUL and newline-sensitive bytes. Do not require returned sensitive output as proof of byte fidelity.                                                         |
| Outcomes and observation              | Exits 0/1/255, lost contact and interrupted observation retain the facts actually known. A 255/drop may remain ambiguous; interruption reaches operation cleanup. No replay or inferred guest termination.                                                      |
| Readiness                             | A bounded invocation works without helper installation, staging or spooling, and without requested profile initialization. Demonstrate guest-stream behavior on this path too; account hooks are the separately documented carrier prerequisite.                |
| I/O ownership and failure             | Focused stream tests prove EOF, borrowed-stream lifetime, short writes, bounded flow control/cancellation and safe source/sink failure. Failure cannot become success, silent discard or confirmed remote cancellation.                                         |
| Non-SSH shape                         | A bounded Proxmox QGA case exercises the same request/report contract, finite input, output limits and no-staging readiness. A fake alone does not establish live feasibility or supported-version coverage.                                                    |

- [x] Complete the proof matrix with observed evidence and a pinned candidate contract. A failed or
      unresolved shell/input/stream boundary blocks broad parallel implementation; revise the seam
      and repeat the affected cases. If the needed mechanism changes scope, return to the operator.
      This gate does not claim exact SSH exit/drop classification or full platform acceptance.

This completion covers the defined buffered candidate: finite bytes/EOF, capture/discard and the
authorized connection identity. The
[acceptance disposition](proof-lld.md#joint-buffered-proof-acceptance-2026-09-17) records each row's
applicable evidence. Optional live/terminal modes, scoped elevation and arbitrary startup hooks are
not advertised or accepted by this slice. The specification and reconciliation tasks remain
responsible for the broader interface before parallel implementation; neither this checkbox nor the
PoC merge enables production use.

## 3. Reconcile designs and publish the implementation boundary

- [ ] Incorporate proof findings into this SDD and have the SSH owner reconcile #796's independent
      carrier design against the same proven contract. #796 supersedes #757; no legacy consolidation
      precedes the rebuild. Each effort edits only its own artifacts.
- [ ] Review and publish proof-informed design revisions before broad parallel work. Record the
      transport-owned contract revision and evidence both efforts will build against. Publishing the
      initial baseline in #795 does not satisfy this post-proof gate.
- [ ] Complete execution/context and file/job LLDs: shell/startup combinations, no-staging
      readiness, bootstrap tools, bounded transfer/path policy, jobs/cancellation/retention and
      stale records. Preserve macOS host jobs before guest creation. Establish remaining
      Proxmox/WSL2 feasibility under an authorized live-test charter; the small proof does not stand
      in for these checks.
- [ ] Before production enablement, implement and validate owned workload lifecycle and explicit
      cancellation, including ordinary descendants, stale/reused process identity, disconnected
      observation, bounded cleanup and truthful confirmation or uncertainty. Keep local waiting
      deadlines distinct from guest lifetime. The operator deferred this from the PoC only.
- [ ] Complete the file LLD for R7: whole-file publication, JSON merge semantics, ownership/mode and
      security metadata preservation, bounded inventory, directories/conditional removal,
      concurrency and uncertain results. Preserve the shipped four settings strategies and JSON
      literal-null behavior rather than adopting RFC 7396 deletion implicitly. Keep TOML and
      generated-section transforms in their domain, backed by snapshots/conditional publication.
      Enumerate tools available during native bootstrap and on supported platform hosts; prove
      destination-side confinement rather than relying on a preflight path check. Define trusted
      ancestors/mounts, private staging/locks, root creation and fail-closed behavior.
- [ ] Inventory intended mutation destinations and actions for harness configuration, `/opt`
      provisioning, `/run` session objects, recovery and platform hosts. Review execution-bearing
      content and select explicit core allowlist entries, trusted dynamic-root resolution and
      recipient subsets for post-removal activation. Consumers choose the appropriate file API
      rather than broad parent intents or public-exec workarounds to avoid a future restriction.
- [ ] Finalize execution/file interfaces and separately granted actions/profiles, including the core
      file ceiling, with production enforcement deferred until legacy removal. Keep registration
      requests, user consent, a general plugin policy evaluator and hostile in-process plugin
      isolation out of scope. Map FRD R11's future workflows to tests.
- [ ] Reconcile #796's pinned transport reference with the final reviewed contract and later
      file-only slice. SSH owns its artifact edits; shared file semantics and cutover stay here.

## 4. Build the independent stacks in parallel

The transport lead owns shared profiles, supervisor lifecycle and session adoption. Migration
delegates consume this implementation rather than building another launcher; SSH owns delivery,
connection and trust only. Before broader lifecycle implementation, complete these additional gates:

- [ ] Review unified execution/file access and independent invocation, observation, I/O, lifetime,
      protection and identity dimensions. Approve typed shell constants, exact additive profile
      grants and refusal without downgrade. The [lifecycle design](execution-lifecycle-lld.md)
      supplies the proposal, not implementation proof.
- [ ] Reconcile the complete #770 requirements, threat model, acceptance cases and exclusions using
      the [preserved input](inputs/session-cgroups-frd-2c406948.md), not the routing index as a
      replacement FRD. Obtain explicit owner/operator disposition before overlapping lifecycle
      implementation or artifact retirement; at transfer carry the accepted text into its designated
      requirements home and reconcile any later source changes.
- [ ] Complete and prove Linux supervisor launch through SSH and native QGA: protected identity,
      secret/source delivery, privilege changes, foreground wait, independent launch, output
      retention and terminal evidence. No workload code runs before boundary entry.
- [ ] Prove lost-acknowledgment reconciliation, wait timeout versus stop, observer-loss cleanup,
      runtime-anchor death, concurrent forks, stale identity and independently verified emptiness.
      Settle the OPERATION liveness/lease protocol before offering target-side cleanup.
- [ ] Complete CONTAINED's access map, compare restricted same-UID and per-run-user designs, and
      prove the complete #770 escape/relaunch and trusted socket-identity acceptance cases. Record
      compatibility costs requiring operator disposition.
- [ ] Resolve non-systemd macOS host jobs, Debian/kernel/systemd floors, WSL2 power lifetime and
      no-staging recovery. Required workflows block delivery when their guarantees cannot be met; no
      profile downgrade or fabricated platform equivalence. Use the
      [observable proof criteria](execution-lifecycle-lld.md#delivery-sequence-and-proof-criteria)
      and [reported test-bed gaps](prior-art-research.md#lifecycle-test-bed-gaps). For macOS host
      jobs, prove all MANAGED ownership/tracking/stop/emptiness promises and independent lifetime;
      workstation SSH evidence is insufficient. Price missing mechanics or infrastructure for
      operator disposition rather than silently dropping required host work.

- [ ] SSH effort builds `execution/carriers/ssh/` and its connection/trust migration. Transport
      builds common execution, scoped context delivery, files/jobs and other adapters, and applies
      SSH policy in platform-host/Lima/provisioning paths. Test reusable host composition separately
      from Lima commands; preserve one SSH implementation in the new stack.
- [ ] Integrate against the agreed contract and run the new-stack tests with legacy modules
      unavailable, including indirect imports and plugin initialization. No production old/new
      selector, shared legacy runner, or duplicate mutation dispatch is allowed.
- [ ] Specify and validate SSH state transition, concurrent-writer ownership and rollback evidence.
      Preserve configuration and complete trust records without importing old execution code.
- [ ] Deliver a shared file-only vertical slice through SSH and native QGA, without caller
      command/job calls: whole-file installation, privileged JSON merge preserving unrelated keys,
      approved directory creation/metadata and conditional removal. Add a session-owned stale-socket
      case; tmux creates sockets and no FIFO creation is required. Keep the initial small carrier
      proof intact; this later slice gates broader file-consumer migration, not the independent SSH
      build. Restricted test composition can withhold execution; production permissions remain
      inactive.
- [ ] Prove file correctness from first use and the future permission boundary in isolated tests:
      default denial, exact-file/subtree scopes, root versus parent authority, prefix
      collisions/traversal, links and concurrent substitution, confined extraction, forbidden
      metadata/removal, and inability to widen grants. Cover attempted helper redirection through
      caller environment, PATH or working directory, cooperating writers, external-writer limits,
      malformed JSON, special-object refusal, sensitive diagnostics, partial transfer/cleanup and
      uncertain publication. No runtime fallback may expose commands to the file-only caller;
      unavailable safe mechanics block acceptance. Record live target/platform evidence under an
      authorized charter. Adapt 0.19.0's boundary, settings, generated-section/ACL,
      publication-checkpoint and native-inventory tests to new delivery; copying tests does not
      establish the stronger race/concurrency promises by itself.

## 5. Add the complete new RunContext surface

The [2026-09-19 ruling](frd.md#operator-rulings-2026-09-19) authorizes three delivery stages, not
one all-callers cutover. PR #830 publishes this design only. The following implementation PR adds
the new surface; migration and removal follow in their own PRs. Permissions are groundwork until
removal: do not enforce new recipient grants or the successor core file ceiling, or rely on their
isolation, in coexistence releases. Operational safety and selected profile guarantees still apply.

- [ ] Add `admin_execution_target()` and `agent_execution_target()` to the existing RunContext,
      returning the new target without changing legacy accessors or callers. Use permanent names, no
      union target type, stack selector or forwarding adapter. Prove passive construction/access and
      composition-owned lifetime, no new effects on existing callers, and independent new-stack
      usability with legacy modules unavailable. Do not claim restricted recipient authority.
- [ ] Validate complete provisioning, native recovery without Tailscale, plugin operations, files,
      backup, host provisioning/rollback and interactive attachment through the new surface. Cover
      required operations, optional refusal, sensitivity and supported workstation/platform
      versions. Resolve trust-state writer ownership before new production use. Missing evidence
      requires operator disposition, never a passing claim.

## 6. Migrate consumers in owned workflow batches

- [ ] Complete the
      [incident-derived behavior inventory](migration-strategy.md#incident-derived-behavior-inventory)
      before deleting legacy code/tests. Each entry records its old source/test, new owner and
      replacement regression, required workstation/platform evidence, and explicit disposition. ADR
      0020, Windows stdin conversion and Git-for-Windows toolchain assumptions are seed cases, not
      an exhaustive inventory or a claim of new-stack validation.
- [ ] Assign non-overlapping migration PRs for harness/artifacts, sessions/consoles and platform/CLI
      consumers as needed. Each batch records legacy call sites, permanent new accessors, owner,
      deliberate operation choices, intended grants/paths, regression/live evidence and surviving
      state disposition. Prove complete production workflows per batch, with no fallback or
      duplicate mutation. Keep unmigrated callers unchanged; freeze new legacy use.
- [ ] Complete the [migration inventory and cutover gates](migration-strategy.md): retain #789's
      historical recovery intent without integrating its closed branch, audit caller
      shells/identity/I/O/lifetimes/grants and approved filesystem destinations, migrate
      file-provisioning shell snippets to FileAccess where it expresses the operation, resolve
      surviving jobs, plugin compatibility, state migration and rollback. Every old entry point has
      a destination and removal point.
- [ ] Replace `NativeFiles`, `files.runner`, exposed staging slots and raw setup/readiness runners
      with RunContext access. Preserve artifact ownership/checkpoints, JSON/TOML settings behavior,
      generated-section surroundings/metadata, session/run identities and restart confirmation.
      Validate native inventory and identity discovery without caller exec in file-only operations.
      Keep genuinely executable harness CLI work behind command access, not a disguised file API.
- [ ] Migrate sessions and other jobs to the same supervisor. Preserve session UUID/run IDs,
      tmux/harness readiness, restart consent, legacy-run uncertainty and owned cleanup. Do not
      certify legacy detached descendants by moving only a surviving parent into a new cgroup.

## 7. Remove legacy and activate permissions

- [ ] Require zero remaining legacy consumers, including direct/lazy imports and external plugin
      entry points under the reviewed compatibility policy. Complete the behavior inventory and
      surviving-job/state disposition before deleting their readers. Transport owns this final PR.
- [ ] Physically delete legacy accessors, factories, the retirement packages including
      `agentworks.native_files`, and temporary scaffolding. Prove installed-package startup and
      complete production workflows without them. Retain operator trust/configuration and cleanup
      evidence; replacing code never authorizes deleting that state.
- [ ] Activate explicit core recipient grants and the successor file allowlist only with legacy
      absent. Inventory all approved workflow actions/paths before activation, then prove restricted
      file-only, observe-only, elevation and exact-profile views, denial before effects and no
      indirect bypass. Keep operational failure, channel support and permission denial distinct.
      This is not registration/consent or an in-process plugin sandbox. If either removal or
      enforcement evidence fails, do not claim permission isolation or close the effort.

Each stage is independently green and updates permanent collateral to the behavior it actually
ships. Temporary coexistence has an explicit final removal owner and gate, not indefinite support
for two APIs. No checkbox above claims that the separately owned SSH work is done.

Future implementation satisfies FRD R1-R11 and promotes implemented contracts into permanent docs
with the code that makes them true. Closeout requires evidence-backed validation, complete
retirement and a truthful final plan before creating `locked.md`.
