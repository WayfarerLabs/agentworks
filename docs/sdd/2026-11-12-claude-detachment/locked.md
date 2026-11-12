# Claude detachment v1 closeout

Completed 2026-11-12 on the local fix branch, based on main at `2253a6819`.

Completed implementation: independent continuation and parked-job detection, evidence-bearing
refusal, history-loss warnings, exact `force_uuid` recovery, and a reusable `InstanceOnly()` config
marker enforced with declaration provenance. No automatic conversation selection or deletion.

The [validation record](validation.md) records the full CLI suite, targeted regressions, static
checks, isolated CLI verification, and independent review dispositions. Production handoffs were not
replayed. All implementation work is complete; publishing or merging is outside this local handoff.
