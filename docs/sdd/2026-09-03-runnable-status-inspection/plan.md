# Implementation Plan: Runnable Status Inspection

<!-- cspell:ignore sdds -->

- Status: Complete; locked on merge of PR #736
- Date: 2026-09-03
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [status-observation-lld.md](./status-observation-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Source baseline: `3f97ea3cd357582ceb71c2c065ad23db8d08379d`
- Delivery: one design-and-implementation PR based on `main`

## Delivery rules

- The effort uses one PR carrying design, implementation, tests, permanent collateral, and closeout.
  It remains draft during design review and implementation, then becomes ready only after complete
  review, CI, and operator-gated live validation.
- The PR carries `sdd:runnable-status-inspection`. A coherent design checkpoint uses the
  `review-requested` signal without implying merge intent.
- The operator authorized up to three published design feedback/fix rounds. Each round waits the
  standard minimum of one hour from the preceding handoff unless the operator supplies a shorter
  interval, collects one fixed batch, critically dispositions every material item, returns the PR to
  private draft work before mutation, reruns the private design reviews after changes, and re-hands
  off one exact head.
- If the design converges, implementation proceeds on the same PR without merging the SDD
  separately. The operator authorized up to three additional published implementation feedback/fix
  rounds under the same batching and handoff rules.
- A changed requirement, capability contract, JSON schema-version need, unresolved security issue,
  or non-converging material finding stops for authenticated operator direction rather than spending
  the remaining budget by assumption.
- The implementation lead does not merge its own PR. Live testing follows the integration-testing
  and agw-test-env skills and requires operator disposition before merge intent.
- Completed plan checkboxes are immutable after merge. A later correction appends a superseding item
  instead of rewriting completed history.

## Full implementation gates

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

The final handoff also requires installed-wheel CLI and completion smoke tests, a residual
nomenclature and release scan, private project and Muntz review, a cold correctness/security review,
CI, and capability-appropriate live tests on representative provider and Windows SSH paths. Tests
assert behavior and structure, never authored prose.

## Requirement traceability

| Requirements | Architecture owner                   | Planned proof                                          |
| ------------ | ------------------------------------ | ------------------------------------------------------ |
| R1-R5        | CLI adapters and list enrichment     | grammar, early-refusal, filtering, table-shape tests   |
| R6-R10       | resource observers and failure model | negative side-effect, timeout, partial-failure tests   |
| R11, R15     | VM observer and projection           | platform, disposition, no-gate tests                   |
| R12, R14     | session observer                     | complete liveness matrix and bounded SSH tests         |
| R13, R14     | console observer                     | canonical/staging matrix and exact-target tests        |
| R16-R18      | focused describe                     | preservation and non-activation tests                  |
| R19-R22      | machine projection                   | exact JSON value/type/order tests                      |
| R23-R25      | migration and collateral             | compatibility, completion, docs, release residual scan |
| R26          | capability boundary                  | descriptor and static contract review                  |

## Phase 0: Complete design checkpoint

- [x] Refresh from `main` at `3f97ea3cd357582ceb71c2c065ad23db8d08379d` and create
      `feat/runnable-status-inspection`.
- [x] Inventory VM, session, and console list/describe command, manager, transport, platform, tmux,
      machine-output, completion, test, and permanent-doc contracts at the baseline.
- [x] Identify the current session list and describe activation side effect, the unbounded session
      SSH call, the local-config exception to strict DB-only session inventory, and provider client
      concurrency limits.
- [x] Research official Azure CLI, systemd, Docker, GCE, Kubernetes, tmux, and GitHub CLI prior art
      and tie each adopted or rejected pattern to a design decision.
- [x] Draft the operator-owned FRD and the lead-owned HLA, status-observation LLD, migration
      strategy, prior-art research, and this plan in one artifact set.
- [x] Run file lint, spelling, formatting, links, locked-SDD, diff, and release-chronology checks on
      the complete artifact set.
- [x] Obtain clean private project-reviewer and Muntz passes on one exact design head and
      incorporate every material finding authorized by the current design charter.
- [x] Commit and push the coherent design head, open the draft PR, apply
      `sdd:runnable-status-inspection` and `review-requested`, and publish the exact design handoff.
- [x] Complete up to three authorized published design feedback/fix rounds, or stop sooner when one
      full batch produces no material changes.
- [x] Record design convergence before delegating or beginning implementation.

### Phase 0 definition of done

- Every requirement has one architecture owner, detailed behavior, migration disposition, planned
  implementation phase, and objective proof.
- Default list, requested list, and describe side effects are explicit for all three resources.
- Unknown versus not-requested, empty results, names-only, human, and JSON behavior are unambiguous.
- Guest timeouts and provider concurrency claims match mechanisms the code can enforce.
- 0.18.0 introduction/deprecation and 0.19.0 compatibility removal are consistent everywhere.
- No material design or complexity finding remains.

## Phase 1: Resource-owned observers

- [x] Rename `SessionStatus.OK` to `RUNNING` across production, tests, comments, help, and docs,
      without an alias or persisted-state migration.
- [x] Add a bounded non-activating canonical transport construction path for status probes, keeping
      the global Windows TTY policy unchanged and selecting `tty=False` per observation call.
- [x] Refactor the session singular classifier and batch status observer, make the batch return a
      complete requested-name mapping, use a 10-second one-attempt transport budget, and preserve
      lifecycle callers.
- [x] Supersede inherited stdin for no-input guest status probes after Windows live validation
      proved intermittent client hangs after complete remote output; send empty stdin while
      retaining the original one-attempt timeout policy.
- [x] Add the console domain enum, canonical/staging pure classifier, exact session-enumeration
      observer, focused describe join, and one-call-per-VM failure isolation in a focused module.
- [x] Add the non-gated VM multi-row observation composition with one all-or-nothing
      registry/preflight/credential setup, site-local serial platform calls, and finite parallelism
      across independent sites after setup.
- [x] Inventory each bundled provider status timeout and add safe provider-local bounds where the
      existing client supports them without a capability change; record honest residual limitations.

### Phase 1 definition of done

- Every requested resource name receives a resource-owned status.
- Session and console probes are bounded, exact, non-interactive, and cannot activate or repair.
- VM status uses only the existing version-1 platform operation and never a guest transport.
- Shared platform instances are not called concurrently.
- No production or test reference retains `SessionStatus.OK`.

## Phase 2: Standard list grammar and projections

- [x] Add `--status` to VM, session, and console list with positive `include_status` service
      parameters and early `--names-only` refusal.
- [x] Make plain session list skip live observation and remove the status column from its human
      default while preserving harness-integration local declaration resolution.
- [x] Join VM observations into optional human `STATUS` and additive JSON `observed_status` and
      `status_disposition` fields.
- [x] Join session observations into optional human `STATUS` and the existing JSON status field,
      with `unavailable` only when not requested.
- [x] Join console observations into optional human `STATUS` and the additive JSON status field.
- [x] Emit aggregate human progress before external dispatch and compact post-table summaries for
      unknown observations, while preserving a clean suppressed JSON envelope.
- [x] Ensure empty and fully filtered lists perform no observation and preserve existing ordering
      and friendly human output.
- [x] Supersede bespoke VM and console row formatting after Windows live validation exposed a
      console status-column alignment defect; route all runnable list tables through the shared
      renderer without truncating historically uncapped VM values.

### Phase 2 definition of done

- Plain list is local and status-free for every resource.
- `list --status` uses only the selected rows and cannot change selection or order.
- Human table shape follows the explicit request, including for empty results.
- Machine fields distinguish not-requested from requested-unknown without a schema-version change.
- Completion names-only paths remain local and fast.

## Phase 3: Focused describe

- [x] Move session describe from `_prepare_vm` to singular non-activating status observation and
      preserve its configured/instance-state facts on expected live failure.
- [x] Add console describe live status through a one-row selection from the console batch observer,
      with no build-plan or pane-secret work and configured membership preserved on unknown.
- [x] Reuse one VM status/disposition projector in list and describe without pulling describe-only
      live resource usage into list.
- [x] Add human pre-observation progress and safe expected-failure reporting; keep JSON
      presentation-free and structurally closed.
- [x] Remove list/describe claims from activation-boundary docstrings and update nearby names and
      comments to reflect lifecycle versus observation ownership.

### Phase 3 definition of done

- All three describe commands report current resource-specific status by default.
- No describe status path activates, repairs, starts, stops, or persists runtime state.
- Expected observation failure renders local facts with unknown, not a false stopped state or an
  empty result.
- List and describe cannot disagree about the meaning of one domain status.

## Phase 4: Compatibility, completion, and permanent collateral

- [x] Implement R23 at the CLI boundary and remove all manager-level negative flags.
- [x] Update completion metadata and Bash, zsh, and PowerShell behavior to offer `--status` on all
      three lists, omit deprecated `--no-status`, and keep names-only sources status-free.
- [x] Update CLI README and command reference for list/describe grammar, progress, resource status
      vocabularies, and JSON shapes.
- [x] Update the session-status and lifecycle guides, plus any VM or console guide that currently
      makes an outdated status claim.
- [x] Create or update the 0.18 upgrade guide and release notes with the session automation example
      and the 0.19 removal schedule.
- [x] Update the permanent CLI-conventions source if its current session-list precedent becomes
      stale, then regenerate all Rulesync targets through the required workflow.
- [x] Create or reconcile the 0.19 removal tracker, without duplicating the lifecycle compatibility
      cleanup, and link it from closeout.
- [x] Run a residual scan proving canonical surfaces contain no old default-live instruction, public
      `--no-status`, Python `no_status`, `SessionStatus.OK`, 0.19 introduction, or 0.20 removal
      claim for this feature.

### Phase 4 definition of done

- Canonical help, completion, docs, examples, and internal APIs teach one positive status grammar.
- The one compatibility flag is CLI-local, hidden, warned in 0.18, and scheduled to disappear in
  0.19.
- Machine documentation exactly matches production field names, meanings, nullability, values, and
  order.
- Capability contracts and versions remain unchanged.

## Phase 5: Verification, feedback, and closeout

- [x] Add focused structural, side-effect, timeout, parser, failure-isolation, CLI, completion,
      projection, and describe tests from the LLD testing-seams section.
- [x] Run focused tests throughout implementation, then the complete Python and repository gate set.
- [x] Build and install the wheel in an isolated environment; smoke plain/status/names-only/JSON
      commands and generated Bash, zsh, and PowerShell completion.
- [x] Load the integration-testing and agw-test-env skills, prepare an operator-reviewed live
      charter, and test representative VM providers plus session/console status on Windows canonical
      SSH.
- [x] Exercise partial failure with one intentionally unreachable target and confirm other rows
      complete inside the documented bound without activation or repair.
- [x] Obtain clean private agentworks-reviewer, Muntz, and cold correctness/security passes on one
      exact implementation head; apply every material authorized correction and rerun affected
      gates.
- [x] Complete up to three authorized published implementation feedback/fix rounds, or stop sooner
      when one complete batch produces no material changes.
- [x] Rebase or merge the latest `main`, resolve conflicts semantically, rerun the full gates and
      reviews, and hand off the exact final head.
- [ ] Promote every load-bearing status contract to permanent code/docs, check every truthful plan
      item, add `locked.md`, remove `review-requested`, mark the PR ready, and obtain operator
      disposition without self-merging.

### Phase 5 definition of done

- Focused and full automated gates pass at the exact pushed head.
- Live evidence covers the full changed command surface, including non-no-op status work and one
  degraded boundary.
- No material correctness, complexity, security, collateral, release, or migration finding remains.
- The SDD is locked with an accurate final-state summary and no permanent artifact depends on its
  path.
- The ready PR is a complete 0.18.0 merge candidate with a bounded 0.19.0 compatibility cleanup.
