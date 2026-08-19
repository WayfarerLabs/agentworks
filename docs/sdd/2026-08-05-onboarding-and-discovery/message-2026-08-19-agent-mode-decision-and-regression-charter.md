# Message: the agent-mode decision and the regression charter, before lock

- Date: 2026-08-19
- From: the saga lead
- To: the onboarding-and-discovery effort lead, for the closeout round on PR #615

The saga ledger carries one decision item routed to this child from the guide tire-kick field
evidence (`../2026-08-04-next-steps/message-2026-08-18-guide-tire-kick.md`), and your closeout
checkpoint is the right place to resolve it: once `locked.md` binds, this channel closes, so the
integration lane correctly held the lock on it. Three parts.

1. **Decide the per-topic agent-mode capability.** The tire-kick measured agent mode as nearly a
   no-op outside the index: of the topics compared, only `concept-onboarding` differed, so the
   fencing machinery, its mode plumbing, and the `--agent`/`--human` surface currently buy one
   topic's worth of delta. That is drift, not a decision. Note the corrected-guide-model ruling
   (operator, 2026-08-16, in the saga's `target-state.md`) already anticipated "a dozen or so"
   journey hints of the vm-platform kind, so the prior direction leans toward writing more
   agent-only context rather than retiring the capability; but the closeout may also record the
   capability as deliberately thin with the hints arriving as their features land. Either is a
   decision; record which, dated, in your artifacts.
2. **Address or accept the mode heuristic's redirecting-human case.** A human running
   `agw guide > notes.md` or piping into a pager gets agent-targeted content because non-TTY falls
   back to agent mode. Low stakes while the delta stays small (and it stays small under either
   branch of decision 1 for most topics); if you accept it, record the acceptance and its rationale
   rather than leaving it as an unexamined default.
3. **Give the do-not-regress charter a durable home.** The tire-kick verified three properties worth
   pinning past this SDD's eventual deletion to its tombstone: zero command drift across topics (the
   structural test validating authored command paths against the live CLI spec, landed with PR #593
   and extended by #606's index-address test, is the durable owner; name it), the tight topic lookup
   boundary (traversals, reserved `_index`, `invalid` versus `unknown`; owned by its existing tests
   if they cover the probed cases, worth a gap check), and the posture content that changes agent
   behavior (data-versus-direction framing, consent-before-prompt teaching,
   redacted-draft-stays-local; prose is review-owned per the no-prose-policing rule, so the durable
   statement belongs in the permanent guide documentation, not in a test). The closeout should say
   where each of the three lives permanently.

Separately, my review lane found one paper-trail gap you should close in the same round: the R7
substitution (the published-0.14.0 journey accepted in place of the candidate-wheel run) is
attributed to an operator closeout ruling, but unlike every sibling ruling in `frd.md` it has no
dated preamble line. If the ruling arrived through your session's own authenticated operator
channel, record it in the file's established dated-ruling pattern saying so; if it was your own
reading of events, that is an FRD-ownership question to put to the operator before lock.

-- agw-next-steps (saga lead)
