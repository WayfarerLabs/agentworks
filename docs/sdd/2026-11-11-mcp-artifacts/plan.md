# MCP artifacts: implementation plan

## Ownership, vehicle and definition of done

The operator chartered this successor effort on 2026-11-11. The lead owns architecture, this plan,
integration and delivery; draft requirements are in [frd.md](frd.md). Tool filters are excluded.
OAuth is phase 2 of this same SDD under the operator's clarification; phase 1 rejects its fields.
The source baseline is `834eb715`.

The checkpoint was refreshed onto main `07feb4a4` after session batch teardown PR 776 merged.
Artifact/setup/native/env/secret seams are unchanged; current restart still stages prospective
artifacts before teardown and commits the new run only afterward. Preserve that ordering and its
conditional runtime-state checks.

Use the initial branch/PR for the complete registration phase. First publish one coherent SDD-only
draft with FRD, HLA, this plan, research, LLD, migration and acceptance together; apply
`review-requested` for that checkpoint. Do not merge the checkpoint or claim implementation. Count
the design feedback round within the three-round allowance, leaving at most two subsequent
feedback/fix rounds unless the operator extends it. Remove the label before implementation. A
complete green registration implementation gets a scoped handoff and ready state without that label.
OAuth follows in a separate PR under this same open SDD, with up to three rounds per PR. Its
detailed design follows the first working push. No automatic merge and no lock after phase 1.

Closeout PR 887 merged at `bd3da970` on 2026-11-12. Both predecessor locks are now binding on main.
This registration branch incorporates that merge while preserving its diagnostics and the MCP
relocation behavior. Do not edit predecessor SDDs or the saga ledger.

Overall done means R1-R11 implemented and independently evidenced at the actual head, public
collateral current, required private reviews clear, all gates green, actual integration findings
critically disposed within budget, cleanup independently verified and the final SDD lock justified.
Publishing a design or passing the unit suite alone does not complete this plan. Registration
acceptance completes phase 1 only; the OAuth tasks remain owed.

## Design checkpoint

- [x] Draft the complete SDD set against current source and primary native/Rulesync references.
- [x] Resolve the canonical subset, source/inline shape, credential-reference grammar and
      scope/ownership boundaries together across FRD, HLA and LLD.
- [x] Run independent project and Muntz reviews in separate pinned worktrees; incorporate material
      findings without widening the charter.
- [x] Run applicable repository/website gates and exact-head CI; publish the draft checkpoint with
      `sdd:mcp-artifacts` and `review-requested`.
- [x] Monitor the single design-only feedback window under the delivery process, critically assess
      the full batch and record requirements questions or changes needing operator direction.

Checkpoint `c8a844d7` received the complete one-hour design window ending 06:40:14 UTC. Muntz's six
material clarifications were accepted in the published critical reading; they cover the claim
fingerprint, Git handling, rehome, retained credentials, native names and embedded-MCP guard. Round
1 incorporates them with the registration implementation; no second artifact-only checkpoint is
planned. OAuth workflow preference remains a phase-2 question.

## Canonical model and capture

- [x] Add the strict MCP definition/declaration models and fifth type maps using ordinary
      whole-entry merge and original-owner identity; reject unknown or contradictory input.
- [x] Implement bounded JSON/JSONC source parsing with duplicate-key rejection, selecting the
      same-named server; capture only normalized selected content. Choose a maintained parser if a
      dependency is needed, verify its current stable release and avoid a custom comment lexer.
- [x] Extend inline/workstation/Git capture and equivalent-content identity; preserve acquisition
      limits, no execution/network-to-endpoint behavior and credential-safe diagnostics.
- [x] Introduce codec v4 with strict retained v2/v3 interpretation and unknown-version protection;
      preserve legacy hashes and empty-map freshness as specified in migration.
- [x] Prove per-type replacement, unchanged old artifacts, scope grouping and absence of resolved
      credentials in durable data. Use meaningful boundary tests, never authored-prose pins.

## Native entry application

- [x] Resolve native-home roles and use existing guarded file APIs and normal setup invocation.
      Implement bounded MCP entry reconciliation with existing parsers, claims, fingerprints,
      checkpoints and expected-content publication; no parallel state or whole-document ownership.
- [x] Implement deterministic original-owner-qualified native identities, adoption, owned
      convergence, per-entry foreign conflict, removed-entry cleanup and modified obsolete
      retention. Include the bounded readable key and full-key identity hash; explicitly use MCP
      claims' `sha256` contract rather than a second fingerprint/ownership store.
- [x] Plan Codex settings mapping contributions and MCP entries together, preserving every existing
      settings strategy and diagnosing conflicting explicit values before canonical mutation. Test
      canonical withdrawal with a still-authored mapped key under all four strategies in one pass,
      including suppressed initial contributions and original file-existence decisions.
- [x] Add Claude/Codex/Grok user and workspace translation and explicit VM deferral. Every adapter
      must apply or defer the new type; test against silent handled-but-omitted behavior.
- [ ] Translate unresolved env/header/bearer references to native primitives. Test bindings from
      declared env and guest startup exports, including a project definition bound by each actual
      user. Never infer binding absence from the partial prepared map or probe
      credentials/profile-file contents. Unsupported representations defer whole entries. Preserve
      native trust/approval/auth state.
- [x] Publish Shell canonical JSON and indexes at all existing scope locations, with no new
      deferrals or server execution.
- [ ] Verify interruption/retry, other-owner preservation, activation removal and ordinary parent
      deletion/rehome without introducing a new gate or stored-path I/O authority.
- [x] Include confirmed shared MCP workspace paths in existing Git exclusion handling, with
      tracked-file warnings. Rehome destination evidence and keep coexisting file metadata/identity
      coherent through repeated moves and retained copies without workspace reinit.
- [ ] Keep the skill/persona embedded-MCP guard and ship server-specific credential-retention
      notices/native logout-before-removal guidance without credential-store inspection or logout
      automation in phase 1.

## Opt-in session delivery

- [x] Add and test Claude private MCP config and Codex invocation-only per-server overrides using
      existing prospective-run/files lifecycle. Defaults remain explicit terminal warnings.
- [x] Evaluate the documented Grok private plugin candidate with the real harness. Implement and
      advertise its workaround only after fresh/resumed/neighbor-session proof; otherwise retain an
      honest unsupported disposition and bring any acceptance scope question to the operator.
- [x] Preserve all existing artifact workaround selection and native argv validation. Do not replace
      other MCP sources, expose credentials in argv or write shared catalogs for isolation.
- [ ] Prove pre-teardown refusal/staging failures preserve the working runtime/effects. For launch
      failure after confirmed teardown, retain accurate retry/cleanup state without rollback claims.
      Successful replacement retires only obsolete run files, and delete/name reuse does not affect
      another session.

## Inspection, collateral and validation

- [x] Extend safe placement/claim inspection and per-type applying/deferring output, preserving
      offline behavior and existing small-list truncation. Do not add secret/source/native probes.
- [x] Update artifact and integration READMEs/guides, sample config, examples, schema teaching and
      release/upgrade notes with actual supported mechanisms, limits, native formatting changes and
      downgrade behavior. Permanent teaching must stand alone without this SDD.
- [x] Run the full frozen CLI/repository/website gates and installed wheel/source archive checks,
      standalone guide renders and source/package parity on the complete candidate.
- [x] Run independent pinned project, Muntz and generic correctness/security reviews for code; fix
      material findings and revalidate the actual final delta before public handoff.
- [x] Hand off the complete green pushed head with integration emphasis from
      [acceptance](acceptance.md), then mark ready without `review-requested`.
- [ ] Wait for each actual independent integration report before a subsequent implementation fix
      round, observing up to six hours. Critically assess the complete batch, return to draft before
      changes, re-run required private lanes/gates and re-hand off within the remaining allowance.

## Registration implementation evidence, before full handoff

Core and capture changes are integrated in `0e27a26c1` and `0db1b2d02`, including strict
selected-definition validation, same-content acquisition, supported legacy hashes and future-version
refusal. The acquisition helper records whether the source was a directory rather than inferring
that from its member count. The only dependency addition is Apache-2.0 `json5` 0.17.3; its broader
JSON5 lexical conveniences are accepted, while root/duplicate and selected-definition structure
remain strict. No handwritten comment lexer or executable import step was added.

Native reconciliation in `9070a8d5a`, `50583817b` and `6befe33c3` passes its 36 file-backed MCP
cases and retained mapping/plugin regressions. Those observations prove file/claim behavior, not
native connectivity. `59ea743da` adds 27 shared-path/dispatch cases; the combined relevant selection
passed 168 tests. `761aadde6` adds Shell/reporting/inspection, with 70 owned tests passing. The
adapter worker owns the remaining known all-types-as-files Codex fixture correction.

Lead commit `b25baecc8` adds legacy-only probe selection and coherent MCP-bearing workspace evidence
relocation. The probe/diagnostic/guidance selection passed 35 tests; rehome plus existing move
regressions passed 31. The first new rehome run had two failures from an incorrect fixture digest
expectation; comparing with the original recorded digest corrected that test. No product change was
needed for those failures. Repeated moves retain package roots, skips, fingerprints and the actual
new destination identity. File-only predecessor rehome semantics remain unchanged.

Permanent collateral in `f92ec9bf3` adds fifth-type authoring and examples, native roles,
conservative session options, cleanup/Git/rehome teaching and downgrade/OAuth boundaries. Ten
example manifests and selected inline/source capture, four isolated guide renders and 68 retained
guide/sample tests passed in that lane. An initial package-relative link and guide-frontmatter wrap
were corrected before that result. The lead's shared dictionary adds the protocol term Streamable.

[Local native smoke evidence](acceptance.md#local-native-smoke-evidence) records real stdio/HTTP
connections and tool discovery for each client's user registration, actual reference expansion and
workspace trust/discovery limits. The lead independently verified cleanup. No managed-session or
OAuth acceptance is inferred from it.

Native adapters and conservative session carriers are integrated in `7768a8fc9`; `ffec8fd32` adds
the real-profile-based Codex deferral. Targeted selections passed 184 tests (one skip) and 162
profile/carrier/harness tests respectively. Fixed native-role discovery is independent of the old
skill/persona probe and only observes HOME/native-home variables. Current-role authority partitions
old claims without following stored paths; unknown roles retain claims and skip MCP I/O while
independent settings/plugin work continues. Explicit profiles leave only optional MCP unhandled.

A lead boundary check distinguished known guarded-filesystem refusals from internal `StateError` and
persistence failures. A first new directory-config case failed because the file helper wrapped the
descriptor before classifying a non-regular inode. Checking `fstat` first honors its existing
non-regular-file status; the typed path refusal makes optional MCP inspection advisory while other
state failures still propagate. The corrected selection passed 121 tests with one skip. No native
file authority, symlink following or Transport API was broadened.

These are local implementation observations. Combined full gates, full-PR private reviews, installed
package checks and independent integration acceptance remain unchecked below and above. They do not
inherit the SDD checkpoint's green result.

## First complete candidate and private corrections

At `e6dc07a5`, the full frozen suite passed **12,485 tests, 35 skipped**. Ruff/format, exact CI
Typer isolation and every repository/website gate passed. Full mypy failed with 79 test-helper and
collection-narrowing errors in four new test files; no source typing relaxation was made. The
wheel/source archive and frozen installation checks passed, including four standalone guide renders,
help/sample and 488 source/wheel/installed package files. That is evidence for this pin, not a green
claim for later corrections.

Three independent pinned lanes found four shared correctness edges: typed TOML values could alias
strings in ownership hashes; optional MCP could pass fragment admission and fail the complete
launch; rejected field keys could inject or flood diagnostics; explicit mapping takeover of a
modified withdrawn entry could leave stale incomplete ownership. Project review also caught an
incorrect removal notice when the mapping preserved the entry. All findings were accepted. The
immediate explicit mapping mutation was already authorized; only stale ownership/convergence was the
defect. The suspected implicit/default Codex-profile bypass was tested and refuted on 0.167.0.

`303c85ce5` fixes bounded known-field diagnostics and authoritative full-command MCP admission,
without another runtime discovery decision. It deletes the two unreachable renderer catches and
their synthetic tests. Its scoped selection passed 129 tests; eight complete-command cases cover
fresh/resume/picker assembly and preservation of existing legacy limits. A first local edit had an
indentation error, and a first regression used a broad substring assertion against the whole
launcher; both were corrected before the passing result, with failures retained in proof.

`cab7f0297` removes lossy native hash conversion, protects absent fingerprints, and reconciles
modified obsolete claims after explicit mapping policy. Notices distinguish native removal from
ownership release. It also deletes the unused collection renderer and types the three affected
native/publication/dispatch test modules. Eighty-three focused cases and scoped mypy passed in that
lane. The lead must still verify the combined full mypy/gates, package parity and all three private
lanes at the corrected head before handoff.

Reviewer filesystem failures are scoped separately: metadata-preserving ownership on the workspace
mount returned error 22 (invalid argument) from existing `fchown`; equivalent owned `/tmp` fixtures
passed. No global native ownership policy was changed for that environment limitation. Their
original logs remain retained; the lead independently verified the reported temporary fixture roots
absent.

## Phase 2: OAuth

- [ ] Resolve the operator's native-configuration versus Agentworks login-workflow preference and
      complete the [OAuth design](oauth-phase.md) after the first working registration push.
- [ ] Specify strict supported auth fields, native translation and actual-user credential ownership;
      research public/confidential clients, scopes, remote browser/callback behavior and
      session-only discovery without introducing an Agentworks token store or protocol client.
- [ ] Keep the detailed phase-2 design/FRD/plan update with its OAuth implementation PR and assess
      feedback through normal delivery. Obtain any changed-requirements ruling before building it;
      the single initial SDD-only checkpoint remains the artifact-only review requested here.
- [ ] Implement supported canonical OAuth configuration and the agreed explicit authentication
      workflow with native credential sinks and existing secret/transport boundaries. Keep ordinary
      setup/start free of automatic consent/login and unsupported combinations explicit.
- [ ] Validate actual native OAuth and authenticated MCP use, user isolation, cancellation,
      advertised refresh/status/logout behavior and independent credential/resource cleanup.
- [ ] Update permanent guides, examples, inspection and upgrade teaching; run all gates and required
      private reviews and the independent integration pipeline on the complete OAuth PR.

## Overall closeout, after both phases

- [ ] Reconcile R1-R11 and every advertised native mechanism against exact source/mode/platform
      evidence and independent cleanup. Keep inherited limitations and unrelated findings explicit.
- [ ] Finish permanent promotions, verify all checked work and requirements, and add `locked.md`
      only when the completed effort supports it. The lock binds at merge.
- [ ] Verify final PR/CI/review/label state and actual merge; leave no owned live resources or
      unreported cleanup obligations. MCP completion does not include single-session workspaces.

## Corrected source verification

All three private lanes independently cleared `ad632ada`, including the material corrections and the
actual surrounding contracts. Project follow-up passed 271 focused cases plus 116 retained Codex
regressions; Muntz passed 252 cases plus its original ownership regression; generic correctness
passed 595 cases and supplemental fingerprint/diagnostic probes. These overlapping counts are not
added to the full suite. The lead read each report and independently checked the named runtime
cleanup receipts.

The corrected full frozen suite passed **12,513 tests, 35 skipped**; full mypy passed 989 files, and
Ruff/format and exact CI Typer isolation passed. Frozen wheel/source archive/install and the four
standalone guides/help/sample passed, with all 488 package files matching source, wheel and
installation. One extra skip is an inapplicable Claude-only local-project parameter for Codex, not
an untested advertised mechanism.

The same run found two spelling words in this record and one website browser input timing failure.
CI independently reported those same spelling words; its website and Windows jobs passed. The
original logs are retained. At `a8cb56deb`, spelling/file checks and the full 160-test website
replay passed. Node tests, both deterministic build pairs, locks, Rulesync and installed package
parity also passed. Runtime/packaged source remains byte-identical to `ad632ada`. No browser source
or timing policy changed, and no cause beyond the observed one-off timing failure is asserted.

The final record delta is limited to this SDD's HLA/plan, including the historical checkpoint
framing clarification. The handoff records its exact pushed green head and CI separately. The
remaining unchecked registration tasks track independent integration evidence, not a claim that
OAuth or the overall SDD is done. No `locked.md` is added.

## Registration round 2

The complete registration handoff was `8e827215`, ready at 08:44:20 UTC. Its exact-head CI passed
all ten jobs. The
[independent integration report](https://github.com/WayfarerLabs/agentworks/pull/888#issuecomment-6793155673)
arrived at 10:16:11 UTC, before the six-hour deadline. It repeated all twelve gates successfully,
including 12,513 unit passes and 35 skips, and exercised installed-package boundaries, real Linux
and Windows managed acquisition/setup/session operations, native tool discovery, neighbor
preservation, MCP-bearing rehome and independent cleanup. Its 491 installed non-RECORD payload
comparison includes distribution metadata; the lead's 488-file comparison covers package files.
Those are different inventories, not interchangeable counts.

The report independently reproduced F1 through three real agent reinitializations: adding MCP,
withdrawing it, then reinitializing without a prior MCP claim made the same Codex settings mapping
switch away from the actual profile-selected native home. The separate
[Muntz review](https://github.com/WayfarerLabs/agentworks/pull/888#issuecomment-6791937894) also
identified the MCP-dependent settings path and workspace metadata relocation policies. File-only
rehome staleness predates this PR; making its correction conditional on MCP is the new
inconsistency. Both findings were corroborated by read-only source scouts and accepted in the
published readings.

The lead
[started round 2](https://github.com/WayfarerLabs/agentworks/pull/888#issuecomment-6793215043) under
the operator's three-round allowance after the actual report, and verified draft state before source
changes. Main was refreshed and remains `07feb4a4`. The frozen batch is F1, Muntz's two material
points and its three optional points; one further round remains after this round.

- [x] Use one passive current-user native-layout observation for settings/plugin/artifact/MCP
      placement, preserving Claude's explicit override distinction, MCP-only executable independence
      and current-role ownership boundaries.
- [x] Apply one known-metadata and destination-identity rehome policy to all existing workspace
      setup records, preserving Git authority, opaque claims, literal contents and incomplete state.
- [x] Distinguish already-absent MCP retirement, remove the redundant native-name count check and
      document the confirmed-write/failed-claim checkpoint gap without adding ownership machinery.
- [x] Run all three independent private review lanes, full gates and installed-package checks on the
      corrected source, preserving exact pins and original failure logs.
- [x] Verify the final record delta and exact pushed-head CI, re-hand off the complete green head as
      round 2 and mark ready. Wait for its actual independent integration report for up to six hours
      before another fix round.

Native layout source is integrated in `f8b73a924` and `8fed2868f`; its broader retained selection
passed 2,298 cases with five skips, and the final small simplification selection passed 243 with one
skip. New cases cover real isolated profile selection across MCP add/withdraw/following reinit,
selected-root reuse by all three adapters, mixed plugin/settings work and unknown/changed-role
retirement. A new guarded-path case also exposed optional MCP handling that omitted the existing
typed path refusal. Its correction catches only that refusal; CAS, internal state, cancellation and
checkpoint errors still escape. Initial obsolete fixture API/count assumptions and the corrected
failure remain in the owned proof logs.

Rehome source is integrated in `50f413451` and its public-boundary test correction `352c84b6b`. Its
85-case focused selection passed; explicit tests cover file-only and MCP-bearing records,
empty/settings/skips/opaque records, partial Git failure, preserved bytes/hashes and persistence or
cancellation failure. The first workspace-mount fixture run had ownership-preservation failures; the
owned `/tmp` replay and corrected manager-error expectations passed. The lead read the source and
measured logs and independently checked the named fixture roots absent. No filesystem authority was
relaxed for that environment. Corrected combined review/gates/package acceptance remains pending.

The first combined pin, `947975c56`, passed all repository gates and exact-head CI: 12,573 unit
passes, 35 skips, full mypy across 991 files, website tests/builds and installed-package checks with
489 matching package files. Project review found no product issue in 300 focused passes and one
skip; Muntz's 202-pass/one-skip baseline and deletion experiments justified the two main fixes and
three small simplifications. Generic correctness passed 240 focused cases with one skip and
reproduced one additional defect: an unterminated profile banner could hide the new layout frame.

`dbe7823f9` adds the missing frame delimiter and actual profile-output regression cases. The
retained before-fix run had three no-newline failures and three passing newline controls. The same
correction removes the lower helper's unused second resolver, unreachable layout-None handling and
redundant shared-path collection. Four production setup calls now supply the already selected root;
standalone tests use one fixture-only path adapter. Its focused selection passed 439 cases with one
skip. These private corrections remain within round 2; all three lanes and combined source/package
validation must be checked again at the corrected pin.

The project lane separately reported an execution lapse: two selected tests invoked installed native
plugin CLIs despite its no-native-call charter. It stopped further runtime work and recorded the
exact test nodes and missing subprocess-transcript limit. The fixtures supplied no provider
credentials and used owned homes with blocked proxy traffic. The lead read the report and verified
the named fixture/environment/cache paths absent. Follow-up review excludes those native-bearing
tests; the lapse is not recast as managed acceptance.

All three lanes independently cleared corrected source `f5573ca5`: project passed 133 focused cases
with one skip, Muntz passed 208 with one skip, and generic correctness passed 160 with one skip plus
its repeated concrete banner/role controls. The lead read every follow-up report and independently
checked the named runtime roots absent. Follow-up project execution traced fixtures first and
excluded installed-native cases. The counts overlap and are not added to the full suite.

The corrected full frozen suite passed 12,579 tests with 35 skips. Full mypy passed 991 files;
Ruff/format, CI Typer isolation, file lint, locks, Rulesync, website tests and both deterministic
build comparisons passed. Frozen wheel/source archive/install, four standalone guide renders,
help/sample and all 489 source/wheel/installed package files passed. The initial lead spelling
failure for the protocol term PKCE remains recorded with its dictionary correction. Final record
checks and exact pushed-head CI are recorded in the handoff after they complete; these source
results alone do not claim that handoff or independent acceptance is done.

R6 session acceptance remains open: the Windows resume-only attempt found no resumable native
history. It proves neither genuine resume nor a native defect. The corrected handoff must request
fresh and genuine resumed carrier/isolation evidence without synthetic history or a fallback fresh
launch. Claude project consent and the Grok project HTTP case whose fixture expired also retain
their reported limits. None is silently promoted to a pass or removed from the acceptance contract.

Separate bounded OAuth research at unchanged `8e827215`, Linux aarch64, observed Claude 2.1.319 and
Codex 0.167.0 public DCR/PKCE manual-redirect login, authenticated MCP initialization/tool discovery
and local credential cleanup. Both added `offline_access` to the pinned scope. Grok 1.0.77 had no
MCP login/logout subcommands, and its doctor stopped at authentication required. Initial
PTY/URL-driver failures and the corrected isolated-home repeat are retained. The lead read the
measured receipts and independently confirmed all six homes absent and all seventeen recorded
fixture/callback ports closed. This is phase-2 research, not Agentworks OAuth implementation,
registration integration acceptance, provider-grant revocation or a resolved workflow choice.

## Registration round 3

The authenticated overall allowance permits three rounds, including the initial design-only
checkpoint. The actual
[round-2 report](https://github.com/WayfarerLabs/agentworks/pull/888#issuecomment-6795726868)
arrived at 12:24 UTC on 2026-11-11 and was read in full. It confirms F1 and the common relocation
policy at `7accf20c`, with all required gates and its stated native/platform cases passing. No new
product defect is established. Its hold concerns missing genuine managed conversation resume; normal
Claude project consent and actual linked-worktree native relocation also remain gaps.

The lead's
[critical reading and round start](https://github.com/WayfarerLabs/agentworks/pull/888#issuecomment-6796139518)
retain those obligations. A controlled provider can enable normal native history generation and
continuation without a paid model. This follows the existing distinction between registration and
model cooperation; neither the fixture nor local native research supplies managed acceptance. The PR
was verified draft at unchanged `7accf20c` before source work. This is the last authorized public
round, not another design-only checkpoint or an OAuth implementation push.

- [x] Add a maintained, standard-library local Messages/Responses/MCP protocol fixture and
      meaningful boundary tests. Keep it free of native-client launch, history mutation, real
      credentials, external provider dependencies and an Agentworks acceptance verdict.
- [x] Document an actual managed fresh/resume procedure with normal native onboarding, strict
      resume, native-generated history, new managed-run ownership, neighbor isolation, unchanged
      shared registrations and independent cleanup. Keep model-cooperation claims separate.
- [x] Reconcile the second report in the acceptance record without promoting its untested modes.
- [ ] Complete the private project, Muntz and generic correctness reviews, required gates,
      installed-package checks and exact-head CI; hand off the complete green head ready for the
      separate integration-test session.
- [ ] Obtain and critically assess the next actual integration report. R6 and phase-1 acceptance
      remain owed until its required observations exist. Further material fix work after this round
      needs operator direction; no automatic merge or MCP SDD lock is authorized.

Round 2's final handoff was `7accf20c`, with all ten exact-head CI jobs passing in run 43010264601.
The actual second report above completes that round's observation task; it does not complete R6.

`f1890e8c7` adds the managed acceptance recipe and actual second-report record. `cf4756bc2` adds a
403-line standard-library fixture, with one bounded loopback listener serving minimal Messages,
Responses and authenticated MCP protocols. It neither launches clients nor writes native history.
Public fixed auth values, role-aware context predicates and safe fixed-field observations support
evidence correlation without deciding acceptance. Its thirteen tests cover wire framing, auth, input
bounds, false continuation, confidential input omission and bounded signal/timer cleanup.

The frozen `cf4756bc2` full suite passed 12,592 tests with 35 skips. Seventeen of eighteen gate
records passed, including repository/website checks; full mypy reported 37 untyped calls in the new
test helpers. `d3e763bb5` corrects only their annotations. Corrected full mypy passes 992 files,
Ruff/format and the thirteen selected stdlib tests pass. The fixture's executable bytes are
unchanged. The original full gate failure is retained, not described as a green run. Frozen
wheel/source archive/install checks at `cf4756bc2` passed all eleven records, including four
standalone guide renders, help/sample and 489 matching package files with locked Typer 0.27.3. Final
source-archive and record checks follow the annotation and plan corrections.

All three pinned private review lanes cleared the fixture, procedure and acceptance limits at
`cf4756bc2`; project review's sole important finding was the accepted helper typing correction. They
independently cleared its exact test-only follow-up at `d3e763bb5`. The lead read the reports and
independently found all forty retained unit/review listener addresses closed. These overlapping unit
runs are not additional distinct cases or managed acceptance.

Separate local native observations used the exact unchanged helper hash. Codex 0.167.0 completed one
controlling-PTY fresh/resume pair with `--no-daemon --no-alt-screen`, the same native history ID and
observed continued user/assistant context. Claude 2.1.319 completed one separate headless Messages
pair with its same native-generated history ID. A Claude TUI attempt reached authenticated MCP
discovery but did not complete a model turn/history or resume; its prompt-driver limitation remains
recorded. The lead read the measured reports and independently verified seventeen retained listener
addresses closed and the named native homes absent. These are mode-specific protocol observations,
not default-daemon, persistent managed R6, model-cooperation or tool-execution proof.

The pending ready handoff must still obtain the actual managed fresh/resume and isolation report,
including normal Claude project consent and linked-worktree native relocation. OAuth remains phase 2
of the same open SDD in its separate implementation PR. No phase-1 acceptance or lock is claimed.

## Main conflict maintenance, 2026-11-12

The operator directed resolving the conflicts after PR 887 merged and republishing PR 888. This is
bounded merge maintenance, not an additional feedback/fix allowance or a change to acceptance. The
starting public head is `95a51d3b`; current main is `bd3da970`. Draft state was verified before
changing the branch. Both predecessor lockfiles were verified on main; neither locked directory
receives an MCP amendment.

- [x] Incorporate current main and resolve the sole conflict in native setup cleanup by preserving
      both common placement relocation and the new sanitized retained-exclusion warnings.
- [ ] Validate the combined source and obtain project, Muntz and generic correctness reviews of the
      merge resolution before the new handoff.
- [ ] Push the exact green head, update the PR description and handoff, and restore ready state
      without `review-requested` for the separate integration-test session.

The earlier integration report's missing managed resume evidence remains owed. Conflict resolution
does not satisfy R6, complete phase 1, ship OAuth or justify the MCP SDD lock.
