# Message: re-scope the sweep around the two live efforts

- Date: 2026-08-19
- From: the saga lead, relaying operator direction
- To: the simplification-pass effort lead, for the sweep's restart and the R4 reassessment

The operator is firing the sweep back up. Two efforts have started since the sweep map settled, and
their surfaces would collide with sweep groups in the noisiest possible files (tests an active
effort is about to rewrite or delete wholesale), so the operator directs a re-scope before the sweep
resumes. Three parts.

1. **Subtract the owned surfaces from the sweep map.** The secrets-preview effort
   (`2026-08-18-secret-preview-contract`, PR #619) rewrites the secret-backend contract, all three
   in-tree backends, and their tests atomically; the instance-model effort
   (`2026-08-19-instance-model`, seed PR #621) will rework the database/persistence layer, the
   resource show and inspect surfaces, and doctor's drift reporting. Sweep groups touching those
   areas (the secret-backend and secrets estates; db/persistence; resources show/inspect; doctor's
   resource-attributable checks) leave the sweep's scope. Record the subtraction against the settled
   map explicitly, group by group, so the reassessment can audit what moved rather than inferring
   it.
2. **The trim moves into the owning efforts, not behind them.** Each subtracted area's
   trim-to-standard becomes part of the owning effort's definition of done: a rewrite that preserves
   worthless tests is a defect under the existing principles, and the riders make that explicit and
   auditable. I am carrying that rider to both efforts (the instance-model design phase for its R2
   surface; the secrets-preview plan in its convergence round); your artifacts just need to name the
   handoff so the reassessment sees one owner per estate.
3. **Everything else proceeds on its own merits and schedule.** The disjoint groups (sessions,
   workspaces, consoles, transports, CLI plumbing, guide, completions, and the rest of the map) are
   unaffected; the sweep stays deletion-heavy and cheap to rebase. The standing prerequisite is
   unchanged: the corrective inventory PR from the post-#573 audit (the seven charter items plus the
   retroactive callee-side raise screen over the same-shape deletions) still gates group 1 and
   should land first.

The R4 reassessment inherits this message: its scope question ("what does the sweep still own") now
has a dated answer to reconcile against, alongside the lesson pile it already carries.

-- agw-next-steps (saga lead)
