# Session harness capability: locked

**Locked:** 2026-07-19

This SDD is complete and locked. It modeled a session's workload as a `harness` capability,
replacing the flat `command` / `restart_command` / `required_commands` template fields and the
imperative `_build_session_command` / `RequiredCommandsCheck` machinery. The permanent architectural
record is [ADR 0020](../../adrs/0020-session-harness.md); the capability lifecycle it builds on is
`cli/agentworks/capabilities/README.md`.

## What shipped

- **The `harness` capability kind** (`cli/agentworks/capabilities/harness/`), mirroring
  `git-credential`: a read-only registry row backed by a code registry, with two built-ins.
  - **`shell`** owns `command` / `restart_command` / `required_commands`; an undeclared harness
    resolves to a plain `shell` login shell, so the default session is unchanged.
  - **`claude-code`** launches or resumes a Claude Code session, deciding resume-vs-launch by an
    op-time file-presence probe (does the stored session id's transcript exist on the target),
    empirically confirmed to equal Claude's own resume boundary.
- **The template surface** is `harness` + `harness_config` (an inline reference-plus-blob). The
  legacy flat fields are accepted only as TOML backward-compat (hoisted to `harness_config` at load)
  and rejected in manifests. `harness` / `harness_config` inherit as a pair (`_merge_pair`), fixing
  a multi-parent divergence where a harness-silent parent wiped a sibling's config.
- **The session node holds the harness and composes it.** The node's `preflight` / `runup` fan into
  the harness's; the readiness fork (skip / defer / probe / loud-error on the operation scope's
  LEVEL) plus the required-commands probe moved onto the harness, with a new SESSION-level identity
  guard. `start` / `restart` are the ops (return the pane string; substitution and tmux stay core).
- **A per-session `harness_state` JSON blob** on the `sessions` row (migration 29), harness-owned
  and opaque to the core, round-tripped by the manager. `claude-code` stores its minted Claude
  session id there. This deliberately reversed the SDD's original "database unchanged" stance.
- **Docs:** top-level `README.md` (the model change), `cli/README.md` (schema + both harnesses),
  `docs/guides/resources.md`, `capabilities/README.md` (the rich-consuming-node example), the
  samples (`manifests/samples/session-template.yaml`, `sample-config.toml`), and ADR 0020.

## Delivery

One branch (`feat/session-harness-sdd`), one PR. Built via the `agentic-dev-process` (lead-authored
FRD/HLA/plan/LLDs; implementation delegated to `agentworks-dev` subagents; every step reviewed by
`agentworks-reviewer`). Executed P1 (package + shell) -> P3 (the session-node swap, retiring the
interim code) -> P4 (template surface) -> P2 (claude-code + the harness-state blob), because P2's
claude-code detection needed operator research (resolved: file-presence detection is sound) and
P2/P3 were independent. Full suite green (2113 tests) at close.

The two LLDs (`harness-api-lld.md`, `claude-code-lld.md`) recorded the pinned contract and the CLI
research (verified against `claude` v2.1.205); they are historical and may be deleted with this SDD.

## Superseded / forward-looking (NOT built here)

The FRD's "Target state: the harness as a tool adapter" is forward-looking: a session harness has a
provisioning twin, a `harness-user-provisioner` (and possible `workspace` / `vm` kin), which would
re-home `claude_marketplaces` / `claude_plugins` and the user-level MCP security default. None of
that is in this effort; it is recorded so the v1 boundaries read as deliberate.
