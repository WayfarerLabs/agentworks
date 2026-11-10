---
description: Inspect, configure, and operate an existing Agentworks installation.
index-order: 30
---

# Managing Agentworks

Agentworks separates declared resources from the live VMs, workspaces, agents, sessions, and
consoles created from them. Use resource commands for configuration and the owning operational group
for live instances.

## Inspect what is available

`agw resource kinds` lists the installed vocabulary. Use
`agw resource list --kind KIND --include-disabled` to inspect one kind, including its origin,
enablement, and readiness. Use `agw resource show KIND/NAME` for the complete focused view of one
loaded resource, including its direct relationships, current live uses, attributable diagnostics,
and normalized declaration when it has one. Selectable instance templates also include the fully
resolved spec and where each value came from. Use the live kind's `describe` command for its current
declaration, stored instance layer, lifecycle evidence, and drift. Use `agw graph show KIND/NAME`
for broader relationship traversal and `agw doctor` for installation-wide health.

Use `--output json` when the result will be consumed programmatically.

For a preview of effective managed env, use `agw env show --session NAME` or select a VM, workspace,
or agent with the corresponding context option. This reads managed declarations without connecting
to the VM and keeps secrets redacted. Plaintext values can use context references such as
`${agw:user_home_dir}`; `agw env show --help` describes the inspection options.

## Change declared resources

`agw resource explain KIND` describes a manifest shape, while `agw resource explain KIND/NAME`
describes one capability implementation. Start new declarations with `agw resource sample KIND`;
edit existing operator-owned declarations with `agw resource edit KIND/NAME`.

Workstation settings and enabled system plugins remain in the operator configuration and can be
changed with `agw config edit`.

## Operate live instances

The `vm`, `workspace`, `agent`, `session`, and `console` groups own their live state. Begin with
`agw GROUP list`, inspect one item with `agw GROUP describe NAME`, and use `agw GROUP --help` for
the current operations. Read the result after a change rather than assuming it succeeded.

Sessions and consoles have explicit runtime lifecycle. `start` realizes a stopped runtime and is a
no-op when it is already running; `restart` replaces it; `stop` removes it; and `attach` only
connects to a running runtime. Session start and restart resume the harness conversation when
possible. For sessions, add `--resume-only` to refuse unless a resume is possible, or `--force-new`
when a new conversation is required. These options are mutually exclusive. An integration that does
not implement the requested policy reports that explicitly before an existing runtime is replaced.
Restart asks before replacing a live running session; `--yes` skips that confirmation. A batch
restart asks once for all running matches. `--force` remains separate and only permits recovery from
broken runtime state.

## Record intent and recover

Runtime exit and external shutdown leave desired state unchanged. Plain lists expose recorded intent
without guest work; `list --status` and describe add observations. Before adopting a legacy session
or console, establish what the operator wants to keep running. Use the owning group's
`set-desired-state NAME STATE` help to record that decision without changing runtimes. Targets are
per-instance state, separate from reusable declarations.

When the operator has authorized recovery of a named VM and its workloads, preview with
`agw vm reconcile NAME --dry-run`, review blockers, then use `agw vm reconcile NAME` to apply its
recorded targets. Execution can activate the VM, start or stop workloads, rebuild views, or stop the
VM according to those targets. `--all` selects the fleet only when that broader scope is intended.
Focused `session reconcile NAME` or `console reconcile NAME` applies only that workload's target;
console recovery does not start member sessions. VM start alone does not restore children.

If intent or recovery scope is unknown, inspect and report the adoption requirement or blocker
instead of selecting targets or changing runtimes. A stopped parent blocks running children while
retaining their targets. Read results before retrying: accepted intent survives later failure or
cancellation, and infrastructure activation/repair may remain after workload-secret failure.

Batch start/restart collects eligible workload secrets before any deferred runtime changes. A
required workload secret failure blocks that complete batch, including deferred VM stops during
reconciliation. An infrastructure credential failure instead blocks the families that require it; if
an eligible workload also needs that credential, the shared workload-secret boundary still fails.
No-op targets contribute no unused workload secrets. Busy families may be waiting on secret
interaction or console deletion consent; finish the existing operation before retrying.

For setup, return to `agw guide show concept-onboarding`. For failures, use
`agw guide show concept-troubleshooting`. Exceptional conversion from retired configuration belongs
to `agw guide show concept-migration`.
