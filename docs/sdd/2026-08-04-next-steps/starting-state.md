# Starting State

- Snapshot date: 2026-08-04 (roadmap start)
- Baseline: Agentworks 0.13.0 (`v0.13.0`)
- Immutability: this document froze when the roadmap got underway. It is the origin of the journey;
  do not update it. `current-state.md` is the moving snapshot, and the distance between this
  document and `target-state.md` is what the roadmap set out to cover.

This records where the system stood when the roadmap began, verified by code reconnaissance at the
0.13.0 baseline rather than assumed from the perspectives.

## Declarative schema

Phase 1 (TOML sunset) was complete but unmerged on `feat/declarative-schema-sdd`: the TOML resource
path hard-errored, the legacy loaders were relocated into the migrator as its independent
verification oracle, the fixture suite was converted and green, and the final review had approved
it. On `main`, the TOML resource path still only warned. Phase 2 was fully specified but held at its
phase gate pending this roadmap's descriptor and removal-ordering decisions.

## Deprecation removal targets

Every surface in the removal perspective existed at the baseline as described:

- `agw vm console` was not a thin alias but a separate legacy implementation
  (`cli/agentworks/sessions/console.py`, ~218 lines) unrelated to the canonical `agw console`
  family's `multi_console` code, with one canonical caller feeding it: session create's roll-forward
  best-effort hook adding new sessions to a live legacy console.
- Two aliases were silently accepted with no warning at all: `[paths].code_workspaces` and
  `agw vm shell --provisioner`.
- The `[user]` section warning bypassed the aggregated deprecation channel (raw stderr print).
- `output.phase()` and `env_compat.py` were fully dead (zero production callers); `UserConfig` was a
  one-line alias.

## Capability framework

- Exactly four capability kinds existed, with the kind set independently enumerated in at least six
  places (adapter table, graph kind set and readiness dispatch, registry loaders, bootstrap
  publication, plugin snapshot/restore, manifest kind sections), plus a five-method adapter class, a
  publisher, an entry dataclass, and a `ResourceKind` strategy repeated per kind. A guard test made
  a fifth kind fail loudly, but every site needed hand-extension.
- The secret-backend registry stored constructed singletons, with adapter and graph special cases
  for that representation, and the backend module's docstring already named the graduation signal
  toward a declarable instance kind ("the secret-backend analog of vm-site").
- There was no Pydantic anywhere in `cli/`, and the tagged-union `{name: ...}` config shape worked
  for three surfaces (vm-site, git-credential, the session harness selector), implemented three
  times independently.
- `_VMPlatformKind` lived in `vms/kinds.py`, outside `capabilities/`, unlike its three siblings.

## Session runtime

- Sessions had no run/incarnation identity: `sessions.name` was the sole key and reusable after
  delete-and-recreate; `boot_id` existed only to detect VM reboots.
- There was no PTY observation, no input interception, no event or fanout infrastructure, and no
  supervisor or heartbeat. tmux owned the PTY (one server per session on a private socket);
  Agentworks only pulled scrollback via `capture-pane`.
- The one push-style precedent was the Codex `notify` recorder (`plugins/codex/recorder.py`), which
  extracted a single thread id per turn and discarded the rest of the payload.
- The Claude integration only probed for its transcript file's existence to decide
  resume-versus-launch; nothing read transcript content.

## Open SDD ledger at start

- Release-spanning with removal phases pending: `2026-08-03-harness-integration` and
  `2026-08-04-session-resume`.
- Operationally complete but unlocked: `2026-03-29-proxmox-provider` (20/20 checked) and
  `2026-05-03-session-enhancements` (69/69 checked).
- `2026-03-26-mise-integration`: 0/30 checked despite a demonstrably landed implementation.
- Unmerged drafts on remote branches: `2026-07-29-harness-transcripts` (superseded in substance by
  the observability perspective), `2026-07-29-herdr-integration` (not scheduled, gated on a spike),
  and `2026-07-19-named-console-template-selector` (drafted, ready).
