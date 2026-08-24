# Plan: Value-free secret resolution preview

- Status: Complete; locked on merge of PR #619
- Date: 2026-08-18
- Amended: 2026-08-23
- Requirements: [FRD](./frd.md)
- Architecture: [HLA](./hla.md)
- Detailed design: [preview contract LLD](./preview-contract-lld.md) and
  [operator surfaces LLD](./operator-surfaces-lld.md)
- Migration: [migration strategy](./migration-strategy.md)
- Delivery vehicle: one operator-directed draft PR carrying artifacts and implementation

## Delivery rules

- The PR stays draft during artifact review and carries `saga:next-steps` plus the author-owned
  `review-requested` label. The label requests review; draft state truthfully says there is no merge
  intent yet.
- The operator pre-authorized at most two artifact feedback/fix iterations based on the saga lead,
  Muntz, and integration tester. Findings are evaluated critically and may be applied only within
  the accepted FRD scope.
- The effort stops for operator direction if review requires a requirements change or another result
  outside the agreed scope. Old-shape compatibility is not part of this effort: the operator
  authorized an atomic rewrite because no external backends exist.
- The PR remains draft through implementation and its private quality loop. A complete, green,
  independently reviewed implementation transitions to ready to trigger the separately operated
  integration pipeline. Any subsequent change returns it to draft before a new exact-head handoff;
  it is ready to merge only after integration disposition and closeout are complete.
- The effort lead does not merge its own PR.

## Phase 0: Artifact checkpoint

- [x] Verify the feature branch is based on current `origin/main` and record the implementation
      baseline in the migration artifact. (2026-08-18: baseline `c01263d0`.)
- [x] Refresh the branch and implementation baseline before public artifact handoff. (2026-08-19:
      rebased onto `667793ee`; intervening changes did not alter runtime secret semantics.)
- [x] Refresh the branch again before revision-1 handoff. (2026-08-20: rebased onto `1d921b3a`;
      intervening process and documentation changes did not alter runtime secret semantics.)
- [x] Refresh the branch before revision-2 handoff. (2026-08-21: rebased onto `202036a6`;
      intervening validation-process, onboarding-guide, and website changes did not alter runtime
      secret semantics.)
- [x] Refresh the branch before final implementation review. (2026-08-21: rebased onto `d83bfa71`;
      intervening changes touched only the parent saga artifacts, and range-diff proved all 15
      child-effort patches unchanged.)
- [x] Complete FRD, prior-art research, HLA, preview-contract LLD, operator-surfaces LLD, migration
      strategy, and this tracked plan. (2026-08-18.)
- [x] Run artifact lint, spelling, link, lockfile, Rulesync, and manual typography checks.
      (2026-08-18: all passed locally; the lockfile gate is rerun against the committed diff before
      handoff.)
- [x] Obtain a private `agentworks-reviewer` pass and resolve material findings before public
      handoff. (2026-08-18: JSON v1, exact resolution flow, pre-lifecycle intent, memoization,
      sequencing, and collateral findings resolved; final focused pass had no findings.)
- [x] Amend the design with the operator-approved tagged result model: ordinary missing,
      impact-limited indeterminate, execution blocked, and hard-stop failed are distinct.
      (2026-08-19.)
- [x] Rerun the private artifact gate after the tagged-result amendment and resolve material
      findings before public handoff. (2026-08-19: corrected no-candidate truthfulness,
      preview/actual result boundaries, aggregate-only preflight, doctor status mapping, and
      fail-closed OnePassword evidence requirements; focused re-review had no findings.)
- [x] Commit the artifact set with the session trailer, push the feature branch, open one draft PR,
      and apply `saga:next-steps` and `review-requested`. (2026-08-19: draft PR #619.)
- [x] Request and collect the three named artifact perspectives: saga lead contract review, Muntz
      complexity review, and integration tester testability review. (2026-08-19: all three returned
      at exact head `74b497f5`; revision requested.)
- [x] Apply no more than two pre-authorized artifact feedback/fix iterations, rerunning artifact
      gates and recording review dispositions on the PR after each round.
- [x] Artifact feedback/fix iteration 1: preserve both JSON v1 applicability projections, make
      aggregation current-impact truthful, close lifecycle and identity boundaries, simplify
      duplicate vocabularies, strengthen README and live-test gates, and carry permanent collateral
      plus test-estate ownership. (2026-08-20: all accepted findings applied, gates passed, and
      focused private re-review reported no blocking or important findings. Its actual-resolution
      staging was later superseded by direct operator ruling.)
- [x] Artifact feedback/fix iteration 2: apply the direct operator ruling that global
      `--non-interactive` disables TTY interaction only, remove impact policy and staged authority
      turns from actual resolution, and prove preview impact is orthogonal to exact TTY access.
      (2026-08-21: added least-authority TTY broker capability, decoupled presentation, removed
      resolution staging, passed artifact gates, and received a clean focused private re-review.)
- [x] Apply the separately authorized post-iteration traceability amendment after final Muntz
      review: record that the 2026-08-21 operator ruling supersedes the seed's general unattended
      fail-fast requirement and that this effort defines no general unattended-resolution mode.
- [x] Confirm the design remains within the accepted FRD and that review has not introduced an
      unnecessary compatibility track or a secret-backend version other than `1`. (2026-08-21:
      confirmed; the dated direct operator ruling remains authoritative.)

### Artifact definition of done

- Every FRD requirement maps to architecture, detailed design, plan work, and an objective test or
  review condition.
- Preview caller semantics contain one impact input and no certainty policy; actual resolution
  contains no impact input.
- Maximum-impact no-indeterminate behavior, best effort, missing TTY, backend-local discard, derived
  hints, and the absence of free-form backend text are unambiguous across all artifacts.
- Global `--non-interactive` means only "do not use the TTY for interactions, even if one is
  present" and does not suppress out-of-band provider work or alter presentation.
- Ordinary missing falls through, blocked retains exhaustion evidence, and failed hard-stops.
- No-candidate exhaustion is blocked/no-candidate rather than a synthetic lookup miss.
- Preflight's existing use of preview and its attempt-aware treatment of higher-precedence
  indeterminate evidence are explicit without distorting the aggregate.
- Reviewers report no unresolved critical or high-severity design finding.

## Requirement traceability

| Requirements | Design authority                                      | Planned proof                                          |
| ------------ | ----------------------------------------------------- | ------------------------------------------------------ |
| R1, R3, R4   | preview LLD result sum and backend algorithm          | phases 1 to 3 contract and backend tests               |
| R2           | HLA operator-impact policy                            | phases 1 and 3 exact-input and fixed-caller tests      |
| R5, R6       | HLA execution capabilities and prompt LLD             | phases 2 and 4 TTY cross-product tests                 |
| R7           | HLA security boundary and preview LLD leak checks     | phases 2 and 6 sentinel scans                          |
| R8           | HLA and preview LLD OnePassword behavior              | phases 2 and 5 config, schema, and fake-provider tests |
| R9, R10      | preview LLD result shapes and exhaustive flow         | phases 1, 3, and 5 shape, flow, and renderer tests     |
| R11          | operator-surfaces LLD caller table                    | phases 3 and 5 caller and exit-status tests            |
| R12          | operator-surfaces LLD policy derivation               | phase 4 CLI-root and propagation sweep                 |
| R13, R14     | HLA and preview LLD aggregation and mapping semantics | phases 2 and 3 precedence and classification matrix    |
| R15          | migration strategy atomic rewrite                     | phases 2 and 3 conformance and residual sweeps         |
| R16          | operator-surfaces LLD human and JSON contracts        | phases 5 and 6 structured-output checks                |
| R17          | operator-surfaces LLD permanent collateral inventory  | phase 5 docs, schema, and completion gates             |
| R18          | operator-surfaces LLD JSON contract                   | phases 3, 5, and 6 frozen-field and additive checks    |

## Phase 1: Contract types and validation scaffolding

- [x] Add preview-only `OperatorImpact`, exact `TtyInteractionPolicy` and `TtyInteractionAccess`,
      exact `supports_tty_interaction`, tagged client intent, lookup-description, closed reason
      enums, tagged preview results, and aggregate attempt types.
- [x] Add the redacted resolved, missing, blocked, and failed actual-resolution variants.
- [x] Add constructor invariants, exact-map validators, safe representations, exhaustive
      variant-to-flow rules, backend-returnable reason subsets, and maximum-impact no-indeterminate
      enforcement.
- [x] Make no-candidate a dedicated aggregate-only variant and revalidate backend-produced secret,
      source, and identifier text at every core wrapper boundary, including hostile control and
      format characters.
- [x] Reject preview-only results and reasons from actual resolution, and reject
      `PreviewIndeterminate` under maximum preview impact as a backend protocol failure.
- [x] Add focused tests for adversarial values, provider text, malformed maps, invalid variants or
      reasons, partial values, and safe representations without changing the live descriptor.
- [x] Replace the legacy runtime resolution category/detail/remediation enums with the same final
      resolved, missing, blocked, and failed vocabulary used by the new source results; retain old
      fields only where a frozen machine schema requires a derived projection.

### Phase 1 definition of done

- The final closed types and validators can be exercised independently of a backend.
- Invalid result shapes fail closed before any value can be copied or rendered.
- The existing version-2 descriptor remains the sole runtime contract until the atomic cutover.

## Phase 2: Backend vertical slices

- [x] Set exact TTY-broker capability on every backend: prompt `True`, env-var and OnePassword
      `False`; prove the latter two receive no broker in any intent or TTY state.
- [x] Refactor env-var acquisition so preview validates and discards locally while resolution uses
      the same private path, behind the current runtime contract.
- [x] Implement prompt TTY-first preview and resolution behavior; prove no broker call or stdin read
      occurs unless TTY use is allowed and usable input is present.
- [x] Add the OnePassword app-authentication impact config and generated schema representation.
- [x] Infer known unattended OnePassword modes where provider facts make that safe; apply the
      conservative source setting otherwise.
- [x] Refactor the bounded `op read` path so preview and resolution share timeout, cleanup, value
      validation, and native-failure classification while returning different boundary types.
- [x] Add fake-provider tests for success, invalid mapping, auth, connectivity, external failure,
      timeout, interruption, source config, and sentinel containment.
- [x] Add backend-specific decision-table tests proving each in-tree backend exhausts every
      permitted route before returning indeterminate; generic conformance validates shape and
      maximum-impact behavior without pretending to know arbitrary provider internals.
- [x] Prove generic valid absence returns missing and falls through, while invalid mapping,
      lookup-rejected, and provider failures return failed and hard-stop.
- [x] Apply the explicit OnePassword evidence fork: with authorized conclusive evidence, record the
      exact supported `op` version and sanitized narrow absence token, add its fixture, and
      reproduce it live; without that evidence, implement no OnePassword missing token, keep
      item/field markers failed/lookup-rejected, and keep unknown text failed/external. Fake output
      is never evidence.
- [x] Prove factory construction and context entry perform no provider or broker work before the
      selected method acts through a client constructed with tagged operation intent and exact TTY
      access.

### Phase 2 definition of done

- All three in-tree backends have private acquisition and final-result seams ready for one atomic
  contract cutover, while legacy public methods still drive runtime.
- Focused final-seam tests prove actual OnePassword resolution runs without a TTY and under global
  `--non-interactive`, while prompt never reads without allowed, usable TTY access.
- Preview and resolution parity tests cover every shared provider classification.

## Phase 3: Atomic contract cutover and core orchestration

- [x] Remove the no-op client `prepare` method and `external_operation_timeout`; extend
      `create_client` with tagged preview-or-resolution intent, exact TTY access, broker, and
      remaining-time inputs before any backend lifecycle hook runs. Keep policy out of
      `preview(requests)` and `resolve(requests)` because it is fixed at construction.
- [x] Apply the 2026-08-21 operator-approved simplification that supersedes the dead factory inputs:
      remove `source_name`, `remaining_time`, `RemainingTime`, and `_MonotonicBudget`; keep source
      identity in core and the one real shrinking OnePassword timeout in source config. (Implemented
      in `da5f1e6a`.)
- [x] Require every concrete secret backend to declare contract version `1`; leave the abstract
      capability annotation-only and add the missing-declaration conformance guard. (Implemented in
      `e679ac38`.)
- [x] Update the `SecretBackend` ABC, descriptor, root exports, and all implementations atomically,
      resetting the exact secret-backend contract sentinel from `2` to `1` while removing
      `interactive`, adding exact `supports_tty_interaction`, and removing `would_attempt`.
- [x] Remove backend-selected failure/remediation exceptions, return exact resolution blocks, and
      derive exception class and guidance directly from the final tag, reason, and backend identity
      in core. Preserve the 1Password pending-approval timeout hint without a backend hint channel.
- [x] Update registration and runtime conformance for method shapes, exact result maps, legal result
      variants and reasons, construction/entry intent, actual-resolution impact absence, and
      maximum-preview-impact no-indeterminate behavior. Reject TTY blocks from backends that declare
      no TTY support.
- [x] Add direct preview and actual-resolution adversarial guards proving that a backend which
      declares no TTY support cannot return either TTY block reason without a protocol failure.
      (Implemented in `e679ac38`.)
- [x] Update both backend-authoring READMEs with a complete rewritten example and conformance rules.
- [x] Make `cli/agentworks/capabilities/secret_backend/README.md` the self-contained permanent
      contract authority for exact variants, reason ownership, core flow, preview impact, TTY broker
      capability and access rules, lifecycle constraints, the exact TTY-only meaning of
      `--non-interactive`, value containment, and conformance; it must not rely on this SDD.
- [x] Run a named manual README contract gate covering exact version-1 signatures, every variant and
      legal reason owner, one normative core-flow section, impact/TTY matrix, broker-capability
      rules, lifecycle and exception boundaries, exact-map and maximum-impact constraints, value
      containment, and a complete conforming example. Prove it has no SDD links and behaviorally
      test the example shape without asserting authored prose.
- [x] Replace `_lookup_projection` and `would_attempt` use with structured static lookup
      descriptions.
- [x] Replace both legacy policy-free and operation-policy preview helpers with one impact-explicit,
      TTY-access-explicit preview batch boundary; leave static inspection on `describe_lookup` only.
- [x] Implement operation-bounded batch preview over active sources with the existing source-turn
      budget and cleanup discipline.
- [x] Implement current-impact preview aggregation with ordinary-missing and execution-block
      fallthrough, failed hard-stop, and ordered earlier-indeterminate evidence.
- [x] Align exhausted preview diagnostics with actual resolution: first indeterminate where legal,
      then TTY block, ordinary missing, readiness or disabled-plugin block, and no-candidate; cover
      both paths with an adversarial category matrix, and make no-candidate guidance cover either an
      absent active source or an absent applicable mapping without expanding the result contract.
      (Implemented in `e679ac38`.)
- [x] Implement actual resolution as one bounded source-first pass with one batch per ready source,
      ordinary-missing and TTY-block fallthrough, failed hard-stop, and no preview reuse.
- [x] Restore complete-batch fail-before-interaction without restoring actual-resolution impact:
      recheck static remaining viability before later provider turns, stop after any hard terminal
      result, emit the core-only `batch-doomed-before-interaction` block for skipped unresolved
      names, and keep explicit partial reveal independent. Static viability uses mapping
      applicability, folded readiness, and plugin enablement only. Core passes TTY access to the
      backend but never uses it as a viability prediction. (Implemented in `dda1f055`.)
- [x] Add event-ledger tests for source-first batching, higher-source failure preventing lower
      provider invocation, earlier-indeterminate/later-available or failed preview aggregation,
      adversarial maximum-impact indeterminate becoming failed/backend-protocol, and actual
      resolution never receiving impact policy.
- [x] Emit aggregate blocked/no-candidate with no runtime attempts when no candidate lookup ran;
      never synthesize missing for that condition.
- [x] Make preflight preview impact fixed at `NONE`, accept available and indeterminate, reject
      missing and blocked, and reject failed unless an earlier higher-precedence attempt is
      indeterminate.
- [x] Add a lazy command-scoped preflight memo keyed by secret name and prove repeated references
      cause one preview without changing first-failing node or reference order.
- [x] Add an eager-full-union mutation test proving a secret referenced only by an unreachable later
      node causes zero preview calls, provider reads, and audit events.
- [x] Prove actual value-bearing resolution still completes before each command's first external
      mutation even when preflight returned indeterminate.
- [x] Update doctor to consume non-disruptive preview and represent uncertainty without treating it
      as backend readiness failure.
- [x] Pin doctor's exact aggregate mapping: available is `OK`; missing, indeterminate, and blocked
      are `WARN`; failed is `FAIL`. Add optional closed `secret_preview` on secret JSON checks and
      cover all five statuses, counts, exit behavior, earlier-indeterminate/later-available, and
      earlier-indeterminate/later-failed.

### Phase 3 definition of done

- A later current-impact success or hard failure is the aggregate; every earlier indeterminate
  attempt remains visible as ordered precedence evidence.
- A failed configured source cannot be hidden by a lower-precedence source or downgraded in doctor.
- Every in-tree backend implements the sole version-1 contract without a core-side value-returning
  preview adapter or legacy runtime branch.
- Missing mappings and blocked chains fail preflight. Failed chains fail unless an earlier
  higher-precedence attempt is indeterminate.
- Preflight never authorizes operator impact, repeats a secret probe, or substitutes for resolution.

## Phase 4: Policy propagation and operation boundaries

- [x] Rename broad `InteractionPolicy` to exact `TtyInteractionPolicy` across production and test
      call sites, preserving early validation and explicit forwarding.
- [x] Keep the 100-plus-file policy rename in one mechanical commit so review can separate it from
      the behavioral contract changes.
- [x] Define global `--non-interactive` everywhere as "do not use the TTY for interactions, even if
      one is present" and derive only `TtyInteractionPolicy.REFUSE` from it.
- [x] Add the flag/physical-TTY cross product at the CLI and service seams: the flag yields
      `DISABLED` regardless of attachment; otherwise usable input yields `AVAILABLE` and absent
      input yields `UNAVAILABLE`.
- [x] Remove every remaining static interactive-source skip and pass preview impact only for
      preview, tagged operation intent for every client, and exact TTY access from service roots.
- [x] Construct prompt brokers only when TTY access is available; leave out-of-band backends
      independent of the broker and global flag through exact `supports_tty_interaction`.
- [x] Remove global-flag color and presentation gating. Preserve color decisions based on output
      stream capability and existing presentation controls, with parity tests on the same stream.
- [x] Sweep all resolving commands and service entry points for exact policy propagation and
      fail-before-mutation ordering.

### Phase 4 definition of done

- The original non-TTY OnePassword scenario succeeds without a plaintext environment workaround.
- Global `--non-interactive` is a truthful TTY-only control and never promises fully unattended
  execution.
- The flag has no presentation side effect; color and formatting follow their own existing inputs.
- Prompt cannot hang a non-TTY invocation.
- OnePassword actual resolution runs with and without global `--non-interactive`.
- No broad `InteractionPolicy`, backend `interactive`, or runtime `would_attempt` authority remains.

## Phase 5: Operator surfaces and collateral

- [x] Add `secret describe --allow-interaction`, help, introspection, and completions; permit it
      with global `--non-interactive` because preview impact and TTY use are orthogonal.
- [x] Render default describe tagged results and closed reasons; prove maximum-impact results
      contain no indeterminate status.
- [x] Reimplement `secret verify` on backend preview, preserving refusal-by-default, name
      deduplication, stable ordering, full-table rendering, and exit status.
- [x] Update `secret list` to use structured static mapping disposition without provider I/O.
- [x] Add the optional nested `secret describe` preview JSON projection while preserving every JSON
      v1 field, type, enum meaning, and collection order; update `cli/command-reference.md`.
- [x] Preserve `secret list --output json`'s `secrets[].sources[].would_attempt` and describe's
      `source_mappings[].would_attempt` as structured-disposition compatibility projections.
- [x] Update CLI README, secrets README, both backend and general plugin-authoring READMEs,
      resources guide, relevant guide topics, sample config, schema snapshots, and generated
      completions in lockstep.
- [x] Update `cli/agentworks/secrets/guide-content/secrets.md` so its consent paragraph covers both
      impact-bearing describe and verify; update `docs/adrs/0013-cli-side-secret-injection.md` so
      its expected workflow uses the configured source instead of teaching `op run`/env-var
      hand-carrying.
- [x] Update the exact stale CLI README teaching around current non-TTY policy and retired
      `would attempt` vocabulary, plus secret describe's static fallthrough wording.
- [x] Remove stale claims that preview is pure, doctor never performs a provider read, or TTY grants
      general interaction consent.
- [x] Update every global flag help/reference surface to state the exact TTY-only meaning and remove
      claims that the flag itself selects plain or colorless output.
- [x] Add one explicit operator-facing behavior-change callout to the upgrade guide: the flag now
      disables TTY use only, no longer controls presentation, does not prevent out-of-band provider
      approval or waiting, and is not an unattended fail-fast guarantee; name unattended source and
      authentication alternatives. (Implemented in `e679ac38`.)
- [x] Trim the secret-backend and secrets test estates to the simplification sweep standard as part
      of this rewrite, deleting worthless tests rather than assigning them to a later cleanup.
- [x] Close the formal integration collateral and coverage findings: stabilize doctor checks by
      structured secret identity, document doctor `secret_preview`, correct orchestration's stale
      pure-preview claim, retain the frozen JSON v1 `refused-interaction` vocabulary, and add
      compact renderer, verify exit-status, and real CLI prompt-broker coverage. (Implemented in
      `dda1f055`.)

### Phase 5 definition of done

- Every diagnostic names an available control for its actual caller.
- Backend result tags and reasons are closed and value-free; core owns all rendered guidance and
  flow.
- Permanent docs are sufficient without this SDD and match shipped behavior.
- The secret-backend README independently teaches the whole author contract and includes a complete
  conforming example.

## Phase 6: Validation, review, and live testing

- [x] Run focused unit and integration tests after each vertical slice. (2026-08-21: final
      adversarial correction gate passed 401 tests.)
- [x] Run the repository's required Python lint, format, type, non-live test, SDD lock, docs,
      schema, Rulesync, website, and deterministic-build gates. (2026-08-21: final implementation
      head passed 7,209 non-live tests with one skip; Ruff, Mypy, file lint, lock, Rulesync,
      generated-package, website, and both deterministic-build variants passed.)
- [x] Run manual em-dash, double-dash punctuation, value-leak, and stale-vocabulary scans.
      (2026-08-21: final implementation scans passed; the remaining string assertion covers a
      provider-supplied containment sentinel rather than authored prose.)
- [x] Rerun the complete implementation and repository gates after removing the dead factory inputs.
      (2026-08-21: focused contract coverage passed 319 tests; full non-live passed 7,223 with one
      skip; Ruff, format, Mypy, file lint, lock, locked-SDD, Rulesync, generated-package, Typer
      isolation, website Python and Node, and both deterministic-build variants passed.)
- [x] Rerun the complete implementation and repository gates after feedback/fix round 1.
      (2026-08-21: focused coverage passed 227 tests; full non-live passed 7,239 with one skip;
      Ruff, format, Mypy, file lint, lock, locked-SDD, Rulesync, generated-package, Typer isolation,
      website Python and Node, and both deterministic-build variants passed.)
- [x] Correct the round-1 exact-head documentation findings: keep the new exhaustion precedence in
      the effort-owned design rather than the accepted FRD, reconcile the numbered LLD algorithm,
      and preserve the earlier completed plan checkbox verbatim.
- [x] Run the complete implementation and repository gates after formal-integration feedback/fix
      round 2, then obtain clean reviewer-of-record and fresh-eyes dispositions on the exact head.
      (2026-08-21: implementation gates passed at `dda1f055`: focused 164 tests; full non-live 7,258
      tests with one skip; Ruff, format, Mypy, file lint, lock, locked-SDD, Rulesync,
      generated-package, Typer isolation, website Python and Node, and both deterministic-build
      variants passed. Reviewer-of-record and fresh-eyes passes reported no material finding at
      exact head `0a4d427d`; their independent focused gates passed 176 and 154 tests, respectively,
      and the fresh-eyes full non-live rerun reproduced 7,258 tests with one skip.)
- [x] Obtain a private `agentworks-reviewer` pass on the complete diff and resolve material
      findings. (2026-08-21: three bounded correction rounds closed lifecycle, timeout, TTY-access,
      exact-map, static-description, cleanup, documentation, and test-quality findings; final exact
      head `1a97e58b` was clean.)
- [x] Request fresh-eyes review after the last material implementation change. (2026-08-21: the
      final pass independently reproduced protected-exit and cleanup precedence and reported exact
      head `1a97e58b` clean.)
- [x] Obtain clean reviewer-of-record and fresh-eyes dispositions on the simplified exact head.
      (2026-08-21: both lanes reported exact head `a86d8a11` clean after the consolidated feedback
      round and final documentation reconciliation.)
- [x] Request the separately operated `agentworks-tester` with the approved environment inventory,
      naming prefix, resource budget, and scoped charter; the effort lead does not duplicate its
      integration run. (2026-08-21: ready head `6964bf21` triggered the pipeline, then returned to
      draft before a report when the operator accepted the Muntz simplification.)
- [x] Re-request the separately operated integration run on the simplified ready head. (2026-08-23:
      the tester completed two passes at exact runtime head `bf3a9a42`, including a real remote-Lima
      positive control, and reported no blocker.)
- [ ] Exercise at minimum non-TTY OnePassword-equivalent out-of-band behavior through the real CLI
      seam, prompt with and without a PTY, default and opted-in describe/verify, global
      `--non-interactive`, preflight indeterminate, missing-versus-failed fallback, and human/JSON
      value containment. On one fixed TTY output stream, prove the global flag does not change color
      or presentation.
- [ ] Use isolated `HOME`, scratch manifests, and a fake-only `op` path for doctor, preflight,
      describe, verify, aggregation, and leak campaigns. Do not traverse broad operator config or
      run broad doctor against a real provider.
- [ ] Exercise an ordinary resolving surface such as seeded `agw env show --vm ... --resolve` with
      non-TTY stdin: OnePassword invokes the fake provider both without and with global
      `--non-interactive`; prompt performs no broker or stdin access in either non-TTY or disabled
      TTY states.
- [x] Limit any real OnePassword exercise to one dedicated authorized reference, record provider
      audit activity as an expected external effect, and never expose a real value. Treat
      desktop-app approval for AC1 separately from error-token evidence; if unavailable, record it
      untested and obtain explicit operator acceptance before readying the PR. (2026-08-23: no real
      OnePassword credential was used; the operator accepted that documented boundary after the
      no-finding live report.)
- [ ] Exercise supported `op` error classification against an authorized real provider when the
      environment permits, record only sanitized value-free tokens, and retain fail-closed behavior
      plus an explicit test disposition when ordinary absence cannot be proven safely.
- [x] Clean up tester-created resources independently of test success and restore or document the
      state snapshot disposition. (2026-08-23: the isolated home was removed, operator config was
      unchanged, the VM and workspace were deleted, remote Lima had no instance, and no scratch
      config or workspace artifact remained. The expected offline tailnet record from a
      non-ephemeral key was documented.)
- [x] Treat integration findings as input, fix only within authenticated scope, and obtain operator
      disposition for any skipped, inconclusive, or environment-blocked critical scenario.
      (2026-08-23: the tester and saga lead reported no blocker; the operator accepted the report
      and directed closeout in this PR.)
- [x] Record the final live-evidence disposition without claiming unexecuted scenarios. The same
      capture mechanism and sentinel detector proved eight preview surfaces silent. The intended
      `env show --resolve` surface revealed the sentinel on a real VM. Non-TTY prompt, JSON shape,
      real provisioning, and cleanup passed. Real OnePassword authentication and error-token
      classification plus the fixed TTY color-parity check were not exercised live and are accepted
      documented test limits.

### Phase 6 definition of done

- Required gates pass at the final commit.
- Independent reviewers have no unresolved critical or high-severity finding.
- Live tests provide evidence for the original failure path, TTY safety, the exact TTY-only global
  mode, maximum-preview-impact no-indeterminate behavior, hard-stop failure, and value containment,
  or the operator explicitly accepts a documented gap.
- The draft PR body and comments contain no credentials, provider output, or secret values.

## Phase 7: Closeout and ready-for-merge handoff

- [x] Rebase or merge current `origin/main` as appropriate, rerun affected gates, and confirm the PR
      diff contains no unrelated user work. (2026-08-21: rebased onto `d83bfa71`; file, SDD lock,
      Rulesync, and diff gates passed; the complete diff is scoped to this child effort.)
- [x] Refresh the baseline again before the revised ready signal. (2026-08-21: rebased onto
      `3912ee65`; intervening changes touched only the parent saga and instance-model SDD seed;
      range-diff preserved all 26 child-effort patches, and file, SDD lock, Rulesync, and diff gates
      passed.)
- [x] Reconcile every FRD acceptance criterion against code, tests, docs, and the evidence available
      before formal integration. (2026-08-21: code and non-live evidence cover the closed contract,
      source flow, TTY policy, provider classification, operator surfaces, and value containment;
      AC1, AC3, and the live portion of AC10 remain assigned to formal integration.)
- [x] Update this plan truthfully without changing any completed checkbox that has merged to main.
      (2026-08-23: completed live work is checked, unexecuted provider-specific scenarios remain
      unchecked, and the operator's acceptance is recorded separately.)
- [x] Remove draft status when the complete, green, independently reviewed implementation is
      intended to merge as-is; that transition requests the separately operated integration run.
      (2026-08-21: handed off `6964bf21`, then returned to draft under authenticated operator
      direction for the dead-parameter removal.)
- [x] Reapply the ready signal after the operator-approved contract simplification is complete,
      green, and independently reviewed. (2026-08-22: exact head `bf3a9a42` was handed off after
      both feedback/fix rounds and the 45-minute collection window.)
- [x] After integration disposition, return the PR to draft before any closeout mutation, then
      create `locked.md` with the final implementation summary, design deltas, validation evidence,
      review disposition, and remaining known limitations. (2026-08-23.)
- [ ] Reapply the ready signal for the exact closeout head; the integration pipeline may cite the
      preceding live run when the runtime diff is byte-identical.
- [ ] Hand the ready PR to the operator with the atomic-rewrite ruling, review rounds, gate results,
      live-test evidence, and any residual risk. Do not merge it.

## Final definition of done

- Every acceptance criterion in the FRD is satisfied.
- The rewritten contract is the sole runtime and documented secret-backend contract. Its descriptor
  and implementations declare version `1`, with no compatibility branch.
- Ordinary commands use configured out-of-band secret providers regardless of TTY state or global
  `--non-interactive`; prompt remains TTY-safe, and the global flag disables only terminal
  interaction without changing presentation.
- Preview is best-effort within impact, tagged as available/missing/indeterminate/blocked/failed,
  contains no indeterminate result at maximum impact, and is value-free across the backend boundary.
- The PR is ready to merge, fully reviewed, fully gated, live-tested or given an explicit operator
  disposition, and not merged by the effort lead.
