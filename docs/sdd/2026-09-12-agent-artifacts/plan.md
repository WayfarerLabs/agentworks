# Agent artifacts: implementation plan

## Current stage and ownership

The operator approved the FRD and HLA on 2026-09-13 and authorized the complete implementation on
PR 794. This includes workstation/Git acquisition, the native support matrix and a launch error for
artifacts still unhandled at the session. The effort lead owns architecture, sequencing and this
plan. Implementation proceeds as one complete delivery on the existing branch; local work units and
commits provide internal checkpoints, with one completed implementation push before handoff.

Keep predecessor closeout separate. Its implementation is merged, but seven acceptance/cleanup
checkboxes remain open in the
[harness-scope-framework plan](../2026-09-06-harness-scope-framework/plan.md#acceptance-and-closeout).
Complete and record that acceptance before locking it, unless the operator explicitly changes its
remaining obligations. A historical lock does not prevent this effort from changing framework code.
Do not change the saga's existing ledger or contracts; route coordination to their owner.

## Delivery

Operator decision, 2026-09-12: keep requirements, research, HLA and the entire implementation on
branch `sdd/agent-artifacts` and [PR 794](https://github.com/WayfarerLabs/agentworks/pull/794). This
supersedes the earlier plan and handoffs that treated the seed as a separate merge increment. No
separate design merge is planned; the operator does not see a coordination need for one here.

Keep the PR draft while work remains. Use `review-requested` for a coherent HLA checkpoint on this
same PR, and remove it before incorporating changes or continuing implementation. Requirements and
HLA approval remain explicit checkpoints; sharing a PR does not approve either. Carry
`sdd:agent-artifacts` and `saga:next-steps` throughout. Mark ready without `review-requested` only
when implementation and local gates are complete, with merge intent. The ready transition requests
the standard integration-testing pipeline; its remaining live acceptance must finish before merge,
and pending CI and acceptance are disclosed at handoff.

## Requirements and research

- [x] Carry forward operator decisions on hints, package normalization, origin, facet ownership,
      lazy passthrough, VM-to-session fallback, shell placement and idempotent cleanup.
- [x] Identify predecessor acceptance separately from future framework changes and record the
      successor's proposed first delivery and open decisions.
- [x] Obtain operator acceptance of the FRD scope and resolve first-delivery source choices.
- [x] Incorporate operator terminology corrections and research the agent persona naming proposal;
      record the recommendation in [prior-art research](prior-art-research.md). Persona naming
      remains pending agreement, and this narrow pass does not complete the research below.
- [x] Record the operator's terminology decision: **agents** is shorthand for **agent personas**.
      This supersedes the pending-agreement status recorded by the preceding research milestone.
- [x] Recover the predecessor's acquisition safeguards into the FRD and prior-art research, with the
      historical commit reference and concrete byte-preservation cases. This preserves design
      context; it does not claim implementation or completion of acquisition research.
- [x] Write `prior-art-research.md`: evaluate Rulesync's model and generators, Agent Skills package
      conventions, source acquisition options, and the actual native artifact mechanisms of each
      shipped harness. Cite primary sources and mark unsupported claims explicitly.
- [x] Resolve the final-session disposition with the operator and present the native support matrix.
      State the handling criteria for shell publication and each native artifact delivery mechanism.

## Design checkpoint

- [x] Write an HLA covering bundle resources, acquisition and capture, normalized representation,
      ownership and lazy routing, integration APIs, native placement, final disposition and cleanup.
      It includes `agw artifacts show`, the core-to-integration pipeline and the later producer
      boundary.
- [x] Include worked manifests and the VM/user/workspace/session flow, with inactive facets,
      multiple consumers, duplicate-route prevention and session name reuse.
- [x] Check the saga's `session_uuid`/`run_id` contract and implementation ownership with the saga
      lead before selecting durable session artifact ownership in the HLA. Do not introduce a
      separate artifact identity to fill an unowned session-identity dependency.
- [x] Record migration from the merged facet API and existing native setup without reintroducing
      speculative reconciliation machinery or source-specific propagation formats.
- [x] Run independent project and complexity reviews, complete the applicable checks, and publish
      the HLA checkpoint for operator and saga review.
- [x] Obtain HLA approval, then replace the initial implementation outline with concrete work units,
      necessary LLDs, acceptance scenarios and delivery boundaries.

## Implementation sequence

All units ship together on PR 794. The lead integrates bounded developer branches locally, updates
this plan as evidence completes each unit, and keeps `review-requested` off during implementation.
Do not push delegate branches or partial implementation checkpoints. The next published head must
contain the full implementation and collateral, followed by standard ready handoff.

### 1. Shared contracts and migration

This effort owns the early identity prerequisite under the existing saga contract, which explicitly
permits that slice to land early. Main has no identity implementation, and PR 770 remains a design
checkpoint. The lead owns identity and session publication together; no containment or event-stream
contract is adopted here. The
[coordination message](../2026-08-04-next-steps/message-2026-09-13-agent-artifacts-identity-implementation.md)
records this implementation assignment for the saga and dependent efforts.

- [x] Resolve the existing session-identity ownership dependency against current saga and child
      work; record the implementing slice without changing another effort's requirements.
- [x] Write [source and codec LLD](source-codec-lld.md): declarations, source selection, capture
      bounds, text/byte rules, typed inputs, identity and versioned persistence.
- [x] Write [native delivery LLD](native-delivery-lld.md): typed facet results, native
      paths/options, ownership claims, private session publication and compatibility checks for each
      integration.
- [x] Implement the agreed durable `session_uuid` and per-incarnation `run_id` slice, including
      migration of existing rows, create/restart/name reuse and backup/restore preservation. Keep
      native conversation identity independent; do not add containment or event-stream machinery.
- [x] Bump the harness integration contract and update first-party hooks together. Old hook returns
      cannot silently acknowledge artifact inputs; artifact-free stored evidence remains readable.

### 2. Bundle resources, capture and normalization

- [x] Register `artifact-bundle` as an ordinary declared resource and add `artifacts.bundles` to VM,
      admin, agent, workspace and session declarations, resolved templates, reference discovery and
      schema generation. Omission inherits, a supplied list replaces, duplicates fail, and resolving
      a declaration never acquires its source.
- [x] Extend shared source handling for bounded workstation and Git package capture. Use immutable
      commit/object reads with workstation auth, consistent per-operation Git resolution and
      complete selected packages. Do not run hooks, filters or bundled scripts, or omit
      export-ignored files.
- [x] Preserve standard skills and persona metadata, supporting paths, executable intent and binary
      bytes. Normalize designated UTF-8 text to LF; make `preserve_bytes` concrete and exclude
      `SKILL.md` from that override. Reject unsafe paths, portable collisions, links, special files,
      local Git metadata, selected submodules, unresolved LFS pointers and observed local mutation.
- [x] Deliver one immutable normalized input representation and lossless bounded state codec with
      content identity independent of source provenance and consuming origin.
- [x] Prove source/normalization contracts with local fixtures: mutable refs vs commit pins,
      repository subpaths, complete packages, CRLF/lone CR, executable scripts, text-like binary
      fixtures including ASCII-only PDF, byte preservation, capture failure and temporary cleanup.

### 3. Core capture, routing and applied state

- [x] Capture owner-local inputs in owning operations even without activated integrations. Commit
      new-owner captures with their rows; replace existing captures only after successful capture.
      Session consumers never fetch or initialize ancestors. Workspace refresh requires recreation.
- [x] Extend the existing instance-state domain and codecs for core captures, applicable input
      identities, typed deferrals and session applied evidence; preserve existing plugin claims.
- [x] Prepare completed env and applicable artifact inputs before each integration. Validate plugin
      results for unchanged origin, input membership, one legal next facet and no duplicate routes.
- [x] Implement lazy resolution through the actual VM, user, workspace and session path. An inactive
      VM routes directly to session; inactive intermediate facets pass through; handled payloads do
      not reach descendants. Results remain reusable independently for every descendant owner.
- [x] Base freshness only on supplied inputs and existing config/env dependencies. Diagnose missing,
      stale or interrupted active results and retained artifact effects awaiting retirement without
      changing unrelated required/recommended setup semantics or blocking parent deletion.
- [x] Prove routing with simple test integrations: all VM destinations, inactive/unimplemented
      distinctions, the diamond, multiple integrations/consumers, origin preservation, malformed
      results, branch-specific freshness, interrupted setup and removed activations.

### 4. Native delivery and owned cleanup

- [x] Implement idempotent whole-file publication/retirement using existing transport and applied
      state. Preflight destination/name collisions, preserve unowned or drifted content, checkpoint
      confirmed effects, and retry partial work without an acknowledgment ledger or generic engine.
- [x] Implement shell publication for all types at user/workspace/session, with fixed ancestor
      locations and the session-only index exposed through `AGENTWORKS_ARTIFACTS_DIR`.
- [x] Implement Claude user/project rules, skills and agents; session additive prompt content,
      private plugin skills with explicit namespace, and agent definitions through native options.
- [x] Implement Codex user/project skills and agents, with hints/rules deferred to session; compose
      session developer instructions and private persona config. Refuse private session skills
      explicitly and keep primary-persona limitations honest.
- [x] Implement Grok Build user/project rules, skills and agents, plus session rules/personas; honor
      discovery exclusions and refuse unsupported interactive session skills explicitly.
- [ ] Validate artifact-specific native options and reject launch overrides that disable or replace
      generated carriers. Verify native discovery and resume behavior against actual executables,
      without model calls or external marketplace dependencies.

### 5. Session lifecycle and inspection

- [x] Integrate prospective session/run identities, completed env and prepared artifact inputs into
      the launch decision. Reject final deferrals before stopping an existing workload.
- [x] Persist ownership before publication; stage each restart in a distinct private run directory,
      preserve the old workload on preparation failure, retire old files after it stops, and clean
      failed creates/deletions idempotently without adopting same-name replacement files.
- [x] Implement `agw artifacts show` with actual-lineage selectors and optional integration filter.
      Include local and ancestor declarations, upstream-handled artifacts, capture provenance,
      recorded native identity/placement and clear missing/stale/interrupted evidence.
- [x] Prove inspection does not acquire sources, invoke integrations, contact remotes, expose bodies
      or secrets, or mutate state. Reject contradictory selectors and preserve admin/agent
      separation.
- [ ] Exercise create, restart/resume, failure before teardown, deletion, source/reference removal,
      name reuse and two actual users sharing one workspace. Verify private paths and safe cleanup
      from observed state, not only return codes.

### 6. Collateral, review and delivery

- [x] Update permanent source/capability/state docs, operator guides, sample bundle/owner manifests,
      CLI help/completions, generated resource schemas and migration guidance with shipped behavior.
      Include the native support matrix, capture/refresh rules and VM-plus-user activation example.
- [x] Run required repository/CLI gates and relevant behavioral tests, package/build checks and
      deterministic website builds. Record actual exits and distinguish local, CI and live evidence.
- [x] Run independent project, Muntz and general correctness/security reviews on isolated pinned
      work trees; critically assess and resolve material findings before handoff.
- [ ] Run scoped live acceptance using an operator-provided inventory and budget; snapshot first,
      exercise the public CLI and native placement boundaries, and independently verify cleanup.
- [ ] Audit R1-R10 and the complete approved matrix against evidence, complete owned cleanup and
      record remaining limitations honestly. Lock this SDD only when its work is actually complete.
- [x] Push the complete implementation once, update the PR around shipped behavior and evidence, and
      mark ready without `review-requested`. Do not merge or enable automatic merging.
- [ ] Monitor the ready handoff under the standard feedback window. Up to three PR feedback/fix
      rounds are authorized by the 2026-09-13 implementation direction; each changed head repeats
      private review, validation and proper handoff. Escalate scope changes or unavailable evidence.

## HLA checkpoint evidence

The operator approved the FRD/HLA at `b2ba177a`, following one published feedback/fix iteration and
the requested artifact-only diagram amendment. Project and Muntz private reviews inspected the
rendering; saga and Muntz PR reviews were clean with no unresolved threads. Local document gates and
rendering passed. GitHub CI remained queued and was explicitly disclosed under the operator's
direction to continue the design checkpoint. That exception does not claim implementation
validation.

## Seed review evidence

Independent project and complexity reviews of `7c744828..5fff8a0c` found no material issues. The
project review's minor signature request was incorporated into the saga coordination message. Both
reviews confirmed that new scope remains proposed and the predecessor's seven acceptance/cleanup
obligations remain open. No code or live resources were changed. The seed handoff records repository
checks and final-head CI separately; this is not artifact implementation or acceptance evidence.

## Implementation private review evidence

The first complete private review inspected `3b4ea08b` against `7c744828` in three isolated work
trees. The lead accepted the concrete project, Muntz and correctness findings:

- Session deletion now holds the existing VM mutation guard through runtime teardown, file cleanup
  and row deletion, then releases it before independent parent cleanup.
- Existing-owner artifact routing is checked before launch secret resolution and checked again
  during guarded preparation. Final deferral errors identify integration, type, bundle entry and
  originating owner, retaining the integration's reason.
- Workspace deletion catches unreadable artifact evidence alongside cleanup failures. Its warning
  identifies the private session directory and explains that remaining files lose managed cleanup
  evidence when the session row is removed. This preserves the existing parent-deletion contract; VM
  deletion gains no artifact cleanup barrier.
- Codex and Grok no longer publish unused copies of inline session guidance. Session staging no
  longer repeats the application validation already performed at the plugin boundary.
- Workstation capture holds directory descriptors or Windows handles so replacing a checked ancestor
  cannot redirect a later read outside the selected package. Persisted input decoding shares
  capture's semantic checks, including forbidden execution metadata and credential-free provenance.
- User artifact publication rejects native directories outside the actual user's home before native
  setup mutations. Supporting those directories needs an explicit publication-root ownership
  contract; the current implementation does not infer authority from an adapter's output path.
  Native discovery checks conservatively refuse relevant configured restrictions when their
  effective precedence cannot be established. They do not claim to be a native configuration
  resolver.

The pre-correction full Linux suite passed 9,224 tests with three skips. The lead's lifecycle and
publication corrections passed 52 focused tests and source/test mypy; capture and codec developers
supplied their separate regression evidence. Final combined gates and re-review are recorded below.
Windows capture execution, live VM/user isolation, and native discovery/resume acceptance are not
established by these local results. No operator live-test inventory has been supplied; local
isolated-HOME CLI acceptance proceeds independently. The SDD remains unlocked while its acceptance
obligations are open.

## Final implementation evidence

Muntz and general correctness reviews were clean at `53bd6eca`. Project re-review found one
remaining mixed-owner preflight case: a pending new agent postponed validation of an existing VM's
artifact capture. Creation now checks each existing ancestor independently before secrets, using the
same routing primitives as final session preparation. That correction passed 44 lifecycle/routing
tests and focused mypy. Source corrections preserve reads through traverse-only ancestors and
validate acquisition provenance before producing inputs that cross the persisted codec. All three
private review lanes confirmed the correction and subsequent forwarding-wrapper removal at
`3b0563ce`, with no unresolved material findings. Both callers now use one
`require_session_ancestors` helper. Its 44 targeted tests, Ruff, format and mypy passed. The final
project review's stale pending-confirmation note is corrected here; no implementation changed.

A separate tester drove the public CLI in an isolated HOME, using real DB APIs only to create owner
and capture fixtures. Resource discovery, schema/explain/sample, reference validation, guide
rendering, actual-lineage artifact inspection, missing/stale/unavailable capture reporting and
inactive VM passthrough passed. Inspection continued to work after deletion of its original source
and omitted artifact bodies. SQLite created its standard shared-memory and empty WAL sidecars on
first read; logical DB, resource and configuration content did not change. Fixture HOME and tester
checkout were independently verified removed. No operator state, VM, SSH or model was touched.

The ready handoff requests the normal PR integration-testing pipeline under the operator's stated
protocol. Its live VM, two-user ownership and native-tool acceptance remain explicit pre-merge
obligations. No live inventory was supplied to this lead, so those checkboxes remain open and this
SDD has no lockfile. This records an acceptance dependency, not known remaining implementation work.
The predecessor's acceptance is separate and unchanged.

Final local gate exits at `cd92efd3`: Ruff 0, format 0, full source/test mypy 0, pytest 0 (9,277
passed, seven skipped), and wheel/source-distribution build 0. File lint, locked-SDD check and
Rulesync drift check passed. Website Python and Node tests passed; both root and project-base double
builds and their diffs returned 0. CI's Typer isolation command returned 0. Four capture locking
tests require Windows; other skips retain their own platform conditions. No local result substitutes
for the pending Windows CI run.

| Requirement | Implemented boundary                                                                       | Evidence and remaining acceptance                                                                      |
| ----------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| R1          | Bundle declarations, references, owner inheritance and discovery                           | Resource/schema tests and isolated public CLI                                                          |
| R2-R3       | Bounded workstation/Git capture, normalized complete packages, shared persisted validation | Source/codec regressions; Windows execution pending                                                    |
| R4          | Actual-owner routing, one destination, lazy inactive passthrough                           | Routing tests and source-free public inspection                                                        |
| R5-R6       | Native support matrix, hints aggregation and explicit unsupported results                  | Renderer/probe tests and native CLI observations; scoped native acceptance pending                     |
| R7-R8       | Session/run identity, private placement, staged restart and owned cleanup                  | Migration/backup, publication and real-manager lifecycle tests; two-user VM acceptance pending         |
| R9          | Qualified errors and read-only actual-lineage inspection                                   | Lifecycle errors and isolated public CLI acceptance                                                    |
| R10         | Local artifacts, local Git and dedicated test integrations                                 | Local gates complete; ready handoff requests remaining scoped VM/native acceptance without model calls |

## Published feedback round 1

The complete implementation was pushed once at `60c20002` and marked ready on 2026-09-13 at 06:25
UTC, without `review-requested`. CI passed on Python 3.12, 3.13 and 3.14 and passed all non-Windows
repository gates. The Windows run reported 185 passes, 15 skips and one fixture failure: text-mode
writing transformed explicit CRLF into CR-CR-LF. The fix writes fixture bytes explicitly. Copilot
reported a quota limit, not a code finding.

The saga lead found no ruling-conformance blockers and requested an optional migration-test naming
clarification. The published Muntz lane requested removal of unused `ArtifactOrigin.facet`, one
shared allowed-deferral definition, and a dedicated `ArtifactApplication.artifacts_dir` result in
place of a generic environment list. All were accepted. Core still validates the plugin boundary and
maps only the session's own private directory to the fixed discovery variable. The optional
unknown-suffix warning was declined: unlisted asset types deliberately retain their bytes, and a
warning would also flag expected opaque assets. Native/live acceptance remains open.

Both lanes had reported by 06:35 UTC. Under the operator's explicit early-start permission, round 1
of up to 3 began at 06:38 UTC after returning the PR to draft. Later feedback belongs to the next
batch. All three private review lanes were clean at `504808aa`. The combined suite passed 9,291
tests with seven skips. Ruff, format, full source/test mypy, package build, file lint, locked-SDD
check, Rulesync drift and Typer isolation returned 0. Website Python/Node tests and both site-base
double builds and diffs returned 0. The final bookkeeping commit records this evidence; no
implementation follows those private reviews. Round 1 is complete and the PR returns to ready
without `review-requested` for CI and the remaining acceptance pipeline. Two authorized fix rounds
remain.

## Published feedback round 2

Both saga and Muntz lanes found round 1 clean. The integration tester independently exercised
capture and inspection on a Lima VM: inline hints, workstation rules, complete skills, current/stale
refresh, source-symlink refusal, and the documented guide/resource commands. This is scoped live
acquisition evidence; it did not exercise native harness launches, two-user isolation or model
calls.

The tester found that capture could accept paths longer than the persisted codec permits, producing
an unreadable record. Round 2 accepts acquisition/write-boundary consistency, distinct
future-version diagnostics, local LFS-pointer refusal, Git stdin isolation, and an ADR clarification
of the shared syntax versus separate acquisition contracts. Remaining recommendations are being
assessed against the approved omitted-input handling contract and actual lifecycle callers.

Windows CI failed on a Git capture that had passed the previous run. An independent local experiment
reproduced a real race: Git removed `shallow.lock` between the storage scanner's `is_file` and
`stat`. The resulting public error matches CI, but the CI log does not identify the underlying
exception. The correction must tolerate vanished temporary entries while preserving other I/O errors
and bounds; Windows CI remains required evidence.

The PR returned to draft before changes. The round began after both named lanes reported, under the
operator's early-start permission, with feedback batched through the round-start comment. This is
round 2 of the three authorized rounds. All three private review lanes returned clean at `fe3796cf`:
project review ran 114 tests, complexity ran 321, and correctness ran 343, each with four Windows
skips. Independent real-Git experiments observed a disappearing `shallow.lock` without capture
failure after the correction; injected I/O errors still propagated. Removing early validation,
encoder validation or the shared context made the corresponding boundary regressions fail.

The combined suite passed 9,310 tests with seven skips. Ruff, format, full source/test mypy (841
files), package build, file lint, locked-SDD check, Rulesync drift, Typer isolation and website
Python/Node tests plus both deterministic site builds passed. Initial gate failures were a missing
test-helper return annotation and a plan spelling; their targeted reruns passed. The final commit
records this validation and contains no implementation changes after the reviewed head. Round 2 is
complete; the handoff returns the PR to ready without `review-requested`. Windows CI and the
remaining scoped native/two-user acceptance are still required. One authorized fix round remains.

## Published feedback round 3

The saga and Muntz lanes found round 2 clean; the Windows job's sole failure was in the new
storage-race fixture. It expected a deleted lock file to disappear from an already-started scan, but
Windows can retain its metadata in a directory entry. Counting those bytes conservatively is a valid
snapshot. The fixture must permit that observation and require a subsequent scan to see only the
retained file. The formerly failing pinned-Git capture passed.

The PR returned to draft before this final authorized round. Both named lanes had reported, so the
operator's early-start permission applies. The batch includes feedback through the round-start
comment. Production behavior is unchanged. Project, complexity and correctness reviews were clean at
`832ba62d`. The fixture explicitly exercises cached metadata on Linux; removing vanished-entry
handling still makes its disappearance regressions fail. Project review ran 75 capture tests with
four Windows skips; the other lanes ran their focused storage checks.

Final local gates passed: 9,311 tests with seven skips, Ruff, format, full source/test mypy, package
build, file lint, locked-SDD check, Rulesync drift and Typer isolation. Website code and public
collateral are unchanged from the preceding green deterministic builds. Linux CI on the preceding
head passed for Python 3.12, 3.13 and 3.14. The final bookkeeping commit records this evidence and
makes no implementation change. Round 3 is complete and the PR returns to ready without
`review-requested`. The authorized fix budget is now spent; monitor the final handoff and assess any
further material feedback for operator direction. Windows CI and remaining native/two-user
acceptance remain open; this SDD remains unlocked.

## Additional authorization: user fallback and guides

The operator superseded the inactive-VM default after round 3: core routes an inactive VM's inputs
to user. Inactive user and workspace facets continue to pass their applicable inputs to session. An
activated VM chooses its deferral routes, and activation of an unimplemented facet remains an error.
Earlier completed milestones record the former behavior; this amendment changes the current contract
rather than rewriting that history.

Two additional feedback/fix rounds are authorized, including the confirmed Claude preflight fix. The
first is overall round 4. Keep the PR draft while implementing this coherent change, then repeat
private project/complexity/correctness review, relevant native diagnostics, gates and ready handoff.

- [ ] Implement the default VM-to-user route with lazy inactive user/workspace passthrough and
      regression coverage for user-only activation, independent consumers, stale prior results and
      the diamond.
- [ ] Correct Claude's abbreviated help-option detection and verify the generated carrier against
      the actual supported executable with positive and negative controls, without model calls.
- [ ] Add a harness-integration guide and expand the agent-artifact guide, using validated examples
      and the ordinary discoverable guide topic mechanism.
- [ ] Update current FRD/HLA, permanent contracts and examples; send the revised routing decision to
      the saga owner without rewriting predecessor history.
- [ ] Complete private reviews, local gates and a coherent ready handoff; assess the next published
      batch within the two additional rounds.

Scoped VM/native/two-user acceptance still requires operator inventory and a budget. The additional
fix authorization does not identify a shared host or permit inventing a cloud test environment.
