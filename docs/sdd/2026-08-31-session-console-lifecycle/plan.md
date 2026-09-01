# Implementation Plan: Session and Console Lifecycle

<!-- cspell:ignore sdds -->

- Status: Design review
- Date: 2026-08-31
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [lifecycle-lld.md](./lifecycle-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Source baseline: `b0924f594d5fe6eeece74b2474a67bdad78c8bad`
- Design PR: #710, stacked on `feat/debian-release-transition-sdd`

## Delivery Rules

- PR #710 is a design-only draft. It contains this SDD directory, carries
  `sdd:session-console-lifecycle`, and uses the author-owned `review-requested` label for coherent
  checkpoints. It has no implementation or merge intent while design review is active.
- The operator authorized up to three published feedback/fix cycles for this design. Each cycle
  waits at least 20 minutes from the preceding handoff, collects one complete batch, critically
  dispositions every material item, removes `review-requested` before mutation, reruns the private
  project and Muntz reviews after changes, then reapplies the signal at the coherent new head.
- A divergent contract, requirement ambiguity, or non-converging finding stops for operator
  direction rather than spending the remaining budget by assumption.
- The design PR does not authorize implementation. After design clearance and merge, implementation
  uses a short two-PR stack based on the then-current `main`, including the Debian
  release-transition work this SDD is stacked on.
- The first implementation PR is domain-complete for session and harness lifecycle. The second is
  domain-complete for console lifecycle and may be stacked on the first. Neither separates a command
  surface from its manager semantics, compatibility wrapper, collateral, or tests. The SDD remains
  open until both land and combined verification completes.
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
| R4          | HLA teardown; LLD shared teardown        | direct/batch/cascade stop and hook tests          |
| R5          | HLA console manager; LLD console matrix  | create/start/stop/restart/attach state tests      |
| R6          | HLA capability; LLD built-in behavior    | contract, binding, retry, and capability tests    |
| R7          | migration cutover; plan residual sweep   | current-surface inventory and static gates        |
| R8          | migration compatibility section          | hidden wrapper, deprecation, and completion tests |
| R9          | HLA explicit non-abstraction boundary    | VM regression gates and code review               |
| R10         | HLA/LLD ordering and security boundaries | fault injection, mutation-order, and secret tests |

## Phase 0: Design Checkpoint

- [x] Base the effort on `feat/debian-release-transition-sdd` and record exact source baseline
      `b0924f594d5fe6eeece74b2474a67bdad78c8bad`.
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
- [ ] Commit and push the coherent complete-design head, refresh the PR body/comment, and reapply
      `review-requested`.
- [ ] Collect the authorized published design feedback window and complete up to three converging
      correction cycles using the 20-minute minimum interval.
- [ ] Record final design clearance and merge PR #710 before implementation begins.

### Phase 0 Definition of Done

- Every requirement has one architecture owner, detailed behavior, migration disposition, planned
  implementation phase, and objective proof.
- Forced fresh behavior is explicit for shell, Agentworks-minted UUID integrations, and Codex's
  tool-assigned identifier, including safe retry behavior.
- Start, restart, attach, force, and force-new interactions have no ambiguous state.
- Session teardown has one core authority and console realization has one builder without a generic
  runnable framework.
- Compatibility is bounded to hidden 0.17 CLI forms with a named 0.18 removal.
- No material design or complexity finding remains.

## Phase 1: CLI Grammar and Harness Contract

- [ ] Add canonical session start/restart commands with thin CLI bodies, exact positional/option
      shapes, and typed manager errors.
- [ ] Add `--force-new` and thread the exact `force_new` spelling through CLI, services, and harness
      start; keep it independent from broken-runtime `--force`.
- [ ] Replace harness-integration contract v2 with the version-1 `HarnessStart`,
      `start(ctx, *, force_new=False)`, and concrete default-no-op `stop(ctx)` contract.
- [ ] Delete capability `resume`, mutable `launch_note`, v2 descriptor requirements, adapters, and
      old lifecycle vocabulary from every built-in implementation and fake.
- [ ] Add one suppressible output deprecation helper and make completion introspection omit hidden
      subcommands as well as hidden parameters.
- [ ] Update the shared completion spec so canonical session verbs complete resource names and flags
      in Bash, Zsh, and PowerShell.

### Phase 1 Definition of Done

- Canonical session help and generated completion expose only the target grammar.
- Capability registration accepts every in-tree version-1 integration and no v2 shape.
- `HarnessStart` is the only launch-result channel, and no compatibility logic exists below CLI.

## Phase 2: Session Start, Restart, and Integration Bindings

- [ ] Reshape current resume orchestration into explicit `start_session`, `restart_session`, batch
      variants, and one private absent-runtime launch path without duplicating graph preparation,
      readiness, secret resolution, environment, or tmux creation.
- [ ] Implement the full status matrix, including ordinary running-start no-op, running
      `start --force-new` refusal, broken-state force, stopped restart, and per-session aggregate
      batch results without replacing running selections.
- [ ] Preserve state-before-tmux ordering and every foreign integration namespace for create, start,
      restart, and failed-launch retry.
- [ ] Implement shell ordinary/forced command selection without renaming truthful `resume_command`
      configuration.
- [ ] Implement Claude and Grok forced UUID rotation, no old-state probe, old-conversation
      preservation, prelaunch persistence, and ordinary retry reuse.
- [ ] Implement Codex forced-fresh binding/recorder cleanup plus a persisted marker containing the
      rejected recorder identity, so pre-exec failure and ordinary retry cannot adopt it before a
      different recorder ID is known.
- [ ] Validate the flat Codex pending-marker type and make malformed persisted state converge
      through a safe fresh launch without degrading to ordinary discovery; only `null` or a
      canonical recorder UUID is valid.
- [ ] Preserve legacy per-session socket migration and current environment/template/overlay
      semantics under the new operations.

### Phase 2 Definition of Done

- Every session status and flag combination has one deterministic behavior.
- A healthy runtime survives every validation/readiness/secret failure before teardown.
- Known fresh bindings survive ordinary retry; unknown post-launch Codex IDs are handled without
  adopting rejected prior state.
- No internal core operation named resume means runtime replacement.

## Phase 3: One Session Teardown Authority

- [ ] Inventory and route direct stop, restart, direct delete, batch stop, workspace delete, agent
      delete, and reachable VM-delete session cleanup through one session-domain teardown authority.
- [ ] Add at-most-once optional harness stop requests with fresh scoped contexts, no new secret
      prompts, one batch-wide concurrent dispatch budget, discarded arbitrary output, and generic
      interrupt fallback.
- [ ] Preserve one shared grace phase for batches, verified liveness, socket cleanup, stopped-state
      persistence, and explicit force/PID recovery for broken sessions.
- [ ] Preserve each parent deletion path's existing best-effort or fail-closed policy when targets
      or integrations cannot be reconstructed; the optional hook never becomes a deletion
      dependency.
- [ ] Delete superseded session kill loops and prove all intended teardown consumers use the shared
      authority.

### Phase 3 Definition of Done

- The harness can request cooperative application shutdown but cannot own or claim runtime death.
- Direct, batch, restart, and cascading operations share one grace/kill implementation.
- Unavailable optional integration behavior cannot strand mandatory cleanup.

## Phase 4: Console Lifecycle

- [ ] Add canonical console start/stop/restart commands with thin CLI bodies, exact
      positional/option shapes, typed manager errors, and Bash/Zsh/PowerShell name/flag completion.
- [ ] Introduce the internal prospective console definition and make build-secret target planning
      work before database insertion without adding persisted runtime state.
- [ ] Extract one verified console teardown that owns canonical and staging artifacts, and retain
      `_build_console_tmux` as the sole realization implementation.
- [ ] Make the console builder construct under the reserved, canonical-name-disjoint
      `aw-console-build+NAME` staging name, fail on every required tmux operation, publish the
      canonical name only after complete success, and verify staging cleanup on failure.
- [ ] Convert every console tmux target to exact `=NAME` selection and prove canonical and staging
      isolation with related valid names such as `foo` and `foobar`.
- [ ] Make create validate and prepare, remove/verify a stale predecessor, insert atomically, then
      build; roll back before insertion and retain the row after a later build failure.
- [ ] Implement idempotent start, state-aware restart, idempotent stop, and attach-only attach using
      the canonical/staging LLD state matrix.
- [ ] Ensure attach does not load build plans or resolve pane secrets, while retaining ordinary VM
      activation and transport authorization.
- [ ] Repoint full-rebuild recovery hints and restore flows to canonical console restart while
      preserving live best-effort membership synchronization.
- [ ] Keep console list, describe, names-only, and completion database-only.

### Phase 4 Definition of Done

- Create/start/restart call one builder and every create/start/stop/restart/delete cleanup calls one
  verified teardown authority for both managed names.
- Attach has no runtime realization or pane-secret side effect.
- A stale deleted predecessor cannot be adopted by a same-name new definition.
- A post-insert build failure leaves an honest stopped console definition with a retry path only
  after canonical and staging absence are verified.
- Canonical and staging names are structurally disjoint across the accepted console-name grammar.

## Phase 5: Per-Domain Compatibility and Permanent Collateral

These tasks land inside the corresponding domain PR, not in a third cleanup PR.

### Session and harness PR

- [ ] Add the hidden 0.17 session resume wrapper and normalize named, all-stopped, and all forms
      into canonical manager operations; accept only the bounded legacy parser surface.
- [ ] Prove the wrapper respects `--no-deprecations`, remains absent from help/completion, and
      carries no service implementation.
- [ ] Update session-facing CLI README/command reference/resources/guide/release/upgrade material,
      harness capability/root/plugin READMEs, sample configuration, and ADR 0020 with a supersession
      note.
- [ ] Record explicit 0.18 session-wrapper removal work, update machine identities/logs/comments
      that misuse resume for runtime replacement, and run the one-time scoped vocabulary review;
      keep persistent guards structural rather than lexical.

### Console PR

- [ ] Add the hidden 0.17 console attach `--recreate` wrapper as restart followed by attach and
      prove it respects `--no-deprecations`, stays absent from help/completion, and carries no
      service implementation.
- [ ] Update console-facing CLI README/command reference/resources/guide/release/upgrade material
      and sample configuration, without duplicating compatibility teaching across permanent
      surfaces.
- [ ] Record explicit 0.18 console-wrapper removal work and update machine identities, operation
      labels, tests, fixtures, comments, and docstrings that misuse attach for runtime mutation.

### Phase 5 Definition of Done

- A new operator sees one canonical vocabulary everywhere.
- Existing 0.17 automation has a bounded warning-producing route in each domain-complete PR.
- Permanent docs describe current behavior without depending on this SDD.

## Phase 6: Verification, Delivery, and Closeout

The implementation, review, CI, and live-evidence steps below apply separately to each
domain-complete PR. Combined verification and SDD closeout occur only after both have landed.

- [ ] Add structural and behavioral coverage from the LLD verification matrix without assertions on
      authored prose.
- [ ] Run focused session, console, capability, secret-boundary, completion, and cascade suites,
      then the full non-integration, static, docs, generation, website, and installed-wheel gates.
- [ ] Run private project, Muntz, and cold correctness/security reviews on one clean exact head and
      resolve every material finding.
- [ ] Open each domain-complete implementation PR ready only after its complete green handoff;
      monitor CI and published feedback under the standard delivery process, then run combined
      verification after both land.
- [ ] Run authorized live validation for console create/start/stop/restart/attach, session
      start/restart/force-new/attach, one Agentworks-minted UUID integration, and Codex's
      tool-assigned identity path where the environment supports them.
- [ ] Confirm the release notes identify the CLI and in-repository capability breaks, the upgrade
      guide is actionable, and 0.18 removal work has an owner.
- [ ] Create `locked.md` only after implementation, live evidence, reviews, and final acceptance are
      complete; run locked-SDD checks and merge through the operator-owned flow.

### Phase 6 Definition of Done

- Full gates and scoped live tests pass on the shipped artifact.
- Review reports no unresolved material correctness, security, architecture, migration, or
  complexity finding.
- The implementation is complete enough that deleting this SDD would not remove any permanent
  operating or contributor knowledge.
- The lock records the exact shipped lifecycle, compatibility window, evidence, and residual 0.18
  removal task.
