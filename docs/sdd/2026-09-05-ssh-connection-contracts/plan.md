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
retirement obligations. There is no SSH design-only PR to merge. The present artifact-only head is
an intermediate review checkpoint on #796, not its intended merge content; keep it draft and use
`review-requested` to request feedback.

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
material only; it is not independent-carrier implementation or proof evidence. No PoC execution,
production state change, full implementation or lockfile is claimed by this artifact checkpoint.

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

- [ ] Consume transport's concrete candidate types and acceptance criteria. Record the exact
      contract and code revisions in the proof record; send unresolved SSH feasibility questions to
      transport before accepting the boundary. Transport owns any common-contract correction.
- [ ] Write `ssh-lld.md` with the SSH decisions needed for the PoC: explicit connection and trust
      inputs, executable selection/version refusal, supported account-shell bootstrap, subprocess
      I/O and cleanup. Separate observed decisions from open hypotheses and from Phase 2 details.
      Inventory workstation, platform-host and provider-inner client locations under the HLA's
      OpenSSH 8.5 boundary, recording actual versions and separate server compatibility for the
      exercised cases. Record unexercised locations as gaps, not inherited compatibility.
- [ ] Resolve proof prerequisites with transport, including initial SSH delivery before Python
      installation and no-staging readiness. A proposed helper must not require the package that the
      first invocation needs to install. Platform-host userspace is a separate inventory.
- [ ] Obtain the bounded live-proof charter naming isolated resources, credentials, versions,
      workload, budgets, cleanup and evidence. Transport owns QGA access and the joint harness; the
      SSH portion must identify its resource limits explicitly. This artifact review does not select
      operator hosts or permit implicit use of operator trust stores.

### Build and demonstrate the complete SSH contribution

- [ ] Implement the independent buffered SSH binding in `execution/carriers/ssh/` and its tests in
      `tests/execution/carriers/ssh/`, using transport's leaf contract and shared preparation. The
      code used by the proof is the destination implementation, not a wrapper around legacy SSH or a
      separate public prototype API. Production factories and RunContext remain on the old stack
      during this phase.
- [ ] Supply explicit isolated connection policy for the proof, including identity/agent selection,
      strict fixture trust, version refusal and ambient-config isolation. Build enough real policy
      to substantiate those guarantees; do not disable verification to make the fixture pass.
      Production config conversion and the complete migration matrix belong to Phase 2.
- [ ] Exercise every SSH-applicable case of the transport-owned PoC matrix and detailed carrier
      contract through the joint harness, including its focused I/O ownership/failure tests. Link
      results to the authoritative cases rather than copying their inventory here. No failed or
      missing SSH proof case can be deferred to Phase 2 and called a complete SSH PoC.
- [ ] Run the proof's new-stack imports and tests with the transport-defined retirement modules
      unavailable. Audit dependency closure, including normal package initialization and fixtures;
      copied tests must not call legacy code to calculate expected results.
- [ ] Record reproducible commands, pinned code/contract revisions, executable/server inventory,
      observed evidence, safe failure reports and independently verified cleanup in
      `poc-results.md`. Keep credentials and payload-bearing diagnostics out of retained evidence.
      Separate local fixtures from live observations and list all unsupported or untested cases.

### Accept the PoC and reconcile

- [ ] Have transport evaluate the combined proof, including its real non-SSH case, against its
      acceptance criteria. Record the disposition and links to its evidence without claiming SSH
      performed or owns that work. Unresolved feasibility blocks acceptance; a changed mechanism
      returns for the appropriate contract or operator decision and affected cases run again.
- [ ] Update this SDD and `ssh-lld.md` with observed SSH decisions and the transport-owned proven
      revision. Transport updates its own artifacts. Carry the reconciled SSH artifacts in #796 with
      the PoC, not in an intervening design-only PR.
- [ ] Complete independent project, complexity and correctness reviews, applicable local/hosted
      gates and authorized live-proof evidence at the exact handoff head. Remove `review-requested`
      when promoting the completed PoC to merge intent. Keep this SDD unlocked.

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
