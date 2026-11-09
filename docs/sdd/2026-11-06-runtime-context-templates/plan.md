# Runtime context templates: plan

Governing requirements: [FRD](frd.md). Architecture: [HLA](hla.md). Detailed contract:
[interpolation LLD](interpolation-lld.md). Background: [prior art](prior-art-research.md).
Lifecycle: [delivery](migration-strategy.md).

Delivery: one PR containing the full feature, full SDD, permanent documentation, and validation.
[PR 867](https://github.com/WayfarerLabs/agentworks/pull/867) contains the complete feature and SDD.
Completion follows the accepted namespaced contract and the operator rulings recorded in the FRD.
Transport observations are tracked separately in issue #873. Shell rendering preflight is
implemented and reviewed. Closeout records implementation completion separately from the ready-head
validation result; the lock takes effect at merge.

## Design checkpoint

- [x] Inspect current env, setup, command interpolation, and managed-home producers at a pinned
      head.
- [x] Research primary-source prior art and record its implications and limitations.
- [x] Draft FRD, architecture, interpolation contract, migration, and this plan.
- [x] Establish a permanent reference describing current runtime context and interpolation.
- [x] Run independent project and complexity reviews of the design checkpoint; disposition material
      findings and record the reviewed head and results here.
- [x] Obtain operator disposition of the draft requirements, including the recommended omission of
      ambient lookup and references between managed env entries.

## Implementation

- [x] Record the accepted namespaced-only contract and destination-focused design, with opaque
      secrets and no ambient lookup or dependencies between env entries.
- [x] Implement the pure shared parser with explicit literal escapes and field-aware errors; prove
      one-pass rendering, unknown/unavailable distinction, and malformed syntax behavior.
- [x] Supply consuming user, VM, agent, workspace, and session context using the existing shared
      model; compute `user_home_dir` from its Linux username through the managed-home helper shared
      with provisioning and setup, without duplicating home state in each context.
- [x] Render winning plaintext env entries after inheritance, stored overlays, and scope merging.
      Remove early session-only expansion and preserve secret collection and identity precedence.
- [x] Reuse plaintext rendering in offline `env show`, retaining winning scope provenance and
      default secret redaction. Prove parity without a transport or target activation.
- [x] Migrate existing command interpolation to the shared parser and catalogue. Verify every
      integration's literal workload fields and intentionally expanded extra args.
- [x] Cover VM/agent shell and exec, session create/start/restart, console extra panes, and setup
      composition, including new agents and contexts missing workspace/session values.
- [x] Check winning env references before workload secret prompts and mutations. Preflight shell's
      selected template rendering before workload/env secret resolution, including planned users and
      workspaces. Keep required-command checks in the existing readiness path, deferring a pending
      target's remote probe until it exists. Do not parse, dry-run, or predict command success.
- [x] Update `docs/arch/runtime-context.md` with the accepted vocabulary, grammar, availability,
      phase, consumer boundaries, examples, and how another consumer should adopt the renderer.
      Update schema-owned field descriptions, CLI reference, applicable guide signposts, sample
      config, and release guidance in the same implementation increment.

## Validation and delivery

- [x] Run focused behavior tests from the LLD vectors and required Python/file-quality checks.
- [x] Run the independent project and complexity lanes, plus the generic correctness lane for code,
      and resolve material findings under the development-process rules.
- [x] Load `integration-testing` and `agw-test-env` before PR validation/live testing; derive a
      bounded charter from the accepted change. Do not infer permission to probe live user
      environments.
- [x] Hand off one exact reviewed and validated state with its documentation and migration notes.
- [ ] Once implementation and documentation are complete, record truthful completion and add the
      lockfile. The permanent reference must stand alone if this entire SDD directory is deleted.

## Final review iterations

The operator authorizes up to three final PR review iterations. Mark the completed implementation
ready, then wait for the integration report before starting each subsequent correction round. Allow
up to four hours for the report. Record the exact reviewed head, findings, fixes, and evidence for
each iteration. Published feedback informs disposition; it does not expand the authorized scope.

## Implementation evidence

Private review head: `c124f36b90b81fe1c75e1f4276bb1ea04f59a70b`. Project, complexity, and generic
correctness lanes returned clean final verdicts. Modeled native arguments preserve literal data;
explicit extra arguments render before quoting. The optional required-workspace-root API change was
declined because both production callers supply it and the current behavior is sound.

Ruff and strict Mypy passed, with 886 files type checked. A real CLI smoke exercise used temporary
config/state and a hermetic child environment: three successful inspection calls, one expected
unavailable-context refusal, and zero transport attempts. It verified plaintext context, scope
precedence, reserved-prefix escaping, shell/Go literals, and opaque secret reveal with fixture data.
The full non-integration suite passed: 10,264 tests, with 11 skipped. File-quality, typer isolation,
rulesync drift, and locked-SDD checks passed. Website validation passed 160 Python tests, 103 Node
tests, and deterministic double builds at both supported site bases. The ready handoff records the
exact published head. Subsequent integration findings and the current acceptance gaps are recorded
below.

## Final review iteration 1

Ready head: `b927a0ff8207a7f49a16f527a8995859c8d6beba`, handed off on 2026-11-07. Muntz's report
accepted the namespace and identified stale resource guidance and avoidable command-output
rendering. The namespace-only contract remains as directed by the operator; no legacy detector is
added.

Integration reports on 2026-11-08 tested that exact head against `c8d4dfb3`: all required gates
passed, with 10,264 non-integration tests passed and 11 skipped. Live Linux/macOS and native Windows
evidence found an identity-precedence regression and the stale guide. Windows measured literal-env
cases passed; Linux/macOS literal delivery failed in inherited SSH SetEnv expansion. Their bounded
live and cleanup evidence is in PR comments 6719400693 and 6725905647. These are not full acceptance
and are not evidence about the subsequently composed build.

## Final review iteration 2

The operator authorized two additional cycles, retaining the requirement to wait for an integration
report before each. Both iteration-one integration reports were present before this correction round
began. PR #867 returned to draft. Latest main `89bb63e40` was merged for a conflict-free composed
build; no other effort's SDD artifacts are edited by this effort.

The fixed feedback batch includes identity precedence before validation and secret collection,
field-owned command rendering, the retained resource guide, dollar-adjacency guidance, and precise
validation timing. Independent private reviews and required gates must pass before the resulting
head is handed off.

### Transport disposition

The inherited SetEnv literal-delivery observations are tracked in
[issue #873](https://github.com/WayfarerLabs/agentworks/issues/873), with the tested heads, paired
Linux/macOS failures, bounded Windows passes, root-cause path, and verification cases for the
SSH/transport rewrite owner. Ordinary Agentworks references render before SSH; intentional literal
text can remain through plaintext escaping, opaque secrets, or ordinary shell-looking text.

The operator directed filing the issue and making no special SSH/transport change in this feature.
The transport concern is therefore disposed for this effort, without claiming that the rewrite or
all transport delivery cases have been verified. No transport code changes are included.

### Shell preflight refinement

The operator accepted the existing command-check boundary, clarified that only rendering and
`required_commands` presence are checked, and then requested earlier shell rendering preflight. The
verbatim rulings are in the FRD. The broad timing checkbox had been premature and was corrected
while unmerged; its definition now follows those rulings.

Bind the planned invocation facts and launch intent before the existing preflight. Shell checks only
its selected `command` or `resume_command`, including when the required-command probe defers for a
pending target. Startup uses the same renderer without caching a rendered command. The regular
preflight precedes workload/env secret resolution. The existing VM activation gate may need its own
credentials before remote preflight; this refinement does not redesign activation.

- [x] Implement and verify early shell rendering preflight for create, start, and restart,
      preserving unused-field selection, pending identities, required-command deferral, and existing
      runtime on refusal.
- [x] Update permanent integration/lifecycle guidance to distinguish template rendering and required
      command availability from executing a command to discover its result.

### Correction evidence

Runtime correction head: `def2ac6e37414ee9f58cb9c99568e965925a3700`. All three private lanes
returned clean scoped follow-up reviews. Project and complexity each passed 102 focused tests;
generic correctness passed 134 and reproduced the setup correction. These selections overlap the
full suite.

The runtime identity fix discards applicable managed-identity collisions before validation,
collection, reveal, and composition. Setup applies its broader reserved-prefix rule through one
setup-owned projection, preserving the original declaration capture. A private-review setup finding
was corrected before this checkpoint. Command rendering belongs to declared integration fields;
returned commands receive no additional interpolation. The resource guide and related collateral
teach that contract. Muntz's optional redundant final setup comprehension was acknowledged and
retained; it costs one extra dictionary pass and does not affect acceptance.

Full non-integration validation passed 11,406 tests with 11 skipped. Ruff lint and formatting passed
for 968 files; strict Mypy passed for 932 files. The exact CI Typer-isolation step, Rulesync drift,
and locked-SDD checks passed. Five real offline CLI invocations verified context, precedence,
redaction/reveal, ignored identity plaintext and secret declarations, and expected
unavailable-context refusal, with zero transport attempts. Website validation passed 160 Python and
103 Node tests and deterministic double builds at both site bases. File-quality checks accompany the
final bookkeeping commit. None of this replaces current-head live acceptance.

The operator disposed the checkpoint questions through the authenticated session: transport is
tracked outside this feature and shell rendering preflight is requested. This continues correction
cycle 1 of the two additional authorized cycles; it is not a new feedback batch. The next ready
handoff must contain the complete reviewed refinement. A subsequent feedback/fix cycle still waits
for that head's integration report. The SDD remains unlocked pending final feedback and closeout. No
merge is authorized or claimed.

## Ready handoff for final review iteration 2

Reviewed runtime head: `45b0bbb12671fb04ebdc0fd40421c6320627b8d4`. Project and complexity each
passed 122 focused tests; generic correctness passed 139 and checked fresh-context, unused-field,
and one-pass behavior independently. All three lanes returned clean verdicts. The subsequent merge
of main `f78ffb3fd` brings only the transport effort's already-landed SDD documents; runtime code
and tests are identical to the reviewed head. No other effort's SDD is edited by this effort.

Full local non-integration validation passed 11,415 tests with 11 skipped. Ruff lint/format passed
for 968 files; strict Mypy passed for 932 files. Exact CI Typer isolation, file quality, Rulesync
drift, and locked-SDD checks passed. Website validation passed 160 Python tests, 103 Node tests, and
deterministic double builds at both site bases. Five actual offline CLI invocations passed context,
precedence, redaction/reveal, ignored identity and expected refusal checks with zero transport
attempts.

The local validation charter covers the changed shell preflight boundary, planned identities and
paths, existing runtime preservation, and the shared env/rendering regressions. It uses owned
fixture state and no operator infrastructure. The standard ready-triggered pipeline owns live
validation under its own environment inventory and budget. Its report must be received before the
next feedback/fix cycle. Remaining acceptance and SDD closeout are tracked by the unchecked delivery
items; the transport observations are disposed to issue #873 under the operator's ruling.

## Final review iteration 3

The completed bounded integration report for `425d015be071a91b5c17235fcb0daa96545dc9cd` arrived on
2026-11-09 in
[PR comment 6739353604](https://github.com/WayfarerLabs/agentworks/pull/867#issuecomment-6739353604).
It explicitly ends that head's campaign, with no test still running. All 12 gates returned zero,
including 11,415 passing tests and 11 skips. Its independent source reviews found no new material
renderer or identity-filtering defect. The PR returned to draft before this final correction round.
This is iteration three of the three authorized iterations, not a correction started from a partial
report.

### Fixed feedback batch

The selected Shell resume command's rendering failure must identify `resume_command`; fresh
selection and an empty-resume fallback must identify `command`. Preflight and launch retain one
selection/rendering helper. The correction changes diagnostic field identity, without changing
selection, rendering timing, or runtime preservation.

Release metadata must exist in actual CLI-component commit history. The delivery correction uses a
`feat!:` subject and one-paragraph `BREAKING CHANGE:` footer alongside CLI reference guidance. It
describes only the active namespace and literal text outside it, preserving the operator's
destination-focused requirement. No legacy detector or configuration rewrite is introduced.

Muntz's optional late context rebind, setup-filter, identity-constant, and probe-order
simplifications remain acknowledged without being taken. Existing readiness guards, recomputation at
launch, and setup's reserved-prefix contract remain explicit. The stale helper reference in the
separate agent artifacts SDD was recorded for that owner; this effort does not edit that artifact.

### Bounded live evidence and remaining acceptance

Linux workstation OpenSSH 10.0p2 to remote Lima 2.3 on Debian 13.7/aarch64 exercised consuming-user
identity, four exec/context paths, four setup scopes, planned agent/workspace creation, console
shells, and persistent Shell lifecycle. Invalid winning values and selected templates preserved old
runtime identity, UUID, and run; invalid unused fields did not block valid selections. Native
Claude, Codex, and Grok argument refusals and opaque-field delivery were exercised. Native argument
rendering follows workload-secret resolution and precedes destructive replacement. Shell rendering
preflight precedes workload/env resolution, under the operator's specific ruling. Neither result
claims resolver-call counts or predicts actual command success.

These results are bounded evidence, not whole-PR clearance:

| Finding or cell               | Disposition                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Quoted global workspace root  | Failed in inherited VM initialization. `vms/initializer/workspaces_dir.py:57` interpolates `mkdir -p` without quoting; the later traversal assignment also interpolates that path and was not reached in the failed cell. The owner must preserve ACL application before parent traversal when correcting both shell fragments. Unchanged owning reinit repeated partial initialization; an ordinary-path control completed. This feature does not correct or claim that cell. |
| Literal SSH env delivery      | Inherited failure on measured Linux/macOS clients, with bounded Windows passes from iteration one. Operator-disposed to issue #873; no transport fix or universal preview/delivery parity claim.                                                                                                                                                                                                                                                                               |
| Refused native launch cleanup | Six inert sockets remained at live inspection after rows/servers rolled back. This is recorded for session lifecycle rollback ownership at `_create_roll.py`'s failed-launch teardown and the tmux cleanup boundary. The cause and complete per-session filesystem cleanup are not established by VM power-off.                                                                                                                                                                |
| Codex API login/catalog       | Passed native login and catalog HTTP 200. Persistent health input remained unvalidated; separate structured native exec was quota-blocked with no assistant completion. No retry or fallback.                                                                                                                                                                                                                                                                                  |
| Grok API print                | Native exit zero and exact health response with one reported turn/end-turn. Original strict-predicate failure remains preserved. Tool availability, token-limit enforcement, and persistent mode are unvalidated.                                                                                                                                                                                                                                                              |
| Other authenticated modes     | Anthropic API/interactive and Grok persistent were not run on that head. No inference job remains queued.                                                                                                                                                                                                                                                                                                                                                                      |
| Workstation coverage          | No fresh full Windows/macOS campaign on that exact head. Other PR campaigns do not substitute.                                                                                                                                                                                                                                                                                                                                                                                 |

The testing session stopped its retained Linux VM, independently confirmed managed workloads absent,
and removed staged model credentials. That bounds its cleanup claim; it does not turn any failed or
untested cell into a pass. Full details and supplementary evidence remain linked from the completed
report.

The final ready-triggered campaign must confirm the diagnostic correction and report remaining
workstation and harness coverage explicitly. No additional inference budget is requested. No merge
or acceptance waiver is authorized. Implementation closeout, private review, and local gates do not
replace that final-head report.
