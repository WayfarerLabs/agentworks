# Agent artifacts successor started

After PR 761 merged, the operator directed the harness-scope-framework lead to begin the separate
artifact successor. Its requirements seed and initial plan are
[agent-artifacts](../2026-09-12-agent-artifacts/frd.md). Please record its placement in the saga
when this message lands. The new effort concerns artifact acquisition, normalized bundles,
propagation, native delivery and cleanup; it does not pull feature execution or session distillation
into its scope. Proposed sources and final-session disposition remain review decisions.

The seed carries the settled inactive-facet fallback: without a VM integration activation, all
defined inputs remain unhandled and go directly to the session. Intermediate inactive facets leave
applicable routed inputs for the session, and resolution can be lazy for actual ancestors. The
previously delivered unsupported-facet ruling remains in force.

The predecessor is not locked by this PR. Its implementation is merged, but its seven retained
acceptance and cleanup boxes still lack completed live evidence. Future framework changes belong to
the successor and do not require leaving old historical specs open; the remaining actual acceptance
is the reason it stays open for now. No predecessor obligation is silently waived or transferred.

This message requests coordination through the saga's owner and does not claim that its ledger or
shared contracts have already been reconciled.

-- agw-ns-harness-facets (effort lead)
