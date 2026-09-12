# Independent SSH Carrier: Design and Delivery Plan

- Updated: 2026-09-12
- Requirements: [frd.md](frd.md)
- Architecture: [hla.md](hla.md)
- Shared sequence and proof matrix:
  [transport plan at `698ddb23`](https://github.com/WayfarerLabs/agentworks/blob/698ddb23b278f364e460f0ae15bac5fab8c74b12/docs/sdd/2026-09-12-transport-improv/plan.md)

## State and delivery

The earlier reduced-design review converged with zero of two additional fix rounds used. Local
legacy-consolidation work through `2f11662d` is superseded as a delivery approach and preserved as
source material; it is not an independent-carrier implementation or proof result.

The current task is rewriting SSH artifacts to match the reviewed #795 proposal. No prototype/live
proof, broad rebuild, production cutover or merge starts under this artifact task. The old
implementation-push plan does not bypass the new proof gate. The operator requested a fresh branch
and draft PR to supersede #757. Branch `feat/ssh-carrier-design` starts from main `7c744828` and
contains these artifacts only. Preserve `feat/ssh-connection-contracts` and its commits as
reference; none of its runtime changes are part of this checkpoint. Keep the replacement PR draft.

The default eventual landing unit is the new stack plus complete transport-owned cutover/removal,
not an independently released replacement of legacy SSH callers. Coordinate the delivery vehicle
with the transport owner after proof. Artifact promotion/merge and new proof resources require
operator direction. No lockfile or new-stack completion is claimed here.

## 1. Agree on the small contract and proof charter

- [ ] Agree with transport on the proposed shared values, including the sole input choice in
      CarrierIO, stream ownership/cancellation, output provenance and partial failure evidence. Done
      when both contributors use one pinned candidate contract and acceptance expectations.
- [ ] Inventory executable selection, OpenSSH versions and server compatibility at workstation,
      platform-host and provider-inner sites under the HLA's version boundary. Specify version
      refusal, account-shell/bootstrap combinations and prerequisites in an SSH LLD before accepting
      proof evidence. Do not infer another hop's compatibility from the workstation client.
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
      process/terminal/forwarding lifetimes, diagnostics and acceptance fixtures. Preserve the
      common layer's ownership of application semantics and use one set of shared carrier types.
- [ ] Confirm transport's execution/file/job LLD and remaining Proxmox/WSL2/macOS-host feasibility
      gates are satisfied or have operator disposition. This is dependency review, not a claim to
      implement or complete the other effort's work.

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
- [ ] Record Linux/macOS/Windows workstation evidence separately from target/provider/inner-client
      versions. Missing required coverage needs operator disposition; mocks are not live acceptance.

## 5. Cutover support and closeout

- [ ] Deliver tested connection/trust migration and rollback evidence to the transport-owned
      cutover. Resolve writer ownership, surviving jobs and plugin compatibility with their owners
      before switching production; no duplicate mutation dispatch or old/new public selector.
- [ ] Verify SSH-backed production workflows through the complete new stack after transport
      physically deletes the retirement set. Run required local/hosted gates and independent
      project, complexity and correctness reviews at the agreed implementation head.
- [ ] Promote implemented SSH contracts, configuration and recovery guidance to permanent docs with
      the code that makes them true. Do not treat existing local guide edits as shipped behavior.
- [ ] Record requirement-by-requirement evidence, review disposition and any remaining operator
      decisions. Close the effort only after its integration/retirement obligations are met; do not
      infer SSH acceptance merely from the other effort's merge or green unit tests.
