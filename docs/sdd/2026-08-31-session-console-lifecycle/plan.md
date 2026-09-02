# Implementation Plan: Session and Console Lifecycle

<!-- cspell:ignore sdds -->

- Status: Implementation
- Date: 2026-08-31
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [lifecycle-lld.md](./lifecycle-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Source baseline: `8695afcd833790ee433b50bb9f5d5c696177233d`
- Delivery PR: #710, based on `main`

## Delivery Rules

- PR #710 is the single integrated design-and-implementation PR. It carries
  `sdd:session-console-lifecycle`, remains draft while implementation is in motion, and becomes
  ready only with a complete green implementation handoff.
- The operator authorized up to three published feedback/fix cycles for this design. Each cycle
  waits at least 20 minutes from the preceding handoff, collects one complete batch, critically
  dispositions every material item, removes `review-requested` before mutation, reruns the private
  project and Muntz reviews after changes, then reapplies the signal at the coherent new head.
- A divergent contract, requirement ambiguity, or non-converging finding stops for operator
  direction rather than spending the remaining budget by assumption.
- Authenticated operator direction on 2026-09-01 superseded the design-first merge and two-PR stack:
  implementation proceeds on PR #710 without merging the SDD separately. Session, harness, console,
  compatibility, collateral, and tests therefore land as one coherent increment; no command surface
  is separated from its manager semantics.
- The implementation lead does not merge its own PR. Before merge it follows the integration-testing
  process and obtains operator-gated live evidence for representative session and console flows.
- Completed plan checkboxes are immutable after merge. A later correction appends a superseding item
  rather than rewriting completed history.

## Full Implementation Gates

From `cli/`:

```console
uv run ruff check agentworks/ tests/
uv run ruff format --check agentworks/ tests/
uv run mypy agentworks/ tests/
uv run pytest tests/ -m 'not integration'
```

From the repository root:

```console
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
python3 -m unittest discover -s website/tests -p 'test_*.py'
node --test website/tests/*.test.mjs
```

The final implementation also receives installed-wheel CLI/completion smoke tests, deterministic
website builds when permanent site content changes, structural stale-surface scans, private project
and Muntz review, a cold correctness/security review for the orchestration code, and capability-
appropriate live tests. Tests assert behavior and structure, never the wording of authored prose.

## Requirement Traceability

| Requirement | Design authority                         | Planned proof                                     |
| ----------- | ---------------------------------------- | ------------------------------------------------- |
| R1          | HLA component split; lifecycle LLD       | CLI grammar and no-generic-framework review       |
| R2, R3      | HLA session manager; LLD status matrix   | singular/batch start and restart tests            |
| R4          | HLA teardown; LLD shared teardown        | direct/batch/cascade stop tests                   |
| R5          | HLA console manager; LLD console matrix  | create/start/stop/restart/attach state tests      |
| R6          | HLA capability; LLD built-in behavior    | contract, binding, retry, and capability tests    |
| R7          | migration cutover; plan residual sweep   | current-surface inventory and static gates        |
| R8          | migration compatibility section          | hidden wrapper, deprecation, and completion tests |
| R9          | HLA explicit non-abstraction boundary    | VM regression gates and code review               |
| R10         | HLA/LLD ordering and security boundaries | fault injection, mutation-order, and secret tests |

## Phase 0: Design Checkpoint

- [x] Base the effort on `feat/debian-release-transition-sdd` and record exact source baseline
      `b0924f594d5fe6eeece74b2474a67bdad78c8bad`.
- [x] Rebase PR #710 after PRs #702 and #709 onto `main` at
      `8695afcd833790ee433b50bb9f5d5c696177233d`, audit their complete delta against this design,
      and shift compatibility to 0.19 with 0.20 removal because 0.18.0 already has a ready release
      PR.
- [x] Inventory session create/resume/stop/delete, direct and cascading teardown, console
      create/attach/recreate/delete, completion, database state, the capability descriptor, and all
      four built-in integrations at the baseline.
- [x] Research primary lifecycle precedents from systemd, Docker, tmux, and Claude Code, then map
      only the demonstrated semantics into Agentworks decisions.
- [x] Draft the operator-owned FRD and record authenticated rulings for start/restart, force-new,
      console create, harness ownership, capability versions, compatibility, and VM non-goals.
- [x] Draft HLA, lifecycle LLD, migration strategy, prior-art research, and this implementation
      plan.
- [x] Run file lint, spelling, links, locked-SDD, and diff checks on the complete artifact set.
- [x] Obtain clean private project-reviewer and Muntz passes on one exact head and incorporate every
      material finding.
- [x] Commit and push the coherent complete-design head, refresh the PR body/comment, and reapply
      `review-requested`.
- [x] Collect the authorized published design feedback window and complete up to three converging
      correction cycles using the 20-minute minimum interval.
- [x] Reconcile the teardown design with the dedicated-server architecture, operator-added panes,
      primary signal/tmux evidence, dedicated `kill-server`, exact legacy `kill-session`, hardened
      proven-absence-only stale cleanup, and the separate systemd/cgroup security follow-up in issue
      #715; rerun the complete design gates and reviews.
- [x] Record final design clearance, then accept the operator's 2026-09-01 ruling to implement on PR
      #710 without an intermediate SDD merge.

### Phase 0 Definition of Done

- Every requirement has one architecture owner, detailed behavior, migration disposition, planned
  implementation phase, and objective proof.
- Forced fresh behavior is explicit for shell, Agentworks-minted UUID integrations, and Codex's
  tool-assigned identifier, including safe retry behavior.
- Start, restart, attach, force, and force-new interactions have no ambiguous state.
- Session teardown has one core authority and console realization has one builder without a generic
  runnable framework.
- Compatibility is bounded to hidden 0.19 CLI forms with a named 0.20 removal.
- No material design or complexity finding remains.

## Phase 1: CLI Grammar and Harness Contract

- [x] Add canonical session start/restart commands with thin CLI bodies, exact positional/option
      shapes, and typed manager errors.
- [x] Add `--force-new` and thread the exact `force_new` spelling through CLI, services, and harness
      start; keep it independent from broken-runtime `--force`.
- [x] Replace harness-integration contract v2 with the version-1 `HarnessStart` and
      `start(ctx, *, force_new=False)` contract.
- [x] Delete capability `resume`, mutable `launch_note`, v2 descriptor requirements, adapters, and
      old lifecycle vocabulary from every built-in implementation and fake.
- [x] Add one suppressible output deprecation helper and make completion introspection omit hidden
      subcommands as well as hidden parameters.
- [x] Update the shared completion spec so canonical session verbs complete resource names and flags
      in Bash, Zsh, and PowerShell.

### Phase 1 Definition of Done

- Canonical session help and generated completion expose only the target grammar.
- Capability registration accepts every in-tree version-1 integration and no v2 shape.
- `HarnessStart` is the only launch-result channel, and no compatibility logic exists below CLI.

## Phase 2: Session Start, Restart, and Integration Bindings

- [x] Reshape current resume orchestration into explicit `start_session`, `restart_session`, batch
      variants, and one private absent-runtime launch path without duplicating graph preparation,
      readiness, secret resolution, environment, or tmux creation.
- [x] Implement the full status matrix, including ordinary running-start no-op, running
      `start --force-new` refusal, broken-state force, stopped restart, and per-session aggregate
      batch results without replacing running selections.
- [x] Preserve state-before-tmux ordering and every foreign integration namespace for create, start,
      restart, and failed-launch retry.
- [x] Implement shell ordinary/forced command selection without renaming truthful `resume_command`
      configuration.
- [x] Implement Claude and Grok forced UUID rotation, no old-state probe, old-conversation
      preservation, prelaunch persistence, and ordinary retry reuse.
- [x] Implement Codex forced-fresh binding/recorder cleanup plus a persisted marker containing the
      rejected recorder identity, so pre-exec failure and ordinary retry cannot adopt it before a
      different recorder ID is known.
- [x] Validate the flat Codex pending-marker type and make malformed persisted state converge
      through a safe fresh launch without degrading to ordinary discovery; only `null` or a
      canonical recorder UUID is valid.
- [x] Preserve legacy per-session socket migration and current environment/template/overlay
      semantics under the new operations.

### Phase 2 Definition of Done

- Every session status and flag combination has one deterministic behavior.
- A healthy runtime survives every validation/readiness/secret failure before teardown.
- Known fresh bindings survive ordinary retry; unknown post-launch Codex IDs are handled without
  adopting rejected prior state.
- No internal core operation named resume means runtime replacement.

## Phase 3: One Session Teardown Authority

- [x] Inventory and route direct stop, restart, direct delete, batch stop, workspace delete, agent
      delete, and reachable VM-delete session cleanup through one session-domain teardown authority.
- [x] Delete pane `C-c` and its grace sleep; use `kill-server` for each reachable dedicated runtime,
      verify all operator-added sessions/panes/windows are gone, and retain exact
      `kill-session -t =NAME` only for reachable legacy shared-server rows.
- [x] Add the explicit residual status for a reachable dedicated server whose canonical session is
      absent; route stop/delete through teardown, start/restart through teardown then launch, and
      make attach refuse without requiring `--force`.
- [x] Add nullable `tmux_server_start_ticks`, atomic fingerprint persistence/clearing, and safe
      reachable-row backfill; make missing or mismatched broken-state identity fail closed.
- [x] Delete raw numeric-PID force kill; permit exact admin/agent stale-socket cleanup only after
      the stored boot/PID/start-time fingerprint proves the old server already exited, and fail
      closed on a matching live process, missing identity, or indeterminate result.
- [x] Preserve each parent deletion path's existing best-effort or fail-closed policy when targets
      cannot be reached or reconstructed.
- [x] Delete superseded session kill loops and prove all intended teardown consumers use the shared
      authority.

### Phase 3 Definition of Done

- Direct, batch, restart, and cascading operations share one exact tmux teardown authority and one
  force-only, proven-absence stale-state recovery implementation with no numeric-PID signaling.
- Harness integrations have no teardown API or responsibility.

## Phase 4: Console Lifecycle

- [x] Add canonical console start/stop/restart commands with thin CLI bodies, exact
      positional/option shapes, typed manager errors, and Bash/Zsh/PowerShell name/flag completion.
- [x] Introduce the internal prospective console definition and make build-secret target planning
      work before database insertion without adding persisted runtime state.
- [x] Extract one verified console teardown that owns canonical and staging artifacts, and retain
      `_build_console_tmux` as the sole realization implementation.
- [x] Make the console builder construct under the reserved, canonical-name-disjoint
      `aw-console-build+NAME` staging name, fail on every required tmux operation, publish the
      canonical name only after complete success, and verify staging cleanup on failure.
- [x] Convert every console tmux target to exact `=NAME` selection and prove canonical and staging
      isolation with related valid names such as `foo` and `foobar`.
- [x] Make create validate and prepare, remove/verify a stale predecessor, insert atomically, then
      build; roll back before insertion and retain the row after a later build failure.
- [x] Implement idempotent start, state-aware restart, idempotent stop, and attach-only attach using
      the canonical/staging LLD state matrix.
- [x] Ensure attach does not load build plans or resolve pane secrets, while retaining ordinary VM
      activation and transport authorization.
- [x] Repoint full-rebuild recovery hints and restore flows to canonical console restart while
      preserving live best-effort membership synchronization.
- [x] Keep console list, describe, names-only, and completion database-only.

### Phase 4 Definition of Done

- Create/start/restart call one builder and every create/start/stop/restart/delete cleanup calls one
  verified teardown authority for both managed names.
- Attach has no runtime realization or pane-secret side effect.
- A stale deleted predecessor cannot be adopted by a same-name new definition.
- A post-insert build failure leaves an honest stopped console definition with a retry path only
  after canonical and staging absence are verified.
- Canonical and staging names are structurally disjoint across the accepted console-name grammar.

## Phase 5: Per-Domain Compatibility and Permanent Collateral

These tasks land with their corresponding domain implementation inside PR #710, not in a later
cleanup PR.

### Session and harness PR

- [x] Add the hidden 0.19 session resume wrapper and normalize named, all-stopped, and all forms
      into canonical manager operations; accept only the bounded legacy parser surface.
- [x] Prove the wrapper respects `--no-deprecations`, remains absent from help/completion, and
      carries no service implementation.
- [x] Update session-facing CLI README/command reference/resources/guide/release/upgrade material,
      harness capability/root/plugin READMEs, sample configuration, and ADR 0020 with a supersession
      note.
- [x] Record explicit 0.20 session-wrapper removal work, update machine identities/logs/comments
      that misuse resume for runtime replacement, and run the one-time scoped vocabulary review;
      keep persistent guards structural rather than lexical. Issue #720 owns removal.

### Console PR

- [x] Add the hidden 0.19 console attach `--recreate` wrapper as restart followed by attach and
      prove it respects `--no-deprecations`, stays absent from help/completion, and carries no
      service implementation.
- [x] Update console-facing CLI README/command reference/resources/guide/release/upgrade material
      and sample configuration, without duplicating compatibility teaching across permanent
      surfaces.
- [x] Record explicit 0.20 console-wrapper removal work and update machine identities, operation
      labels, tests, fixtures, comments, and docstrings that misuse attach for runtime mutation.
      Issue #720 owns removal.

### Phase 5 Definition of Done

- A new operator sees one canonical vocabulary everywhere.
- Existing 0.18 automation has a bounded warning-producing route through 0.19 in each
  domain-complete PR.
- Permanent docs describe current behavior without depending on this SDD.

## Phase 6: Verification, Delivery, and Closeout

The implementation, review, CI, and live-evidence steps below apply to the complete integrated PR.

- [x] Add structural and behavioral coverage from the LLD verification matrix without assertions on
      authored prose.
- [x] Run focused session, console, capability, secret-boundary, completion, and cascade suites,
      then the full non-integration, static, docs, generation, website, and installed-wheel gates.
- [x] Run private project, Muntz, and cold correctness/security reviews on one clean exact head and
      resolve every material finding.
- [x] Mark PR #710 ready only after its complete green handoff, then monitor CI and published
      feedback under the standard delivery process.
- [x] Run authorized live validation for representative console and session lifecycle operations
      plus the hidden compatibility wrapper on a real VM; record the nested-tmux attach limitation
      and retain structural/orchestration coverage for paths the tester cannot safely automate.
- [x] Confirm the breaking-change release input identifies the CLI and in-repository capability
      breaks, the upgrade guide is actionable, and issue #720 owns 0.20 wrapper removal.
- [x] Create `locked.md` only after implementation, live evidence, reviews, and final acceptance are
      complete; run locked-SDD checks and merge through the operator-owned flow.

### Post-ready correction (2026-09-01)

- [x] Preserve the hidden 0.19 `session resume` wrapper's running-session confirmation and legacy
      `--yes` bypass at the CLI boundary, while keeping canonical restart services prompt-free and
      naming the exact start/restart mapping in migration guidance.
- [x] Correct the accepted status diagnostic, test-docstring, and harness-continuation wording nits
      without adding authored-prose assertions.
- [x] Make compatibility consent read-only and fail closed without `--yes` when interactive input is
      unavailable; canonical restart and its manager contract remain unchanged.
- [x] Keep VM-delete teardown warnings free of transport-originated exception text and correct the
      console restart recovery spelling; leave pre-existing test-isolation hygiene to issue #722.
- [x] Reset process-global CLI request flags at each pytest boundary after the recurring cross-test
      `--non-interactive` leak blocked exact-head CI; retain issue #722 for the broader pre-existing
      isolation audit.
- [x] Make console `--all-running` distinguish live legacy rows from transport indeterminacy and
      reuse one immutable config load across selection and create.
- [x] Make every test fixture that claims a valid runtime identity use a canonical boot UUID while
      retaining malformed values only in explicit rejection and opaque database-storage tests.
- [x] Make console `--all-running` apply the established safe identity-repair pass before batched
      status classification and refuse unresolved non-stopped rows rather than silently omitting
      them; correct the one-round-trip and read-only collateral claims.
- [x] Refresh the lock to the final production checkpoint, complete correction/review record, and
      honest live-versus-structural evidence before the final merge gate.
- [x] Incorporate the late exact-production tester pass, including live `--all-running` selection
      and the explicit legacy/incomplete-identity live-test limits, into the lock before merge.
- [x] Complete final review hygiene by making the remaining live-runtime fixture fingerprint
      truthful, preserving structured console error metadata, and naming repeated-start tests for
      the version-1 harness contract.
- [x] Make the atomic session-runtime update participate in explicit database transactions and prove
      rollback leaves no partial runtime fingerprint.
- [x] Keep migration repair fail closed when a fingerprint is indeterminate, inspect agent-owned
      tmux and process identity through the established batch root boundary, and accept only the
      canonical local Windows forced-TTY close advisory alongside one remote tmux absence result.
- [x] Give ordinary session listing one aggregate status-check indication while suppressing per-row
      repair and normalization narration; retain the same durable convergence and status derivation.
- [x] Replace the elevated PID probe's ambiguous shell-test exit code with fixed facts emitted only
      after the selected shell starts; treat sudo refusal, malformed output, and every other failure
      as indeterminate in singular status, absence proof, and batched status.
- [x] Keep root probing confined to dedicated agent-owned runtime state and remove the impossible
      legacy agent/shared-server probe surface and fixture.

### Phase 6 Definition of Done

- Full gates and scoped live tests pass on the shipped artifact.
- Review reports no unresolved material correctness, security, architecture, migration, or
  complexity finding.
- The implementation is complete enough that deleting this SDD would not remove any permanent
  operating or contributor knowledge.
- The lock records the exact shipped lifecycle, compatibility window, evidence, and residual 0.20
  removal task.
