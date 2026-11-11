# MCP artifacts: implementation plan

## Ownership, vehicle and definition of done

The operator chartered this successor effort on 2026-11-11. The lead owns architecture, this plan,
integration and delivery; draft requirements are in [frd.md](frd.md). Scope excludes tool filters
and OAuth automation under the recorded operator ruling. The source baseline is `834eb715`.

Use one branch/PR for the complete implementation. First publish one coherent SDD-only draft with
FRD, HLA, this plan, research, LLD, migration and acceptance together; apply `review-requested` for
that checkpoint. Do not merge the checkpoint or claim implementation. Count the design feedback
round within the three-round allowance, leaving at most two subsequent feedback/fix rounds unless
the operator extends it. Remove the label before implementation. A complete green implementation
gets a scoped handoff and ready state without that label. No automatic merge.

Closeout PR 887 remains a separate ready predecessor. Its green head and clean report are retained;
MCP research/design can proceed while its normal merge is pending. Refresh main and assess actual
predecessor state before implementation handoff. Do not edit predecessor SDDs or the saga ledger.

Done means R1-R10 implemented and independently evidenced at the actual head, public collateral
current, required private reviews clear, all gates green, actual integration findings critically
disposed within budget, cleanup independently verified and the final SDD lock justified. Publishing
a design or passing the unit suite alone does not complete this plan.

## Design checkpoint

- [x] Draft the complete SDD set against current source and primary native/Rulesync references.
- [x] Resolve the canonical subset, source/inline shape, credential-reference grammar and
      scope/ownership boundaries together across FRD, HLA and LLD.
- [ ] Run independent project and Muntz reviews in separate pinned worktrees; incorporate material
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
      settings strategy and diagnosing conflicting explicit values before canonical mutation.
- [ ] Add Claude/Codex/Grok user and workspace translation and explicit VM deferral. Every adapter
      must apply or defer the new type; test against silent handled-but-omitted behavior.
- [ ] Translate the supported unresolved env/header/bearer references to actual native primitives;
      missing or unsupported bindings defer whole entries. Preserve native trust/approval/auth
      state.
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
- [ ] Prove failed restart leaves the working runtime/effects intact, successful replacement retires
      only obsolete run files, and delete/name reuse does not affect another session.

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

## Closeout

- [ ] Reconcile R1-R10 and every advertised native mechanism against exact source/mode/platform
      evidence and independent cleanup. Keep inherited limitations and unrelated findings explicit.
- [ ] Finish permanent promotions, verify all checked work and requirements, and add `locked.md`
      only when the completed effort supports it. The lock binds at merge.
- [ ] Verify final PR/CI/review/label state and actual merge; leave no owned live resources or
      unreported cleanup obligations. MCP completion does not include single-session workspaces.
