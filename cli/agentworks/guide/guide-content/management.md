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

## Maintain SSH trust

The explicit SSH path uses an owned trust bundle selected by `[operator.ssh].trust_store`.
`agw config describe-ssh-trust DIRECTORY` shows its state, generation, policy authority, and source
attribution without loading operator configuration or a database. An available maintenance state
does not prove that a target will authenticate or that every policy file is intact; each new SSH
operation checks its selected policy. These commands do not change the trust used by older SSH
callers or manual SSH aliases.

When authorized to establish this policy, use `agw config import-ssh-trust --help` to create a new
bundle from explicit, complete known-host snapshots, with `--authority` identifying their
responsible maintainer and `--revoked-host-keys` supplying a complete revocation snapshot when
applicable. Use absolute native paths, with no symbolic links in the paths. Keep all source
snapshots stable during maintenance. Import preserves their bytes, including comments, hashed names,
certificate authorities, and revocations; it never discovers files or rewrites the sources or your
configuration. Configure `trust_store` explicitly after a successful import. If policy ownership or
completeness is uncertain, inspect the sources with their maintainer and leave the current
configuration unchanged.

When authorized to replace superseded policy, first inspect its current generation. Use
`agw config block-ssh-trust DIRECTORY --expected-generation GENERATION` to refuse new operations
through that bundle while retaining its evidence. Already admitted operations can continue. Then use
`agw config refresh-ssh-trust --help` to publish **all** replacement known-host snapshots and the
revocation snapshot with the observed generation and authority. Omitting `--revoked-host-keys` means
that the replacement has no revocation file. Refresh blocks admission before copying and only
reactivates the bundle after complete publication. It does not enroll an unknown host or clear a
mismatch. If replacement policy is unavailable, leave the bundle blocked.

After a refusal or interrupted maintenance, inspect the bundle before retrying. A stale generation
requires a fresh inspection, not an unconditional overwrite. The literal generation `none` is only
for recovery of an initial partial import that has never published a generation. Failed work retains
its files as evidence; do not delete the bundle to bypass a trust refusal. If storage cannot record
blocking durably, stop new use of the bundle and repair storage before continuing. Use each
command's `--help` for its current arguments.

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

For setup, return to `agw guide show concept-onboarding`. For failures, use
`agw guide show concept-troubleshooting`. Exceptional conversion from retired configuration belongs
to `agw guide show concept-migration`.
