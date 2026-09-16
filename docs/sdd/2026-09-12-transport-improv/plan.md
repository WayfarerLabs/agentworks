# Transport Improvements: Design and Delivery Sequence

- Status: Revised draft; current authorization covers artifact rewriting and draft PR publication
- Delivery vehicle: Draft PR #795 for design review, labeled `sdd:transport-improv`
- Requirements: [FRD](frd.md)
- Architecture: [HLA](hla.md)
- Proposed interfaces and layout: [Execution contract](execution-contract.md)

The required order is: agree on the small contract, prove it, reconcile both SDDs, build
independently in parallel, validate complete workflows, then cut over and physically delete the old
stack. The proof is a bounded joint slice, not permission to start the broad rebuild. This revision
runs no prototype or live test and claims no implementation completion. All gates below remain open.

## 1. Agree on the small contract and proof charter

- [ ] Agree on `PreparedInvocation`, the single input choice in `CarrierIO`, stream ownership,
      failure behavior and `CarrierReport`. Done when both efforts use the same proposed values,
      single-attempt semantics and proof expectations rather than independently inventing them.
- [ ] Record the OpenSSH 8.5 minimum's applicable binaries/locations and server compatibility in the
      SSH-owned design. Include workstation, platform-host and provider-inner invocation sites; no
      version requirement may be silently assumed from another hop's client.
- [ ] Confirm ownership: transport owns shared preparation/public outcomes and applying SSH policy
      in platform adapters and provisioning; SSH owns reusable connection/isolation policy, carrier
      delivery and trust/configuration migration. Remote Lima is the first consumer of SSH-backed
      platform access, not a concept inside SSH or a dependency for later platform consumers.
- [ ] Obtain a bounded proof/live-test charter naming isolated resources, tool versions, workload,
      cleanup and evidence. Current documentation authorization does not cover running the proof.

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

- [ ] Complete the proof matrix with observed evidence and a pinned candidate contract. A failed or
      unresolved shell/input/stream boundary blocks broad parallel implementation; revise the seam
      and repeat the affected cases. If the needed mechanism changes scope, return to the operator.
      This gate does not claim exact SSH exit/drop classification or full platform acceptance.

## 3. Reconcile designs and publish the implementation boundary

- [ ] Incorporate proof findings into this SDD and have the SSH owner reconcile #796's independent
      carrier design against the same proven contract. #796 supersedes #757; no legacy consolidation
      precedes the rebuild. Each effort edits only its own artifacts.
- [ ] Review and publish matching design revisions before broad parallel work. The current draft is
      a review vehicle; artifact promotion/merge requires operator direction. Record the common
      contract revision and evidence both efforts will build against.
- [ ] Complete execution/context and file/job LLDs: shell/startup combinations, no-staging
      readiness, bootstrap tools, bounded transfer/path policy, jobs/cancellation/retention and
      stale records. Preserve macOS host jobs before guest creation. Establish remaining
      Proxmox/WSL2 feasibility under an authorized live-test charter; the small proof does not stand
      in for these checks.
- [ ] Complete the file LLD for R7: whole-file publication, JSON merge semantics, ownership/mode and
      security metadata preservation, directories/FIFOs/removal, concurrency and uncertain results.
      Enumerate tools available during native bootstrap and on supported platform hosts; prove
      destination-side confinement rather than relying on a preflight path check. Define trusted
      ancestors/mounts, private staging/locks, root creation and fail-closed behavior.
- [ ] Inventory required mutation destinations and actions for harness configuration, `/opt`
      provisioning, `/run` session objects, recovery and platform hosts. Review execution-bearing
      content and select explicit core allowlist entries, trusted dynamic-root resolution and
      recipient subsets. No broad parent grant or public-exec workaround to make a caller pass.
- [ ] Finalize scoped command/file/job interfaces and bound restrictions, including the core file
      ceiling. Keep registration requests, user consent, a general plugin policy evaluator and
      hostile-code isolation out of scope. Map FRD R11's future workflows to tests.

## 4. Build the independent stacks in parallel

- [ ] SSH effort builds `execution/carriers/ssh/` and its connection/trust migration. Transport
      builds common execution, scoped context delivery, files/jobs and other adapters, and applies
      SSH policy in platform-host/Lima/provisioning paths. Test reusable host composition separately
      from Lima commands; preserve one SSH implementation in the new stack.
- [ ] Integrate against the agreed contract and run the new-stack tests with legacy modules
      unavailable, including indirect imports and plugin initialization. No production old/new
      selector, shared legacy runner, or duplicate mutation dispatch is allowed.
- [ ] Specify and validate SSH state transition, concurrent-writer ownership and rollback evidence.
      Preserve configuration and complete trust records without importing old execution code.
- [ ] Deliver a shared file-only vertical slice through SSH and native QGA, with commands/jobs
      withheld: whole-file installation, privileged JSON merge preserving unrelated keys, approved
      directory creation/metadata and FIFO lifecycle. Keep the initial small carrier proof intact;
      this later slice gates broader file-consumer migration, not the independent SSH build.
- [ ] Prove the file boundary with behavioral tests: default denial, exact-file/subtree scopes, root
      versus parent authority, prefix collisions/traversal, links and concurrent substitution,
      confined extraction, forbidden metadata/removal, and inability to widen grants. Cover
      attempted helper redirection through caller environment, PATH or working directory,
      cooperating writers, external-writer limits, malformed JSON, special-object refusal, sensitive
      diagnostics, partial transfer/cleanup and uncertain publication. No runtime fallback may
      expose commands to the file-only caller; unavailable safe mechanics block acceptance. Record
      live target/platform evidence under an authorized charter.

## 5. Validate complete workflows, cut over, and retire

- [ ] Validate complete provisioning, native recovery without Tailscale, scoped plugin operations,
      backup, host provisioning/rollback and interactive attachment through new internal entry
      points. Cover required operations, optional refusal, sensitive data and supported workstation/
      platform versions. Missing evidence requires operator disposition, never a passing claim.
- [ ] Complete the [migration inventory and cutover gates](migration-strategy.md): reconcile #789,
      audit caller shells/identity/I/O/lifetimes/grants and approved filesystem destinations,
      migrate file-provisioning shell snippets to FileAccess where it expresses the operation,
      resolve surviving jobs, plugin compatibility, state migration and rollback. Every old entry
      point has a destination and removal point.
- [ ] Switch factories, `RunContext` producers/consumers, plugins and direct services coherently;
      prove real production workflows after physically deleting old execution modules and temporary
      scaffolding. Transport owns this complete cutover, not just preference for the new runner.

The default implementation landing unit contains the new stack and complete cutover together.
Separating delivery later requires independently complete units and an explicit removal point, not
releasing two public stacks. No checkbox above claims that the separately owned SSH work is done.

Future implementation satisfies FRD R1-R11 and promotes implemented contracts into permanent docs
with the code that makes them true. Closeout requires evidence-backed validation, complete
retirement and a truthful final plan before creating `locked.md`.
