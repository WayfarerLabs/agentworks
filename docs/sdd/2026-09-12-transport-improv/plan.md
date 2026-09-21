# Transport Improvements: Design and Delivery Sequence

- Status: Additive implementation started from merged #830; production execution factories and
  RunContext remain unchanged. New-guest bootstrap installs distribution Python. The private
  destination-lock implementation and its setup have been removed under the coordination ruling
  below. A database operation-ownership primitive is implemented but not yet wired into production
  orchestration or RunContext; it does not yet prevent production operation conflicts.
- Delivery vehicle: Design PR #830, then additive implementation, consumer migration PR(s), and
  final removal/activation PR; all labeled `sdd:transport-improv`
- Requirements: [FRD](frd.md)
- Architecture: [HLA](hla.md)
- Proposed interfaces and layout: [Execution contract](execution-contract.md)
- Active proof implementation: [Proof LLD and evidence](proof-lld.md)
- Proposed lifecycle: [Execution profiles and supervisor design](execution-lifecycle-lld.md)
- Shared I/O experiment: [Carrier I/O candidate](carrier-io-lld.md), pending joint review/proof
- Detailed candidates: [Preparation and results](preparation-lld.md) and
  [file operations](file-operations-lld.md), with their substrate decisions and proofs still open

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

## Operation coordination correction, 2026-09-20

The operator approved database-level operation coordination, unique scratch names and conservative
file checks instead of blanket machine-wide destination locking and privileged host setup. The
completed lock experiments below remain historical records; the lock implementation and its
associated pending acceptance gates are superseded by this ruling.

- [x] Remove destination lock acquisition, setup, bundled dependencies and lock-only failure codes
      from the private file helpers and new-guest provisioning. Preserve Python installation,
      identity checks, object refusal, revisions, bounds, expiry and exact cleanup evidence. Prove
      bounded read/stat and file operations work without an installed lock namespace.
- [x] Implement atomic database operation admission for conflicting resource scopes, with durable
      ownership and explicit terminal release. Keep SQL transactions short; do not hold a database
      write lock during remote execution or coordinate independent databases through a new service.
- [ ] Carry the same operation ownership through core orchestration, RunContext and nested file
      composition. Serialize conflicting exchanges inside that ownership; cover user/admin writers
      and shared platform-host resources without splitting ownership by transport route or identity.
      Retire superseded local harness coordination during consumer migration, not through a second
      competing new-stack lock.
- [ ] Cover pre-context activation and nested teardown when wiring ownership. At `806741ca`,
      `gated_vm_boundary` enters `activation_gate` before assembling its ordinary operation context,
      and `LiveVMNode` constructs a separate gate context. Context factories, harness setup's
      explicit held-guard chain and parameterless realization teardown must retain the same
      core-owned operation when migrated. Adding a field to RunContext alone is insufficient.
- [ ] Select shared platform-host resource keys before enabling their admission. Canonical VM names
      are available before create dispatch; site names and authored SSH routes are not canonical
      host identities. Do not silently treat different aliases or users as independent hosts.
- [ ] Prove crash, disconnect and deadline handling retain unresolved ownership. Recovery must
      establish that prior remote work cannot still mutate before admitting conflicting work, and
      must not replay uncertain mutation or silently expire a claim. Report incomplete recovery
      rather than deleting ownership or inventing remote fencing from a database row.
- [ ] Keep VM-host lifecycle platform-owned. Prove actual Lima readiness, stop, rollback and
      disconnected-operation recovery without requiring a generic macOS MANAGED supervisor or an
      administrator-installed file lock. Keep Linux guest MANAGED guarantees unchanged.
- [ ] Complete the private reviews and gates for this replacement, update permanent collateral, and
      publish the corrected design and implementation as part of the still-draft effort.

The first implementation increment is privately reviewed at `063cd0bc` by the project, complexity
and generic correctness lanes. It removes the lock/setup stack and supplies `Database.operations`,
not production operation coordination. Admission through core orchestration, nested RunContext and
file exchanges, resource-key selection and operation-specific recovery remain unchecked above.

Review restored initial deadline refusal before filesystem access in all five helper families,
preserved phase and known cleanup debt for all four staging operations, corrected unsafe backup
retry guidance and removed redundant typed-interior validation. The eight-case deadline regression
uses synthetic identity rather than Unix-only calls during collection. Final reviewers each pass 34
affected deadline/staging tests. No native VM or host acceptance is claimed. The independent local
launch-owner cleanup-entry interrupt gap remains open and is documented in the preparation LLD; this
increment does not introduce global signal handling or declare launch conformance.

### Hierarchical coordination follow-up (#377)

The operator directs compatibility with
[#377](https://github.com/WayfarerLabs/agentworks/issues/377), not completion of all its
functionality in this effort. The [HLA](hla.md#operation-coordination-and-hierarchical-extension)
keeps admission centralized and requires conflicts in both directions between a resource and its
ancestors/descendants. The current `Database.operations` implementation checks exact
VM/platform-host keys only; it is neither a hierarchy nor a complete production lock service. The
completed primitive checkbox above records that exact-key implementation, not broader #377
acceptance.

Not implemented in the current tree, and deferred to the #377 follow-up unless explicitly brought
into this effort:

- System, workspace, agent, session and console claim types and atomic ancestor/descendant
  admission.
- Independent sibling concurrency inside a VM, multi-resource acquisition and the schema/caller
  transition that prevents fine-grained claims from bypassing existing coarse ownership.
- Long-lived console/session claims and the VM-upgrade-versus-attached-console acceptance case,
  including ordinary detach/release and stale lifetime-claim recovery.
- Operator CLI commands to list locks and explicitly force-unlock, with blocker-specific diagnostics
  and a clear distinction between an unsafe override and evidence-backed recovery.
- The repository-wide concurrency sweep requested by #377. Passing this transport effort's tests
  must not imply that unrelated legacy commands permit all non-conflicting concurrent work.

The transport-owned production wiring and recovery gates immediately above remain required; this
follow-up does not defer them. Existing claim timestamps and bounded operation labels are already
implemented, while the broader inspection and conflict UI are not. No automatic expiry or
force-release is added under the guise of hierarchy compatibility.

- [ ] Review the implemented admission boundary against the #377 extension: centralized resource
      identity/conflict decisions, ownership preserved through activation/nested contexts, and no
      new fine-grained key that bypasses coarse exclusion. Record exact delivered scope and
      evidence.
- [ ] At final SDD closeout, explicitly list in `locked.md` the delivered coordination levels and
      production paths, remaining limitations, and each still-unimplemented item above with #377 as
      follow-up. Keep the issue open unless separately completed and verified. Do not create the
      lockfile early or represent deferred hierarchy work as completed transport implementation.

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
production consumers. SSH proceeds in its own lane. Development delegates use isolated working trees
from the same published baseline; the transport lead integrates their reviewed work.

The first bounded assignments complete the file-operation and invocation/result LLDs and refresh the
RunContext/platform adoption inventory. These close implementation details already called out below,
not another requirements phase or a new prerequisite design-only PR. The lead owns shared types,
carrier-contract changes, composition and the overall plan. SSH implementation files and its SDD
remain SSH-owned. Broad changes wait for their relevant detailed-design/proof gate; limited
experiments and implementation of settled pieces stay outside production until acceptance.

The operator confirmed transport ownership of the shared cgroup/supervisor implementation and
session adoption on 2026-09-19. #770 is closed; its final head matches the preserved requirements
input at `2c406948`. The ownership gate is resolved, not the containment or compatibility proofs.
Recipient permissions and the successor core file ceiling stay inactive until final legacy removal,
while operational safety and deliberately selected profile guarantees apply immediately.

No new public feedback/fix allowance is inferred from the completed #830 review. A coherent
checkpoint receives the normal private reviews and validation before a testing brief and
`review-requested`; the additive implementation is marked ready only when its own gates pass.

The operator separately authorized up to three public feedback/fix rounds for #833. Round 1 began
2026-09-19 at 20:35:58 UTC, after the initial handoff's one-hour collection window and the complete
tester report. Its batch is the checkpoint tester report, the complexity review and its subsequent
directory-depth retraction. The agreed documentation corrections and the separate JSON work unit
passed project, complexity and correctness review at `ac18444f`. The operator subsequently
authorized publication, and those corrections plus later privately reviewed increments were pushed
at `8fec9e07`; its hosted checks passed. Round closure and a new checkpoint handoff remain
outstanding. No second round has begun. The operator subsequently authorized continued
implementation and confirmed three public feedback/fix loops remain available for the completed PR.
Intermediate pushes and private reviews are not public handoffs.

SSH's implementation continues in #832. Its owner agreed to transport extracting the shared bounded
subprocess pump, while SSH retains environment sanitation, carrier-specific report interpretation,
call-site adaptation and combined regression evidence. The shared module is available at
`execution/carriers/_subprocess.py` in published head `8fec9e07`; the extraction does not accept the
separate sink/terminal extensions. SSH also owns correcting its incidental preparation-module
`Command` import when it integrates the new invocation values.

### Initial implementation checkpoint

- [x] Extract immutable command/script values and explicit shell constants into `execution.models`,
      update transport-owned proof consumers, and preserve the buffered carrier interface. Local
      execution tests report 309 passed and four platform-scoped skips; production remains
      unchanged.
- [x] Implement the private local JSON transformation with the four shipped strategies, literal
      null, strict input checks and byte/depth bounds. At `ac18444f`, all 69 focused cases and the
      three private review lanes pass. This returns proposed bytes or a skip decision only; no
      filesystem publication, FileAccess wiring or permission boundary is claimed.
- [x] Add distribution `python3` to the shared early provisioning package list, retaining the Phase
      B package for existing guests. The implementation is included here; local tests cover
      native-bootstrap and cloud-init rendering. Existing-VM native recovery, live provisioning and
      helper compatibility retain their separate acceptance gates.
- [x] Demonstrate the two-phase bootstrap handoff on an owned local Linux PTY with Python 3.11. The
      executable experiment and tests are included here. Payload-ready precedes sensitive transfer;
      interactive-ready follows terminal restoration. Premature input retains raw carriage-return
      semantics, while post-handoff input receives canonical translation. This is not SSH delivery,
      workstation-platform acceptance or application-start proof.
- [x] Compose a private same-identity Linux file read through one carrier attempt without staging,
      spool or lock creation. The implementation includes strict file-response collection and actual
      local Python 3.11 reads; the focused file, inline, terminal and import suite passes 404 cases.
      Native acceptance, stat-only operations, mutation, locking and FileAccess remain separate
      gates.
- [x] Bind private buffered command/script and file-read preparation to one explicit identity plan.
      The shared launcher selects direct delivery, non-interactive root sudo or fixed non-root
      demotion; the guest checks Linux real/effective/saved IDs and normalized groups before
      workload access. At `c3cebea5`, all three private review lanes are clean, including portable
      identity stubs and mutation-proven payload assertions. Account resolution, actual sudo/root
      transitions, native acceptance and production RunContext remain separate gates.
- [x] Resolve a core-bound destination account's IDs/groups through a private read-only helper and
      one carrier attempt. At `1688da5d`, all three private review lanes are clean and all 42 new
      account tests pass. Local Python 3.11 lookup and lookup-to-inline composition preserve the
      distinction between database membership and actual process credentials. This adds no public
      account selector, privilege transition, staging or permission enforcement; native acceptance
      and production composition remain separate gates.
- [ ] Accept the preparation/result and file-operation LLDs after private review and disposition of
      their helper/runtime, launch-evidence, cross-identity operation coordination, and platform
      prerequisites.
- [x] Implement private revision-aware publication with explicit Create/Replace/Match conditions,
      bounded streaming from verified scratch, stat-only observations without old-content reads, and
      a content-bound post-publication revision. At `75aaaa5e`, all three private lanes are clean
      and the focused file suite passes 169 tests, including conflict, deadline and
      uncertain-publication behavior. External-writer atomicity and native acceptance are not
      claimed.
- [x] Implement the private read-only transaction-lock primitive with local contention, deadline,
      refusal and release tests. At `75aaaa5e`, all three private lanes are clean. The corrected
      post-acquisition deadline test is mutation-proven. Protected namespace setup, ordinary/admin
      sharing and native macOS acceptance remain separate gates.
- [ ] Validate finite nonnegative relative budgets at the file request boundary before deriving a
      guest-local expiry. Keep deadline and post-cleanup evidence independent of the removed
      destination-lock prerequisite.
- [x] Review and validate private exact-kind revision-bound object stat/removal and the Debian
      create-time lock setup. Keep setup idempotent without replacing a valid lock inode;
      distinguish local fixture evidence from privileged native bootstrap and ordinary/admin
      contention. All three private lanes are clean at `876355c7`; the file/Proxmox suite passes 305
      tests.
- [ ] Compose fixed inline file-operation bundles with one-stream compression and data-only scratch.
      Prove every final envelope against both complete Proxmox HTTP-body and workstation
      process-command bounds; no executable-helper staging fallback. The Proxmox compatibility floor
      remains 64 KiB even on providers accepting larger requests.
- [x] Review and integrate private held-object metadata, bounded inventory, search-only root
      traversal and read-only fixed lock-namespace composition. Preserve explicit partial/uncertain
      mutation facts and requested-depth completeness without claiming public FileAccess or native
      acceptance.
- [x] Deliver concrete stat/removal helper exchanges using the shared framing, identity check and
      fixed lock. Validate paths, revision/kind and relative time budget at the request boundary;
      prove no staging for stat and no replay after uncertain removal before extending the same
      composition to the remaining file operations.
- [x] Resolve metadata owner/group pairs through the fixed account helper without caller exec or
      identity transition. Preserve account lookup's independent full execution-identity result;
      prove that responses cannot cross the two operation kinds.
- [x] Bring private bounded reads under the fixed transaction lock and guest-local relative budget.
      One host call prepares and dispatches once; the guest materializes the snapshot and checks
      expiry while locked, then unlocks before emitting bytes. Private review at `2ad918bc` is
      clean; removing the final expiry check makes the absence and snapshot tests fail. Native
      acceptance and production FileAccess remain separate gates.
- [x] Deliver private locked inventory and metadata/ensure-directory exchanges. Validate complete
      inventory framing and bounded entries, preserve metadata partial/uncertain effects, and keep
      identity checks before lock and target access. All three private lanes are clean at
      `fef0045d`; the combined file/scratch selection passes 604 tests. Native acceptance and public
      FileAccess remain separate gates.
- [ ] Compose the remaining publication and streaming-transfer exchanges. Preserve exact-leaf
      requests for approved-root operations through core-owned parent decomposition, without
      granting parent/sibling authority. Bind those operations in the complete FileAccess surface
      before the additive RunContext gate, not as a forwarding layer over legacy files.
- [x] Deliver private stage creation and exact-offset chunk exchanges using sensitive input,
      complete typed observations, original destination binding and the fixed transaction lock. All
      three private lanes are clean at `85d27904`, including post-cleanup expiry and exact chunk
      cleanup-debt binding. These two operations do not complete upload, recovery delivery,
      publication, public FileAccess or native acceptance.
- [x] Separate unverified scratch identity/length from final digest verification, permitting
      one-pass upload without rewinding the source or using a whole-file host buffer. Add
      cooperative acquisition/transfer expiry checks while retaining bounded exact cleanup after
      expiry. Private review at `fef0045d` is clean; mutation tests prove final digest verification
      and cleanup-only normalization remain necessary. This is a local primitive, not transfer
      delivery.
- [x] Add streaming from one held source inode into a private snapshot, including source-change
      refusal, deadline checks and exact cleanup evidence, before wiring snapshot/chunk/publication
      exchanges. The local primitive at `5b58e8f5` passes all three private review lanes with the
      pre-existing signal-atomic descriptor-bookkeeping limitation retained explicitly; this does
      not complete remote snapshot delivery or helper lifecycle acceptance.
- [x] Implement bounded immutable scratch ownership receipts, core-allocated tokens and
      identity-bound snapshot creation. Historical reconciliation recovers exact cleanup ownership,
      not ready content or publication authority. All three private lanes are clean at `5a9d3b8f`,
      including post-cleanup deadline checks and inherited-group handling. This is local evidence;
      remote reconciliation, dispatch ordering and publication-stage recovery remain open below.
- [ ] Settle and fault-test cleanup ownership when the first scratch/snapshot creation reply or
      publication-stage cleanup-debt reply is lost. Missing identity is not absence; do not recover
      by replaying creation or scanning a prefix. Prove the bounded immutable ownership-receipt
      candidate, including original-parent binding, interrupted receipt creation/removal and late
      requests. Every follow-on mutation must validate its still-existing operation receipt under
      the caller's operation ownership before creating any artifact; read-only snapshot chunks
      retain their existing unlocked exact-reference checks.
- [x] Deliver private stage reconciliation and exact cleanup through the fixed identity-bound
      helper. Accept complete historical cleanup ownership only, preserve missing-evidence
      uncertainty and exact failure debt, and check expiry before explicit cleanup mutation and
      after descriptor closure. All three private lanes are clean at `b0840a37`. This does not
      complete snapshot/publication recovery or prove earlier-request quiescence.
- [x] Admit the fixed Linux snapshot scratch parent independently of source-write authority. The
      selector creates and repairs nothing, refuses links and unsafe ownership/mode, and preserves
      deadline/descriptor handling. Local fixtures prove a non-root download from a read-only source
      parent, and a separate read-only host probe admits UID 0/mode 01777. Native VM/macOS selection
      and remote snapshot delivery remain unproved.
- [ ] Jointly accept and prove the privately implemented carrier sink extension with the SSH owner
      before enabling its production use or exposing it through RunContext.
- [ ] Complete the additive-surface gates below before exporting or wiring production RunContext
      access. The models-only checkpoint is not additive-surface completion.

The [operator ruling](frd.md#file-safety-and-guest-runtime-rulings) approves adding `python3` to
early guest provisioning, with helper code compatible with Bookworm's distribution Python. The
package addition is implemented; live provisioning and existing-VM native recovery must establish
availability on their actual paths. macOS platform hosts must provide preinstalled Python 3.11 or
newer; detect missing, unsupported, or Xcode-shim interpreters and report clean actionable errors
without implicit installation or an installation prompt. Prerequisite checks, file-only no-staging
readiness and exact direct-launch evidence retain their own implementation and proof obligations.

The same ruling bounds file safety to untrusted requests, conservative regular-file publication and
required access metadata, without containment of malicious target-user processes. Preserve safe
object checks and explicit trust assumptions, but do not build same-user namespace isolation to
close the earlier ancestor-rename/hard-link adversarial gate. Unsupported objects or metadata refuse
before publication; required workflows still need an implemented safe path or explicit disposition,
not silent omission.

The byte-endpoint work unit now includes borrowed live input, delivered-output retention, the shared
subprocess pump and buffered QGA sink delivery. Private review caught the post-exit pipe timer
incorrectly limiting temporary sink stalls. The correction keeps one accumulated collection budget
while pending sink delivery consumes the original operation deadline. The lexical provisioning tests
from the initial package increment were removed; package coverage and the separate live-provisioning
gate remain. These corrections do not establish production or joint SSH acceptance. Before
publication of the new types, the old SSH adapter must explicitly refuse unsupported I/O shapes
instead of silently interpreting them as EOF or discard. The SSH owner supplied standalone commit
`678e487d`, integrated here as `fc1c5310`. The integration tests exercise actual `LiveInput` and
`SinkOutput` values, including sensitivity and both sink-delivery modes, and verify refusal before
connection access or endpoint consumption.

At `354c7a17`, all three private code-review lanes are clean, and the corrected full local suite
reports 10,413 passed and 11 skipped. The focused execution/provisioning suite reports 496 passed
and four skipped. These are workstation tests, not live platform acceptance. Subsequent
combined-stream testing exposed a further gap: a pending sink paused accounting while the other pipe
could keep collecting. The scheduler now suspends all fresh reads during post-exit pending delivery,
then resumes the unchanged accumulated budget. Its correction received fresh review with the next
bounded work unit, rather than inheriting the earlier clean verdict.

The safe ancestor `a885ef5a` is published with only early guest Python provisioning and the local
PTY experiment, including the provisioning-test correction. Its execution package and RunContext are
unchanged from `e85e9f5c`. The exact publication pin passes 10,370 local tests with 11 skips and all
hosted checks. The branch retains that ancestor without changing the implementation tree. The
subsequent compatibility guard removes the prerequisite for publishing the new I/O types; progress
pushes remain distinct from a public review handoff or joint acceptance.

The [Darwin prerequisite candidate](preparation-lld.md#darwin-inline-prerequisite-candidate) keeps
runtime selection above carriers and checks interpreter compatibility inside the inline invocation.
It does not install Python, execute the known Xcode shim, or add a preliminary readiness probe.
Implementation and native macOS proof remain open. Its local executable experiment now reuses the
shared pump and fixed minimal environment; the separate private file snapshot primitive implements
bounded read-only observations. Their
[runtime](preparation-lld.md#darwin-inline-prerequisite-candidate) and
[filesystem](file-operations-lld.md#confinement-and-filesystem-mechanics) evidence descriptions keep
production composition, native platform acceptance, full mount handling and locking gates open.

At `b881942d`, project, complexity and generic correctness reviews are clean for the runtime
experiment, file snapshot primitive and combined-stream correction. Independent mutations prove that
the corrected representation tests detect disclosure of either snapshot bytes or its digest. The
runtime-identical `be168621` passes 10,470 local tests with 11 skips; the final corrected execution
suite passes 549 with four skips. Ruff, mypy, file lint, rulesync and locked-SDD checks pass. This
is private implementation evidence, not native platform acceptance or a completed additive
RunContext handoff.

At `6d089f67`, the private Linux publication primitive and retained exec-evidence experiment pass
project, complexity and generic correctness review. Publication uses caller-owned staging state,
preserves supported access metadata, refuses unsupported objects and retains exact cleanup debt;
acquisition and post-rename interruption regressions pass. Arbitrary asynchronous interruption and
complete helper ownership remain unproved. The same seven exec-evidence cases assert their complete
results under the current interpreter and distribution Python 3.11, without accepting a production
launcher or eager application-start claim. The exact head passes 10,517 non-integration tests with
11 skips; the execution suite passes 596 with four skips. All 33 publication cases also pass under
distribution Python 3.11. Ruff, mypy, file lint, typer isolation, rulesync, locked-SDD and website
gates pass. These remain private building blocks: FileAccess, remote helper delivery, locking,
platform acceptance and additive RunContext composition are not complete.

At `1a5958fa`, all three private review lanes are clean for the exact-child wait correction, the
SSH-owned compatibility guard and its shared-type integration tests, and the lifecycle
clarifications. Repeated review exposed two post-exit scheduling defects: a newly pending stream
could permit another fresh read in the same pass, and alternating pending streams could keep the
collection timer paused indefinitely. Separate mutation-tested regressions now cover both
transitions. Test callbacks no longer compete with the pump's reaper, and the external-reaper
fixture explicitly establishes ordering. The final head passes 10,541 non-integration tests with 12
skips; the full execution suite passes 620 with five skips. Ruff, formatting, mypy, file lint, typer
isolation, rulesync, locked-SDD and website gates pass. This is a draft implementation progress
push, not joint live-I/O acceptance or a completed public feedback/fix round. Launch-interruption,
native-platform, helper, lifecycle and production RunContext gates remain open.

At `e6860525`, all three private review lanes are clean for the public-launch experiment and its
bounded evidence record. The experiment no longer forces Python's private launch selector; it
observes the route and actual session creation under local CPython 3.12.13 and Debian 3.11.2. The
focused suite passes 81 tests with one skip. This head also gives the oversized live-input fixture
short parameter IDs: Windows CI at `4cf5261f` could not set pytest's environment variable for its
65,634-character generated test identifier. The input and assertions remain unchanged. All hosted
checks subsequently pass at `e6860525`, including Windows Python 3.13 and Linux Python
3.12/3.13/3.14; no production-launch or native-platform gate is closed by this test correction.

At `9c1993ef`, the private scratch-transfer primitive passes all three review lanes. It reopens
identity-bound objects, verifies bounded exact-offset transfers, and retains known cleanup debt.
Review corrected acquisition/interruption ownership, setgid inheritance ordering and interrupted
descriptor closing, and removed checks that did not strengthen the stated guarantees. All 29 scratch
cases pass on the current interpreter and distribution Python 3.11; the runtime head passes 10,572
non-integration tests with 12 skips. The corrected full execution suite passes 651 with five skips.
FileAccess, wire validation, remote delivery, concurrency composition and native-platform acceptance
remain open. The Lima resource-lifetime candidate at `8bac9560` and shared process-core design at
`367553fa` separately pass project and complexity review; neither claims an implemented supervisor
or destination helper. These remain draft progress increments, not public feedback/fix rounds or
readiness for production adoption.

At `c89a349d`, the process pump is extracted into the private standard-library-only `_process`
module, with carrier policy and report mapping retained in `carriers/_subprocess.py`. Standalone
execution proves binary input/output and exit handling without importing Agentworks, on the current
interpreter and distribution Python 3.11. The full suite at `2b22da86` passes 10,578 tests with 12
skips; the final simplification removes one redundant source-inspection test and passes all 656
execution cases with five skips, plus Ruff, formatting and strict mypy. Private review removed a
redundant type check that could silently discard an unexpected failure. These are reuse and local
process facts, not destination-helper or production-launch acceptance.

Windows CI at `b59bf286` exposed a timing assumption in the live-input early-close test: exit 23
could first be observed during cleanup, leaving completion legitimately unknown. The correction
keeps that conservative runtime behavior and adds deterministic coverage for both pre-cleanup exit
evidence and cleanup-only status. All hosted checks at `37a36aae`, including Windows Python 3.13 and
Linux Python 3.12/3.13/3.14, pass in
[run 35496253664](https://github.com/WayfarerLabs/agentworks/actions/runs/35496253664). The
[Darwin ownership investigation](prior-art-research.md#darwin-ownership-feasibility) separately
identifies a public-mechanism gap for generic MANAGED host jobs. Its suggested narrower supervision
contract awaits operator disposition; the current requirements remain unchanged.

SSH's implementation at `174187d2` includes transport `a885ef5a` and adopts the reviewed finite
subprocess pump. Its owner has separately supplied the buffered compatibility guard integrated here.
It still needs the extended shared I/O implementation and terminal preparation, plus production
target/trust composition. Pump adoption does not close the launch interruption gate. Next, settle
and jointly prove live source/sink reports and terminal preparation with a synchronized
payload-to-interactive handoff. A raw envelope through an unprepared PTY is not accepted. The
[same-terminal experiment](carrier-io-lld.md#same-terminal-preparation-experiment) separates remote
bootstrap feasibility from the remaining local client adapter proof. Full file/lifecycle
implementation is not a prerequisite for that bounded shared-boundary proof.

At `b1250da5`, the private Linux inline candidate delivers a fixed Python helper and a separate
bounded stdin manifest in one carrier attempt. It verifies the bound identity, separates script
source from application input with a memory file, and validates framed wait/output facts without
inferring application success or eager start. All three private review lanes are clean. Review
corrected false terminal evidence after contradictory output, portable identity fixtures and an
interpreter-path refusal, and removed duplicate parsing and unused size bookkeeping. The execution
suite passes 1,012 tests with five skips; the full non-integration suite passes 10,933 with 12
skips. Ruff, formatting, strict mypy and file lint pass.

A built-wheel check imports the candidate from the wheel, outside the checkout and without site
initialization, then executes its packaged helper on distribution Python 3.11. It verifies separate
script/stdin, all 256 stdout byte values, two binary stderr bytes and exact exit 255. Local
serialization for `/bin/true` with identity 1001 measures 63,277 fixed-source bytes, 63,391 argv
bytes including terminators, a 249-byte manifest and a 63,754-byte Proxmox JSON request body. These
are measurements of the current candidate, not accepted provider limits. Native delivery, eager
start, interruption ownership, staging, elevation, terminal preparation, lifecycle and production
RunContext remain open; the candidate is not a completed additive delivery or a public feedback/fix
round.

Hosted checks for `f768ade0` passed except Windows in
[run 35498733365](https://github.com/WayfarerLabs/agentworks/actions/runs/35498733365). Package-wide
import discovery exposed the guest module's eager POSIX account-database import. The fix at
`22511320` defers that import until guest default-shell lookup and adds a fresh-process regression
with the module unavailable; it does not skip the independence check. At `3b10267a`, the full local
non-integration suite passes 10,934 tests with 12 skips, with Ruff, formatting, strict mypy and file
lint passing. Hosted Windows confirmation subsequently passes in
[run 35499698325](https://github.com/WayfarerLabs/agentworks/actions/runs/35499698325) at
`616508bc`. That run's Linux 3.13 job exposes a test-only assumption: `pwd` was already loaded
before the import finder guard. The correction at `728b556d` marks the module unavailable
explicitly, preserving the regression without changing runtime behavior. All hosted checks
subsequently pass at `7641fa7f` in
[run 35501242049](https://github.com/WayfarerLabs/agentworks/actions/runs/35501242049), including
Windows Python 3.13 and Linux Python 3.12/3.13/3.14.

The fixed helper's packaged sources now use zlib compression before ASCII armoring; caller payload
remains in stdin. At `728b556d`, the minimal `/bin/true` request with identity 1001 measures 18,583
fixed-source bytes, 18,697 argv bytes including terminators, a 249-byte manifest and a 19,060-byte
Proxmox JSON body. The full local suite passes 10,934 tests with 12 skips, including the helper's
actual distribution-Python-3.11 execution cases. Ruff, formatting, strict mypy and file lint pass.
These are local delivery-size and compatibility facts, not native provider acceptance.

The [terminal input proposal](carrier-io-lld.md#proposed-terminal-input-adapter) now gives bootstrap
EOF a terminal-only handoff meaning, preserves preparation-owned readiness parsing and keeps
presentation above the carrier. Explicit input and output descriptors supply native terminal facts
without process-global stdio lookup. This remains a candidate for joint native proof with SSH, not
an enabled terminal mode or accepted platform evidence.

At `35c72e84`, Linux snapshot lookup uses `openat2` for every descendant open without a weaker
fallback, and private terminal preparation implements the nonce-bound two-gate source/collector and
one-shot Linux guest. The host side is workstation-neutral. Private review corrected inherited
Python signal dispositions and nonce transformation under restored terminal output modes, and
removed unsupported same-process guest reuse. All three private lanes are clean. The full local
suite passes 10,961 tests with 12 skips; Ruff, formatting, strict mypy, file lint, typer isolation,
rulesync, locked-SDD, 160 Python/103 Node website tests and both deterministic build comparisons
pass. Actual local Python 3.11 PTY and kernel lookup tests do not establish SSH/native workstation,
same-filesystem bind-mount or macOS acceptance. Complete files, lifecycle and additive RunContext
remain open; no public feedback/fix round is consumed.

The private no-staging file-read slice now composes the shared fixed-source packager, strict
file-response framing and the existing snapshot reader. Local composition drives the actual Proxmox
carrier against a fake provider and real helper subprocess; it is not native QGA evidence. Review
removed duplicate size/metadata and terminal bookkeeping, rejected undefined Linux mode bits, and
corrected retained collector/reader state on exceptions. A mutation-proven partial-record fixture at
`5e832bd7` covers reader cleanup. This does not promise secure erasure of transient Python locals or
asynchronous interruption atomicity.

Project, complexity and generic correctness reviews are clean at `7aa08d0b`; project and complexity
rechecks cover the test-only correction and final limitation wording. The corrective round is
private implementation work, not one of the three authorized public feedback/fix rounds.

At runtime pin `7aa08d0b`, the full local suite passes 11,009 tests with 12 skips. The final
test-only correction passes all 124 file tests. Ruff, formatting, CI-scoped mypy (933 sources), file
lint, typer isolation, rulesync, locked-SDD, 160 Python/103 Node website tests and both
deterministic build comparisons pass. The Python website run also emitted a server-thread
`BrokenPipeError` while returning success; no assertion failed. An extra mypy invocation over the
entire CLI directory, outside CI's scope, reports missing hatchling build-hook stubs; the required
`agentworks/ tests/` invocation passes. No dependency or unrelated website code was changed.

Hosted [run 35503399897](https://github.com/WayfarerLabs/agentworks/actions/runs/35503399897) at
`b0d62063` passes Windows and Linux 3.12/3.14, but the Linux 3.13 import test assumes its blocked
modules were not preloaded. The helper import succeeds; the test's final assertion fails. The
test-only correction at `ed19ee21` explicitly marks those modules unavailable, like the existing
terminal test. All three private review lanes are clean; negative import probes still fail, and the
four focused import tests pass. Runtime code is unchanged.

All hosted checks subsequently pass at `4f60e9c4` in
[run 35503894965](https://github.com/WayfarerLabs/agentworks/actions/runs/35503894965), including
Windows Python 3.13 and Linux Python 3.12/3.13/3.14.

At `c3cebea5`, explicit private helper identity plans pass all three review lanes after test-only
corrections. The full final non-integration suite passes 11,031 tests with 12 skips; Ruff,
formatting and CI-scoped mypy (936 sources) pass. File lint, typer isolation, rulesync and
locked-SDD checks pass. Website validation at the runtime-identical `400a634a` passes 160 Python and
103 Node tests plus both deterministic build comparisons; temporary build outputs were removed. The
shared carrier interface remains unchanged. Actual privilege transitions, target account resolution,
native acceptance, full files/lifecycle and additive RunContext remain open. No live infrastructure
was touched and no public feedback/fix round is consumed.

All hosted checks subsequently pass at `3ada8ff0` in
[run 35504674348](https://github.com/WayfarerLabs/agentworks/actions/runs/35504674348), including
Windows Python 3.13 and Linux Python 3.12/3.13/3.14.

The private account-discovery increment at `40a6bade` reads only the core-bound account's IDs/groups
under the delivery identity and feeds the existing identity plan. It uses a bounded one-shot JSON
reply, no staging or privilege change, and no public account selector. The developer's focused suite
passes 64 tests; execution tests pass 1,151 with six skips. Real distribution-Python-3.11 lookup and
lookup-to-inline composition run locally. The latter correctly refuses this container's differing
database and inherited group memberships. Private review and final gates were pending at that
integration pin. This does not close native identity-transition, Darwin acceptance or production
composition gates.

All three private lanes are subsequently clean at `1688da5d`. Review removed duplicate encoder
validation and a redundant result-construction check, deleted an unrelated directory assertion that
could not detect helper staging, and strengthened the account-payload assertion with a unique
substring canary. An actual argv-embedding mutation fails that assertion. The final full suite
passes 11,073 tests with 12 skips; Ruff, formatting and CI-scoped mypy (942 sources) pass. Website
gates at that pin pass 160 Python tests, 103 Node tests and both deterministic build comparisons.
Temporary build outputs were removed and no live infrastructure was touched. These private
corrections consume no public feedback/fix round.

All hosted checks subsequently pass at `aa82b8f2` in
[run 35506094459](https://github.com/WayfarerLabs/agentworks/actions/runs/35506094459), including
Windows Python 3.13 and Linux Python 3.12/3.13/3.14.

The file-mechanics increment is privately reviewed at `75aaaa5e`. Publication now accepts explicit
Create/Replace/Match conditions, uses metadata-only observations when no old-content match is
required, streams verified scratch in bounded chunks, and verifies a content-bound revision after
rename. The fixed read-only lock primitive proves local contention/refusal/release but does not
install its namespace or close cross-identity/native acceptance. Review removed a duplicate
post-publication reader and corrected a mistimed deadline test; deleting the post-acquisition check
now makes that test fail. All three private lanes are clean.

The final full suite at `e9cce152` passes 11,118 tests with 12 skips. Ruff, formatting and CI-scoped
mypy (945 sources) pass. The focused file suite passes 169 tests. File lint, typer isolation,
rulesync and locked-SDD checks pass. Website gates at `4c35aafe` pass 160 Python and 103 Node tests
plus both deterministic build comparisons; its local server emitted a BrokenPipeError without a
failed assertion. Temporary build output was removed. No live infrastructure was touched. Shared
namespace setup, complete file delivery, launch ownership, lifecycle, native proof and the additive
RunContext remain open; neither pending macOS decision is waived. These are private implementation
corrections, not a public feedback/fix round.

The next increment is privately reviewed at `876355c7`. Linux object stat/removal now binds exact
kind and revision, and shared new-guest bootstrap provisions the protected lock after Python
installation. Existing valid lock identity is retained; only new core-owned objects are finalized.
Setup failures report closed phase/kind/creation facts. Review corrected a post-open descriptor
leak, removed a redundant create-result flag and kept leaf validation at the future request
boundary. An ACL-fixture audit found unmapped `nobody` IDs, not missing filesystem support;
mapped-group fixtures execute the real inherited/existing ACL cases without skips.

The source bundler now compresses trusted modules together, and standalone publication executes
under Bookworm Python 3.11. The file design selects fixed operation-family inline bundles with
data-only scratch, avoiding executable installation. The Proxmox carrier also refuses a serialized
POST over 64 KiB before dispatch, including argv/framing/escaping. These changes do not prove the
final file dispatcher or native carrier request sizes. Shared SSH types are unchanged; Windows SSH
command size and complete native file delivery remain acceptance gates.

At `876355c7`, the final full local suite passes 11,177 tests with 12 skips; the file/Proxmox suite
passes 305 without skips. Ruff, formatting, CI-scoped mypy (951 sources), file lint and diff checks
pass. Typer isolation, rulesync and locked-SDD checks also pass in this increment. The unchanged
website code passes 160 Python and 103 Node tests and both deterministic build comparisons;
temporary build outputs were removed. No live infrastructure was touched. All three private review
lanes rechecked the correction pin. Native privileged setup, cross-identity contention, complete
file delivery, lifecycle and additive RunContext remain open; both macOS operator decisions are
still pending. No public review/test signal is raised and no public fix round is consumed.

The metadata/inventory increment is privately reviewed at `4e8921eb`. Linux metadata convergence
uses verified held-object procfs references, supports required directory set-group-ID modes and
preserves explicit completed versus uncertain mutation facts. Inventory returns complete results
within the requested depth with exact entry/name/encoded bounds. Shared file framing now has one
concrete codec, while read schemas remain operation-specific. Root and fixed lock-namespace walks
use path-only descriptors without unnecessary directory read authority; neither installs state.

All three independent lanes are clear on the correction pin. Review moved invalid directory-mode
refusal before target I/O, added a deadline check after final ACL verification, removed duplicate
inventory serialization and redundant metadata error reconstruction, and preserved handled-control
descriptor cleanup. Final local suite: 11,282 passed, 12 skipped. The combined file and Proxmox
request/trust/sink suite passes 448 without skips. Ruff, formatting, strict mypy (957 sources), file
lint, typer isolation, rulesync, locked-SDD and diff checks pass. Unchanged website code passes 160
Python and 103 Node tests plus both deterministic build comparisons. No live infrastructure was
touched. This is a private increment, not complete FileAccess or a public feedback round. The next
implementation step is the concrete stat/removal helper exchange; public composition, native and
cross-identity acceptance, lifecycle and additive RunContext remain open. Both macOS operator
decisions remain pending.

The stat/removal, metadata-ownership lookup and locked-read exchanges are privately reviewed at
`2ad918bc`. Object requests retain exact kind/revision conditions, closed relative budgets and
uncertain-removal evidence without replay. Ownership lookup returns only numeric owner/group IDs,
independently of execution identity. Read snapshots use the existing protected lock and emit after
unlocking; missing lock state refuses even when the target is absent. Neither read nor stat creates
prerequisite state. The full local suite at that pin passes 11,449 tests with 12 skips.

All three private lanes found no material issues at that pin. Corrections clear sensitive exception
chains, preserve Windows import-test selection and remove a Proxmox fixture's dependency on the
host's lock state. Shared identity decoding and one-pass account request parsing remove duplicate
checks. The subsequent cleanup at `b09e1212` deletes two unused account decoder wrappers and moves
their unchanged malformed-input cases to the real guest entry point. Public FileAccess, native
ordinary/elevated acceptance, lifecycle and additive RunContext remain open; this private work does
not consume a public feedback/fix round.

All three lanes also verified the narrow cleanup at `b09e1212`, and the full local suite again
passes 11,449 tests with 12 skips. Ruff, formatting and CI-scoped mypy (966 sources) pass. Website
gates pass 160 Python and 103 Node tests and both deterministic build comparisons. File lint, typer
isolation, rulesync, locked-SDD and diff checks pass. Generated test/build outputs were removed; no
live infrastructure was touched. Hosted checks pass at the preceding published `6edbd94c` in
[run 35511446645](https://github.com/WayfarerLabs/agentworks/actions/runs/35511446645); the new
progress publication still needs its own hosted confirmation.

The inventory/metadata exchanges and scratch-finalization increment are privately reviewed at
`fef0045d`. Inventory returns entries only after complete framing, length, digest, schema and
carrier-stream checks. Metadata and directory convergence preserve known partial versus uncertain
effects without replay. All four file guests share one concrete record writer. Review corrected
missing response-key handling and made compatibility cases select Python 3.11 explicitly.

Scratch creation now binds identity and length without requiring the final digest in advance;
verification establishes the ready reference. Cooperative expiry stops further acquisition and
transfer while retaining exact cleanup. An independent reproduction found that a slow successful
directory creation could otherwise be followed by data creation after expiry. The correction
prevents that new object, preserving only identity capture and mode normalization necessary for
cleanup. Mutation experiments establish the need for both final digest verification and that cleanup
normalization.

All three private lanes are clean at `fef0045d`. The final full local suite passes 11,595 tests with
12 skips; the file/scratch selection passes 604. Ruff, formatting and CI-scoped mypy (978 sources)
pass. File lint, typer isolation, rulesync, locked-SDD and diff checks pass. Unchanged website code
passes 160 Python and 103 Node tests and both deterministic build comparisons. A prior full run at
`14fb93b3` emitted multiprocessing resource-tracker warnings from a database test; the final run did
not, and no shared-memory files remained when checked. Owned test/build output was removed. No live
infrastructure was touched.

The bounded immutable ownership-receipt candidate has independent project and complexity review, not
implementation acceptance. Lost replies, late requests and partial receipt cleanup remain explicit
proof gates. Streaming source-to-scratch snapshots are now assigned for implementation;
transfer/publication exchanges, full FileAccess, lifecycle, native acceptance and additive
RunContext remain required. Shared SSH types are unchanged. Both macOS operator decisions remain
pending. This increment consumes no public feedback/fix round. Hosted checks passed at prior
published `63022be8`; fresh confirmation remains required after the next progress push.

The source-to-scratch snapshot increment is reviewed at `5b58e8f5`. It copies bounded chunks from
one held source, verifies length/EOF/digest and final source identity/metadata, and returns a ready
private copy plus the source revision. Initial absence is checked for expiry after descriptor
closure. Review reproduced and corrected skipped parent cleanup after a leaf-close interruption and
lost scratch cleanup debt when source closure interrupted a failed creation. An independent mutation
of final source verification fails four tests.

The generic lane also reproduced an existing traversal limitation: an asynchronous exception before
an intermediate ancestor close can leave that descriptor until helper exit. The same ordering
predates this increment. It remains inside the documented non-signal-atomic bookkeeping limit; blind
close retry is not added because interruption does not establish whether that numeric descriptor has
already been closed. Complete helper lifetime/interruption remains a production gate, not an
acceptance claim from these local primitives.

The final local suite at that runtime pin passes 11,617 tests with 12 skips. Ruff/format, CI-scoped
mypy (980 sources), file lint, typer isolation, locked-SDD, rulesync and diff checks pass. Website
gates pass 160 Python and 103 Node tests plus both deterministic build comparisons. Owned temporary
test/build output was removed. No live infrastructure was touched. All hosted checks pass at the
preceding published CI correction `46f5d948` in
[run 35517251196](https://github.com/WayfarerLabs/agentworks/actions/runs/35517251196); the snapshot
increment still needs fresh hosted confirmation after publication.

The focused systemd 252 source audit records helper acceptance separately from payload entry,
completion retention across unit collection and independent boundary-emptiness observation after
stop. These constrain the next supervisor experiment; no managed lifecycle is implemented by them.
Creation receipts are under implementation. Transfer/publication exchanges, full FileAccess, native
acceptance, lifecycle and additive RunContext remain required; both macOS decisions remain pending.
No public feedback/fix round is consumed by this increment.

The receipt increment is reviewed at `5a9d3b8f`. Core supplies a fresh token and execution identity
before staging or snapshot creation. The immutable receipt binds the closed operation and original
parent/object identities. Historical recovery permits cleanup only; active references separately
bind the original receipt inode. Recovery neither proves that a delayed request cannot still arrive
nor returns verified content. Remote exchange ordering and publication-stage recovery remain open.

Private review corrected reconciliation expiry after missing-name lookup and descriptor closure,
ensured the directory descriptor closes even when receipt closure interrupts, and removed redundant
receipt decoding. The lead reproduced a real inherited-group failure and verified its fix outside
the namespace sandbox. Both data and receipt inherit the parent's group before private directory
mode is finalized. An early data-open failure now cleans its exactly owned empty directory; unknown
objects remain untouched with explicit debt. Removing that normalization makes both new failure
tests fail. The future snapshot helper must check its bound execution identity before any source
access, including absence lookup; the local spool's receipt context is not that invocation boundary.

Final full local suite: 11,634 passed, 13 skipped. The extra skip is the sandbox's unavailable
alternate supplementary group; the committed regression passes separately outside that sandbox. The
independent correctness lane also reports 1,713 execution tests passed, six skipped, and a Python
3.11.2 receipt/transfer/reconciliation roundtrip. Ruff/format, CI-scoped mypy (982 sources), file
lint, typer isolation, locked-SDD, rulesync and diff checks pass. Unchanged website gates pass 160
Python and 103 Node tests plus both deterministic build comparisons. Owned temporary test/build
output was removed and absence verified; no live infrastructure was touched. Hosted checks pass at
the preceding snapshot increment `0e7a9edf` in
[run 35518292881](https://github.com/WayfarerLabs/agentworks/actions/runs/35518292881); this receipt
increment still needs hosted confirmation after publication.

The staging exchange is assigned separately against these receipt types. Full FileAccess, native
acceptance, launch ownership, lifecycle and additive RunContext remain required. Shared SSH types
are unchanged, and both macOS operator decisions remain pending. This progress push consumes no
public feedback/fix round; all three remain available for the completed PR.

The private stage increment is reviewed at `85d27904`. Creation and chunk requests use the original
destination binding, fixed lock and sensitive private protocol. Review corrected expiry after path
and lock cleanup, oversized pytest IDs on Windows, and returned chunk cleanup debt that could
conflict with the known active reference. Removing the debt-equality check makes all seven mismatch
cases fail. The three private lanes pass 86 stage tests, including real local Python 3.11 helpers.

The final full suite passes 11,720 tests with 13 skips. Ruff/format, CI-scoped mypy (991 sources),
file lint, typer isolation, locked-SDD, rulesync and diff checks pass. Website Node tests pass 103
cases and both deterministic double-builds match. One Python website run failed the unchanged
browser keyboard-hold launch witness; a complete rerun passed all 160 tests. This is an observed
intermittent gate failure, not a diagnosed or fixed website defect. Hosted receipt checks at
`9d8100b2` pass in
[run 35520474884](https://github.com/WayfarerLabs/agentworks/actions/runs/35520474884); the stage
increment requires fresh hosted confirmation. Remote recovery is the next file implementation; full
FileAccess, native acceptance, launch ownership, lifecycle and additive RunContext remain open.

The stage recovery and Linux scratch-root increment is privately reviewed at `b0840a37`. Lost
creation replies can recover complete historical cleanup ownership through the fixed helper, never
an active or ready content reference. Cleanup failures cannot introduce different debt. Missing
receipts remain uncertainty; delayed follow-on chunks refuse after receipt removal, but this does
not establish that an earlier original creation cannot arrive later.

Review corrected explicit cleanup after an expired path lookup and rejected historical replies with
missing identities or widened receipt modes. The typed result encoder no longer repeats those
external checks. Removing the incoming checks makes all ten malformed-history cases fail; removing
pre-cleanup admission mutates data despite a final refusal. Six real-helper deadline tests now
advance a controlled guest clock at the intended boundary, replacing a reproduced scheduling race.
The final correctness lane passes 147 focused tests, all 60 repeated deadline cases and 20,000
malformed-request probes. The complete file selection passes 736 tests. Root admission is local
Linux evidence only: namespace-sandbox `/tmp` has UID 65534 and correctly refuses, while the same
read-only probe outside that sandbox observes UID 0/mode 01777 and closes the admitted descriptor.

The final full suite at `b0840a37` passes 11,781 tests with 13 skips. Ruff/format, CI-scoped mypy
(993 sources), file lint, typer isolation, locked-SDD, rulesync and diff checks pass. Website gates
pass 160 Python and 103 Node tests and both deterministic double-build comparisons. Owned temporary
test/build output was removed and absence verified; no live infrastructure was touched.

All hosted checks pass at the prior stage publication `37d8150b` in
[run 35522218730](https://github.com/WayfarerLabs/agentworks/actions/runs/35522218730). The new
increment still needs hosted confirmation after publication. Snapshot/publication delivery, full
FileAccess, launch ownership, lifecycle, native acceptance and additive RunContext remain required.
The launch-owner candidate is being implemented separately; its design evidence is not production
acceptance. Shared SSH interfaces remain unchanged. The macOS decisions pending at that checkpoint
were subsequently disposed by the operation-coordination correction above: no blanket host lock
setup and no generic macOS MANAGED supervisor prerequisite for VM platforms. This progress increment
consumes no public feedback/fix round; all three remain available for the completed PR.

After the shared seam and LLD gates, the lead may charter bounded migration packages against one
pinned contract. The following is an assignment plan, not a claim that developers are allocated:

| Package                                       | Exclusive responsibility                                                                                                                                                            |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transport lead                                | Shared execution API/helpers/file policy, `capabilities/base.py`, composition boundaries, integration gates and final deletion.                                                     |
| SSH developer                                 | `execution/carriers/ssh/`, connection/trust migration and associated tests, currently in #832.                                                                                      |
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
      ancestors/mounts, private staging, database operation ownership, root creation and fail-closed
      behavior.
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

- [ ] Establish exact child-status ownership wherever a native wait becomes execution evidence.
      CPython can substitute zero after ignored `SIGCHLD` or another reaper consumes the status; the
      helper and workstation observers must refuse missing evidence rather than report that
      synthetic value. Validate the retrospective completion candidate independently from eager
      launch acknowledgment and preserve uncertainty for unproved signal termination.
- [ ] Establish local process ownership through launch interruption on supported workstation hosts.
      A real SIGINT probe on Linux CPython 3.12.13 left a child alive when `Popen` construction was
      interrupted before returning its handle. Shared pump extraction and the existing SSH copy do
      not satisfy this gate. Prove cleanup or obtain an explicit contract disposition before
      claiming production interruption conformance; see the
      [process-startup evidence](prior-art-research.md#local-process-startup-and-interruption).

The transport lead owns shared profiles, supervisor lifecycle and session adoption. Migration
delegates consume this implementation rather than building another launcher; SSH owns delivery,
connection and trust only. Before broader lifecycle implementation, complete these additional gates:

- [ ] Review unified execution/file access and independent invocation, observation, I/O, lifetime,
      protection and identity dimensions. Approve typed shell constants, exact additive profile
      grants and refusal without downgrade. The [lifecycle design](execution-lifecycle-lld.md)
      supplies the proposal, not implementation proof.
- [ ] Reconcile the complete #770 requirements, threat model, acceptance cases and exclusions using
      the [preserved input](inputs/session-cgroups-frd-2c406948.md), not the routing index as a
      replacement FRD. Ownership is assigned to transport and #770 is closed; carry the accepted
      text into its designated requirements home. The closed source head is `2c406948`, matching the
      preserved snapshot; closing the PR does not complete requirements reconciliation.
- [ ] Complete and prove Linux supervisor launch through SSH and native QGA: protected identity,
      secret/source delivery, privilege changes, foreground wait, independent launch, output
      retention and terminal evidence. No workload code runs before boundary entry.
- [ ] Prove lost-acknowledgment reconciliation, wait timeout versus stop, observer-loss cleanup,
      runtime-anchor death, concurrent forks, stale identity and independently verified emptiness.
      Settle the OPERATION liveness/lease protocol before offering target-side cleanup.
- [ ] Reconcile #770's historical escape/relaunch proposal against the later exclusion of malicious
      target-user containment. Deliver DIRECT/MANAGED without a CONTAINED profile or
      per-run-user/jail implementation; retain future profile extensibility. Prove trusted
      socket-membership identity and ordinary lifecycle cases within that stated boundary.
- [ ] Resolve platform-owned macOS host workflows, Debian/kernel/systemd floors, WSL2 power lifetime
      and no-staging recovery. Required workflows block delivery when their guarantees cannot be
      met; no profile downgrade or fabricated platform equivalence. Use the
      [observable proof criteria](execution-lifecycle-lld.md#delivery-sequence-and-proof-criteria)
      and [reported test-bed gaps](prior-art-research.md#lifecycle-test-bed-gaps). For macOS hosts,
      prove the platform's actual start/status/stop and disconnected-operation recovery, not a
      generic Linux-equivalent MANAGED profile. Workstation SSH evidence is insufficient; report
      cleanup uncertainty without dropping required platform operations.
- [ ] Prove the placement-host VM-resource lifetime separately from provisioning completion. For
      Lima, use supported platform lifecycle operations and prove readiness, disconnect recovery,
      stop and rollback for the supported drivers. A foreground anchor is an option to justify for a
      concrete workflow, not a required replacement for Lima's runtime. Do not treat numeric PID
      records as authority to kill unrelated host work.

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
