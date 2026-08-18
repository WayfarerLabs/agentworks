# Resource Show: Implementation Plan

- Status: Reopened for machine-output safety correction
- Date: 2026-08-18
- Requirements: `frd.md`
- Architecture: `hla.md`
- Code basis: `origin/main` at `217930fd`
- Delivery vehicle: one branch and draft PR, `feat/resource-show` / #597

## Delivery posture

The first implementation checkpoint proved the command, normalized declaration, machine contract,
completion, and terminal-safety foundations. Public review then exposed an incorrect product
premise: the artifacts treated overlap with list, graph, and doctor as a defect. The operator ruled
that a focused show should be a superset of the bulk views. The SDD was reopened, its premature lock
removed, and the same draft PR remains the delivery vehicle.

The predecessor SDD remains historical and unchanged. This successor records the later ruling. The
PR stays draft until the revised implementation, review, verification, and explicit merge intent are
complete.

## Superseded first checkpoint (historical record)

The following completed work remains truthful. The operator's focused-superset ruling supersedes
only its no-overlap boundary and requires the new phases below.

### Original Phase 1: contract and safe projection

- [x] Add the closed resource-show and readiness records in a focused resource module.
- [x] Implement shared identity resolution, category lookup, row description, stored enablement,
      stored readiness, and safe origin carry-through.
- [x] Implement the normalized declaration projector from `DeclaredResource`, `METADATA_FIELDS`,
      Pydantic JSON mode, and the canonical manifest API version.
- [x] Prove outer, metadata, and spec ordering; defaults; null omission; nested JSON conversion;
      framework-field exclusion; capability null declaration; and category/row invariants.
- [x] Prove representative secret declarations contain only lookup configuration and never resolved
      values.
- [x] Prove disabled rows project null readiness while enabled ready, not-ready, and unavailable
      verdicts retain their structural facts.

### Original Phase 2: projections and CLI wiring

- [x] Add the `resource.show` JSON v1 command identifier and exact data projector.
- [x] Add the terminal-safe human renderer, including deterministic normalized manifest YAML for
      declarable resources and a legible null declaration for capabilities.
- [x] Register `resource show REF --output human|json` in the existing resource group with lazy
      imports and no `get_db` path.
- [x] Add `resource.show/ref -> resource_refs` to the completion specification and verify all three
      shell generators.
- [x] Add service, renderer, CLI, machine-output, completion, help, malformed/unknown selector, and
      no-partial-output coverage, including silent loader flags, without prose-policing assertions.
- [x] Add structural line-injection and terminal-control tests for scalar fact lines and parseable
      sanitized YAML declaration output.

### Original Phase 3: collateral-complete cutover

- [x] Update the machine-output schema and resource command table in `cli/command-reference.md`.
- [x] Update `cli/README.md`, `docs/guides/resources.md`, and the 0.14 upgrade map to distinguish
      show, explain, graph, doctor, edit, and secret describe.
- [x] Sweep active code, help, completions, tests, and permanent documentation for stale ownership
      claims. Leave historical ADR and locked SDD text intact.
- [x] Confirm no guide command or generated topic is added merely to duplicate the CLI command.

### Original Phase 4: verification and delivery

- [x] Run focused resource-show, manifest, machine-output, completion, graph, explain, edit, and
      secret-describe tests.
- [x] Run `uv run pytest tests/ -m 'not integration'`, `uv run ruff check .`,
      `uv run ruff format --check .`, and `uv run mypy agentworks/ tests/` from `cli/`.
- [x] Run repository file lint, locked-SDD, Rulesync drift, and committed-diff guards.
- [x] Exercise real local human and JSON commands for declarable, capability, disabled/not-ready,
      malformed, and unknown selectors without live external backends.
- [x] Obtain an equal-or-higher-tier project review and a fresh-eyes review of the exact committed
      implementation; fix every clear material finding and rerun affected gates.
- [x] Mark the SDD complete with exact evidence, add `locked.md`, commit with the required session
      trailer, push, and update the draft PR. Move it to ready only under the process merge-intent
      rule.

## Phase 1: shared focused facts

- [x] Extract a one-row `ResourceSummary` builder and make `resource list` consume it without public
      output change.
- [x] Add direct declared dependency/dependent projection beside graph traversal, reusing canonical
      graph identity, edge construction, and ordering.
- [x] Add lazy read-only focused live usage with the same optional kind hook and absent-database
      semantics as graph/list inspection.
- [x] Prove list/show row parity, count/detail reconciliation, direct-only edges, ordering,
      duplicates, inherited declarer provenance, supported/unsupported live use, and database
      lifecycle.

Definition of done: one selected resource can be projected with every list fact plus direct
relationship and live-use detail, while list and graph retain their public behavior.

## Phase 2: reusable doctor checks

- [x] Extract structured per-row health-check builders from the VM-platform, VM-site,
      secret-backend, secret-source, secret, and applicable admin-template doctor paths.
- [x] Add `checks_for_resource` without running or filtering the complete doctor report.
- [x] Make bulk doctor groups consume the same builders and preserve existing group order,
      empty/degraded behavior, counts, status, message, and hint output.
- [x] Prove focused/bulk check parity and exclusion of global/cross-row checks.
- [x] Prove no prompt, secret resolution, authenticated runup, remote provider mutation, or
      unrelated system sweep occurs.

Definition of done: every health fact doctor attributes to the selected row is available to show
from the same structured producer.

## Phase 3: complete show composition and projections

- [x] Expand `ResourceShow` and JSON v1 with exact list-row fields, structural state axes, direct
      relationships, live usage, diagnostics, and normalized declaration.
- [x] Update the human renderer with safe condition, diagnostics, relationship, live-use, and
      declaration sections.
- [x] Change the CLI to ordinary human loader warnings, clean JSON warnings, the read-only live
      source, focused diagnostics, and completed-record rendering.
- [x] Preserve the existing parser/resolver, `resource.show` identifier, completion source,
      declarable/capability projection, disabled/readiness truth, and Unicode/terminal safety.
- [x] Extend structural service, renderer, machine-output, CLI, help, completion, typed-error, and
      no-prose-policing coverage.

Definition of done: human and JSON project the same complete focused superset and all compact facts
reconcile with their detailed sections.

## Phase 4: collateral and review

- [x] Update command reference, CLI overview, installed management guide, resource guide, and 0.14
      upgrade map from “ownership without overlap” to the focused-superset model.
- [x] Sweep active code, help, tests, and docs for stale no-database/no-graph/no-diagnostic claims.
- [x] Run the artifact checkpoint review against the revised FRD/HLA before implementation is
      declared final.
- [x] Run equal-or-higher-tier project review and independent fresh-eyes review on the exact revised
      implementation; fix every clear material finding and rerun affected gates.

Definition of done: active teaching matches the operator ruling and independent review finds no
remaining Critical or Important issue.

## Phase 5: verification and delivery

- [x] Run focused summary, graph, doctor, show, manifest, machine-output, completion, explain, edit,
      and secret-describe tests.
- [x] Run `uv run pytest tests/ -m 'not integration'`, `uv run ruff check .`,
      `uv run ruff format --check .`, and `uv run mypy agentworks/ tests/` from `cli/`.
- [x] Run repository file lint, locked-SDD, Rulesync drift, and committed-diff guards.
- [x] Exercise real local human and JSON output for declarable, capability, disabled/not-ready,
      relationship/live-use, diagnostics, malformed, and unknown cases without an external backend.
- [x] Record exact evidence, restore `locked.md` only after the revised artifacts and implementation
      are final, commit with the required session trailer, push, and update the draft PR.

Definition of done: the exact reviewed head is green, collateral-complete, and remains draft until
the operator supplies merge intent.

Final implementation checkpoint `bcd3781d` passed 7,216 non-integration tests, the focused test
sets, Ruff lint and format, strict mypy, repository file lint, locked-SDD validation, Rulesync
drift, and committed-diff guards. Equal-tier project and independent fresh-eyes reviews reported no
remaining Critical or Important issue. Real isolated-HOME CLI acceptance passed for list, graph,
doctor, relationship, live-use, diagnostic, declaration, failure, secret-safety, and terminal-safety
parity without touching provider or operator state. The published integration report found no
product issue or blocker, and every forge CI job passed.

## Phase 6: final review cleanup

The operator approved one bounded cleanup round after the final complexity and saga-lead reviews.
The focused-superset product contract and public JSON shape remain unchanged.

- [x] Make focused direct-edge projection independent of graph traversal policy and remove redundant
      runtime reconciliation plumbing while retaining structural count/detail parity coverage.
- [x] Reuse the graph readiness record and one exported framework-field derivation without changing
      the `resource.show` JSON contract.
- [x] Share the strict fact-line sanitizer between resource and graph human output, remove redundant
      post-YAML sanitization, and retain hostile-input and round-trip coverage.
- [x] Correct the service-boundary test name and acceptance wording, document the closed JSON
      carrier's external-data boundary, and date the operator's superseding ruling and dual-state
      decision.
- [x] Run equal-tier project review and independent fresh-eyes review; fix every clear material
      finding and rerun affected gates.
- [x] Run focused tests, the full non-integration suite, Ruff, format, strict mypy, repository
      guards, and focused isolated-HOME CLI parity/safety acceptance.
- [x] Restore the final lock record, push the complete round, publish the exact-head handoff, and
      request final re-review.

Definition of done: the cleanup changes no current machine schema or product scope, the exact final
head is green, independent review finds no remaining Critical or Important issue, and the PR is
handed off under the draft/ready signal chosen by the operator's merge intent.

Final Phase 6 implementation checkpoint `23773dfd` is based directly on `origin/main` at `0e529ce0`.
The exact head passed 7,220 non-integration tests, Ruff lint and format, strict mypy, repository
file lint, locked-SDD validation, Rulesync drift, and diff guards. Equal-tier project and
independent fresh-eyes reviews found no remaining Critical or Important issue after one
model-ownership wording correction. Fresh isolated-HOME acceptance passed the real CLI parity,
graph, declaration, JSON, secret-safety, Unicode-safety, and cleanup crux with no product finding.

## Phase 7: machine-output Unicode correction

After the operator supplied merge intent, exact installed-wheel testing found that a valid manifest
string containing a lone surrogate crashed `resource show --output json` during UTF-8 encoding and
that Unicode format and line controls remained active bytes in JSON output. The operator directed a
bounded shared-encoder correction and separately directed this branch to relinquish all installed
guide-content edits to the guide effort that will follow after `resource show` lands.

- [x] Rebase onto current `main`, preserve its rewritten guide content, and prove this branch has no
      diff under `cli/agentworks/guide/guide-content/`.
- [x] Extend the shared JSON encoder to escape every `Cc`, `Cf`, `Cs`, `Zl`, and `Zp` character with
      JSON's own ASCII escape spelling before UTF-8 encoding, retaining ordinary Unicode and parsed
      values without changing the v1 schema.
- [x] Add structural shared-encoder coverage and an end-to-end `resource show --output json`
      manifest regression for the reported surrogate, format-control, and separator path.
- [x] Update permanent machine-output collateral and this in-flight SDD without editing the separate
      installed guide-content surface or saga-owned artifacts.
- [ ] Run focused/full tests, Ruff, format, strict mypy, repository guards, equal-tier review, and
      isolated-HOME real-CLI acceptance against the exact rebased head.
- [ ] Restore the final lock, publish the complete handoff and review disposition, and set the PR
      ready again only after every corrective check is green.

Definition of done: every JSON v1 command has one atomic terminal-safe UTF-8 encoding boundary,
`resource show` succeeds on the tester's exact hostile-manifest class with semantically identical
parsed data, ordinary Unicode remains UTF-8, the installed guide-content diff is empty, and the
exact final head is reviewed and green.

## Coordination and escalation

- The active saga owns changes to its target-state artifacts. This PR carries `saga:next-steps` for
  visibility and does not edit saga-owned files.
- The separate guide effort owns the installed guide-content rewrite and its eventual
  `resource show` teaching update. This effort carries no change under the installed guide-content
  tree.
- Stop for operator direction if focused parity requires authenticated runup, secret values, remote
  provider calls, mutation, transitive graph semantics, source-exact manifest retention, capability
  facets, or a compatibility alias.

## Research disposition

External prior-art research remains unnecessary. The deciding product input is the operator's
focused-superset ruling; the implementation authorities are the repository's current list, graph,
doctor, manifest, machine-output, and CLI conventions.
