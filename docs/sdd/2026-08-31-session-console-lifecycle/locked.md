# Session and console lifecycle: locked

**Locked:** 2026-09-01

This effort is complete in PR #710. The lock takes effect when that PR lands on `main`; until then,
this file records the final reviewed and operator-accepted implementation state.

## What shipped

- Sessions use explicit `start`, `stop`, `restart`, and `attach` operations. Ordinary start is
  idempotent for an already-running session, restart deliberately replaces it, and `--force-new`
  asks a stopped start or a restart to reject prior harness continuation state.
- The harness-integration contract remains version 1 and exposes one start operation returning the
  launch command and an optional pre-launch note. Each integration owns namespaced durable
  continuation state, which the session manager persists separately. Core owns stop/restart
  orchestration; no unused integration stop hook or generic runnable abstraction shipped.
- Session teardown is one fail-closed authority. Dedicated tmux servers are identified by socket,
  PID, boot ID, and positive process start ticks and are stopped with exact `kill-server`; legacy
  shared-server rows use exact `kill-session`. Direct operations, batch operations, parent deletion,
  and VM cascades all route through that authority.
- Tmux, process, and persisted-identity probes preserve `UNKNOWN` rather than treating transport,
  permission, protocol, malformed-state, or transition failures as absence. `RESIDUAL` reports a
  reachable managed server without the expected session. Recovery mutates durable state only after
  positive identity or absence proof.
- Consoles have explicit create, start, stop, restart, and attach-only operations. Create validates
  and stages before publishing, retains its durable definition after a post-commit build failure,
  and reports whether runtime absence was verified or remained indeterminate. Exact canonical and
  staging tmux names share the same fail-closed probe and teardown rules. `--all-running` applies
  safe runtime-identity repair before batched status selection and refuses unresolved non-stopped
  rows rather than silently creating a partial console.
- The hidden 0.19 `session resume` and `console attach --recreate` compatibility spellings remain
  CLI-only and are scheduled for removal in 0.20 by issue #720. Resume reports its exact canonical
  mapping, preserves the former confirmation before replacing known or conservatively possible
  running state, honors `--yes`, and refuses non-interactive replacement without that explicit
  bypass. Canonical restart stays prompt-free.
- Database schema version 36 stores the tmux server start ticks. Atomic runtime updates replace the
  former partial PID setters, and migration repair backfills only from authoritative live facts or
  records stopped state only after exact absence proof.

## Final design boundaries

The design deliberately keeps VMs distinct rather than introducing a shared runnable framework.
Sessions and consoles share vocabulary and teardown principles, not a synthetic lifecycle type.
Harness integrations decide whether and how to continue their own conversation state when core asks
them to start; core owns resource state, tmux ownership, secret resolution, process realization, and
teardown.

The proposed integration-level cooperative stop hook was removed under the operator's later YAGNI
ruling. Dedicated tmux servers are not shared, and exact tmux server/session teardown is the current
domain authority. A future integration with a concrete checkpoint-or-shutdown requirement may add a
bounded hook with its first consumer and tests. Stronger systemd-backed session containment is a
separate security effort tracked by issue #715.

## Verification and review

The final production checkpoint is `8efcc98f6fbee2b796736c5c265eeb92aa1ac89d`, based on
`origin/main` at `8695afcd833790ee433b50bb9f5d5c696177233d`. The complete pipeline and live
validation ran at `1081470fa0ab315c3e0221da8142a87ad9b292a1`. Subsequent bounded corrections
completed one runtime fixture fingerprint, added established entity metadata to three console
errors, corrected two repeated-start test names, and made the atomic session-runtime update
participate in explicit database transactions. The final correction also made migration repair stop
on indeterminate fingerprints, normalized only the canonical local Windows forced-TTY close
advisory, replaced elevated PID exit-code inference with explicit present/absent facts, and reduced
ordinary session-list repair output to one aggregate status-check indication. Verification recorded:

- 8,232 non-integration tests with one platform-specific skip at the final production checkpoint;
- focused session, console, harness, compatibility, completion, cascade, migration, and adversarial
  suites;
- Ruff check and format plus strict mypy across 745 source files;
- file lint, locked-SDD, Rulesync drift, Typer-isolation, package/install, and diff gates;
- 160 Python and 103 Node website tests plus deterministic root and project builds; and
- hosted CI on Python 3.12, 3.13, and 3.14 plus CodeQL and every non-website repository gate. That
  full-validation checkpoint's Website job ran 160 Python tests; one browser test failed during
  setup, before its assertions, when Chromium did not publish a DevTools endpoint. The identical
  suite passed locally, and the final docs-only lock head must clear the complete aggregate gate
  before merge.

The private project-values, Muntz, and cold correctness/security reviews converged cleanly at the
exact final production checkpoint. Final hosted CI and targeted operator Windows validation of the
corrected artifact remain required before merge. Their correction rounds closed fail-open transport
and tmux probes, legacy and parent-cascade teardown gaps, malformed identity handling, ambiguous
console state, post-launch cleanup, duplicated status classification, stale collateral,
compatibility safety, process-global test isolation, invalid runtime-identity fixtures, and
test-quality findings. They also replayed privileged-shell refusal, malformed and spoofed PID facts,
and forced-TTY stream handling against the final production checkpoint. The final compatibility
correction uses only read-only status facts before consent and conservatively gates incomplete
dedicated rows whose status is missing or unknown. The final console correction reuses the
established safe repair authority before live selection, preserves the typed legacy migration
refusal, and fails closed when incomplete identity remains unresolved.

The integration tester first drove the shipped lifecycle on a real VM at `f4d3a920`: session stop,
start, idempotent start, and restart; console create, stop, start, and restart; the hidden resume
wrapper; parent-delete gating; and cleanup. The follow-up at `715d6d44` reran all gates and verified
the three compatibility mappings plus effective `--yes` handling. A third pass at `1081470f` reran
the full pipeline and drove `console create --all-running` with two running sessions and with mixed
running/stopped state, confirmed the distinct `--all` behavior, rechecked compatibility handling,
and deleted the VM without residue. All three runs reported no blocker or open finding and left no
tester-created repository change. Current writers cannot create the legacy null-socket rows, and the
tester did not corrupt live runtime identity; those migration, repair, and unresolved-state cases
remain covered by behavioral and orchestration tests.

## Permanent homes and accepted limits

The operator contract lives in `cli/command-reference.md`, `cli/README.md`, the root `README.md`,
`docs/guides/resources.md`, `docs/guides/session-status.md`, `docs/guides/upgrading-to-0.19.md`, and
the management guide topic. The executable harness contract lives in
`cli/agentworks/capabilities/harness_integration/` and its README; lifecycle code, database
migrations, completion projections, and behavioral tests carry the implementation contract. Nothing
in this SDD directory is required to operate or maintain the feature.

Interactive console attach was not driven live because the tester itself ran inside tmux; the
nesting guard correctly refused it, while behavioral and orchestration tests cover attach-only and
the explicit override. Force-new integration identities, Agentworks-minted UUIDs, Codex's
tool-assigned identity path, malformed and indeterminate transport cases, and destructive cascade
faults, plus incomplete `--all-running` identity repair and refusal, are covered by focused
structural/orchestration tests rather than destructive live fault injection. The operator accepted
the tester's clean report with these stated limits.

Issue #720 owns deletion of both hidden 0.19 compatibility spellings in 0.20. Issue #715 owns any
future systemd/cgroup containment work. No other in-scope implementation, review, migration, or
documentation finding remains.

The operator owns merging PR #710. The effort lead does not merge it.

-- agw-ns-onboard-disco
