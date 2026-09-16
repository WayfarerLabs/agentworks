# Independent SSH Carrier: Design and Delivery Plan

- Updated: 2026-09-16
- Requirements: [frd.md](frd.md)
- Architecture: [hla.md](hla.md)
- Shared sequence and proof matrix:
  [transport plan at `6809827f`](https://github.com/WayfarerLabs/agentworks/blob/6809827f64fb288880167fe2a4d9d7b42e29a21e/docs/sdd/2026-09-12-transport-improv/plan.md)

## State and delivery

The earlier reduced-design review converged with zero of two additional fix rounds used. Local
legacy-consolidation work through `2f11662d` is superseded as a delivery approach and preserved as
source material; it is not an independent-carrier implementation or proof result.

The current checkpoint compares the SSH design with main and the current #795 proposal under the
operator's 2026-09-16 lead handoff. The [main comparison](main-comparison.md) pins the inspected
revisions, records source evidence and identifies migration/proof risks. PR #796 remains a draft
artifact checkpoint. Its branch `feat/ssh-carrier-design` now includes main `e440a28c`; the earlier
`feat/ssh-connection-contracts` runtime work remains reference material only. No prototype/live
proof, broad rebuild, production cutover or merge is claimed by this revision.

The default eventual landing unit is the new stack plus complete transport-owned cutover/removal,
not an independently released replacement of legacy SSH callers. Coordinate the delivery vehicle
with the transport owner after proof. Artifact promotion/merge and new proof resources require
operator direction. No lockfile or new-stack completion is claimed here.

## 0. Compare the design with current code

- [x] Compare main `e440a28c` with the original `7c744828` baseline and transport proposal
      `6809827f`; record source anchors and revise SSH-owned design/migration obligations. This
      completes the source comparison only, not contract agreement or runtime proof.

## 1. Agree on the small contract and proof charter

- [ ] Agree with transport on the proposed shared values, including the sole input choice in
      CarrierIO, stream ownership/cancellation, output provenance and partial failure evidence. Done
      when both contributors use one pinned candidate contract and acceptance expectations.
- [ ] Inventory executable selection, OpenSSH versions and server compatibility at workstation,
      platform-host and provider-inner sites under the HLA's version boundary. Specify version
      refusal, account-shell/bootstrap combinations and prerequisites in an SSH LLD before accepting
      proof evidence. Include initial delivery before Python installation and supported
      platform-host prerequisites. Do not infer another hop's compatibility from the workstation
      client.
- [ ] Obtain an authorized bounded proof charter with isolated resources, credentials, workload,
      versions, cleanup and evidence. Coordinate real QGA access with transport; no operator hosts,
      trust stores or credentials are implicitly in scope.

## 2. Prove the joint boundary

Transport owns shared preparation, public result interpretation and the harness; SSH owns the
independent connection/delivery portion. The linked transport matrix is the shared acceptance
definition, not a second protocol specification maintained here.

- [ ] Supply the SSH portion of an end-to-end buffered slice: exact literal argv and supported shell
      bootstrap; separate script/application stdin and EOF; ordinary binary guest streams separate
      from client diagnostics; sensitive-input suppression; exits 0/1/255; loss and interruption
      without replay or inferred termination; no-staging readiness with guest-stream evidence.
- [ ] Prove stream ownership and failure handling: short writes, EOF, bounded
      buffering/cancellation, no surviving pumps on borrowed streams, safe source/sink errors and
      partial output evidence.
- [ ] Review transport's bounded real QGA evidence against the same contract. Record the combined
      proof revision, observed cases and gaps. A fake or old-stack SSH fixture is not joint proof.
      Unresolved shell/input/output behavior blocks broad implementation; scope changes return to
      the operator rather than introducing an SSH protocol or new library implicitly.

## 3. Reconcile after proof

- [ ] Incorporate observations into these artifacts and the SSH LLD; have transport reconcile its
      own artifacts. Publish matching candidate-contract revisions through the agreed design
      checkpoint before broad parallel implementation.
- [ ] Resolve detailed SSH policy, executable/path handling, trust/config schema and migration,
      process/terminal/forwarding lifetimes, diagnostics and acceptance fixtures. Name the authority
      and refresh procedure for copied CA/revocation policy, including failure and rollback
      behavior. Preserve the common layer's ownership of application semantics and use one set of
      shared carrier types.
- [ ] Confirm the shared execution boundary needed for the SSH build; track transport's file/job LLD
      and remaining Proxmox/WSL2/macOS-host feasibility gates against their consumers. The later
      file-only slice gates broader file migration, not independent SSH implementation. This is
      dependency review, not a claim to complete the other effort's work.

## 4. Build and verify independently

- [ ] Implement the SSH package and independent fixtures, copying/adapting useful code with
      provenance while removing all legacy dependencies. Include explicit connection validation,
      version refusal, identity/agent selection, trust migration and one shared SSH/scp policy.
- [ ] Implement single-attempt buffered/live/terminal delivery and explicitly owned forwarding.
      Honor shared deadlines, sensitivity, borrowed streams and truthful reports without application
      wrappers, automatic replay, text normalization or a second job implementation.
- [ ] Test disruptive operator/system configuration, actual identity offers with unrelated agent
      keys and sibling certificates, selected-agent signing, migrated trust, aliases/ports,
      mismatches, revocations, genuine enrollment and refusal for existing unknown targets.
- [ ] Verify binary I/O, output completeness, sensitive data, interruption, terminal restoration,
      intentional forwarding, listener failure and cleanup. Run the new-stack suite with retirement
      modules unavailable, including dependency closure and normal package imports.
- [ ] Integrate with transport's host/platform/provisioning paths and review their provider-inner
      isolation evidence. Exercise host composition independently of Lima management commands.
- [ ] Supply SSH delivery and failure evidence for transport's later file-only SSH/QGA slice:
      whole-file publication, privileged JSON updates, directories/metadata and FIFO lifecycle with
      public commands/jobs withheld. Transport owns destination confinement, grants and mutation
      semantics; optional SCP must honor them. Review transport's results before wider file
      migration, without duplicating its file API or proof harness.
- [ ] Record Linux/macOS/Windows workstation evidence separately from target/provider/inner-client
      versions. Missing required coverage needs operator disposition; mocks are not live acceptance.

## 5. Cutover support and closeout

- [ ] Deliver tested connection/trust migration and rollback evidence to the transport-owned
      cutover. Resolve writer ownership, surviving jobs and plugin compatibility with their owners
      before switching production; no duplicate mutation dispatch or old/new public selector.
- [ ] Reconcile the current artifact/setup/session consumers recorded in the main comparison with
      transport's migration inventory. Resolve structured discovery under sensitive environments,
      preserve ownership/cleanup checkpoints and activation-map behavior, and verify initial
      provisioning separately from initialized guests. No caller may weaken sensitivity to recover
      output or use public exec to bypass file grants.
- [ ] Verify SSH-backed production workflows through the complete new stack after transport
      physically deletes the retirement set. Run required local/hosted gates and independent
      project, complexity and correctness reviews at the agreed implementation head.
- [ ] Promote implemented SSH contracts, configuration and recovery guidance to permanent docs with
      the code that makes them true. Do not treat existing local guide edits as shipped behavior.
- [ ] Record requirement-by-requirement evidence, review disposition and any remaining operator
      decisions. Close the effort only after its integration/retirement obligations are met; do not
      infer SSH acceptance merely from the other effort's merge or green unit tests.
