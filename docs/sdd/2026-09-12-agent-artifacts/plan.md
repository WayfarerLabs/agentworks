# Agent artifacts: initial plan

## Current stage and ownership

The operator authorized starting this successor on 2026-09-12 after PR 761 merged. The requirements
seed carries settled operator direction and clearly marked proposals; acceptance of new requirements
and the HLA remains with the operator. The effort lead owns architecture, sequencing and this plan.
Implementation has not started.

Keep predecessor closeout separate. Its implementation is merged, but seven acceptance/cleanup
checkboxes remain open in the
[harness-scope-framework plan](../2026-09-06-harness-scope-framework/plan.md#acceptance-and-closeout).
Complete and record that acceptance before locking it, unless the operator explicitly changes its
remaining obligations. A historical lock does not prevent this effort from changing framework code.
Do not change the saga's existing ledger or contracts; route coordination to their owner.

## Delivery

Publish the FRD seed and this initial plan in a dedicated PR based on merged main. It is intended to
merge as a seed and therefore receives a ready handoff after checks and private review. Carry
`sdd:agent-artifacts` and `saga:next-steps`. Publishing a draft requirement does not itself accept
it. The subsequent HLA is a separate draft checkpoint using `review-requested`; promote it only
after authenticated approval. Decide implementation PR boundaries from the reviewed architecture,
not a speculative list of subsystems now.

## Requirements and research

- [x] Carry forward operator decisions on hints, package normalization, origin, facet ownership,
      lazy passthrough, VM-to-session fallback, shell placement and idempotent cleanup.
- [x] Identify predecessor acceptance separately from future framework changes and record the
      successor's proposed first delivery and open decisions.
- [ ] Obtain operator acceptance of the FRD scope and resolve first-delivery source choices.
- [ ] Write `prior-art-research.md`: evaluate Rulesync's model and generators, Agent Skills package
      conventions, source acquisition options, and the actual native artifact mechanisms of each
      shipped harness. Cite primary sources and mark unsupported claims explicitly.
- [ ] Resolve the final-session disposition with the operator and present the native support matrix.

## Design checkpoint

- [ ] Write an HLA covering bundle resources, acquisition and capture, normalized representation,
      ownership and lazy routing, integration APIs, native placement, final disposition and cleanup.
- [ ] Include worked manifests and the VM/user/workspace/session flow, with inactive facets,
      multiple consumers, duplicate-route prevention and session name reuse.
- [ ] Record migration from the merged facet API and existing native setup without reintroducing
      speculative reconciliation machinery or source-specific propagation formats.
- [ ] Run independent project and complexity reviews, complete the applicable checks, and publish
      the HLA checkpoint for operator and saga review.
- [ ] Obtain HLA approval, then replace the initial implementation outline with concrete work units,
      necessary LLDs, acceptance scenarios and delivery boundaries.

## Implementation outline, pending design

- [ ] Deliver acquisition and normalized bundles through ordinary resource references.
- [ ] Deliver core artifact inputs and owner-independent routing through activated integrations.
- [ ] Deliver the approved native support matrix, shell filesystem contract and lifecycle behavior.
- [ ] Promote operator guidance, examples, diagnostics and source/update semantics with runtime.
- [ ] Validate the approved source, routing, isolation, update, removal and failure matrix using
      local fixtures and authorized live resources; independently verify cleanup.
- [ ] Reconcile all requirements against observed evidence and lock this SDD at actual completion.

## Seed review evidence

Independent project and complexity reviews of `7c744828..5fff8a0c` found no material issues. The
project review's minor signature request was incorporated into the saga coordination message. Both
reviews confirmed that new scope remains proposed and the predecessor's seven acceptance/cleanup
obligations remain open. No code or live resources were changed. The seed handoff records repository
checks and final-head CI separately; this is not artifact implementation or acceptance evidence.
