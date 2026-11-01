# Message: facet independence, and finishing this effort

From the next-steps saga lead (`agw-next-steps`), 2026-11-01. This is colleague input describing an
operator ruling, not a work order. Only the operator's authenticated direction authorizes the work
below. The ruling changes requirements in an FRD the operator owns, so its amendment comes through
that direction.

## The ruling

The operator ruled on 2026-10-31 that facets are independent and converge. The ruling is recorded
verbatim in the saga's `target-state.md` under "Facets are independent and converge":

- **No facet blocks another.** A stale or incomplete upstream facet is reported at session start,
  naming its owner and repair command, and the session launches with what is placed.
- **Applying a facet converges what it owns** to its declaration rather than refusing on current
  state.
- **Refusal is reserved** for the facet's own failures and for overwriting content Agentworks does
  not own that differs from the declaration, scoped to that entry.
- **"Never silently lost" means reported, not blocked.** This settles the 2026-09-07 question this
  effort inherited: an entry still deferred at the session facet is reported, never a launch block.

## What it changes here

**Session start refuses on upstream state**
([issue 841](https://github.com/WayfarerLabs/agentworks/issues/841) records the impact):

- `prepare_session_artifacts` checks every active VM, user and workspace facet before runtime
  teardown. It raises "ancestor artifact placement is not current" for any facet that is not current
  (`cli/agentworks/artifacts/session.py:58-71`).
- "Current" requires a complete record, an unchanged declaration digest, a matching destination
  identity (`cli/agentworks/harness_setup/readiness.py:56-70`) and a current bundle capture
  (`cli/agentworks/artifacts/routing.py:68-72,101`).
- So a bundle added to a widely inherited template made every descendant session refuse to restart,
  and the hint named a reinit that was itself failing.

Under the ruling, each of those becomes a report at session start, naming the owner and the repair
command. The session launches with what is placed. The session's own facet failures still refuse.

<!-- cspell:ignore MSYS -->

**Issue 841's probe failure is a separate cause.** Its discussion identifies a root-cause candidate
for the `zsh:106: unmatched "` probe error: Git for Windows' MSYS `ssh.exe` truncating the remote
command. transport-take2 refuses MSYS clients, so that cause belongs to transport, not to artifact
code. The session-start behavior above is the part that belongs here.

## What this effort still owes

- Native option validation and the artifact-specific launch-override refusal, checked against real
  executables.
- The session lifecycle matrix: create, restart and resume, failure before teardown, deletion,
  source and reference removal, name reuse, and two users sharing one workspace.
- Scoped live acceptance, then the R1-R10 audit and the lock.
- Three "publish" boxes for follow-up PRs (issue 812, round 1 and PR 825) that appear finished.
  Check them off with their merge evidence, or say why they remain open.

## Coordination

- **Transport.** transport-take2 (PR 856) migrates session artifact delivery in a later slice. Write
  against today's transport; that slice moves you.
- **Harness scope framework.** The native-setup half of the ruling, and the note on the readiness
  gate, are in `2026-09-06-harness-scope-framework`'s message.

-- agw-next-steps (agentworks saga lead)
