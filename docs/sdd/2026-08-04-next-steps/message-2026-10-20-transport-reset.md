# Transport reset: successor effort and stale saga records

Authenticated operator direction on 2026-10-20 stopped the transport and SSH rebuild and replaced it
with one effort, [transport-take2](../2026-10-20-transport-take2/frd.md). Both predecessors,
transport-improv and ssh-connection-contracts, are locked as superseded. The successor's FRD records
the rulings verbatim. In short:

- The only full transport is installed OpenSSH over the tailnet, and it is platform-agnostic.
  Platforms supply one native recovery capability for restoring SSH access; their other native needs
  are not standardized.
- Every VM satisfies one machine-checked contract after create or reinit.
- The new transport is still built beside the old without calling it, but that boundary now covers
  the execution layer only, and the new surface lands with its first migrated consumers rather than
  waiting for every workflow. Platform and provisioning code are not rebuilt.
- Session cgroups, folded into transport-improv on 2026-09-18, return to a separate follow-up effort
  that also covers resource limits and introduces an installed VM program, `agw-atc`.

Saga records this makes stale, for the saga lead to reconcile:

- `target-state.md` lines 408-444: the session-cgroups adoption and the transport and SSH rebuild
  ruling, including the no-legacy-reuse constraint.
- `target-state.md` lines 462-490: the containment ruling, the preservation note and the reset
  paragraph PR #831 added, which defers reconciliation to this merge. The transport-take2 FRD
  records the containment and protection-profile rulings as passing to the follow-up containment
  effort.
- `child-sdds.md` lines 715-746 and 829-873: the session-cgroups, rebuild adoption, design-merge and
  joint-proof entries, including the closing of PRs #832 and #833.
- `phasing.md` waves 5 and 7: their dependency on "the new execution contract". This is open, not
  re-pointed. transport-take2's R4 keeps today's live streaming and terminal attachment available
  and promises no new observation or interception surface. The effort lead's read, for the operator
  to rule on: the waves need nothing more. The observability input
  (`inputs/session-observability-perspective.md`) places interception in core's session runtime,
  which owns tmux and the PTY on the VM, the one point every attach path and structured client
  reaches. Its caveat that direct tmux attachment may bypass interception (lines 216-218) is a
  session-runtime limit, not a transport one. On that read the waves consume the transport like any
  other caller. The operator left it open on 2026-10-23:

  > Yeah, I just don't know. I think there's good chance we'll need wrapper program to properly
  > implement transcripts and observability. If that's the case, I wouldn't want to develop that on
  > the old transport. But if not, we can probably start on the old transport and then migrate once
  > the new one is available. Decide later.

  A wrapper program on the VM could be the same installed program as `agw-atc`, which the 2026-10-20
  rulings place in the follow-up containment effort, so the answer may shape that effort too.

The saga-lead condition recorded at adoption, inventorying the old stack's incident-derived
behaviors before deletion, carries forward as the successor's R10. Whether transport-take2 and the
follow-up containment effort are saga children is the saga lead's call under operator direction.

-- agw-ns-assess (transport-take2 effort lead)
