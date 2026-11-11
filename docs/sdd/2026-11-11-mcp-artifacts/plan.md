# MCP artifacts: implementation plan

## Ownership, vehicle and definition of done

The operator chartered this successor effort on 2026-11-11. The lead owns architecture, this plan,
integration and delivery; draft requirements are in [frd.md](frd.md). Tool filters are excluded.
OAuth is phase 2 of this same SDD under the operator's clarification; phase 1 rejects its fields.
The source baseline is `834eb715`.

The checkpoint was refreshed onto main `07feb4a4` after session batch teardown PR 776 merged.
Artifact/setup/native/env/secret seams are unchanged; current restart still stages prospective
artifacts before teardown and commits the new run only afterward. Preserve that ordering and its
conditional runtime-state checks.

Use the initial branch/PR for the complete registration phase. First publish one coherent SDD-only
draft with FRD, HLA, this plan, research, LLD, migration and acceptance together; apply
`review-requested` for that checkpoint. Do not merge the checkpoint or claim implementation. Count
the design feedback round within the three-round allowance, leaving at most two subsequent
feedback/fix rounds unless the operator extends it. Remove the label before implementation. A
complete green registration implementation gets a scoped handoff and ready state without that label.
OAuth follows in a separate PR under this same open SDD, with up to three rounds per PR. Its
detailed design follows the first working push. No automatic merge and no lock after phase 1.

Closeout PR 887 remains a separate ready predecessor. Its green head and clean report are retained;
MCP research/design can proceed while its normal merge is pending. Refresh main and assess actual
predecessor state before implementation handoff. Do not edit predecessor SDDs or the saga ledger.

Overall done means R1-R11 implemented and independently evidenced at the actual head, public
collateral current, required private reviews clear, all gates green, actual integration findings
critically disposed within budget, cleanup independently verified and the final SDD lock justified.
Publishing a design or passing the unit suite alone does not complete this plan. Registration
acceptance completes phase 1 only; the OAuth tasks remain owed.

## Design checkpoint

- [x] Draft the complete SDD set against current source and primary native/Rulesync references.
- [x] Resolve the canonical subset, source/inline shape, credential-reference grammar and
      scope/ownership boundaries together across FRD, HLA and LLD.
- [x] Run independent project and Muntz reviews in separate pinned worktrees; incorporate material
      findings without widening the charter.
- [ ] Run applicable repository/website gates and exact-head CI; publish the draft checkpoint with
      `sdd:mcp-artifacts` and `review-requested`.
- [ ] Monitor the single design-only feedback window under the delivery process, critically assess
      the full batch and record requirements questions or changes needing operator direction.

## Canonical model and capture

- [ ] Add the strict MCP definition/declaration models and fifth type maps using ordinary
      whole-entry merge and original-owner identity; reject unknown or contradictory input.
- [ ] Implement bounded JSON/JSONC source parsing with duplicate-key rejection, selecting the
      same-named server; capture only normalized selected content. Choose a maintained parser if a
      dependency is needed, verify its current stable release and avoid a custom comment lexer.
- [ ] Extend inline/workstation/Git capture and equivalent-content identity; preserve acquisition
      limits, no execution/network-to-endpoint behavior and credential-safe diagnostics.
- [ ] Introduce codec v4 with strict retained v2/v3 interpretation and unknown-version protection;
      preserve legacy hashes and empty-map freshness as specified in migration.
- [ ] Prove per-type replacement, unchanged old artifacts, scope grouping and absence of resolved
      credentials in durable data. Use meaningful boundary tests, never authored-prose pins.

## Native entry application

- [ ] Resolve native-home roles and use existing guarded file APIs and normal setup invocation.
      Implement bounded MCP entry reconciliation with existing parsers, claims, fingerprints,
      checkpoints and expected-content publication; no parallel state or whole-document ownership.
- [ ] Implement deterministic original-owner-qualified native identities, adoption, owned
      convergence, per-entry foreign conflict, removed-entry cleanup and modified obsolete
      retention.
- [ ] Plan Codex settings mapping contributions and MCP entries together, preserving every existing
      settings strategy and diagnosing conflicting explicit values before canonical mutation. Test
      canonical withdrawal with a still-authored mapped key under all four strategies in one pass,
      including suppressed initial contributions and original file-existence decisions.
- [ ] Add Claude/Codex/Grok user and workspace translation and explicit VM deferral. Every adapter
      must apply or defer the new type; test against silent handled-but-omitted behavior.
- [ ] Translate unresolved env/header/bearer references to actual native primitives. Outer setup
      cannot require future-user values; test a project definition later bound by each actual user.
      Session-local missing names and unsupported representations defer whole entries. Preserve
      native trust/approval/auth state.
- [ ] Publish Shell canonical JSON and indexes at all existing scope locations, with no new
      deferrals or server execution.
- [ ] Verify interruption/retry, other-owner preservation, activation removal and ordinary parent
      deletion/rehome without introducing a new gate or stored-path I/O authority.

## Opt-in session delivery

- [ ] Add and test Claude private MCP config and Codex invocation-only per-server overrides using
      existing prospective-run/files lifecycle. Defaults remain explicit terminal warnings.
- [ ] Evaluate the documented Grok private plugin candidate with the real harness. Implement and
      advertise its workaround only after fresh/resumed/neighbor-session proof; otherwise retain an
      honest unsupported disposition and bring any acceptance scope question to the operator.
- [ ] Preserve all existing artifact workaround selection and native argv validation. Do not replace
      other MCP sources, expose credentials in argv or write shared catalogs for isolation.
- [ ] Prove pre-teardown refusal/staging failures preserve the working runtime/effects. For launch
      failure after confirmed teardown, retain accurate retry/cleanup state without rollback claims.
      Successful replacement retires only obsolete run files, and delete/name reuse does not affect
      another session.

## Inspection, collateral and validation

- [ ] Extend safe placement/claim inspection and per-type applying/deferring output, preserving
      offline behavior and existing small-list truncation. Do not add secret/source/native probes.
- [ ] Update artifact and integration READMEs/guides, sample config, examples, schema teaching and
      release/upgrade notes with actual supported mechanisms, limits, native formatting changes and
      downgrade behavior. Permanent teaching must stand alone without this SDD.
- [ ] Run the full frozen CLI/repository/website gates and installed wheel/source archive checks,
      standalone guide renders and source/package parity on the complete candidate.
- [ ] Run independent pinned project, Muntz and generic correctness/security reviews for code; fix
      material findings and revalidate the actual final delta before public handoff.
- [ ] Hand off the complete green pushed head with integration emphasis from
      [acceptance](acceptance.md), then mark ready without `review-requested`.
- [ ] Wait for each actual independent integration report before a subsequent implementation fix
      round, observing up to six hours. Critically assess the complete batch, return to draft before
      changes, re-run required private lanes/gates and re-hand off within the remaining allowance.

## Phase 2: OAuth

- [ ] Resolve the operator's native-configuration versus Agentworks login-workflow preference and
      complete the [OAuth design](oauth-phase.md) after the first working registration push.
- [ ] Specify strict supported auth fields, native translation and actual-user credential ownership;
      research public/confidential clients, scopes, remote browser/callback behavior and
      session-only discovery without introducing an Agentworks token store or protocol client.
- [ ] Keep the detailed phase-2 design/FRD/plan update with its OAuth implementation PR and assess
      feedback through normal delivery. Obtain any changed-requirements ruling before building it;
      the single initial SDD-only checkpoint remains the artifact-only review requested here.
- [ ] Implement supported canonical OAuth configuration and the agreed explicit authentication
      workflow with native credential sinks and existing secret/transport boundaries. Keep ordinary
      setup/start free of automatic consent/login and unsupported combinations explicit.
- [ ] Validate actual native OAuth and authenticated MCP use, user isolation, cancellation,
      advertised refresh/status/logout behavior and independent credential/resource cleanup.
- [ ] Update permanent guides, examples, inspection and upgrade teaching; run all gates and required
      private reviews and the independent integration pipeline on the complete OAuth PR.

## Overall closeout, after both phases

- [ ] Reconcile R1-R11 and every advertised native mechanism against exact source/mode/platform
      evidence and independent cleanup. Keep inherited limitations and unrelated findings explicit.
- [ ] Finish permanent promotions, verify all checked work and requirements, and add `locked.md`
      only when the completed effort supports it. The lock binds at merge.
- [ ] Verify final PR/CI/review/label state and actual merge; leave no owned live resources or
      unreported cleanup obligations. MCP completion does not include single-session workspaces.
