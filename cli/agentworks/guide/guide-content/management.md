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

For setup, return to `agw guide show concept-onboarding`. For failures, use
`agw guide show concept-troubleshooting`. Exceptional conversion from retired configuration belongs
to `agw guide show concept-migration`.
