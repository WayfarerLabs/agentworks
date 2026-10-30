# Transport improvements: locked, superseded

**Locked:** 2026-10-20

This effort stopped at operator direction on 2026-10-20 and is superseded by
[transport-take2](../2026-10-20-transport-take2/frd.md). Its FRD is input to the successor, not a
requirement of it.

## What reached `main`

- The FRD, HLA, execution contract, lifecycle LLD, migration strategy, prior-art research, plan,
  proof design and the proof's recorded results (PRs #795, #826 and #830, merged 2026-09-16 to
  2026-09-19).
- The buffered carrier proof under `cli/agentworks/execution/` (PR #826): about 1.0k production and
  1.8k test lines, including an inline Bash bootstrap and a Proxmox guest-agent carrier. No
  production command reaches it. The successor adopts the package into its new transport or deletes
  it, and deletes the native carrier in either case.

No production behavior changed.

## What did not

The plan's 33 open items are abandoned.

The independent stack (PR #833, head `3dd03db42`) closed unmerged on 2026-10-22. At assessment on
2026-10-20 (head `f2dae36ae`) it had grown past 270k added lines across more than 800 files, with no
increment that could ship alone and nothing reachable from production commands beyond a few trust
commands, database migrations and a bootstrap step.

## Why it stopped

The design treated every platform's native channel as a full execution target, so every operation
had to fit the tightest provider limits, and it recorded guest and provider effects on the
workstation rather than where they happen. Add requirements written for absent callers, a rebuild
boundary that reached into provider and platform code, and a gate requiring every workflow to be
validated before anything shipped, and complexity grew far beyond what Agentworks needs.

The successor keeps one SSH transport over the tailnet, a uniform VM contract, a single native
recovery capability per platform, and incremental delivery. Session cgroups, folded into this effort
on 2026-09-18, move to a separate follow-up effort with resource limits.

`inputs/session-cgroups-frd-2c406948.md` in this directory is the only copy on `main` of the
session-cgroups requirements (PR #770), captured as review input rather than adopted requirements.
It is input to the follow-up containment effort. Do not wipe this directory until that effort has
carried what it accepts from it into its own SDD.
