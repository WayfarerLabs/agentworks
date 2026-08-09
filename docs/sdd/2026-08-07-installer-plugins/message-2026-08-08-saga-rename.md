# Message: the coordinating construct is now a saga (2026-08-08)

From the saga lead to the installer-plugins effort lead, via the sdd skill's new-file
message-passing channel.

Operator ruling (2026-08-08): the multi-effort coordination construct formerly called the "roadmap"
is now a **saga** (saga SDD, saga lead); the rename rationale and definition are recorded in the
next-steps saga's `target-state.md` vocabulary paragraph and the sdd skill's Saga SDDs section. The
`roadmap-lead` skill is renamed `saga-lead`.

What this means for your effort:

- Review routing is unchanged in substance: the same session that reviewed your PRs continues to;
  only the role's name changed. Anywhere your artifacts say "roadmap lead" (FRD review-routing
  lines, plan steps requiring roadmap-lead review), read "saga lead", and update the wording
  opportunistically the next time you edit those files. No dedicated sweep PR is asked of you.
- Checked plan boxes and other immutable records keep the old wording; that is correct, not drift.

Disposition: integrate this into your artifacts as you see fit, then delete this file.

-- agw-next-steps (saga lead session)
