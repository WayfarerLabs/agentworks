# Independent SSH Carrier: Two-Phase Delivery Plan

- Updated: 2026-09-17
- Requirements: [frd.md](frd.md)
- Architecture: [hla.md](hla.md)
- Shared contract:
  [transport-owned definition](../2026-09-12-transport-improv/execution-contract.md)
- Shared PoC:
  [transport plan](../2026-09-12-transport-improv/plan.md#2-prove-the-shared-boundary-before-broad-implementation)
- Coordination baseline: PR #795 merged as `857110df`; reviewed transport content at `7228e3a2`

## Delivery and ownership

The operator directs two SSH implementation PRs under this SDD. PR #796, on
`feat/ssh-carrier-design`, carries the entire SSH portion of the joint proof of concept (PoC) and
these artifacts. The second SSH PR carries full implementation and the remaining integration and
retirement obligations. There is no SSH design-only PR to merge. The operator has accepted the
artifact checkpoint and authorized PoC implementation. The joint buffered proof and SSH closeout
reviews are complete. The final #796 handoff records its published head and gates before merge
intent; `review-requested` is for implementation checkpoints. The operator's existing integration
tester combines the two branches locally and supplies live evidence. A design-only main merge is
explicitly unnecessary.

The operator authorizes up to four feedback/fix rounds. Post integration-testing considerations
before raising `review-requested`; all private review lanes must be complete first. Remove the label
before each fix round and wait for full integration reports before starting the next one. Record
each handoff and round disposition on #796. Contract changes or unexpected scope stop the round for
operator direction.

Transport solely owns the carrier contract, common types, acceptance criteria, shared preparation,
public outcomes and proof harness. SSH owns its independent carrier, connection/trust migration,
fixtures and implementation evidence. SSH raises feasibility concerns to transport rather than
maintaining another contract or acceptance matrix. Requirement changes return to the operator.
Transport also owns non-SSH adapters, all consumer migration, coherent cutover and physical
deletion.

Use the transport artifacts in the same checkout as the live design reference. Record the exact
transport contract and implementation commits with each proof or integration result; a provenance
pin is not a competing definition. When common implementation code is not yet on main, make its
transport PR an explicit dependency instead of adding SSH-local substitute types or a second
harness. Stack only actual dependencies and merge them in order. This does not add another SSH PR or
authorize edits to transport-owned artifacts.

The earlier legacy-consolidation work through `2f11662d` is superseded and retained as source
material only; it is not independent-carrier implementation or proof evidence. Local proof fixtures
now exercise the new buffered carrier. Transport's
[acceptance disposition](../2026-09-12-transport-improv/proof-lld.md#joint-buffered-proof-acceptance-2026-09-17)
accepts the joint buffered PoC. Production state changes, full implementation and this SDD's
lockfile remain Phase 2.

Current dependency provenance lives in [poc-results.md](poc-results.md#revisions-and-delivery).
Completed checkboxes below record the revisions used when their work happened, including the first
candidate pin; they are historical records, not floating dependency declarations. The operator-owned
FRD retains its earlier checkpoint narration; this plan and the proof record carry current delivery
status without changing the accepted requirements.

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
      implementation consumes #826 at `81e5f6c3`; [poc-results.md](poc-results.md) records the
      boundary and outstanding combined acceptance.
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
full terminal/forwarding support and production cutover are Phase 2 work, except for anything the
transport PoC itself requires. Unsupported optional behavior refuses before dispatch.

## Phase 2: Full SSH implementation in the second PR

Start from the accepted PoC after its material findings are resolved. Continue this SDD and extend
the proven carrier. Coordinate this PR's integration and landing order with transport's complete
implementation/cutover; the SSH PR does not take ownership of transport's files or retirement work.

### Complete the carrier and migration

- [ ] Finish the SSH LLD for R1-R5: complete connection validation, installed-client/path policy,
      config schema and conversion, trust preservation/enrollment/refusal, process/terminal and
      forwarding lifetimes, diagnostics and acceptance fixtures. Name the authority and refresh
      procedure for copied CA/revocation policy, failed updates and rollback.
- [ ] Complete the carrier's supported delivery modes and explicitly owned forwarding, retaining the
      proven buffered implementation and the transport-owned execution semantics. Optional SCP
      acceleration must use the same connection policy and obey shared file semantics.
- [ ] Implement and validate connection/trust migration with isolated copies: supported operator
      policy, authentication offers, strict verification, genuine creation provenance, concurrent
      writer ownership and rollback evidence. Follow the
      [migration strategy](migration-strategy.md).
- [ ] Verify R1-R5 across supported Linux/macOS/Windows workstations and target/provider boundaries.
      Exercise terminal restoration, forwarding/listener failure and cleanup, plus the complete
      isolation and trust acceptance requirements. Record missing live coverage for operator
      disposition; inherited PoC evidence covers only its observed cases.

### Integrate, cut over and close

- [ ] Integrate with transport's platform-host/provisioning consumers and review provider-inner
      isolation evidence. Exercise reusable host composition separately from Lima management.
- [ ] Supply SSH delivery/failure evidence for transport's later file-only slice and other required
      workflows, using its current acceptance definitions. Transport owns the file API, policy and
      harness; this SDD does not mirror the operation list. The later slice gates broader file
      migration, not the Phase 1 carrier proof.
- [ ] Reconcile current consumer risks with transport's migration inventory, including bootstrap
      tools, sensitive discovery, ownership/cleanup checkpoints and activation behavior. Transport
      owns those caller changes. Retire `main-comparison.md` after recording the disposition of
      every material risk in this plan or the relevant LLD; retain the truthful baseline checkboxes.
- [ ] Deliver connection/trust migration and rollback evidence for the transport-owned cutover.
      Resolve its writer, job and plugin compatibility dependencies without a second public stack,
      duplicate mutation dispatch or a permanent bridge.
- [ ] Verify complete SSH-backed production workflows in the integrated state after transport's
      physical legacy deletion. Refresh the independence audit against its authoritative retirement
      set. Remove or promote SSH-owned proof-only scaffolding and retain useful regression tests.
- [ ] Pass independent project, complexity and correctness reviews, applicable local/hosted gates
      and authorized live validation at the final integrated head. Promote implemented SSH
      contracts, configuration and recovery guidance into permanent documentation with the code.
- [ ] Record requirement-by-requirement acceptance and dependency dispositions. Add `locked.md` only
      with final completion, after integration and retirement obligations are satisfied.

**Phase 2 definition of done:** the full SSH implementation and migration satisfy R1-R5, the
transport-owned production switch and physical retirement are verified in the integrated result,
permanent collateral is current, and this SDD has a truthful final acceptance record. A leaf-package
merge alone does not complete Phase 2. There is no third deferred SSH migration or closeout phase.
