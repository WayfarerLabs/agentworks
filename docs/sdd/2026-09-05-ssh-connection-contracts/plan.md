# Independent SSH Carrier: Staged Delivery Plan

- Updated: 2026-10-06
- Requirements: [frd.md](frd.md)
- Architecture: [hla.md](hla.md)
- Shared contract:
  [transport-owned definition](../2026-09-12-transport-improv/execution-contract.md)
- Shared PoC:
  [transport plan](../2026-09-12-transport-improv/plan.md#2-prove-the-shared-boundary-before-broad-implementation)
- Coordination baseline: SSH PoC #796 merged; transport design #830 merged as `cea5e852`

## Delivery and ownership

The SSH PoC in #796 is merged. PR #832 on `feat/ssh-full-implementation` delivers the full
independent carrier and connection/trust migration machinery for the new execution surface.
Transport exposes that surface alongside the unchanged old SSH/transport path in RunContext, with
both available and usable. It then leads consumer migration, removes legacy RunContext access, and
deletes old transport. SSH waits during those stages except for issues needing its attention. A
later operator request starts a separate PR to delete old SSH and close this SDD.

The [2026-09-19 ruling](frd.md#operator-ruling-2026-09-19) supersedes the earlier two-PR retirement
sequence. Implementation delivery may finish before migration and deletion; it does not finish the
SDD. These artifacts ride the SSH work, with no separate design-only PR. Use coordinated PRs from
main where independent, and stack only actual dependencies. The integration tester can combine
pinned SSH/transport branches for evidence before they land.

Post integration-testing considerations before a checkpoint's `review-requested` signal; complete
private reviews first. Remove the label before fixes and wait for full integration reports before
another iteration. Use ready when the complete green handoff has merge intent, with no checkpoint
label. The four-round PoC allowance below is historical and exhausted, not a new implementation
feedback budget. The operator authorized one artifact feedback/fix round on #832, followed by
implementation on the same PR and up to three additional public feedback/fix rounds after its ready
handoff. The artifact checkpoint at `66298c59` closed without a fix round: full tester and
complexity reports passed the documents; the title finding assumed a design-only merge and was
refuted by this single-PR scope. Its
[disposition](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-5744746307) records
the reports and removal of `review-requested` before implementation. Record each later handoff and
budget disposition on #832. Contract changes or unexpected scope return to the operator.

Transport solely owns the carrier contract, common types, acceptance criteria, shared preparation,
public outcomes and proof harness. SSH owns its independent carrier, connection/trust migration,
fixtures and implementation evidence. SSH raises feasibility concerns to transport rather than
maintaining another contract or acceptance matrix. Requirement changes return to the operator.
Transport also owns non-SSH adapters, all consumer migration, legacy RunContext removal and old
transport deletion. SSH owns final old SSH deletion under the later operator request.

Use the transport artifacts in the same checkout as the live design reference. Record the exact
transport contract and implementation commits with each proof or integration result; a provenance
pin is not a competing definition. When common implementation code is not yet on main, make its
transport PR an explicit dependency instead of adding SSH-local substitute types or a second
harness. Stack only actual dependencies and merge them in order. This does not authorize edits to
transport-owned artifacts. Its removal inventory remains the shared dependency boundary; the latest
operator ruling assigns the final old SSH deletion here after old transport is gone.

The earlier legacy-consolidation work through `2f11662d` is superseded and retained as source
material only; it is not independent-carrier implementation or proof evidence. Local proof fixtures
now exercise the new buffered carrier. Transport's
[acceptance disposition](../2026-09-12-transport-improv/proof-lld.md#joint-buffered-proof-acceptance-2026-09-17)
accepts the joint buffered PoC. Full implementation remains Phase 2; final SSH retirement and this
SDD's lockfile belong to Phase 3, after the intervening transport stages.

Current dependency provenance lives in [poc-results.md](poc-results.md#revisions-and-delivery).
Completed checkboxes below record the revisions used when their work happened; they are historical
records, not floating dependency declarations. The FRD preserves earlier requirements and records
the new operator ruling verbatim; this plan carries the current delivery sequence. R1-R5 acceptance
obligations remain in force through final retirement.

## External feedback rounds

- [x] Receive the full first live report for SSH `1c32e415`, its later combined-tree report on
      transport, and the published complexity review. Remove `review-requested` and open round 1
      under the operator's four-round allowance after the review window. Record findings and
      dispositions on #796 before changing the implementation.
- [x] Complete round 1 policy and process regressions, investigate the real Windows timeout, update
      measured evidence and limitations, and re-run private reviews and affected gates. Post exact
      tester instructions before raising `review-requested` again. Windows completion and joint
      acceptance remain open until measured; a synthetic or parser-only pass cannot close them.
- [x] Complete round 2 after the full reports and collection window: record the authenticated
      Windows resolution, align SSH wording and its harness field use with transport's reviewed
      completion evidence correction, and retain affected macOS coverage as open until measured. Run
      the private review lanes and applicable gates, then post the tester handoff before raising the
      label. This round does not weaken public outcomes or claim completed joint acceptance.
- [x] Complete round 3 after the full reports and collection window: correct the macOS agent-socket
      fixture's path length without changing carrier behavior, record the six current live cells and
      their qualifications, and run private reviews and applicable gates. Post the exact macOS
      fixture retest considerations before raising the label. Joint acceptance remains with
      transport; one authorized fix round remains after this handoff.
- [x] Complete round 4 after the full reports and collection window: consume the reviewed transport
      acceptance record, record the successful macOS fixture retest and unchanged-tree evidence,
      reconcile the SSH artifacts and rebase onto the final transport head. Complete independent
      closeout reviews and gates, and prepare testing considerations for the final handoff. This
      exhausts the operator's four-round allowance; further material fixes need direction.

## Baseline work

- [x] Compare main `e440a28c` with the original `7c744828` baseline and transport proposal
      `6809827f`; record source anchors and revise SSH-owned design/migration obligations. This
      completes the source comparison only, not contract agreement or runtime proof.
- [x] Rebase #796 onto the merged #795 baseline `857110df`, reconcile published ownership and
      mirrored-inventory feedback, and define the two implementation phases under this SDD. The
      [main comparison](main-comparison.md) retains the earlier source snapshot and records this
      reconciliation; neither checked item claims PoC completion.

## Phase 1: Complete SSH PoC in PR #796

### Prepare the proof

- [x] Consume transport's concrete candidate types and acceptance criteria. Record the exact
      contract and code revisions in the proof record; send unresolved SSH feasibility questions to
      transport before accepting the boundary. Transport owns any common-contract correction. The
      [proof record](poc-results.md#revisions-and-delivery) records the consumed transport
      revisions, boundary and combined acceptance.
- [x] Write [ssh-lld.md](ssh-lld.md) with the SSH decisions needed for the PoC: explicit connection
      and trust inputs, executable selection/version refusal, supported account-shell bootstrap,
      subprocess I/O and cleanup. Separate observed decisions from open hypotheses and from Phase 2
      details. Inventory workstation, platform-host and provider-inner client locations under the
      HLA's OpenSSH 8.5 boundary, recording actual versions and separate server compatibility for
      the exercised cases. Record unexercised locations as gaps, not inherited compatibility.
- [x] Resolve proof prerequisites with transport, including initial SSH delivery before Python
      installation and no-staging readiness. A proposed helper must not require the package that the
      first invocation needs to install. Platform-host userspace is a separate inventory.
- [x] Use the operator's integration tester and existing authorized inventory/budgets for the
      bounded live-proof charter. The [proof record](poc-results.md#proof-charter-and-prerequisites)
      links the shared and SSH handoffs, explicit fixture identity/trust, measured versions, bounded
      workloads, carrier resource limits and independent cleanup reports. Transport owns QGA access
      and the joint harness. Private inventory and spending ceilings remain with the tester; no new
      resource authority or implicit operator trust-store access is conveyed by this SDD.

### Build and demonstrate the complete SSH contribution

- [x] Implement the independent buffered SSH binding in `execution/carriers/ssh/` and its tests in
      `tests/execution/carriers/ssh/`, using transport's leaf contract and shared preparation. The
      code used by the proof is the destination implementation, not a wrapper around legacy SSH or a
      separate public prototype API. Production factories and RunContext remain on the old stack
      during this phase.
- [x] Supply explicit isolated connection policy for the proof, including identity/agent selection,
      strict fixture trust, version refusal and ambient-config isolation. Build enough real policy
      to substantiate those guarantees; do not disable verification to make the fixture pass.
      Production config conversion and the complete migration matrix belong to Phase 2.
- [x] Exercise every SSH-applicable case of the transport-owned PoC matrix and detailed carrier
      contract through the joint harness, including its focused I/O ownership/failure tests. Link
      results to the authoritative cases rather than copying their inventory here. No failed or
      missing SSH proof case can be deferred to Phase 2 and called a complete SSH PoC.
- [x] Run the proof's new-stack imports and tests with the transport-defined retirement modules
      unavailable. Audit dependency closure, including normal package initialization and fixtures;
      copied tests must not call legacy code to calculate expected results.
- [x] Record reproducible commands, pinned code/contract revisions, executable/server inventory,
      observed evidence, safe failure reports and independently verified cleanup in
      `poc-results.md`. Keep credentials and payload-bearing diagnostics out of retained evidence.
      Separate local fixtures from live observations and list all unsupported or untested cases.

### Accept the PoC and reconcile

- [x] Have transport evaluate the combined proof, including its real non-SSH case, against its
      acceptance criteria. Record the disposition and links to its evidence without claiming SSH
      performed or owns that work. Unresolved feasibility blocks acceptance; a changed mechanism
      returns for the appropriate contract or operator decision and affected cases run again.
- [x] Update this SDD and `ssh-lld.md` with observed SSH decisions and the transport-owned proven
      revision. Transport updates its own artifacts. Carry the reconciled SSH artifacts in #796 with
      the PoC, not in an intervening design-only PR.
- [x] Complete independent project, complexity and correctness reviews, applicable local/hosted
      gates and authorized live-proof evidence at the evaluated closeout head. Record its pinned
      results and the unchanged-code basis for final readiness bookkeeping in `poc-results.md`. Keep
      this SDD unlocked.

Final delivery on #796 publishes the testing considerations before the ready signal, with the exact
published head's CI green and `review-requested` removed when no checkpoint remains. Transport #826
lands first. This PR's ready state, head and checks record that handoff; these completed work items
do not authorize a merge or replace its final checks.

**Phase 1 definition of done:** #796 contains all SSH PoC code, independent fixtures, observed SSH
evidence, cleanup results and reconciled artifacts. Its transport implementation dependencies are
available in the landing order, and transport has accepted the combined proof against its one
contract. Missing required evidence needs explicit operator disposition; an artifact review, green
unit suite or merged transport design cannot stand in for that disposition. Broader file workflows,
full terminal/forwarding support and additive production delivery are Phase 2 work, except for
anything the transport PoC itself requires. Later migration and retirement follow the current staged
sequence. Unsupported optional behavior refuses before dispatch.

## Phase 2: Full SSH implementation in the second PR

Start from the accepted PoC and merged #830 design. Continue this SDD and extend the proven carrier.
Coordinate integration and landing order with transport's complete additive RunContext delivery;
consumer migration and retirement do not gate this implementation PR. SSH does not take ownership of
transport's files, shared supervision or consumer migration.

The [implementation progress record](phase2-results.md) pins the completed independent code and
local validation separately from the remaining shared integration and platform acceptance. The
[trust workflow record](trust-workflow-results.md) distinguishes newly composed installed-client
proof fixtures from native execution and genuine production publication.

The
[local persisted-configuration record](trust-workflow-results.md#local-persisted-configuration-restoration)
proves that restoring old TOML does not discard current managed policy, revocations, blocking or
partial-update custody. Production writer coexistence and rollback through both usable RunContext
paths still require transport composition and native acceptance.

Shared subprocess adoption introduces an implementation dependency on transport #833. Current
integration uses `e1d4c9b1189edbc247210c6c900975c6290ac531`. Its shared carrier, process,
preparation and terminal-input interfaces remain unchanged from the preceding SSH integration.
Private execution binds the selected owner scope and bootstrap boot; the WSL2 native factory
requires both prepared identity plans and shares one elevated-plan/full-guest bootstrap between file
and execution state. Numeric file workflows retain that context and each record's actual envelope
version. New private recovery/lifecycle candidates and fixed body-free Proxmox generation
observation advance transport's composition, with source and faked-provider evidence recorded in its
plan. Its latest private already-running Proxmox composition retains one VM owner and numeric
bootstrap, while account and guest preparation preserve observed facts and original interruption
when borrow release fails. Its private stopped-Proxmox composition now retains the selected VM owner
and root-QGA route before one durably armed start request. Only a fully matching successful ordinary
task settles that request; unknown acknowledgments, errors and HA handoffs retain custody without
replay. Fresh running-power and passive guest-info observations precede exact guest/account
preparation, and normal teardown leaves the VM running. Plugin registration now imports without the
retirement roots; old operation imports remain local to their legacy callers. These source and
scripted-provider results do not prove native startup, atomic stale-request prevention or
queued-request drain. Native recovery, provider freshness at dispatch, fencing/drain, complete
platform availability, Windows terminal delivery and additive RunContext remain open. These private
increments supply no new SSH terminal or production acceptance. Earlier rebased code pin
`e38f5fcff35bd85d099129fdbe5dd8480017c007` passes 15,768 non-integration tests with 51 skips and 27
warnings, full Ruff/format, strict mypy (1,277 sources), exact CI typer isolation, file quality,
locked-SDD, Rulesync and whitespace checks. Website validation passes 160 Python and 103 Node tests,
four builds and both deterministic comparisons. The first build target was refused inside the
checkout; corrected external owned output roots pass. No SSH code changes or native acceptance
follow from that dependency refresh.

The latest dependency implements the private guest operation lease and its controller consumer.
Expiry remains bound to one launch/boot and an earlier guest-clock sample plus 60 seconds. Fresh
clock checks after renewal reads and immediately before workload release prevent delayed I/O from
reviving expired work or admitting an expired initial lease. Packed Python 3.11/3.12 real-child
fixtures observe body exclusion under a synthetic process-group boundary; they supply no native
cgroup, SSH or QGA proof. Host OPERATION admission still refuses. The keeper, recovery and aggregate
lifecycle remain open, including measured exchange budgets and publisher drain. Service admission
uses existing trusted-source docstring compaction without changing other helper callers' default.
The exact 10,000-byte workload fixture fits Proxmox's unchanged 65,536-byte HTTP cap with only 581
bytes of independent-request margin and 217 bytes of private-operation margin; this is no general
workload-size or native envelope proof. The Windows migration fixture now uses bounded coordination
and failure-path child cleanup without changing product deadlines or schema assertions. Transport
records all three private lanes clear at `a2d27446c`; its final head changes only evidence from that
source pin. No shared SSH interface changed.

Transport's next carrier contract requires caller-held `LocalDeliveryCustody` at actual delivery. It
retains the existing inert native process owner before admission, so bounded local cleanup may
return with exact ownership still held by the enclosing operation, keeper or provider workflow.
Pending/lost local ownership prevents another exchange; remote effects remain separate. The SSH LLD
records native descriptor/terminal retention and aggregate drain without reopening dispatch. This is
planned integration, not an implemented adapter guarantee. The design-only dependency and SSH
response preserve executable/configuration trees from published `2ef07cd6e`; the code-gate evidence
below retains its measured pin. The preceding transport source's hosted CI `37513250709` and CodeQL
`37513246961` pass, including the corrected Windows fixture. Its original failure's cause remains
unknown. No native gate follows from these hosted results.

Combined code pin `38934134e3b8d12af814f9371b62075daa49db3e` preserves all 64 Python paths in the
SSH contribution, including deletions, from published `186458307b`. Its full suite passes 16,125
non-integration tests with 51 skips and 27 warnings, exit 0 in 267.18 seconds. Full Ruff/format
(1,329 files), strict mypy (1,292 sources), exact CI typer isolation, file quality, locked-SDD,
Rulesync and whitespace checks pass. Website validation passes 160 Python and 103 Node tests, four
builds and both deterministic comparisons. Exact settled test/build roots have no same-user live
cwd/fd references and are removed; logs are retained. All 25 completed plan records remain
unchanged, and the SDD remains unlocked. This refresh closes no native SSH, terminal, trust or
production RunContext acceptance gate.

The preceding combined code pin `a38e5814fd180fe65899001d5a74426c4587dcf0` preserves all 64 Python
files in the SSH contribution, including retained deletions, from published `47013bcf2`. Its full
suite passes 16,046 non-integration tests with 51 skips and 27 warnings, exit 0 in 251.43 seconds.
Full Ruff/format (1,320 files), strict mypy (1,283 sources), exact CI typer isolation, file quality,
locked-SDD, Rulesync and whitespace checks pass. Fresh website checks pass 160 Python and 103 Node
tests, four builds and both deterministic comparisons. Exact settled test/build roots have no
same-user live cwd/fd references and are removed; logs remain retained. All 25 completed records
remain unchanged and this SDD remains unlocked. This dependency refresh adds no native SSH terminal,
authentication, trust or production RunContext acceptance.

The preceding code pin `7e88676faaf5566f9f7b75a394e6e94ac4c6ab9d` preserves every SSH source and
test byte from published `f4dccab54`. Its complete suite passes 15,881 non-integration tests with 51
skips and 27 warnings, exit 0 in 257.82 seconds. Full Ruff/format (1,316 files), strict mypy (1,279
sources), exact CI typer isolation, file quality, locked-SDD, Rulesync and whitespace checks pass.
Website validation passes 160 Python and 103 Node tests, four builds and both deterministic
comparisons. This proves the local combined dependency refresh, not native activation or new SSH
terminal/production acceptance. The
[Windows ancestor-sharing correction](phase2-results.md#windows-ancestor-sharing-correction) records
that rebase and its remaining limits. The earlier
[local-download stack refresh](phase2-results.md#private-local-download-stack-refresh) and
[advanced-stack refresh](phase2-results.md#advanced-transport-stack-refresh) retain the pins they
validated. The
[structural preflight integration](phase2-results.md#structural-preflight-integration) records the
combined SSH result. The
[private-access dependency update](phase2-results.md#private-access-and-paired-plan-dependency-update)
records an earlier rebased SSH head and its scope. The preceding
[recovery-dispatch dependency update](phase2-results.md#recovery-dispatch-dependency-update) retains
its measured composition. The
[file-custody adoption](phase2-results.md#durable-file-call-custody-adoption) records the
managed-target binding and durable obligations in the SSH upload proof. The earlier
[joint Windows/WSL2 report](phase2-results.md#joint-windowswsl2-mechanism-proof) retains its
measured composition and mechanism scope. #832 stacks on transport's implementation branch;
transport lands first. Buffered adapter validation does not close the shared launch-interruption
gate, live I/O native acceptance, terminal preparation or additive RunContext delivery. Terminal/PTY
work is proceeding in parallel in the transport lane, as confirmed by the operator.

Transport's
[launch-boundary response](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6009186902)
confirms that it owns the borrowed native-stdin addition to the existing held process owner. Its
[published dependency checkpoint](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010031679)
now supplies the privately reviewed shared types and launch option. The
[terminal implementation record](terminal-results.md) separates shared adoption, the privately
reviewed POSIX relay and Windows caller resource from native acceptance. The public terminal feature
remains disabled. SSH retains its PTY slave until launcher/client settlement and keeps the native
resource worker through interrupted waits and relay cleanup. An
[owner-mediated resize boundary](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6010061812)
is now
[accepted by transport](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6010143834),
with its
[reviewed pin now published](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010798164).
The private SSH relay uses that actual finite-budget notification API; an accepted local signal does
not prove remote geometry propagation. SSH does not access a private client PID or add a second
process owner. Transport's
[Windows response](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010798696)
accepts native launch/preparation ownership while leaving selection and native proof open. The
[SSH reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6009208568) records
the current resource increment and remaining proof separately from that planned shared seam.

### Complete the carrier and migration

The [POSIX resource implementation](terminal-lld.md#posix-resource-implementation) and privately
reviewed relay advance acquisition, cleanup evidence and retained restoration. Resize composition,
shared presentation cleanup and complete native terminal delivery remain open; the completed-carrier
and supported-workstation gates below are unchanged.

- [x] Adopt transport #833's shared finite-input subprocess pump at `e85e9f5c` while preserving SSH
      environment policy and report provenance. Integration code `1b2c6832` passes the combined
      execution and full local suites, recorded in
      [phase2-results.md](phase2-results.md#shared-subprocess-adoption). This records buffered
      adoption only; launch-interruption ownership, live/terminal delivery and production
      composition remain open.
- [x] Supply the standalone buffered-mode refusal in `678e487d` so transport can widen shared I/O
      before SSH adopts the new modes. Validate its separate cherry-pick onto `a885ef5a`, existing
      buffered behavior and refusal before admission/process work. The
      [integration record](phase2-results.md#remaining-integration-and-acceptance) distinguishes
      simulated future types from pending real-extension proof.
- [x] Adopt transport `bb7ebb58` runtime selection and stdin helper framing in the SSH file
      fixtures. Require positive runtime admission before helper observations, retain explicit SSH
      trust in command-size fixtures, and repeat read, stage and snapshot delivery through installed
      Linux OpenSSH. All three cases pass at `e852aef2`; production file and native acceptance
      remain open.
- [x] Adopt the transport-owned `LocalProcessOwner` for forwarding, retaining ownership before
      dispatch and stopping pipe borrowers before release. The operator authorized the scoped SSH
      contribution on 2026-09-21; transport already published the extraction, so reuse it and keep
      any integration-required shared correction separable. Terminal and RunContext remain with
      transport. Implementation `8c3c4e5b` passes the focused and full local suites, installed Linux
      forwarding cases and all three private reviews; the
      [evidence record](phase2-results.md#forwarding-uses-the-shared-held-process-owner) retains the
      remaining shared interruption and native acceptance limits.
- [x] Prove the private owned upload/publication composition through real Linux SSH at `eea4ba2ba`:
      create, revision-matched replacement, duplicate-create and stale-revision refusal, typed
      reduction, binary integrity, sensitive delivery and exact custody/scratch cleanup. The
      [file-workflow record](phase2-results.md#owned-upload-delivery-over-ssh) scopes this to the
      private composition; production FileAccess, recovery faults and native platform acceptance
      remain open.
- [x] Refuse input unsupported by SSH's byte adapter before trust admission, installed-client
      probing or dispatch, including an already expired operation. This prepares for transport's
      terminal input extension without mapping it to pipe EOF. The synthetic extension regression
      proves passive refusal; native terminal implementation and proof remain open.
- [ ] Finish the SSH LLD for R1-R5: complete connection validation, installed-client/path policy,
      config schema and conversion, trust preservation/enrollment/refusal, process/terminal and
      forwarding lifetimes, diagnostics and acceptance fixtures. Name the authority and refresh
      procedure for copied CA/revocation policy, failed updates and rollback.
- [ ] Complete the carrier's supported delivery modes and explicitly owned forwarding, retaining the
      proven buffered implementation and the transport-owned execution semantics. Optional SCP
      acceleration must use the same connection policy and obey shared file semantics. Adopt the
      concrete shared caller-held delivery custody at actual SSH delivery, with no temporary
      adapter-local storage. Prove bounded pending construction/cleanup, serialized settlement,
      refusal while ownership is pending/lost, native descriptor/terminal retention and aggregate
      drain after borrow handoff. Retain immutable evidence and original interruption; local cleanup
      establishes no remote cancellation or dispatch-debt resolution.
- [ ] Implement and validate connection/trust migration with isolated copies: supported operator
      policy, authentication offers, strict verification, genuine creation provenance, concurrent
      writer ownership and rollback evidence. Follow the
      [migration strategy](migration-strategy.md).
- [ ] Verify R1-R5 across supported Linux/macOS/Windows workstations and target/provider boundaries.
      Exercise terminal restoration, forwarding/listener failure and cleanup, plus the complete
      isolation and trust acceptance requirements. Record missing live coverage for operator
      disposition; inherited PoC evidence covers only its observed cases. Include
      [issue #845](https://github.com/WayfarerLabs/agentworks/issues/845)'s percent-encoded JSON
      environment shape: verify exact guest bytes for percent tokens, quotes, backslashes, Unicode
      and multiline values through the production new path, with values absent from SSH argv. Cover
      the reported Windows OpenSSH 9.5 client and an expansion-enabled 10.x client, alongside
      supported Linux/macOS workstations. Private Linux candidate proof does not close this
      production/native gate or fix callers still using the legacy path.

The [native environment report](phase2-results.md#native-environment-delivery-on-four-clients) now
proves all 14 synthetic cases through private preparation and SSH carrier composition at `4e32a9f9`,
on Linux 9.2p1, Windows 9.5p2/10.0p2 and macOS 10.2p1 clients. Production RunContext delivery
remains open. Its unchanged production-path comparison reproduces expansion failures on 10.x and
exact delivery on 9.x; the original Windows 11 executable remains unconfirmed. No legacy fix or
completion checkbox follows from that evidence.

The
[native managed-lifecycle report](phase2-results.md#native-managed-lifecycle-on-the-composed-ssh-branch)
adds test-scoped Linux/GCE evidence on the #833 + #832 composition. The
[follow-up native boot-fence report](phase2-results.md#derived-guest-boot-fence-proof-on-composed-ssh)
verifies stable and changed guest-boot identity across real Linux SSH reboots and WSL2 distribution
restarts. Neither supplies the production composer, public RunContext path, lost-hold recovery,
terminal delivery or supported-workstation acceptance. The boot-fence recovery gate remains with
transport. The later
[transport schema-correction report](phase2-results.md#transport-schema-correction-on-the-unchanged-execution-path)
resolves the database-open collision and records green hosted CI without adding a new SSH runtime
composition or closing those Phase 2 gates.

The
[native workstation report](phase2-results.md#native-workstation-report-and-remaining-delivery-findings)
adds private Linux/macOS SSH publication and Windows WSL2 observations. It also finds macOS home ACL
refusal, a hardened-admin boot-probe failure and slow repeated-chunk SSH downloads. Transport owns
their corrections; ordinary admin composition, usable home destinations and practical file delivery
remain unproved. The earlier root-only fence proof does not close the admin-path gate. These
findings remain within the open workflow acceptance below, not new completion claims.

### Integrate and deliver the usable new path

- [ ] Integrate with transport's platform-host/provisioning consumers and review provider-inner
      isolation evidence. Exercise reusable host composition separately from Lima management.
- [ ] Supply SSH delivery/failure evidence for transport's later file-only slice and other required
      workflows, using its current acceptance definitions. Transport owns the file API, policy and
      harness; this SDD does not mirror the operation list. The later slice gates broader file
      migration, not the Phase 1 carrier proof.
- [ ] Reconcile current consumer risks with transport's migration inventory, including bootstrap
      tools, sensitive discovery, ownership/cleanup checkpoints and activation behavior. Transport
      owns those caller changes. Use the
      [current risk disposition](migration-strategy.md#current-migration-risk-disposition),
      retaining the historical comparison pointer and truthful baseline checkboxes. Source
      reconciliation does not complete the required workflow evidence.
- [ ] Deliver connection/trust migration and rollback evidence before the first new production use.
      Resolve old/new writer ownership and config compatibility while both stacks remain usable; no
      duplicate mutation dispatch or permanent bridge. Shared job/plugin migration stays with
      transport.
- [ ] Verify the complete SSH-backed workflows required for additive delivery with transport's new
      surface. Prove new targets usable through `admin_execution_target()` and
      `agent_execution_target()` alongside unchanged `admin_target()` and `agent_target()` behavior.
      Run new-stack tests with retirement modules unavailable in isolation; this does not delete the
      old production path. Remove or promote SSH-owned proof-only scaffolding while retaining useful
      regressions.
- [ ] Pass independent project, complexity and correctness reviews, applicable local/hosted gates
      and authorized live validation at the integrated implementation head. Promote implemented SSH
      contracts, configuration and recovery guidance into permanent documentation with the code.
- [ ] Record R1-R5 implementation acceptance, exact integration evidence and remaining retirement
      obligations. Hand off the implementation PR without `locked.md` or a claim of full SDD
      closure.

**Phase 2 definition of done:** the full carrier and connection/trust migration machinery satisfy
R1-R5 for additive delivery, required integrated workflows pass, both paths are usable through
RunContext with old behavior preserved, and permanent collateral reflects what ships. The SSH PR may
land in the agreed dependency order; transport's consumer migration and physical deletion are later
work, not prerequisites for implementation delivery or claims of completion here.

## Intervening transport stages: SSH waits and responds to issues

Transport leads these ordered stages, with SSH assistance only when an issue requires it:

1. Migrate all production/plugin consumers, including direct calls, from old to new.
2. Remove legacy access from RunContext after callers have migrated.
3. Delete old transport and validate the resulting production workflows, leaving old SSH in place.

These are external dependencies, not SSH implementation tasks or completed claims. Record their
actual merged revisions and acceptance evidence when the operator requests final SSH retirement.
Keep this SDD open and unlocked while waiting. Do not begin old SSH deletion automatically when
transport finishes. No new recipient permission isolation is claimed during coexistence; transport
owns activation against its complete removal gate. SSH trust and operational safety apply from the
first new-stack use.

## Phase 3: Remove old SSH on the operator's later request

This final SSH PR begins only after consumer migration, legacy RunContext removal, old transport
deletion and explicit operator direction. It completes this SDD rather than creating a new effort.

- [ ] Receive the retirement request and verify the preceding stages against their merged revisions
      and full reports. Refresh the legacy SSH inventory, including direct/lazy imports, provider
      paths, plugin entry points, fixtures and retained utility dependencies. Resolve remaining
      consumers before deleting their implementation.
- [ ] Physically delete the old SSH stack and obsolete SSH-only scaffolding/tests. Retain useful
      replacement regressions and deliberately retained utilities with an audited dependency
      closure. Preserve operator configuration, credentials, complete trust/revocation records and
      rollback/cleanup evidence; deleting implementation does not authorize deleting that state.
- [ ] Verify installed-package startup and complete SSH-backed production workflows after physical
      deletion, including supported workstation/platform coverage. Refresh independence against
      transport's authoritative retirement set and record any missing evidence for operator
      disposition rather than claiming a pass.
- [ ] Complete private reviews, applicable local/hosted gates and full integration reports at the
      retirement head. Update permanent guidance for the behavior now shipped, and record R1-R5
      final acceptance and dependency dispositions.
- [ ] Add `locked.md` only with this final retirement and evidence-backed closeout.

**Phase 3 definition of done:** old SSH is physically absent after the earlier transport stages,
complete installed workflows pass without either legacy stack, retained state is preserved, and the
final acceptance record and permanent collateral are current. Only then is this SDD complete.
