# Plan: Value-free secret resolution preview

- Status: Draft for review
- Date: 2026-08-18
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
- The same PR becomes ready only after implementation, full gates, independent code review, live
  integration disposition, permanent collateral, and closeout are complete.
- The effort lead does not merge its own PR.

## Phase 0: Artifact checkpoint

- [x] Verify the feature branch is based on current `origin/main` and record the implementation
      baseline in the migration artifact. (2026-08-18: baseline `c01263d0`.)
- [x] Complete FRD, prior-art research, HLA, preview-contract LLD, operator-surfaces LLD, migration
      strategy, and this tracked plan. (2026-08-18.)
- [x] Run artifact lint, spelling, link, lockfile, Rulesync, and manual typography checks.
      (2026-08-18: all passed locally; the lockfile gate is rerun against the committed diff before
      handoff.)
- [x] Obtain a private `agentworks-reviewer` pass and resolve material findings before public
      handoff. (2026-08-18: JSON v1, exact resolution flow, pre-lifecycle authority, batch doom,
      memoization, sequencing, and collateral findings resolved; final focused pass had no
      findings.)
- [x] Amend the design with the operator-approved tagged result model: ordinary missing,
      impact-limited indeterminate, execution blocked, and hard-stop failed are distinct.
      (2026-08-19.)
- [x] Rerun the private artifact gate after the tagged-result amendment and resolve material
      findings before public handoff. (2026-08-19: corrected no-candidate truthfulness, maximum-impact
      actual-resolution validation, aggregate-only preflight, doctor status mapping, and fail-closed
      OnePassword evidence requirements; focused re-review had no findings.)
- [ ] Commit the artifact set with the session trailer, push the feature branch, open one draft PR,
      and apply `saga:next-steps` and `review-requested`.
- [ ] Request and collect the three named artifact perspectives: saga lead contract review, Muntz
      complexity review, and integration tester testability review.
- [ ] Apply no more than two pre-authorized artifact feedback/fix iterations, rerunning artifact
      gates and recording review dispositions on the PR after each round.
- [ ] Confirm the design remains within the accepted FRD and that review has not introduced an
      unnecessary compatibility track or a secret-backend version other than `1`.

### Artifact definition of done

- Every FRD requirement maps to architecture, detailed design, plan work, and an objective test or
  review condition.
- Caller semantics contain one impact input and no certainty policy.
- Maximum-impact no-indeterminate behavior, best effort, missing TTY, backend-local discard, derived
  hints, and the absence of free-form backend text are unambiguous across all artifacts.
- Ordinary missing falls through, blocked retains exhaustion evidence, and failed hard-stops.
- No-candidate exhaustion is blocked/no-candidate rather than a synthetic lookup miss.
- Preflight's existing use of preview and its treatment of indeterminate are explicit.
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

## Phase 1: Contract types and validation scaffolding

- [ ] Add `OperatorImpact`, exact validation, `TerminalAvailability`, lookup-description, closed
      reason enums, tagged preview results, and aggregate attempt types.
- [ ] Add the redacted resolved, missing, blocked, and failed actual-resolution variants.
- [ ] Add constructor invariants, exact-map validators, safe representations, exhaustive
      variant-to-flow rules, backend-returnable reason subsets, and maximum-impact no-indeterminate
      enforcement.
- [ ] Reject `BackendBlocked(OPERATOR_IMPACT_LIMITED)` from an `ALLOW` resolution turn as a
      whole-turn backend protocol failure so an authority frontier cannot repeat.
- [ ] Add focused tests for adversarial values, provider text, malformed maps, invalid variants or
      reasons, partial values, and safe representations without changing the live descriptor.

### Phase 1 definition of done

- The final closed types and validators can be exercised independently of a backend.
- Invalid result shapes fail closed before any value can be copied or rendered.
- The existing version-2 descriptor remains the sole runtime contract until the atomic cutover.

## Phase 2: Backend vertical slices

- [ ] Refactor env-var acquisition so preview validates and discards locally while resolution uses
      the same private path, behind the current runtime contract.
- [ ] Implement prompt terminal-first preview and resolution behavior; prove no broker call or stdin
      read occurs without both authority and terminal capability.
- [ ] Add the OnePassword app-authentication impact config and generated schema representation.
- [ ] Infer known unattended OnePassword modes where provider facts make that safe; apply the
      conservative source setting otherwise.
- [ ] Refactor the bounded `op read` path so preview and resolution share timeout, cleanup, value
      validation, and native-failure classification while returning different boundary types.
- [ ] Add fake-provider tests for success, invalid mapping, auth, connectivity, external failure,
      timeout, interruption, source config, and sentinel containment.
- [ ] Prove valid provider absence returns missing and falls through, while invalid mapping and
      provider failures return failed and hard-stop.
- [ ] Record sanitized real-provider evidence for the supported `op` version before classifying any
      external token as ordinary absence. Keep ambiguous item/field not-found text fail-closed as
      invalid mapping, and match recorded tokens narrowly in regression tests.
- [ ] Prove factory construction and context entry perform no provider or broker work before the
      selected method receives the exact impact and terminal facts.

### Phase 2 definition of done

- All three in-tree backends have private acquisition and final-result seams ready for one atomic
  contract cutover, while legacy public methods still drive runtime.
- Focused final-seam tests prove OnePassword can run from a non-TTY process when impact permits it,
  while prompt never reads without a TTY.
- Preview and resolution parity tests cover every shared provider classification.

## Phase 3: Atomic contract cutover and core orchestration

- [ ] Remove the no-op client `prepare` method and extend `create_client`, `preview`, and `resolve`
      with exact impact and terminal inputs before any backend lifecycle hook runs.
- [ ] Update the `SecretBackend` ABC, descriptor, root exports, and all implementations atomically,
      resetting the exact secret-backend contract sentinel from `2` to `1` while removing
      `interactive` and `would_attempt`.
- [ ] Remove backend-selected failure/remediation exceptions, return exact resolution blocks, and
      derive existing resolution categories and remediation in core.
- [ ] Update registration and runtime conformance for method shapes, exact result maps, legal result
      variants and reasons, construction/entry authority, and maximum-impact no-indeterminate
      behavior.
- [ ] Update both backend-authoring READMEs with a complete rewritten example and conformance rules.
- [ ] Make `cli/agentworks/capabilities/secret_backend/README.md` the self-contained permanent
      contract authority for exact variants, reason ownership, core flow, impact and terminal rules,
      lifecycle constraints, value containment, and conformance; it must not rely on this SDD.
- [ ] Replace `_lookup_projection` and `would_attempt` use with structured static lookup
      descriptions.
- [ ] Implement operation-bounded batch preview over active sources with the existing source-turn
      budget and cleanup discipline.
- [ ] Implement precedence-aware tagged aggregation, ordinary-missing and execution-block
      fallthrough, failed hard-stop, ordered attempt retention, and iterative no-impact closure plus
      before-every-`ALLOW` complete-batch doom checks.
- [ ] Emit aggregate blocked/no-candidate with no runtime attempts when no candidate lookup ran;
      never synthesize missing for that condition.
- [ ] Make preflight preview impact fixed at `NONE`, accept available and indeterminate, and reject
      missing, blocked, or failed with structured context.
- [ ] Add a lazy command-scoped preflight memo keyed by secret name and prove repeated references
      cause one preview without changing first-failing node or reference order.
- [ ] Prove actual value-bearing resolution still completes before each command's first external
      mutation even when preflight returned indeterminate.
- [ ] Update doctor to consume non-disruptive preview and represent uncertainty without treating it
      as backend readiness failure.
- [ ] Pin doctor's exact aggregate mapping: available is `OK`; missing, indeterminate, and blocked
      are `WARN`; failed is `FAIL`. Cover counts, JSON status, and exit behavior, including an
      earlier indeterminate followed by a retained failure.

### Phase 3 definition of done

- Earlier indeterminate precedence cannot be hidden by later source success or failure.
- A failed configured source cannot be hidden by a lower-precedence source.
- Every in-tree backend implements the sole version-1 contract without a core-side value-returning
  preview adapter or legacy runtime branch.
- Missing mappings, blocked chains, and failed chains fail preflight.
- Preflight never authorizes operator impact, repeats a secret probe, or substitutes for resolution.

## Phase 4: Policy propagation and operation boundaries

- [ ] Replace `InteractionPolicy` with exact `OperatorImpact` across production and test call sites,
      preserving early validation and explicit forwarding.
- [ ] Derive ordinary CLI impact only from global `--non-interactive`, never TTY.
- [ ] Remove every remaining static interactive-source skip and pass impact and terminal facts from
      service roots to the cut-over source clients.
- [ ] Construct prompt brokers only when impact and terminal capability permit; leave out-of-band
      backends independent of the broker.
- [ ] Sweep all resolving commands and service entry points for exact policy propagation and
      fail-before-mutation ordering.

### Phase 4 definition of done

- The original non-TTY OnePassword scenario succeeds without a plaintext environment workaround.
- Global `--non-interactive` remains the truthful unattended control.
- Prompt cannot hang a non-TTY invocation.
- No `InteractionPolicy`, backend `interactive`, or runtime `would_attempt` authority remains.

## Phase 5: Operator surfaces and collateral

- [ ] Add `secret describe --allow-interaction`, its global-mode conflict, help, introspection, and
      completions.
- [ ] Render default describe tagged results and closed reasons; prove maximum-impact results
      contain no indeterminate status.
- [ ] Reimplement `secret verify` on backend preview, preserving refusal-by-default, name
      deduplication, stable ordering, full-table rendering, and exit status.
- [ ] Update `secret list` to use structured static mapping disposition without provider I/O.
- [ ] Add the optional nested `secret describe` preview JSON projection while preserving every JSON
      v1 field, type, enum meaning, and collection order; update the machine-output reference.
- [ ] Update CLI README, secrets README, both backend and general plugin-authoring READMEs,
      resources guide, relevant guide topics, sample config, schema snapshots, and generated
      completions in lockstep.
- [ ] Remove stale claims that preview is pure, doctor never performs a provider read, or TTY grants
      general interaction consent.

### Phase 5 definition of done

- Every diagnostic names an available control for its actual caller.
- Backend result tags and reasons are closed and value-free; core owns all rendered guidance and
  flow.
- Permanent docs are sufficient without this SDD and match shipped behavior.
- The secret-backend README independently teaches the whole author contract and includes a complete
  conforming example.

## Phase 6: Validation, review, and live testing

- [ ] Run focused unit and integration tests after each vertical slice.
- [ ] Run the repository's required Python lint, format, type, non-live test, SDD lock, docs,
      schema, Rulesync, website, and deterministic-build gates.
- [ ] Run manual em-dash, double-dash punctuation, value-leak, and stale-vocabulary scans.
- [ ] Obtain a private `agentworks-reviewer` pass on the complete diff and resolve material
      findings.
- [ ] Request fresh-eyes review after the last material implementation change.
- [ ] Snapshot live Agentworks state before any tester mutation and invoke `agentworks-tester` with
      the approved environment inventory, naming prefix, resource budget, and scoped charter.
- [ ] Exercise at minimum non-TTY OnePassword-equivalent out-of-band behavior through the real CLI
      seam, prompt with and without a PTY, default and opted-in describe/verify, global
      `--non-interactive`, preflight indeterminate, missing-versus-failed fallback, and human/JSON
      value containment.
- [ ] Exercise supported `op` error classification against an authorized real provider when the
      environment permits, record only sanitized value-free tokens, and retain fail-closed behavior
      plus an explicit test disposition when ordinary absence cannot be proven safely.
- [ ] Clean up tester-created resources independently of test success and restore or document the
      state snapshot disposition.
- [ ] Treat integration findings as input, fix only within authenticated scope, and obtain operator
      disposition for any skipped, inconclusive, or environment-blocked critical scenario.

### Phase 6 definition of done

- Required gates pass at the final commit.
- Independent reviewers have no unresolved critical or high-severity finding.
- Live tests provide evidence for the original failure path, TTY safety, explicit unattended mode,
  maximum-impact no-indeterminate behavior, hard-stop failure, and value containment, or the
  operator explicitly accepts a documented gap.
- The draft PR body and comments contain no credentials, provider output, or secret values.

## Phase 7: Closeout and ready-for-merge handoff

- [ ] Reconcile every FRD acceptance criterion against code, tests, docs, and live evidence.
- [ ] Update this plan truthfully without changing any completed checkbox that has merged to main.
- [ ] Create `locked.md` with the final implementation summary, design deltas, validation evidence,
      review disposition, and remaining known limitations.
- [ ] Rebase or merge current `origin/main` as appropriate, rerun affected gates, and confirm the PR
      diff contains no unrelated user work.
- [ ] Remove draft status only when the complete diff is intended to merge as-is and apply the
      repository's ready-for-merge review signal.
- [ ] Hand the ready PR to the operator with the atomic-rewrite ruling, review rounds, gate results,
      live-test evidence, and any residual risk. Do not merge it.

## Final definition of done

- Every acceptance criterion in the FRD is satisfied.
- The rewritten contract is the sole runtime and documented secret-backend contract. Its descriptor
  and implementations declare version `1`, with no compatibility branch.
- Ordinary non-TTY commands can use permitted out-of-band secret providers, prompt remains TTY-safe,
  and global `--non-interactive` remains explicit and effective.
- Preview is best-effort within impact, tagged as available/missing/indeterminate/blocked/failed,
  contains no indeterminate result at maximum impact, and is value-free across the backend boundary.
- The PR is ready to merge, fully reviewed, fully gated, live-tested or given an explicit operator
  disposition, and not merged by the effort lead.
