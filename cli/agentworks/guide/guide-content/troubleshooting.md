---
description: Narrow an Agentworks failure to configuration, readiness, state, or connectivity.
index-order: 40
---

# Troubleshooting

Start with the exact error from the command that failed. Run `agw doctor` once for a broad check of
the workstation, configuration, dependencies, and database; then move to the smallest relevant
surface instead of changing several things at once.

For a disabled or not-ready resource, use `agw resource show KIND/NAME` to inspect its focused
condition, direct relationships, current live uses, and attributable diagnostics together. Use
`agw resource explain KIND` or `agw resource explain KIND/NAME` when the question is what a kind or
capability accepts.

## VM and SSH connectivity

`agw vm describe NAME` shows the recorded power state, Tailscale address, recent events, current
resolved declaration, lifecycle evidence, and structural drift. Its hardware evidence is the
recorded provisioning request, not provider-observed realized hardware. `agw doctor` reports SSH
identity drift, orphaned instance records, malformed known records, and well-formed records from a
newer release. `agw vm verify-connection NAME` tests the canonical admin connection without starting
the VM. If that succeeds, `agw vm shell NAME` opens the Agentworks-managed SSH path.

For raw SSH, the default alias is `ssh awvm--NAME`. Run `agw config sync-ssh-config` if the
generated entry is stale; installations may configure a different alias prefix.

When the failure appears to be below SSH, use `tailscale status` to confirm the workstation is
connected and can see the VM, then `tailscale ping HOST` with the Tailscale address from
`agw vm describe NAME`. `agw vm logs NAME` shows the VM's recent SSH logs.

If Tailscale itself needs repair, `agw vm shell NAME --platform` provides a platform-native recovery
path where the selected platform supports one. It is an explicit recovery tool, not the routine
connection path.

## Runtime recovery and busy operations

A runtime exiting does not retire its desired-running target. Inspect the target separately from
status, and do not infer intent from an outage. Legacy sessions or consoles awaiting adoption need
the operator's intended target recorded through their `set-desired-state` command. A stopped VM
blocks running children while preserving their targets; `vm start` restores the parent alone.

For recovery already authorized by the operator, preview the narrow scope with
`agw vm reconcile NAME --dry-run` or the focused session/console equivalent. Apply that existing
target only when those runtime changes are intended. A focused console retry restores its view
without starting member sessions. Otherwise report the proposed work or blocker and leave intent and
runtimes unchanged. Unknown observations and unsafe recovery remain blockers, not permission to
replace a runtime.

A required workload secret failure prevents all deferred changes in the selected batch. Earlier
infrastructure activation or Tailscale repair may remain. An infrastructure credential failure
blocks only dependent families unless an eligible workload also requires it. Correct the input,
inspect completed effects, and retry the intended scope; cancellation does not erase accepted
targets or roll back provider work.

A busy VM family can be held by reconciliation secret interaction or console deletion consent.
Finish that operation and retry instead of bypassing the guard. Even diagnostic access to a live VM
can report busy during hold admission. On WSL2, a live VM with stopped intent has no idle protection
during access or management work and can disappear mid-command. If the operator intends normal holds
and future activation, explicitly select `agw vm set-desired-state NAME on-demand` or `running`;
otherwise retain stopped intent and report that limitation.

## Git authentication

First inspect the selected `git-credential` and its provider with `agw resource show` and
`agw resource explain`. A secret source reports failures during resolution or optional runup. A
`gh-cli` or `az-cli` source receives a read-only readiness check in that user's login/interactive
shell during enabled runup, but a warning does not block helper installation. The check can run
before later install/profile steps, so verify again after initialization when those steps may add
the CLI. Confirm that the matching CLI is on that user's `PATH` and authenticated as the intended
identity. Azure DevOps also requires that identity to have access to the configured organization and
repository. Final credential acquisition still happens only when the target user's Git operation
invokes the helper.

Fixing CLI authentication takes effect on the next Git operation because the helper reacquires each
time. After changing a manifest or a template's `git_credentials`, reinitialize the VM admin or
agent so Agentworks can rebuild the complete managed state. Removing every declared credential and
reinitializing removes Agentworks-owned credential state.

After one change, rerun the narrow check that exposed the problem. Use
`agw guide show concept-reporting-bugs` when the failure needs to be reported.
