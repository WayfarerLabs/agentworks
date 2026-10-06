# Transport Improvements: Design and Delivery Sequence

- Status: Additive implementation started from merged #830; production execution factories and
  RunContext remain unchanged. New-guest bootstrap installs distribution Python. The private
  destination-lock implementation and its setup have been removed under the coordination ruling
  below. A database operation-ownership primitive is implemented but not yet wired into production
  orchestration or RunContext; it does not yet prevent production operation conflicts.
- Delivery vehicle: Design PR #830, then additive implementation, consumer migration PR(s), and
  final removal/activation PR; all labeled `sdd:transport-improv`
- Requirements: [FRD](frd.md)
- Architecture: [HLA](hla.md)
- Proposed interfaces and layout: [Execution contract](execution-contract.md)
- Active proof implementation: [Proof LLD and evidence](proof-lld.md)
- Proposed lifecycle: [Execution profiles and supervisor design](execution-lifecycle-lld.md)
- Shared I/O experiment: [Carrier I/O candidate](carrier-io-lld.md), pending joint review/proof
- Detailed candidates: [Preparation and results](preparation-lld.md) and
  [file operations](file-operations-lld.md), with their substrate decisions and proofs still open

The required order is: settle the transport-owned small contract, prove it, reconcile both SDDs,
build independently in parallel, validate complete workflows, add the new RunContext surface,
migrate consumers in separate PRs, then physically delete legacy and activate permissions. The proof
is a bounded joint slice, not permission to start the broad rebuild. This proof now has a
transport-side implementation, local tests and successive joint live reports. The
[proof evidence](proof-lld.md) records acceptance of the finite-input slice and its limits. No
broad-build or production-cutover gate is completed by those measurements.

## Current acceptance checkpoint, 2026-10-06

Public feedback/fix round 1 of the operator-authorized 3 began after the one-hour collection window
and the complete
[native round-9 report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6008723722).
The PR is draft with `review-requested` removed while the fixed feedback batch is corrected. Its
published head at the batch start was `08bf36fd6`. Intermediate draft WIP publications do not close
the round or request a new public review or integration-test run.

Native Linux, macOS and Windows measurements establish the tested caller-private Create and explicit
Replace behavior, not complete supported-path or production acceptance. Stock macOS home ancestor
ACLs still refuse; hardened guest admins cannot read PID 1 through `hidepid=1`; and the bounded
snapshot exchanges produce about 20 KiB/s over remote SSH. The report's SSH composition used
`508192ac`. Current SSH `dfe02e81` already contains transport `08bf36fd6`, so the next report must
identify fresh integrated pins instead of carrying forward that older test-file conflict.

- [ ] Correct macOS ancestor ACL admission and share POSIX custody, staging and Create mechanics
      without weakening caller-private staging or the approved host-specific Replace semantics.
      Prove ordinary home paths and conservative metadata/object refusals natively. State the
      Windows guarantee under existing caller authority, including already-enabled backup
      privileges.
- [ ] Make fresh boot/init observation work on hardened guests for preparation, WSL holds and
      selected-UID file effect gates. Preserve the fence and perform ordinary file effects as the
      target user. A privileged initial probe alone does not fix later same-UID gate observations.
- [ ] Add one bounded live stream of an already-held snapshot between the existing begin and exact
      cleanup exchanges. Keep buffered carriers' bounded chunk path; no replay or alternate-path
      fallback after possible dispatch. Require digest, stream completion, cleanup and normal
      carrier termination before local publication, and measure remote SSH throughput natively.
- [ ] Share managed stop/disposal attempt registration and exceptional settlement while keeping
      action-specific exchanges, accepted states and results explicit. Specify common local-stage
      abort guarantees, not identical host cleanup ordering.
- [ ] Complete private core composition corrections before publishing that increment: cap explicit
      execution deadlines by the original operation budget, retain an exact caller cleanup handle on
      unresolved pre-yield failure, and fence each body invocation to the prepared guest rather than
      merely its distribution name. A host-only precheck does not close the dispatch race.

The private core composition at `152b8cec8` acquires the actual database claim before bounded
passive power observation and selected WSL hold startup. Its full local suite passes 14,184 tests
with 50 skips; strict mypy passes 1,197 sources. Three private reviews found the deadline,
cleanup-handoff and stale-body gaps above. This is an implementation checkpoint, not a completed
factory, native availability proof or public RunContext surface. No database data has been deleted.

The correction at private `340bdf015` caps caller deadlines by the original operation deadline and
retains the exact workflow through an exceptional cleanup fact, including failures before the
context yields. Its finite cleanup retry cannot reopen body admission or repeat hold startup.
Finalization resumes the owner's existing database-release reconciliation after an interrupted reply
rather than reading a claim that may already have been removed. All three independent private review
lanes clear these corrections; the lead's adjacent run passes 241 tests and strict mypy passes 1,196
sources after removal of the obsolete migration-guard test. The prepared-guest body fence and the
other native gates above remain open. This private correction has not changed the published head or
exposed a production RunContext surface.

The private POSIX correction at `363e2b368` clears project, Muntz and generic review. The lead's
publisher/coordinator run passes 174 tests with one skip, including actual Linux search-only
ancestor permissions. macOS deny-only ACL inspection remains synthetic evidence pending stock-home
native validation. The snapshot-stream unit at `c74f093d` likewise clears all three lanes after
correcting the public reducer's missing stream-phase mapping; four real FileAccess failure cases now
preserve typed sanitized transfer errors and retained scratch custody rather than raising KeyError.
Native paired SSH throughput and heap/retention evidence remain open.

The lead's separate shared native-stdin candidate at `7059b8a83` passes 104 adjacent process tests
with one skip, the full local non-integration suite (14,226 passed, 50 skipped), strict mypy (1,198
sources), Ruff/format and file/SDD/Rulesync/website gates. All three independent private review
lanes clear that exact candidate. Actual SSH/Windows terminal proof remains pending; those local
gates neither publish the contract nor complete terminal delivery. The combined private branch at
`e769eaeaa` also passes the full local suite after snapshot-stream integration (14,252 passed, 50
skipped), Ruff/format and strict mypy (1,198 sources).

The shared stop/disposal custody implementation at `61eb4668f` clears all three private lanes,
including registration and release interruption mutations. Test-only typing cleanup at `1065f724d`
removes the added exemptions without changing production code. The lead's integrated adjacent runs
pass 43 action tests and 17 native-operation composition tests; strict mypy passes 1,200 sources.
These are local custody tests, not native production job-action acceptance.

The hardened-guest bootstrap at `f1ab22682` clears all three private review lanes after its direct
bundle test was corrected to supply the real trampoline's argv shape under default parallel pytest.
The lead's integrated runtime/loader/guest-identity run passes 119 tests; Ruff/format pass and
strict mypy passes 1,202 sources at `c6564006b`. Root admission precedes the sole privileged
init-stat open; exact target credentials and capability checks precede helper loading. The held
descriptor provides fresh bounded reads and closes across exec without a process-global fork hook.
This remains a separate private primitive, not a completed body fence or permission-policy surface.
No production consumer uses that entry yet and no database data has been deleted.

The draft WIP publication at `d9315847c` makes the reviewed shared stdin dependency available to
SSH. Its Python tree is identical to `e769eaeaa`; the final commit only formats the plan paragraph.
Fresh local validation passes 14,252 tests with 50 skips, strict mypy (1,198 sources), Ruff/format,
typer isolation, file lint, locked-SDD and Rulesync checks. Website validation passes 160 Python and
103 Node tests and both deterministic double-build comparisons. Native acceptance and the public
round remain open. The publication excludes the later managed-action and hardened-guest bootstrap
units. At private `508c59802`, their combined full suite passes 14,282 tests with 50 skips but fails
three snapshot request-size cases: the fixed helper plus maximum manifest exceeds QGA's 65,536-byte
HTTP body limit. Root-bootstrap delivery requires further packaging work before consumer adoption;
no provider limit or request-bound test has been weakened.

The privately reviewed packaging unit at `2b1f4018a` replaces the duplicate observer with one
canonical identity checkpoint and separates four file-module responsibilities. All three code lanes
found no material implementation failure; the complexity lane identified the obsolete README
description, which now reflects mandatory full guest checking and single module loading. Follow-up
`ab9cc6a80` removes the dead reader-rebinding emitter and the redundant decoded-name check,
retaining the early identity-dependency check. Its local file/bootstrap/runtime run passes 1,648
tests with 25 skips, including isolated distribution Python 3.11. Under the worker's CPython 3.12.13
test values, actual QGA serialization measures an ordinary 21-vector maximum of 61,974 bytes and
root snapshot bodies of 64,048/64,090 bytes with a 32 KiB manifest. The separate reviewer measured
slightly different root values at the earlier pin; these fixture measurements are not fixed sizes or
a promise for every identity. A valid large-group publication request is refused before provider
dispatch by the aggregate carrier bound. No protocol/carrier limit was reduced, test weakened or
database data removed. Final code and collateral re-reviews are clear at `6781521f3`; its Python
tree is identical to the full-suite pin `912a85328`, which passes 14,296 non-integration tests with
50 skips. Strict mypy passes 1,206 source files. Native root behavior, producer/body fencing and the
remaining publication gates remain open. This does not close public feedback/fix round 1 or
authorize merge readiness.

The shared resize candidate at `099800b35` adds one owner-mediated POSIX SIGWINCH notification
without another process owner, native handle exposure or a general signal API. Its local results
distinguish refusal, kernel-call acceptance and uncertainty; remote terminal acknowledgment remains
outside that evidence. Fresh Linux non-integration validation passes 14,263 tests with 50 skips. The
private project and complexity lanes found a Windows test-selection error and a test scheduling
race. Both are corrected, and all three independent lanes clear the final candidate. The unsupported
platform mock now restores within a bounded context and uses the directly imported module; strict
mypy passes all 1,198 source and test files. This record does not close native terminal,
cleanup-entry interruption, production RunContext or public feedback/fix round 1.

The named-account early admission leaf at `792ae935d` clears all three independent private review
lanes after sharing the root-launch wrapper. Numeric full-guest checking remains mandatory; the
separate named body receives its fixed reader only after verified credentials. The worker's focused
selection passes 167 tests; the project and complexity lanes independently pass 138 and 161 tests.
Successful transitions remain mocked, while generated-source non-root refusal runs on Python 3.11.
The unchanged numeric snapshot sizing fixture measures 64,568/64,610 bytes against QGA's 65,536-byte
limit. No source pruning, guest module split, protocol ceiling reduction or data deletion was used.
The integrated preceding head `3fdb286fe` passes 14,307 non-integration tests with 50 skips and
complete local gates. The new leaf's integrated full run at `5dba2ab68` passes 14,357 tests with 50
skips, and strict mypy passes 1,208 source and test files. Its Python tree is unchanged by the
subsequent collateral review. Initial hold/query/probe, durable launch/body account recovery, native
transitions and complete production fencing remain open. This leaf completes no public operation,
permission boundary or feedback/fix round.

The private early-consumer units at `6e4712729` and `9dea16be7` adopt named admission for the WSL
hold, query and initial canonical guest probe. The first worker's focused selection passes 222 tests
plus 74 adjacent tests; the second passes 246 focused tests. Both affected static selections exit 0.
Generated bodies run on Python 3.11 with mocked successful credentials; actual non-root entry
produces runtime readiness followed by refusal, not guest evidence. New version-4 hold records
separate root launch and named body accounts, while canonical version-3 recovery preserves its
former route and version. The initial probe uses a separate carrier under the existing borrow;
ordinary file and command delivery remains unchanged. This supersedes the earlier private
same-carrier probe/dispatch composition. Integration review, complete gates and native acceptance
remain pending; ordinary file/service body fencing and production composition remain open.

The private follow-up at `807637168` rejects unsupported WSL runtime selections in the shared
binding factory before hold custody is constructed. The normal platform resolver already selects
Linux system Python; directly constructed operations now refuse outside that same domain before
activation. Composition fixtures distinguish the initial root route from ordinary account, command
and file delivery without depending on uncompressed source text. The worker's affected and adjacent
selection passes 277 tests with no skips; full integration gates and independent re-review remain
pending. Neither synthetic dispatch nor runtime readiness establishes successful native credential
transition.

The integrated early-consumer candidate at `651a81bf8` passes 14,429 non-integration tests with 50
skips. All three independent private lanes clear the correction and retain their whole-unit
verdicts; the project and complexity lanes each independently pass the 52 changed-module tests, and
the generic lane passes 63 focused tests. Removing the runtime guard makes all eight refusal cases
fail. Complete strict mypy passes 1,213 source and test files; the local static, file, SDD,
Rulesync, typer-isolation and website gates exit 0. Website tests pass 160 Python and 103 Node
cases, and both site bases pass deterministic double-build comparisons. The subsequent evidence edit
changes no Python bytes. Native successful transitions, ordinary file/service fencing, all-platform
availability and complete additive RunContext composition remain open. This draft checkpoint does
not close public feedback/fix round 1 or request merge readiness; no database data was removed.

The next private numeric helper unit at `7bfc10603` binds a prepared root-entry plan and full
verified guest in one immutable context. Gate-control and all five snapshot exchanges select their
corresponding fixed-prefix root program when given that context, retaining the ordinary carrier and
the body's independent numeric identity. The worker's related selection passes 559 tests, including
34 new adoption cases. Success exercises real packed family modules with mocked credentials; actual
non-root Python 3.11 entry refuses. Complete snapshot QGA sizing remains 64,568/64,610 bytes.
Operation-owned binding, remaining families, recovery, independent review, complete integration
gates and native success remain pending. The preceding published head's Python 3.14 SSH exit-255
framing assertion failed; its cause is unknown, and the diagnostic correction records only safe
measurements, never payloads. No deadline or conformance assertion is weakened by that diagnostic
change.

The numeric leaf and payload-free conformance diagnostics pass the integrated full suite: 14,464
non-integration tests with 50 skips at `28d504dd7`. The later collateral pin `059daebc0` has the
same Python tree. Private project and generic reviews are clear; the project lane independently
passes all 37 new-adoption and SSH conformance cases. Complete strict mypy passes 1,214 source and
test files, and all local static, file, SDD, Rulesync, typer and website gates exit 0. Website
validation passes 160 Python and 103 Node tests and both deterministic site-base comparisons. These
local results do not resolve the preceding hosted Python 3.14 failure or establish successful native
credentials. Operation-owned binding, recovery, remaining body families and complete RunContext
composition remain open.

The next bounded units extend the numeric context to the remaining file exchange families and add an
explicit version-two durable file-call codec. Version-one bytes and semantics remain unchanged; new
bootstrap-bound records retain root entry and the full guest separately from body identity.
Context-free records remain private candidates or historical evidence, not a production VM fallback.
The codec unit owns only serialization and validation. Operation-owned binding, correct envelope
versions on every update, child/continuation custody, fresh recovery preparation, native acceptance
and public RunContext wiring remain subsequent integration work. No database migration or data
removal is implied by this record extension.

The remaining six exchange families are privately integrated from `c6047bf52`. Their related
selection passes 933 tests, including 139 new cases. Representative maximum-path staging and
publication requests fit the complete QGA envelope, while valid oversized aggregates refuse before
dispatch without fallback. Real packed protocols retain binary transfer, publication conditions,
reconciliation, cleanup and fresh gated observations; successful admission remains mocked.

The separate codec unit at `868014766` preserves version-one bytes and adds explicit version-two
bootstrap records with a derived payload version. Its 170 codec tests and related 1,669-test
selection pass with 25 skips. Immutable bootstrap facts add the same bytes to initial and retained
records, so recovery-growth reserves are unchanged; exact maximum retained records reach 8,192
bytes. These units await independent integrated review and full lead gates. Neither binds production
operation state, migrates envelope-version callers, prepares fresh recovery facts or proves native
success. The preceding public `a128c09eb` now passes every hosted CI and CodeQL gate, including
Python 3.14; the earlier framing failure remains unexplained rather than classified as fixed.

Private integrated review at `404fa5d2a` is clear in the project and generic lanes. The project lane
independently passes 309 focused and 59 adjacent cases; the generic lane passes 265 new cases. The
complexity lane's accepted simplification removes redundant reconstruction of already-validated
frozen bootstrap objects and nine constructor-bypass tests. Persisted decoding and cross-record
target/gate validation remain intact. The corrected codec selection passes 161 cases, with its
related 1,669-test selection unchanged. A separate fixture-only correction normalizes exactly four
new test namespace directories, not production paths, and passes all 139 family cases both normally
and under this workspace's inherited-ACL build directory. Integrated re-review and final lead gates
remain pending.

The first lead run at `404fa5d2a` passes 14,729 CLI tests with 50 skips, but one unchanged website
browser input case fails while that run is active. The isolated case then passes, and a separate
full website run passes all 160 cases. Its first failure's cause is not established; no browser
assertion or timing was changed. The final publication must retain that failed-run evidence rather
than describe the first validation as entirely green.

The corrected integrated pin `97e4cc118` passes 14,720 non-integration CLI tests with 50 skips. All
three private lanes are clear: project and complexity each pass 300 focused cases, generic passes
256 new cases, and the complexity lane independently proves the ACL fixture correction by observing
14 failures without it and 14 passes with it under inherited ACLs. Complete strict mypy passes 1,217
source and test files; Ruff, format, file quality, typer isolation, locked-SDD, Rulesync and
whitespace checks exit 0. Final website tests pass 160 Python and 103 Node cases, with four builds
and both deterministic comparisons. This closes this private unit's local validation, not the
native, production-binding, recovery or RunContext gates. Public feedback/fix round 1 remains open
and the PR remains draft without a review/readiness signal. The next private unit forwards the same
context through file workflow bindings and JSON children before operation-owned adoption.

Hosted run `37437893374` at `e0ad14ded` subsequently passes every required check, including Windows
Python 3.13 and Linux Python 3.12/3.13/3.14. The earlier Python 3.14 framing failure remains
unexplained; a later green run does not establish its cause or a fix.

The private workflow-context unit from `387859250` adds one optional final field to each existing
binding. Download, upload, JSON and single-call workflows forward the same immutable context into
every file exchange, including reconciliation and cleanup, and retain it in their ordinary and
control outcomes. JSON derives upload children with that context rather than having a fixture supply
it. The worker's 48 focused cases and 625 related cases pass; its complete strict mypy pass covers
1,218 source and test files. The workflow tests use explicit local exchange doubles and do not
establish privileged admission or native success. Independent integrated review and lead gates
remain pending. Core ownership, actual envelope-version callers, fresh recovery and production
composition still need implementation; no checkbox or public feedback/fix round closes here.

All three independent lanes clear workflow code/test pin `2e9e92450`. Each passes the 48 new cases;
the project lane also passes 196 adjacent cases. The complexity lane removes the JSON child forward
and observes seven failures, then removes publication-cleanup forwarding and observes two failures;
restoring both returns all 48 cases to passing. The lead's complete local suite passes 14,769
non-integration tests with 49 skips. Complete Ruff/format, strict mypy (1,218 source and test
files), typer isolation, file quality, locked-SDD, Rulesync and whitespace gates exit 0. Website
validation passes 160 Python and 103 Node cases, four builds and both deterministic comparisons. No
native backend was exercised. Publication changes only the validation record and prose formatting
after that pin; operation-owned binding and exact envelope-version consumers are the next private
unit. The earlier browser and Python 3.14 framing failures remain unexplained despite the later
green gates. Public feedback/fix round 1 remains open, without a review/readiness signal.

The private operation-context unit at `a5678fe5b` now binds one immutable bootstrap in
`FileOperation`, matching its VM boot before any call. Each preparation and all three gate setup
paths receive that context before dispatch; bound promotion, JSON children and serial package
children retain it. Admissions and every originating payload update use the typed record's actual
version. Exact package-publication retry repeats the record's version and bytes; the existing
revision/bytes confirmation remains before the next child dispatch. Context-free private calls
retain their version-one behavior, not a production VM fallback. The worker passes 35 focused cases
and its related 2,124-case selection with 25 skips; complete strict mypy passes 1,219 source and
test files. Its exchange and consecutive-child evidence is simulated, not native credential or
root-entry proof. Independent integrated review and full lead gates remain pending. Fresh recovery,
production VM construction, inline and service entry, and complete RunContext remain open; no
checkpoint or public round closes here.

Private project and generic review are clear at `93e134600`; the project passes 35 new and 312
adjacent cases, while generic passes the new cases and a 148-case related selection. The complexity
lane's accepted correction removes an extra returned-version comparison and the test that published
a correct row before substituting a contradictory internal return. The concrete repository already
checks or writes version and bytes together within its transaction. All typed version writes and the
lost-both-publication-replies custody test remain. The corrected worker selection passes 34 new and
2,123 related cases with 25 skips, plus complete strict mypy and file/static gates. Integrated
re-review and final lead gates remain pending.

The preceding lead run at `93e134600` passes 14,804 non-integration tests with 49 skips, complete
strict mypy (1,219 source and test files) and all static/file/SDD/Rulesync gates. Website validation
passes 160 Python and 103 Node cases, four builds and both deterministic comparisons. These results
are for the pre-deletion pin, not a substitute for the corrected state. The published workflow head
`0be885044` now passes every hosted CI and CodeQL gate. Native, recovery and production-composition
obligations remain open.

All three independent lanes clear the corrected operation-context pin `713d57df1`. Project passes 41
focused/adjacent cases, complexity passes 57 core/repository cases and generic passes the 34 new
cases. The final complete lead suite passes 14,803 non-integration tests with 49 skips. Complete
Ruff/format, strict mypy (1,219 source and test files), typer isolation, file quality, locked-SDD,
Rulesync and whitespace gates exit 0. Final website validation passes 160 Python and 103 Node cases,
four builds and both deterministic comparisons. Publication adds validation evidence only after that
pin. Native backend behavior, fresh v2 recovery, production context construction, inline/service
ownership and complete additive RunContext remain open. No database data was removed; public
feedback/fix round 1 remains open without review-requested or ready.

The private inline leaf at `d1bbdbbc9` accepts the explicit numeric-bootstrap context through the
existing INLINE root program, without changing the manifest, stdin protocol or completion evidence.
The worker passes 21 focused, 366 related and 24 resource/adoption cases, complete strict mypy
(1,220 source and test files), changed Ruff/format and full file quality. Packed command/script
success mocks privileged admission; the real non-root entry refuses before request consumption or
application launch. The measured maximum manifest fits representative direct/sudo QGA envelopes and
Windows argv quoting; valid oversized group aggregates still refuse before provider dispatch. The
worker's full suite was interrupted with exit 130 to avoid overlapping the lead's complete run; its
eleven failure markers have no node identities or traces and remain unclassified. Independent review
and lead gates are pending. Operation-owned execution context, fresh recovery, service entry and
native production proof remain open. No public handoff or feedback/fix round closes here.

Project and generic review clear the first inline pin `79dda7ae8`. Project passes 21 focused and 357
adjacent cases; generic passes the focused cases. The accepted complexity correction removes
positional AST extraction and split execution from the test mock. A one-shot profile observer now
substitutes only admission at bootstrap entry, then disables itself and runs the delivered program
intact. An inert wrapper statement produces thirteen failures with the former mock and none with the
corrected mock. The owner passes all 390 focused/related cases and all 21 perturbed-wrapper cases,
complete strict mypy and scoped/file gates; production bytes are unchanged. Corrected integrated
re-review and lead gates remain pending, without any native or production adoption claim.

All three lanes clear corrected inline code/test pin `919c54156`. Project passes 378
focused/adjacent cases; generic passes 21 focused cases. Complexity passes the same 21 cases both
normally and after an inert wrapper statement, then restores its experiment. The final lead state at
`644960423` has identical CLI and website bytes and incorporates the preceding published operation
evidence. Its complete suite passes 14,824 non-integration tests with 49 skips, complete
Ruff/format, strict mypy (1,220 source and test files), typer isolation, file quality, locked-SDD,
Rulesync and whitespace gates. Website validation passes 160 Python and 103 Node cases, four builds
and both deterministic comparisons. Representative maximum-manifest QGA envelopes measure
57,225/57,267 bytes for direct/sudo entry, with Windows quoting at 24,266/24,296 characters; valid
large-group aggregates exceed the carrier bound and refuse. These are sizing and mocked-admission
results, not native Windows or privileged acceptance. The interrupted worker's unidentified failure
markers remain unclassified, not diagnosed by this clean complete run. The published
operation-context head `51d5222d5` passes all hosted CI and CodeQL gates. Operation-owned execution
context, fresh recovery, service entry and native production composition remain open; no review
signal or public round closes.

The private execution-state unit at `864ad91dd` requires an explicit managed target matching the
owner scope, validates the optional bootstrap's full derived VM boot, and forwards one immutable
context before preparation/borrowing. Body identity remains explicit per call. Its four existing
constructor callers now supply an actual prepared or fixture target, with no optional target shim.
The native caller supplies its prepared target only; it still does not construct the bootstrap. The
worker passes 96 focused/adjacent cases, including 14 new cases, complete strict mypy (1,221 source
and test files), changed Ruff/format, full file quality and whitespace. Packed success uses the
existing simulated-admission test helper, not a new fixture framework or native privilege proof.
Independent integrated review and lead gates remain pending. Fresh recovery, native context
construction, service entry, full availability and additive RunContext remain open; no completion
checkbox or public feedback/fix round closes here.

Project and generic review clear execution-state pin `716cd6eaa`, passing 121 and 58
focused/adjacent cases respectively. The accepted complexity correction changes only the existing
kind-mismatch test input to omit bootstrap, isolating owner-kind matching from platform-host
bootstrap refusal. Deleting that owner-kind guard then fails the corrected case; no extra test or
production change is needed. The owner again passes all 96 scoped cases and full strict typing/file
gates. Corrected integrated re-review and final lead gates remain pending, without native acceptance
or a public handoff.

All three lanes clear corrected execution-state pin `8e31905e7`. Project passes 96 scoped cases,
generic passes 14 new cases, and complexity confirms the owner-kind mutation fails the corrected
test before restoring its experiment. The complete lead test log reports 14,838 passed with 49
skips; the retained terminal handle expired before its exit status could be recovered, so that log
is not claimed as an exit-code gate. Complete Ruff/format and strict mypy (1,221 source and test
files), plus four website builds and both deterministic comparisons, exit 0 at that pin. The
preceding production-identical pin passes file quality, locked-SDD, Rulesync and website tests (160
Python, 103 Node). The next combined native state receives a fresh complete gate run. Published
inline head `f49d060bc` passes hosted CI and CodeQL. No public round or merge handoff closes here.

The private native unit at `f7d821c0c` requires ordinary and elevated identity plans before exposing
operation views. It constructs one numeric bootstrap from the prepared elevated plan and the actual
full guest, then shares that object between file and buffered execution state. Its 627 scoped tests
pass, including intact packed stat/read/command success, v2 file obligations, missing-root refusal,
full guest mismatches and cleanup/custody regressions. Privileged admission, external guest
observations and a fixture-owned scratch parent are explicitly simulated; body credentials and
packed helper protocols execute locally. Strict mypy, scoped Ruff/format, full file quality and
whitespace gates pass. Independent integrated review and lead gates remain pending. This is the
private WSL2 factory, not all-platform production adoption. Fresh recovery, service fencing,
RunContext and native acceptance remain open.

All three independent lanes clear final native pin `8abbf28e4`. Project passes 323 focused/adjacent
cases and generic passes 33 native cases. Complexity passes the same 33 cases, observes four
failures after deleting execution-bootstrap forwarding, and passes all 33 with an inert wrapper
statement before restoring its experiments. The accepted correction fixes one earlier README
paragraph that contradicted the implemented private producer. Code/test bytes are unchanged by that
correction. The complete combined lead suite passes 14,854 non-integration tests with 49 skips and
exit 0. Complete Ruff/format, strict mypy (1,221 source and test files), typer isolation, file
quality, locked-SDD, Rulesync and whitespace gates exit 0. Website validation passes 160 Python and
103 Node cases, four builds and both deterministic comparisons. Publication adds evidence only after
that pin. SSH head `699c8517e` also passes hosted CI; this does not prove paired composition or
production trust/provisioning workflows. Native successful transitions, fresh v2 recovery, service
fencing, complete platform availability, terminal delivery and additive RunContext remain open. No
database data was removed. The PR remains draft with no review-requested or ready signal; public
feedback/fix round 1 stays open.

Fresh recovery composition exposes a missing ownership seam: takeover seals ordinary registration,
while the existing WSL2 hold recovery only reconciles the predecessor anchor and cannot own a
renewed availability span. Read-only scouting and independent project/complexity design reviews
select separate bounded recovery-support obligations under the same claim, not a multi-anchor
payload or a second VM claim. The lifecycle response now specifies atomic recovery-only
append-and-admit, exact persistence retry without launch replay, unchanged ordinary seals and
retention of every predecessor/support debt across another takeover. This changes no accepted
requirement or stopped intent and activates no permission enforcement. Implementation, private
review and native proof remain open.

- [x] Implement atomic recovery-support admission in the existing owner/repository, preserving
      generation fencing, bounds, exact retry, serial custody and sealed ordinary registration.
      Prove rollback/lost reply, stale owners, terminal/conflicting rows and recovery of recovery.
- [ ] Bind the concrete recovery availability span and fixed read-only preparation through existing
      recovery dispatch. Retain every old and new cleanup debt and exact carrier/lifetime binding;
      never treat observed guest or anchor facts as a lease. Then adopt actual record versions and
      fresh prepared facts in all retained-file adapters. Native acceptance remains required.

The private service-entry unit at `5dc498d11` validates canonical bounded controller/guest argv
data, then reuses the existing numeric admission and INLINE loader before controller/store modules.
It transports actual controller credentials and the full guest without changing workload request or
launch-fact schemas. Its 231 focused/adjacent tests pass, as do complete strict mypy (1,222 source
and test files), Ruff/format and full file quality. Admission and guest observations are simulated,
not native root/systemd proof. Representative complete Proxmox envelopes measure 64,127 bytes for
10,000 workload-input bytes and pass; 12,000 input bytes produce 66,791 bytes and refuse the
existing aggregate bound. Independent integrated review and lead gates remain pending. Outer managed
helpers, later workload-child admission, native acceptance and public RunContext remain open. This
is private work, not a public handoff or feedback/fix round closure.

The private support-admission unit at `ae1f8d02a` adds one owner/repository operation that
atomically retains a new possible-effect row under exact sealed recovery ownership. Required
caller-retained IDs support exact persistence retry, without uncertain dispatch replay or promotion
of predecessor registered rows. It retains all old debts, existing row/payload bounds, generation
fencing and serial custody. Its 99 focused/adjacent database/owner tests and complete strict mypy
(1,222 source and test files), Ruff/format, full file quality and whitespace gates pass. No schema
or data was changed. Independent integrated review and lead gates remain pending. The concrete
availability span, read-only preparation, v2 adapter adoption and native proof remain unfinished;
the support implementation gate stays unchecked until those scoped reviews and gates complete.

All three independent lanes clear the combined service/admission pin `e1128eb96`. Project passes 344
focused/adjacent cases; generic passes 128 service/owner/repository cases and 41 controller cases.
Complexity passes 128 scoped cases, observes two relevant failures after removing the serial
admission and requested-root guards, and restores its experiments. The lead's complete combined
suite passes 14,885 non-integration tests with 49 skips, 27 existing fork warnings and exit 0.
Complete Ruff/format, strict mypy (1,223 source and test files), typer isolation, file quality,
locked-SDD, Rulesync and whitespace gates exit 0. Website validation passes 160 Python and 103 Node
cases, four builds and both deterministic comparisons. The atomic support-admission checkbox now
records only that implemented, reviewed boundary. Concrete renewed availability, fresh recovery
preparation, retained-file adapter adoption, native service proof and additive RunContext remain
open. No schema or database data was changed. Publication remains draft WIP without ready or
review-requested; public feedback/fix round 1 remains open.

The private recovery-hold unit at `4d58174fc` adds an explicit startup entry under the existing
sealed recovery owner, then reuses the same single-use anchor, transition lock, READY publication
and exact release. Independent project/complexity design review rejected another launch-custody
coordinator: an unresolved support row and retained native object own the intentional live effect.
Takeover between admission and launch inherits debt rather than promising a remote dispatch fence.
The worker passes 238 focused/adjacent cases, including 24 new SQLite cases, complete strict mypy
(1,224 source and test files), Ruff/format, file quality and whitespace gates. The sole initial
fixture failure was corrected to inject sibling work once at READY, not again at EXITING; no
production semantics changed. Its base sync adopts the lead's published plan-only formatting fix
without changing owned source/test bytes. Native calls remain faked. Integrated code reviews and
lead gates are pending. Outer availability composition, fixed fresh preparation and retained-file
adapters remain open; no broader completion checkbox or public round closes.

All three independent lanes clear the recovery-hold pin `08dfb40cd`. Project passes 244
focused/adjacent cases; generic passes 24 new and 37 existing hold cases. Complexity passes 65
hold/repository cases after restoring its deletion experiments. Removing the early ID check still
lets the repository reject invalid spelling, but consumes startup and performs native observation; a
temporary invalid-then-valid same-object probe confirms why the small preflight earns its place. The
lead's complete suite passes 14,909 non-integration tests with 49 skips and 27 existing fork
warnings, exit 0. Complete Ruff/format, strict mypy (1,224 source and test files), typer isolation,
file quality, locked-SDD, Rulesync and whitespace gates exit 0. Website validation passes 160 Python
and 103 Node cases, four builds and both deterministic comparisons. The published predecessor
`1490c034c` passes hosted CI and CodeQL. Outer availability composition, fresh preparation,
retained-file adapters and native acceptance remain open; no public round closes.

Source-backed preparation scouting finds that anchor history cannot account for delayed finite
guest/account queries across takeover. Independent project and complexity review select one separate
version-one, empty-payload `carrier-dispatch` row per logical serial preparation batch, reusing
ordinary fixed-helper meaning rather than adding a codec or per-query registry. Resolve it only
after all concrete queries settle and the batch stops; unknown/control/coordination outcomes retain
it independently of the hold. Native endpoint/drain reconstruction remains unproved, not supplied by
this empty record. The lifecycle response records this refinement, and its bounded implementation is
delegated. It does not complete fresh preparation or outer recovery.

The private preparation-batch unit at `011d1b9fb` pins a native binding, sealed recovery owner and
fresh ID, then admits one separate empty `carrier-dispatch` row. Reused guest/locator and numeric
account composers execute serially through exact recovery custody. Unknown or interrupted queries
retain the batch; a lost resolution reply can reconcile only its stopped, settled row without
replaying probes. The worker passes 362 focused/adjacent cases, including 52 new behavioral checks,
scoped strict mypy, Ruff/format, full file quality and whitespace gates with exit 0. Integrated
independent reviews and lead gates are pending. Outer durable-ready availability composition,
retained-file adoption and native endpoint/drain evidence remain open. No public round closes or
broader completion checkbox changes.

All three independent lanes clear preparation-batch pin `2d95648e2`. Project passes 211 batch,
ordinary preparation and ownership cases plus 91 adjacent native-operation/binding cases. Complexity
passes 221 after restoring experiments: deleting fresh-ID refusal permits an existing row, while
deleting resolution-ready state breaks both lost-resolution-reply cases. Generic passes 150 and
withdraws its initial material finding after contract review: interrupted local dispatch open/close
can retain same-controller custody, but neither loses durable debt nor permits replay or false
success. Project independently reproduces both states with four passing focused cases. The lead
passes 14,961 non-integration tests with 49 skips and 27 existing fork warnings, exit 0. Complete
Ruff/format, strict mypy (1,226 source and test files), typer isolation, file quality, locked-SDD,
Rulesync and whitespace gates exit 0. Website validation passes 160 Python and 103 Node cases, four
builds and both deterministic comparisons. Published predecessor `6b55e8215` passes hosted CI and
CodeQL. No native acceptance or public round closure is claimed.

Before production cleanup completeness, add a bounded local reconciliation follow-up for an
unreturned dispatch open and an interrupted close after all queries settle. Current conservative
retention is truthful, but can keep a VM claim even when no probe ran or all probes terminated.
Retain exact local handles before activation; cleanup may close only that handle with no outstanding
attempt and may retry only stopped, settled row resolution. Never clear a different dispatcher,
reopen observation admission, infer native drain from the empty row or replay probes. This is not an
owner-transfer framework or general signal-atomic bookkeeping promise. Outer durable-ready
availability composition, retained-file adoption and native endpoint/drain evidence remain open.

The private local-cleanup follow-up at `3acac1653` retains an exact dispatcher candidate before
activation and clears that slot before a reused binding's next preflight. Same-dispatch close can
retry only its own validated close transition; private cleanup of an unreturned opening refuses
outstanding or conflicting custody. The batch separates stopped-query accounting from whether close
returned, so resolution retry may finish only local cleanup and its own row without another probe.
The worker passes 319 focused/adjacent cases, scoped strict mypy, Ruff/format, full file quality and
whitespace gates, exit 0. It adds no activation marker, schema, ordinary-borrow change or wider
adapter migration. Integrated reviews and lead gates remain pending. Outer availability,
retained-file adoption and native drain proof remain open; no public round closes.

Fresh version-two recovery compares logical numeric root/body credentials, not equality of
delivery-relative transition modes. Source-backed project review confirms that the numeric bootstrap
consumes the body's UID/GID/complete groups separately from the selected root-entry launcher. Native
direct root entry and a historical SSH sudo entry can therefore preserve the same bound authority
without rewriting the retained record. The file LLD now distinguishes immutable historical plans
from a freshly prepared current launcher; no mode fallback, changed non-root body, version-one
reinterpretation or drain inference is permitted. This is response clarification, not implemented
adapter adoption or native privilege proof.

Independent project and complexity lanes clear local-cleanup pin `7652235c9`. Project passes 320
focused/adjacent cases and confirms that normalization after a resolved row does not touch a
successor owner's rows or dispatch. Complexity passes 120 after restoring deletion experiments;
removing either truthful retention or completed-resolution normalization breaks its regression.
Generic passes 63 and identifies the corrected post-resolution interruption: cleanup retry now
clears retained local coordination without another close, row update or guest probe. A separate
repeated interrupted opening on one low-level binding is outside this private cleanup contract: the
sole production caller retains a fresh exclusive binding, opens once and retries cleanup, never
reopening. Project independently confirms this reachability boundary; preserving a prior candidate
on every refused reuse would instead permit closing an earlier legitimate caller's dispatcher. No
general resumption framework is added.

The lead's complete suite at this pin passes 14,972 non-integration tests with 49 skips and 27
existing fork warnings, exit 0. Complete Ruff/format, strict mypy (1,227 source and test files),
file quality, locked-SDD and whitespace gates exit 0. Website validation passes 160 Python and 103
Node cases, four builds and both deterministic comparisons. The published predecessor `a424fe3f6`
passes hosted CI and CodeQL. Outer retained availability, fresh version-two file-adapter adoption,
native predecessor drain and full integration acceptance remain open. Publication is draft WIP; no
ready/review-requested signal, broader completion checkbox or public round closure is claimed.

The private passive Proxmox power unit at `d820fd0d3` adds one fixed body-free provider
`GET /status/current` route to the existing owned HTTP worker and shares verified connection
preparation with the new native resolver. It preserves QGA and legacy behavior, uses configured
trust/token authority and recorded node/VMID, never starts a VM or probes its guest, and rejects
late observations. Exact status values establish power; malformed or unavailable reads are UNKNOWN.
Local configuration/secret resolution is checked against the budget but cannot be forcibly
interrupted. The worker passes 369 focused/adjacent cases, scoped strict mypy, Ruff/format, full
file quality and whitespace gates, exit 0. Integrated private reviews and lead gates remain pending.
Provider-locator composition, whole-operation availability and native acceptance remain open; no
completion checkbox or public round closes.

All three independent lanes clear the Proxmox power unit: project at `4703176ca` passes 201 focused
and 160 adjacent cases with scoped typing/style; complexity and generic at `429c13547` pass 164
restored and 153 cases respectively. The latter pin changes only collateral wording, not source or
tests. Complexity's deletion experiments separately demonstrate that removing early budget
validation reaches secret preparation and removing the final check accepts late results. The lead
passes 15,018 non-integration tests with 49 skips and 27 existing fork warnings, exit 0. Complete
Ruff/format, strict mypy (1,228 source and test files), typer isolation, file quality, locked-SDD,
Rulesync and whitespace gates exit 0. Website validation passes 160 Python and 103 Node cases, four
builds and both deterministic comparisons. Published predecessor `6cd18fa65` passes hosted CI and
CodeQL. This completes local validation of passive power only; whole-platform composition, native
availability and provider identity acceptance remain open. Draft WIP publication closes no public
round or broader plan gate.

The private outer recovery-span unit at `9c3f53e05` composes one retained WSL2 hold and independent
preparation batch under the existing sealed recovery owner, without another VM claim. It requires
explicit administrative identity, fresh VM/site/marker/intent, exact bounded power, copied route and
durable READY before preparation. Per-action guarded carriers bind the actual span, thread and
finite budget and become inert afterward. Close stops only view admission; exact settled batch
cleanup precedes idle validation, while every predecessor effect row must resolve before own hold
release. No unknown probe replays or aggregate owner resolution occur. Lead inspection corrected
plain-string power acceptance and cleanup ordering before the worker's final handoff. The worker
passes 451 focused/adjacent cases, scoped strict mypy, Ruff/format, full file quality and whitespace
gates, exit 0. Independent integrated reviews and lead gates remain pending. Retained-file
version-two adoption, native predecessor drain, other platform availability and complete public
recovery remain open; no broader checkbox or public round closes.

The first span review at `8c4865661` finds one important negative-screen gap: project independently
changes the retained native owner's status to EXITED, yet the READY-time anchor cache still permits
another query. Its 246 main and 89 adjacent cases pass with 9 skips; this reproduced path is not
disproved by those tests. Generic passes 145 and finds no additional issue. Complexity passes 139
baseline/deletion/restored cases plus four independent persisted-identity changes, selecting removal
of duplicate comparisons while preserving both fresh VM reads. The lead's baseline suite passes
15,067 cases with 49 skips and 27 existing warnings; it is baseline evidence, not correction proof.
The worker correction at `5504496d6` reads the current retained-native local snapshot, keeps
historical guest/READY facts separate and rechecks the budget. Actual native-state changes, snapshot
exceptions/interruptions and late reads now prevent preparation/action/dispatch. Redundant
comparisons are removed without removing either fresh read. The worker passes 472 adjacent cases and
scoped static, full file-quality and whitespace gates, exit 0. Integrated re-review and final lead
gates remain pending; no native liveness/drain guarantee or public round closure is claimed.

Before full additive RunContext adoption, address the execution ledger's clean-call capacity. The
current private inline path registers one new carrier-dispatch row per command or script and keeps
resolved rows until owner release. The 128-row total limit therefore refuses the next call even when
every earlier call completed cleanly; preparation and holds reduce that budget further. This is an
adoption gap, not current production behavior: production consumers still use the old stack. The
file-package serial checkpoint already shares one row across many members. Do not silently remove
bounds, prune retry evidence or introduce a generic workflow registry to mask the gap. Choose and
review a bounded execution-specific reuse/accounting solution, then demonstrate more than 128 clean
sequential commands with mixed file operations while preserving exact uncertainty,
registration/resolution retry and recovery fencing. No implementation or capacity acceptance is
claimed at this checkpoint.

All three independent lanes clear the corrected whole span at `d3cc1d970`. Project passes 356
focused/adjacent cases with 9 skips and scoped typing/style, and independently confirms its original
EXITED reproducer now refuses without another query. Complexity passes 197 baseline and restored
cases; substituting historical local custody makes 12 cases fail, while deleting the post-snapshot
budget check produces three failures and six passes. Generic passes 203 cases and finds no
additional issue. Each review tree is restored clean at the exact pin.

The lead passes 15,088 non-integration tests with 49 skips and 27 existing fork warnings, exit 0.
Complete Ruff/format, strict mypy (1,230 source and test files), typer isolation, file quality,
locked-SDD, Rulesync and whitespace gates exit 0. Website validation passes 160 Python and 103 Node
cases, four builds and both deterministic comparisons. Published predecessor `118983e1b` passes
hosted CI and CodeQL. These gates validate private composition and the corrected negative failure
screen, not native continuing availability or predecessor drain. Version-two retained-file adapter
adoption, independent-job availability, remaining native factories, complete RunContext and full
integration acceptance remain open. Draft WIP publication closes no public round or broader plan
gate; no database data or SSH branch is changed.

The version-two retained-file worker at `bb650a15b` delivers the three private adapters with an
actual span-bound action context, exact envelope/encoded version checks and fresh numeric delivery.
Each action revalidates before proposal publication, debt retry or dispatch; free carriers,
escaped/swapped contexts and changed authority refuse. Historical plans/versions survive intended
CAS updates and exact reply-loss adoption. Version one keeps its explicit carrier semantics. The
worker passes 675 adjacent cases, including 82 new SQLite/span and synthetic-wire cases, scoped
strict mypy, Ruff/format, full file quality and whitespace gates, exit 0. Five separate fresh
interpreters import the helper, adapters and span successfully. Integrated private reviews and lead
gates remain pending. DOWNLOAD has no production drain producer; native privilege transitions,
fencing/drain, other platform spans and production recovery remain open. No broader plan gate or
public feedback/fix round closes.

The inline-lifetime worker at `98a69107d` implements one lazily registered empty row shared by the
execution state. Fresh borrows explicitly install/arm and retain that row across clean calls;
bookkeeping retry never replays the request. Final resolution closes only execution admission and
its row, before native aggregate teardown. A first registration lost reply is reconciled by exact
fenced observation during finish without creating a new row. The worker passes 215 adjacent cases,
including 20 new custody cases with 160 actual LocalCarrier commands/scripts and eight actual file
stats, scoped strict mypy, Ruff/format, full file quality and whitespace gates, exit 0. Shared
ordinary/elevated views in that local fixture use the same actual host identity; they do not prove
native elevation. Distinct numeric bootstrap guards retain their separate tests. Integrated
concurrency inspection, all three private reviews and full lead gates remain pending; neither
capacity acceptance nor native/RunContext completion is claimed yet.

Lead concurrency inspection then requests a deterministic verification: finalization could consume a
call after its failed-bookkeeping flag became visible but before original terminal capture ended.
The worker reproduces duplicate capture losing the safe control fact while preserving the original
validation exception; no remote safety failure is demonstrated. Correction `453a66974` serializes
terminal capture and failure-flag publication under the existing short admission guard, without
holding it during carrier execution or nesting it inside finish retry. Four event/barrier
regressions cover finish/retry against control capture and failed handoff. The worker passes 219
focused/adjacent cases, scoped typing/style, full file quality and whitespace gates, exit 0.
Integrated whole-unit re-review and final lead gates remain pending.

The lead's complete baseline at `66a245bb1` passes 15,189 cases with 49 skips and 27 existing
warnings, but fails one private target-composition cleanup test. That test still seals and resolves
the aggregate owner without first finishing the newly retained execution lifetime. The actual native
workflow already finishes execution before aggregate cleanup. The delegated correction retains the
execution state explicitly in the standalone fixture, finishes the clean lifetime and also checks
that uncertain execution refuses finish. Existing dispatch, file and ownership assertions remain.
This failed baseline is not a green gate or correction proof; final combined reviews and full gates
remain required.

Whole-unit review at `3f9e90d73` finds one supported retry gap: the second possible-effect write,
inside attempt admission, can lose its reply before the carrier is entered. Retry relinquishes its
local call without reconciling owner uncertainty, preventing another command. Project reproduces
that path with real SQLite and zero carrier calls, alongside 454 passing adjacent cases. Generic
passes 273 cases without another finding. Complexity passes 168 restored cases and verifies that
deleting explicit arming or fresh root-identity comparison breaks their regressions. Its accepted
deletion removes only an interior borrow-owner comparison, not the lock or current local-state read.
Worker correction `3eb00371e` reconciles the fenced owner before relinquishing the unreturned
attempt. Seven new cases cover second-write failure before/after commit, failed observation, stale
takeover and successor custody. The worker passes 228 cases, complete strict mypy (1,231 files),
Ruff/format, full file quality and whitespace gates, exit 0. The lead also corrects a test-only type
annotation and formats the new evidence paragraph. Corrected whole-unit re-review and exact final
lead gates remain pending; no native, RunContext, broader plan or public-round completion is
claimed.

All three independent lanes clear the corrected whole unit at `fe1a7b4e9`: project passes 461
focused/adjacent cases and scoped typing/style; generic passes 307 cases; complexity passes 175
baseline and restored cases. Independently reverting only fenced reconciliation makes six
second-write, failed-observation and stale-owner regressions fail. Project also verifies that a
conflicting successor retains its borrow, attempt and rows while the old retry refuses. Each review
tree is restored clean at the exact pin.

The lead's exact corrected suite passes 15,201 non-integration tests with 49 skips and 27 existing
fork warnings, exit 0. Complete Ruff/format, strict mypy (1,233 source and test files), typer
isolation, file quality, locked-SDD, Rulesync and whitespace gates exit 0. Website validation passes
160 Python and 103 Node cases, four builds and both deterministic comparisons. Published predecessor
`aebeff4a0` passes hosted CI and CodeQL. These results establish the reviewed private file-recovery
and inline-lifetime mechanisms, not native privilege/fencing/drain, production recovery, independent
job availability, remaining platform factories or complete additive RunContext. Draft WIP
publication closes no public round or broader plan gate; no database data or SSH branch changes.

The next private increment integrates Proxmox worker commits `c5f266597` and `4d93e9562`. One fixed
current-config GET shares the existing verified, deadline-owned HTTP worker; the platform hook
derives a bounded opaque token from the exact configured origin, VMID and current nonzero generation
UUID. Attempted provider failure raises a sanitized connectivity error; unusable generation raises a
state refusal, preserving the shared deliberate-unavailable contract. Node, credentials and CA paths
are excluded from incarnation identity. The preceding sequencing response at `70cd90300` makes
source implementation available for native proof without waiving that proof. The worker reports 405
focused/adjacent tests, complete strict mypy (1,234 files), owned-file style, file quality and
whitespace gates, exit 0. Combined source review and final lead gates remain pending. This is not
native PVE 8/9 acceptance, disabled-generation/adoption policy, request drain, production target
composition or a completed plan checkbox.

Whole-unit review at `3a6cc5436` finds one concrete authority-boundary defect: inherited VMID
conversion accepts JSON booleans or truncates floats before constructing the new connection. The
generic lane reproduces a positive locator for `true` through a mocked provider response; this is
not dismissed because ordinary producers use decimal strings. Worker correction `101aa653f`
validates raw stored VMID once before scoped secret lookup, accepting only positive exact integers
or ASCII decimal strings; legacy conversion/callers remain unchanged. Invalid values refuse without
secret/provider access, and supported integer/string forms preserve the same locator. Complexity
review also replaces deadline-check call ordinals with elapsed fake time at preparation, response,
hashing and failure boundaries (`d8e1a469a`). Deleting the final or post-request check still fails
the revised tests, while a harmless added check no longer changes their meaning. A scoped-secret
fixture now supplies a valid VMID so it continues to test its intended refusal. The corrected worker
reports 275 adjacent tests, complete strict mypy (1,234 files), owned style, full file quality and
whitespace checks, exit 0. The lead's pre-correction baseline passes 15,280 tests with 49 skips and
27 existing fork warnings; it is not final correction acceptance. Corrected whole-unit reviews and
lead gates remain pending.

All three independent lanes clear the corrected whole unit at `b47c8b1f2`: project passes 418
focused/adjacent cases and scoped style/typing; generic passes 235 cases and independently checks
oversized decimal refusal without secret access; complexity passes 235 baseline/restored cases. Its
extra-check experiment passes 75 locator cases, while restoring lossy VMID conversion makes three
admission regressions fail. Review trees are restored clean at the exact pin. The final
spelling-only correction changes no source/test bytes from `09ce9e416`.

The lead's complete corrected suite passes 15,303 non-integration tests, 49 skips and 27 existing
fork warnings, exit 0. Complete Ruff/format (1,274 files), strict mypy (1,234 files), typer
isolation, file quality, locked-SDD, Rulesync and whitespace checks exit 0. Website gates pass 160
Python and 103 Node cases, four builds and both deterministic comparisons. Published predecessor
`ba7350e0e` passes hosted CI and CodeQL. These are source/local-worker results, not native PVE 8/9
generation, restricted-token permissions, adoption, queued-request drain or production
recovery/RunContext acceptance. Draft WIP publication closes no public round or broader checklist
gate. No database data was deleted or SSH branch changed.

A separate private Proxmox existing-VM composition unit is assigned for already-running VMs only,
under ownership acquired before observation and shared through file/DIRECT body and cleanup. Stopped
startup remains refused until a bounded activation producer and its acknowledgment/unknown custody
are implemented. Selected-binding preparation and full guest/numeric body guards are not
per-dispatch provider-route freshness. Existing account preparation remains a bounded read-only
probe; its facts cannot authorize body effects without the prepared full guest fence. These are
explicit later composition/identity gates, not reasons to advertise complete platform availability
or RunContext. At the initial assignment, no implementation or native acceptance was claimed.

The private running-Proxmox composition now integrates worker `2b549bef5` and test correction
`f591e7042`. Ownership precedes passive power, locator and selected root-QGA binding preparation;
one common numeric guest bootstrap and target bind file/DIRECT views through aggregate teardown.
Only already-running Proxmox is admitted; stopped activation remains refused. Static marker and
administrative-account validation precede observation. Preparation and account evidence are retained
on failed/control paths. The shared account seam no longer loses completed/partial facts or replaces
original control when borrow release fails; conservative uncertain custody remains retained rather
than authorizing a body or replay. Worker tests cover SQLite release failures before/after commit.

The worker reports 2,426 focused/adjacent tests with 25 skips, complete strict mypy (1,235 files),
Ruff/format (1,275 files), full file quality and whitespace checks, exit 0. Its final running import
guard correction passes 115 composition/native/identity cases and owned typing/style. The actual
platform and test dependencies are constructed before the guard; packed file and DIRECT bodies then
refuse all retired-root imports. A separate fresh-process test proves only native-module import and
stopped refusal. Fresh full plugin registration still reaches legacy SSH through AWS/Tailscale
registration; this remains a production-factory independence gate, not waived by cached imports.
Whole-unit private reviews and final lead gates remain pending. Native PVE behavior, startup,
provider freshness, account-probe guest fencing and complete availability/RunContext remain open.

The first review pass at `dbf833f96` clears project and generic correctness, with 347 and 169
focused/adjacent cases respectively. Complexity's baseline, deletion and restored selections each
pass 115 cases; removing account observations fails four custody regressions. The lead accepts its
ten-line deletion of redundant concrete Proxmox producer revalidation. Project review also confirms
an existing adjacent guest-preparation defect: a borrow-release error could replace the original
locator control. The new composition uses that seam, so the lead includes the bounded correction
under the existing primary-control contract rather than expanding recovery semantics.

Worker correction `6104ace26` preserves original control plus conservative guest facts across SQLite
release failures before/after commit and removes the redundant checks. Its focused
Proxmox/preparation/identity/WSL selection passes 235 cases, with owned typing/style and whitespace
checks passing. The lead's pre-correction full suite passes 15,341 cases with 49 skips and 27
existing fork warnings; complete static, file, Rulesync and website gates pass. Those baseline
results do not accept the corrected source. Corrected whole-unit private reviews and complete lead
gates remain pending. No public round, native gate or broader checklist is closed by this record.

All three independent lanes clear corrected whole-unit pin `521c57cf2`. Project passes 360
focused/adjacent cases and six-file typing/style, independently confirming original interruption,
observed guest facts, exact owner retention and one carrier attempt before/after SQLite commit.
Complexity passes 182 baseline/restored cases; reverting only the guest-preparation correction fails
12 control/custody regressions. Generic passes 149 composition/preparation/identity cases. All
review trees are restored clean at the exact pin, with whitespace checks passing.

The lead's complete corrected suite passes 15,354 non-integration cases with 49 skips and 27
existing fork warnings, exit 0. Complete Ruff/format (1,275 files), strict mypy (1,235 files), typer
isolation, file quality, locked-SDD, Rulesync and whitespace checks pass. The initial command used
an unsupported locked-SDD option; the corrected positional-base invocation passes against
`cea5e8523`. Website gates pass 160 Python and 103 Node cases, four builds and both deterministic
comparisons. Published predecessor `3d98a1a67` passes hosted CI and CodeQL. These are scripted
provider/account and local packed-body results, not native PVE behavior, startup, provider
freshness, account-probe fencing, plugin-registration independence or complete
availability/RunContext acceptance. The evidence-only update changes no reviewed source/test bytes,
closes no public round or broader checklist and introduces no schema or shared carrier contract. No
database data was deleted or SSH branch changed. The approved explicit macOS/Windows in-place local
Replace behavior and its partial-failure reporting remain unchanged.

The next private wire unit integrates worker `10f5f9160`. A closed explicit endpoint choice replaces
the private current-config flag and adds one fixed body-free VM-start POST and literal task-status
GET. Raw bounded scalar acknowledgment remains separate from dictionary observation. New control
methods require positive finite budgets before worker creation, preserve verified authority and
owned worker cleanup, and introduce no start replay, owner integration or definite-rejection claim.
The worker reports 390 focused/adjacent cases, five-file typing/style, file quality and whitespace
checks, exit 0. Whole-unit private reviews and complete lead gates remain pending.

The lead records pinned start/task research and an explicit future activation custody boundary.
Private complexity review keeps the design but corrects inaccurate source highlights and an
overstated permission claim: a token's owning user can observe its tasks without node audit as well.
The spelling gate also found missing UPID vocabulary, now added for its existing permanent-code use.
These corrections do not enable stopped startup, HA settlement, atomic generation preconditions,
queued-request drain or complete availability/RunContext. Both new plan items remain unchecked.

All three independent lanes clear the whole wire unit at `4f5039478`. Project passes 545
focused/adjacent cases plus five-file typing/style and file quality. Complexity passes 337
baseline/restored cases; deleting guest-route validation fails three cases and sends the escaped
route to the mocked opener, while deleting dot encoding fails two local TLS cases. Generic passes
337 cases. The first complete lead suite finds one missed fixture adaptation: the maximum inventory
test still sends the old private worker envelope. Correction `a00408929` adds the guest-agent
endpoint without changing runtime code or weakening inventory assertions. All three lanes clear that
correction; project and generic each pass 306 relevant cases, and complexity passes 44 inventory
cases. Caller searches find no other missing endpoint adaptations.

The corrected complete lead suite passes 15,465 non-integration cases with 49 skips and 27 existing
fork warnings, exit 0. Complete Ruff/format (1,276 files), strict mypy (1,236 files), file quality,
locked-SDD, Rulesync and whitespace gates pass. Typer isolation and website gates pass: 160 Python
and 103 Node cases, four builds and both deterministic comparisons. Published predecessor
`1a5441722` passes hosted CI and CodeQL. These are local TLS, scripted provider and local helper
results, not native PVE acceptance, validated activation custody, startup, HA settlement, freshness,
queued-request drain or complete RunContext. The next private adapter is delegated under the
existing owner ledger; it does not change the completed wire evidence. Both plan items remain
unchecked, public round 1 stays open, and no handoff signal or broader completion is claimed. No
database data was deleted or SSH branch changed. Explicit macOS/Windows in-place local Replace
retains the approved supported-metadata preservation and partial-failure reporting.

The next private unit integrates activation-custody worker `caa0fd5e9`: one fresh versioned ledger
row before at most one start POST, receipt retention before publication, exact bookkeeping-only
reconciliation and fenced task observation under finite budgets. Its 330 focused/adjacent cases (87
adapter cases), owned typing/style, repository file quality and whitespace checks pass. Whole-unit
source reviews, stopped-VM composition and complete lead validation remain pending.

Private architecture review rejects a disappearance-only settlement draft: an ordinary Proxmox
worker killed during its synchronous fork helper can leave the launch child preparing or launching
QEMU. Project and complexity independently verify the pinned source and clear corrected design
`19ddffa81`: only fully matching stopped ordinary work with exact successful `OK` or supported
warnings may settle this request. Unknown/error/missing evidence and HA handoff retain custody.
Native interrupted-launch and successful/warning acceptance remains open. This corrects the private
draft before publication; no requirement, shared API, schema or broader completion changes.

Project and generic clear whole adapter unit `f19e5fe7f`; project passes 444 focused/adjacent cases
plus 25 Windows-selected cases on Linux, and generic passes 355 cases. Complexity passes 196
baseline/deletion/restored adapter/wire cases, but requests deleting redundant locator construction
and an unused dispatch flag. Removing the successful-outcome condition fails three unknown-result
cases. Its count check also finds an arithmetic error in the worker handoff: the adapter has 87
cases, not 88. The worker confirms the correction; the broader 330-case selection remains accurate.
The lead passes 313 focused/adjacent cases, independently passes all 87 adapter cases and passes
complete typing/style, file quality, locked-SDD, Rulesync and whitespace checks. These do not accept
the pending simplification or stopped-VM composition, and no full new-source suite or native
acceptance is claimed.

Worker correction `c0596c5dc` removes the repeated locator construction and unused dispatch state,
leaving actual wire-call assertions and cleanup behavior unchanged. Corrected selections pass 87,
196 and 330 cases; owned typing/style, repository file quality and whitespace pass. Corrected
whole-unit private clearance and stopped-VM composition remain pending.

All three independent lanes clear corrected adapter pin `44d71d1a6`. Project and complexity each
pass 196 adapter/wire cases; project also passes owned typing/style. Generic passes 317
activation/wire/carrier cases. The project lane carries its prior 444-case whole-unit review and 25
Windows-selected cases on Linux; these are not native Windows acceptance. Each review tree is
restored clean at the exact pin. The stopped-VM consumer is now delegated as the same risk unit: one
retained selected connection, activation custody, task settlement, fresh power, passive guest-agent
responsiveness and one exact guest/numeric preparation, with aggregate cleanup and bookkeeping-only
recovery. Whole-unit composition reviews, complete lead gates and native PVE acceptance remain
pending. The adapter is not yet published; no public round or broader checklist is closed.

Stopped-VM composition worker `a9369907b` now retains the selected root QGA connection and adapter
before one activation attempt, waits for exact successful ordinary settlement, reads fresh power and
polls only fixed passive guest information before one exact guest/account preparation. Failed paths
retain unresolved custody and original control; fresh cleanup performs only bookkeeping and known
task observation. Proven absence of the exact registration row before a returned handle permits
pre-POST cleanup, while a committed row follows adapter reconciliation. Normal teardown leaves the
VM running. The worker passes 891 focused/adjacent cases, including 39 stopped-workflow and 147 wire
cases, six-file typing/style, repository file quality and whitespace checks. Provider/account facts
are scripted and packed Linux bodies run locally. Whole-unit private reviews and complete lead gates
remain pending; native PVE, Windows-native, HA, generation/drain and complete RunContext remain
open. This source is integrated privately without a handoff signal or completion checkbox.

All three independent lanes clear the complete stopped-startup and shipped-registration unit at
`7145e4c2a`. Project carries its full earlier source and adjacent-operation review, then passes 481
corrected cases and 310 final adapter/composition cases. Complexity passes 349 final parallel cases;
generic passes 349 relevant cases and all 87 adapter cases. Project's 63 Windows-selected cases run
on Linux, not a native Windows workstation. The reviews accept removal of redundant interior
carrier/root validation and copied registration inventories, without changing external validation.
Complexity deletion experiments break four settlement-restriction cases, six retained-terminal cases
and both fresh-registration cases; restoring the candidate returns them to passing. Every review
tree is restored clean at its exact pin.

The first complete lead run at `a4375182c` fails one managed-output test with 15,629 passes and 49
skips: the three-byte `abc` disclosure canary matches the hexadecimal address in an ordinary
payload-free object representation. `81262bf7a` uses the same-length `q!z` canary, retaining the
capture limit, exact returned bytes and disclosure assertion. Its complete suite passes 15,630 tests
with 49 skips and 27 existing fork warnings. The complexity lane separately observes a
20-millisecond activation-test budget expiring during SQLite setup before its simulated late reply.
`7145e4c2a` replaces three sleep-based fixtures with controlled monotonic advancement at the late
event, preserving every assertion and deadline. Removing advancement makes all four affected
parameterized cases fail their timeout assertions; the corrected worker selection passes 310 cases.
These diagnoses explain only the observed canary collision and scheduling assumption, not older
unidentified CI failures. The final complete lead suite at `7145e4c2a` passes 15,630 tests with 49
skips and 27 existing fork warnings, exit 0. Full Ruff/format (1,280 files), strict mypy (1,240
sources), typer isolation, file quality, locked-SDD, Rulesync and whitespace checks pass. An initial
static invocation cannot write its default uv cache; the corrected workspace-cache invocation
passes. Website gates pass 160 Python and 103 Node cases, four builds and both deterministic
comparisons. Native PVE, Windows-native, generation/drain, other-platform factories and complete
additive RunContext remain open; no broader checkbox, public feedback round or SDD lock is completed
by these local results. Explicit macOS/Windows in-place local Replace retains approved
supported-metadata preservation and partial-failure reporting; no database data or SSH branch is
changed.

The transport lead owns this entire sequence, not just the API design. The operator confirms the
mandate to build with the SSH developer, migrate all consumers and physically delete the old stack.
The [0.19.0 migration inventory](migration-strategy.md) is the release baseline. The target state
retires `NativeFiles`, while preserving useful domain behavior and evidence through direct
RunContext access. Production callers still use `NativeFiles` until their migration batch lands.

The operator directs publication of this reviewed baseline to `main` before the proof. Publication
gives both efforts a common design reference; it does not pass the proof, complete an LLD or freeze
the SDD. Proof-informed amendments follow through the same artifact owners. The transport lead owns
the carrier contract and acceptance criteria; SSH supplies implementation and feasibility input, not
a separately owned copy of that contract. Requirement changes still return to the operator.

After #795 merged, the operator authorized transport-side PoC work in a new PR while the SSH owner
updates its SDD. Both efforts start from the published contract, without another prerequisite
design-only merge. Transport integrates one joint proof delivery with the SSH contribution; SSH's
complete carrier implementation follows proof acceptance as a separate code delivery. The
[proof LLD](proof-lld.md) records this first implementation's exact subset, placement and evidence
gaps. None of the joint proof checkboxes below is completed by starting that work.

On 2026-09-17 the operator accepted deferring guest cancellation from the buffered PoC. Deadlines
remain local observation bounds. Live tests demonstrated surviving guest process trees after expiry;
the PoC has neither a remote cancellation handle nor a reaper. Recording that limitation does not
waive the production workload-lifecycle gate below or permit automatic replay.

## Operation coordination correction, 2026-09-20

The operator approved database-level operation coordination, unique scratch names and conservative
file checks instead of blanket machine-wide destination locking and privileged host setup. The
completed lock experiments below remain historical records; the lock implementation and its
associated pending acceptance gates are superseded by this ruling.

- [x] Remove destination lock acquisition, setup, bundled dependencies and lock-only failure codes
      from the private file helpers and new-guest provisioning. Preserve Python installation,
      identity checks, object refusal, revisions, bounds, expiry and exact cleanup evidence. Prove
      bounded read/stat and file operations work without an installed lock namespace.
- [x] Implement atomic database operation admission for conflicting resource scopes, with durable
      ownership and explicit terminal release. Keep SQL transactions short; do not hold a database
      write lock during remote execution or coordinate independent databases through a new service.
- [x] Separate whole-operation lifecycle admission and resolution from child dispatch attempts in
      the private ownership primitive. Core can durably arm the claim before activation without
      holding an in-memory borrow, lend the same owner to sequential child operations, and release
      only after core records explicit whole-workflow no-further-effects evidence. Settled child
      attempts never imply that activation, holds, routes, workflow or teardown are quiescent;
      interrupted admission, resolution and release reconcile against the fenced claim.
- [ ] Carry the same operation ownership through core orchestration, RunContext and nested file
      composition. Serialize conflicting exchanges inside that ownership; cover user/admin writers
      and shared platform-host resources without splitting ownership by transport route or identity.
      Retire superseded local harness coordination during consumer migration, not through a second
      competing new-stack lock.
- [ ] Establish one platform-neutral VM availability boundary around every authorized new-stack VM
      operation that can perform guest work, entered after core admits the operation but before
      activation and retained through route, body and teardown. Prove no-op platforms and stateful
      holds with the same caller contract; each stateful hold contributes exact lifecycle evidence
      before whole-operation release. Distinguish pre-activation core custody from the platform's
      actual keep-awake start: check stopped intent and authorize activation first, then start the
      hold before guest work. Explicit stop/reboot share conflict admission but cannot acquire a
      hold that defeats the requested power transition. Cover file, execution, recovery and later
      job actions, not only foreground command paths. Passive readiness/preflight only inspects
      already-existing availability; it cannot activate a VM or start or extend a hold. Preserve the
      distinction between stopped intent and observed power: intent forbids automatic startup of a
      definitively stopped VM, but an already-running VM can still be used. Treat transitional and
      unknown provider states explicitly rather than assuming they are stopped. Implement this
      boundary with its concrete activation producer and selected platform hold; a freely
      constructible active-now token or test-only no-op adapter does not prove availability.
- [ ] Add fixed bounded Proxmox start/task-status wire primitives, preserving scalar acknowledgment
      and object observation as separate envelopes. Prove exact body-free routes, literal task-ID
      encoding, verified authority, finite local worker custody and unchanged guest/power/config
      delivery without advertising operation admission or completed startup.
- [ ] Bind stopped Proxmox activation into the existing exact VM owner before the one start POST.
      Preserve a validated task/node receipt in a versioned obligation, retain missing/lost/late
      acknowledgment and task-observation uncertainty without replay, and aggregate only supported
      activation settlement with fresh power and guest preparation. Cover stopped intent,
      interruption and SQLite transition failures before/after commit. Prove HA handoff, changed
      generations and native PVE 8/9 behavior separately; receipt observation is not queued-request
      drain or atomic generation-conditional activation.
- [ ] Cover pre-context activation and nested teardown when wiring ownership. At `806741ca`,
      `gated_vm_boundary` enters `activation_gate` before assembling its ordinary operation context,
      and `LiveVMNode` constructs a separate gate context. Context factories, harness setup's
      explicit held-guard chain and realization teardown without arguments must retain the same
      core-owned operation when migrated. Adding a field to RunContext alone is insufficient. The
      [2026-09-21 integration inventory](migration-strategy.md#owned-boundary-integration-inventory-2026-09-21)
      identifies common boundaries, bypassing activation roots and retained teardown paths.
- [ ] Require typed aggregate no-further-effects evidence before releasing production ownership.
      Activation, power hold, route/repair, workflow and nested teardown contribute facts to one
      whole-operation decision; no individual component releases the claim. Ordinary success or an
      exception from the legacy `start`, `vm_active`, transient-route or Tailscale-repair APIs does
      not prove quiescence. An uncertain hold exit or cleanup retains the claim. Build this as a
      parallel new-stack lifecycle contract and keep legacy callers unchanged until their migration
      batch supplies the required evidence.
- [x] Implement the bounded durable lifecycle-obligation ledger. Permit several independently
      identified obligations of the same registered kind; commit `possible-effect` before each
      effect; publish bounded, versioned, non-secret adapter-owned recovery identity when observed;
      retain unresolved work; and seal the ledger before whole-operation resolution. Release only
      when every obligation has typed no-further-effects evidence and no in-memory custody remains.
      Keep managed runs specialized and do not add a workflow engine, scheduler, automatic expiry or
      generic payload interpreter.
- [ ] Complete and prove recovery after takeover before any restarted controller acts on an old
      obligation. The generic database takeover kernel already retains one stable logical operation
      identifier, rotates a separate caller-chosen generation, seals the ledger atomically and
      fences stale predecessors. Its exact retries share one local recovery owner and serial guard;
      handled failures retain uncertain dispatch custody. The takeover neither moves obligation rows
      nor proves that an admitted request is drained or remote work has stopped. For every admitted
      obligation, prove that earlier dispatches have drained or cannot cause further effects,
      including work already active, using carrier-proved non-dispatch plus exact absence, an
      operation-specific remote fence, or equally strong synchronous-substrate evidence. Otherwise
      retain the obligation and report incomplete recovery. For WSL2, persist a versioned,
      domain-separated digest of its opaque provider locator, expected VM marker, distribution and
      account plus exact Windows controller identity before dispatch, publish exact guest
      boot/init/PID/start-time after `READY`, and give each `vm_active()` lifetime an independent
      obligation. Replace the legacy hold only after nested lifetimes, delayed delivery, every crash
      window, controller/locator/marker/boot mismatch and production recovery pass live validation.
- [ ] Select shared platform-host resource keys before enabling their admission. Canonical VM names
      are available before create dispatch; site names and authored SSH routes are not canonical
      host identities. Do not silently treat different aliases or users as independent hosts.
- [ ] Prove additive ownership without silently migrating legacy callers: enter the new workflow's
      core operation boundary before activation, carry the claim through nested contexts and
      retained teardown nodes, and validate new-only workflows there. Keep old boundary calls and
      passive accessor construction unchanged. Consumer migration explicitly adopts the new
      operation boundary; neither first accessor use after activation nor a generic legacy exit
      substitutes for acquisition or no-further-effects evidence.
- [ ] Prove crash, disconnect and deadline handling retain unresolved ownership. Recovery must
      establish that prior remote work cannot still mutate before admitting conflicting work, and
      must not replay uncertain mutation or silently expire a claim. Report incomplete recovery
      rather than deleting ownership or inventing remote fencing from a database row.
- [ ] Keep VM-host lifecycle platform-owned. Prove actual Lima readiness, stop, rollback and
      disconnected-operation recovery without requiring a generic macOS MANAGED supervisor or an
      administrator-installed file lock. Keep Linux guest MANAGED guarantees unchanged.
- [ ] Complete the private reviews and gates for this replacement, update permanent collateral, and
      publish the corrected design and implementation as part of the still-draft effort.
- [ ] Resolve the local cleanup-entry interrupt policy before production execution adoption. The
      preparation LLD records the application-boundary inventory and proposed scoped policy;
      operator discussion remains open. Prove both workstation and separate fixed-helper behavior,
      including handler restoration and repeated interruption, without implicitly changing legacy
      provisioning rollback or treating forced process termination as successful cleanup.

The lifecycle-ledger checkpoint is privately accepted at `3f06c91c`. Project, complexity and
correctness review corrected a per-dispatch row budget that would have limited ordinary uploads,
isolated operation transactions from the legacy database connection, preserved late recovery
identity publication while closing, and fenced interrupted admission, close and unresolved custody
handoff. Final reviewers found no remaining material issue after 47 to 50 injected interruption
boundaries. The complete non-integration suite passes 12,819 tests with 21 skips; Ruff, mypy (1,087
sources), file lint, rulesync and locked-SDD checks pass. This evidence accepts the private
primitive, not production orchestration, recovery takeover, RunContext exposure or the #377
follow-up.

The first implementation increment is privately reviewed at `063cd0bc` by the project, complexity
and generic correctness lanes. It removes the lock/setup stack and supplies `Database.operations`,
not production operation coordination. Admission through core orchestration, nested RunContext and
file exchanges, resource-key selection and operation-specific recovery remain unchecked above.

Review restored initial deadline refusal before filesystem access in all five helper families,
preserved phase and known cleanup debt for all four staging operations, corrected unsafe backup
retry guidance and removed redundant typed-interior validation. The eight-case deadline regression
uses synthetic identity rather than Unix-only calls during collection. Final reviewers each pass 34
affected deadline/staging tests. No native VM or host acceptance is claimed. The independent local
launch-owner cleanup-entry interrupt gap remains open and is documented in the preparation LLD; this
increment does not introduce global signal handling or declare launch conformance.

- [ ] Factor one private local process owner for both the ordinary byte pump and SSH's held
      forwarding resource. Prove once-only admission, cancellation before admission, natural exit
      with held input, pipe relinquishment after all I/O users stop, idempotent settlement and
      cleanup-only status separation. Transport owns the interface and implementation; SSH owns its
      consumer adaptation and drainer ordering. This does not close the independently recorded
      asynchronous cleanup-entry interruption or native-workstation acceptance gates.
- [x] Expose the private `LocalProcessOwner` interface and use it in the ordinary byte pump. Local
      tests cover canceled admission, held-pipe natural exit, settlement and failure facts. Private
      review corrected inconsistent snapshot evidence after a pre-start dispatch denial; the real
      audit-hook regression fails with the old publication order. All three lanes are clean at
      `ee2d0a8f`, whose full local suite passes 11,912 tests with 13 skips. SSH forwarding adoption,
      joint proof and the independent asynchronous-interruption/native gates remain open above.

- [ ] Publish and prove the shared owner's explicit EOF/pipe/borrowed-descriptor stdin choice with
      the SSH lane. Keep request construction passive, the adapter-owned descriptor retained through
      interrupted launch settlement, stdout/stderr separate and terminal policy in the adapter.
      Local Linux process tests do not complete supported-workstation terminal acceptance or close
      the independent asynchronous cleanup-entry gate.

### Hierarchical coordination follow-up (#377)

The operator directs compatibility with
[#377](https://github.com/WayfarerLabs/agentworks/issues/377), not completion of all its
functionality in this effort. The [HLA](hla.md#operation-coordination-and-hierarchical-extension)
keeps admission centralized and requires conflicts in both directions between a resource and its
ancestors/descendants. The current `Database.operations` implementation checks exact
VM/platform-host keys only; it is neither a hierarchy nor a complete production lock service. The
completed primitive checkbox above records that exact-key implementation, not broader #377
acceptance.

The platform-host inventory at `f646b04d` found no demonstrated need to serialize every VM operation
on a Lima placement host. Lifecycle commands address one instance; provisioning templates use
`mktemp -d`, and two-hop copies use a fresh transfer UUID. Current remote-create wrapper files under
`/var/tmp` instead use a deterministic instance basename, and rollback reads the corresponding PID.
Their migration must preserve exact attempt ownership, not carry that ambiguous fixed-name cleanup
into the new implementation. VM ownership covers the demonstrated per-VM coordination; it does not
establish a guarantee about Lima-internal shared resources. If a real shared-host mutation requires
admission, canonical host-resource identity remains the gate above. Site, SSH alias and account
strings do not prove equivalence. This inventory is source inspection of
`capabilities/vm_platform/lima.py`, `transports/remote_lima.py` and their remote-execution helper,
not native concurrency acceptance or authorization for a blanket host lock.

Not implemented in the current tree, and deferred to the #377 follow-up unless explicitly brought
into this effort:

- System, workspace, agent, session and console claim types and atomic ancestor/descendant
  admission.
- Independent sibling concurrency inside a VM, multi-resource acquisition and the schema/caller
  transition that prevents fine-grained claims from bypassing existing coarse ownership.
- Long-lived console/session claims and the VM-upgrade-versus-attached-console acceptance case,
  including ordinary detach/release and stale lifetime-claim recovery.
- Operator CLI commands to list locks and explicitly force-unlock, with blocker-specific diagnostics
  and a clear distinction between an unsafe override and evidence-backed recovery.
- The repository-wide concurrency sweep requested by #377. Passing this transport effort's tests
  must not imply that unrelated legacy commands permit all non-conflicting concurrent work.

The transport-owned production wiring and recovery gates immediately above remain required; this
follow-up does not defer them. Existing claim timestamps and bounded operation labels are already
implemented, while the broader inspection and conflict UI are not. No automatic expiry or
force-release is added under the guise of hierarchy compatibility.

- [ ] Review the implemented admission boundary against the #377 extension: centralized resource
      identity/conflict decisions, ownership preserved through activation/nested contexts, and no
      new fine-grained key that bypasses coarse exclusion. Record exact delivered scope and
      evidence.
- [ ] At final SDD closeout, explicitly list in `locked.md` the delivered coordination levels and
      production paths, remaining limitations, and each still-unimplemented item above with #377 as
      follow-up. Keep the issue open unless separately completed and verified. Do not create the
      lockfile early or represent deferred hierarchy work as completed transport implementation.

## Buffered PoC checkpoint record

- [x] Publish the finite-input transport candidate and local fault evidence at `e3d93736` in #826;
      production factories and RunContext remain unchanged.
- [x] Obtain the first joint live report on 2026-09-17 at SSH `1c32e415` containing transport
      `a570a2de`: all eight shared vectors passed on the measured SSH cells and native PVE 8/9. The
      [evidence record](proof-lld.md) preserves gaps and does not declare joint acceptance.
- [x] Close the authorized feedback rounds and retest affected behavior at the final pinned
      transport/SSH combination, with independent cleanup evidence, before proof merge readiness.
- [x] Obtain the second joint live report on 2026-09-17 at transport `d75c0bd3` and SSH `1c32e415`:
      native TLS retesting, the Bash 5.1 floor and cleanup addendum are measured. The Windows SSH
      failure remains unresolved; this is evidence collection, not proof acceptance.
- [x] Obtain explicit live destination-account default-shell evidence through both carriers, and the
      reviewed SSH candidate's Windows disposition and affected-case retest. The eight shared
      vectors alone do not exercise `Shell.user_default()` or establish Windows delivery.
- [x] Retest the strengthened sensitive-reflection vector through both carriers on the final
      integrated head, and obtain affected macOS drain evidence or a precise case-level
      justification. Raw completion alone must not establish bootstrap or application execution.

The final-candidate report at transport `6617f6e6` / SSH `1ccc304b` measures exit 37 with
suppression in all six cells and fresh macOS live/local-pipe evidence. At transport `e41a4482` / SSH
`bc2a0711`, the tester independently verified unchanged runtime and carried those live cells
forward, rather than claiming fresh measurements. The affected macOS socket-fixture retest passed
under normal and long temporary paths; its local execution suite now reports 255 passed, 45
accounted platform-scoped skips and zero failures. The lead independently verified ancestry and
runtime equivalence and ran the combined Linux suite, 296 passed and four skipped. The four
authorized feedback rounds are closed. The final transport delta at `931ad8ef` is documentation
only; its local combination with SSH `bc2a0711` at `2b1f1d39` has an identical `cli/` tree to the
tested SSH candidate. The tester explicitly carried forward native evidence and found no need for
another live retest. Transport accepts the joint buffered proof using those reports and verified
tree equivalence, not an additional tester acknowledgment. The
[proof evidence](proof-lld.md#final-macos-delta-and-unchanged-runtime-evidence-2026-09-17) records
exact pins, independent cleanup and unchanged broader limitations. The
[acceptance disposition](proof-lld.md#joint-buffered-proof-acceptance-2026-09-17) maps the proof
matrix to that evidence. Design reconciliation, the broader contract and production gates remain
open; the SDD is not complete and must not be locked.

## Parallel ownership without overlapping edits

### Active implementation, 2026-09-19

The operator directed implementation after merging #830 at `cea5e852`. The additive delivery branch
is `feat/transport-execution-stack`; it builds the complete new surface without migrating existing
production consumers. SSH proceeds in its own lane. Development delegates use isolated working trees
from the same published baseline; the transport lead integrates their reviewed work.

The first bounded assignments complete the file-operation and invocation/result LLDs and refresh the
RunContext/platform adoption inventory. These close implementation details already called out below,
not another requirements phase or a new prerequisite design-only PR. The lead owns shared types,
carrier-contract changes, composition and the overall plan. SSH implementation files and its SDD
remain SSH-owned. Broad changes wait for their relevant detailed-design/proof gate; limited
experiments and implementation of settled pieces stay outside production until acceptance.

The operator confirmed transport ownership of the shared cgroup/supervisor implementation and
session adoption on 2026-09-19. #770 is closed; its final head matches the preserved requirements
input at `2c406948`. The ownership gate is resolved, not the containment or compatibility proofs.
Recipient permissions and the successor core file ceiling stay inactive until final legacy removal,
while operational safety and deliberately selected profile guarantees apply immediately.

No new public feedback/fix allowance is inferred from the completed #830 review. A coherent
checkpoint receives the normal private reviews and validation before a testing brief and
`review-requested`; the additive implementation is marked ready only when its own gates pass.

The operator separately authorized up to three public feedback/fix rounds for #833. Round 1 began
2026-09-19 at 20:35:58 UTC, after the initial handoff's one-hour collection window and the complete
tester report. Its batch is the checkpoint tester report, the complexity review and its subsequent
directory-depth retraction. The agreed documentation corrections and the separate JSON work unit
passed project, complexity and correctness review at `ac18444f`. The operator subsequently
authorized publication, and those corrections plus later privately reviewed increments were pushed
at `8fec9e07`; its hosted checks passed. Round closure and a new checkpoint handoff remain
outstanding. No second round has begun. The operator subsequently authorized continued
implementation and confirmed three public feedback/fix loops remain available for the completed PR.
Intermediate pushes and private reviews are not public handoffs.

SSH's implementation continues in #832. Its owner agreed to transport extracting the shared bounded
subprocess pump, while SSH retains environment sanitation, carrier-specific report interpretation,
call-site adaptation and combined regression evidence. The shared module is available at
`execution/carriers/_subprocess.py` in published head `8fec9e07`; the extraction does not accept the
separate sink/terminal extensions. SSH also owns correcting its incidental preparation-module
`Command` import when it integrates the new invocation values.

### Initial implementation checkpoint

- [x] Extract immutable command/script values and explicit shell constants into `execution.models`,
      update transport-owned proof consumers, and preserve the buffered carrier interface. Local
      execution tests report 309 passed and four platform-scoped skips; production remains
      unchanged.
- [x] Implement the private local JSON transformation with the four shipped strategies, literal
      null, strict input checks and byte/depth bounds. At `ac18444f`, all 69 focused cases and the
      three private review lanes pass. This returns proposed bytes or a skip decision only; no
      filesystem publication, FileAccess wiring or permission boundary is claimed.
- [x] Add distribution `python3` to the shared early provisioning package list, retaining the Phase
      B package for existing guests. The implementation is included here; local tests cover
      native-bootstrap and cloud-init rendering. Existing-VM native recovery, live provisioning and
      helper compatibility retain their separate acceptance gates.
- [x] Demonstrate the two-phase bootstrap handoff on an owned local Linux PTY with Python 3.11. The
      executable experiment and tests are included here. Payload-ready precedes sensitive transfer;
      interactive-ready follows terminal restoration. Premature input retains raw carriage-return
      semantics, while post-handoff input receives canonical translation. This is not SSH delivery,
      workstation-platform acceptance or application-start proof.
- [x] Implement passive shared `TerminalInput` as the sole terminal input choice in CarrierIO and
      bind the existing two-gate preparation endpoints through its private connector. Require
      explicit borrowed Python descriptors, terminal type and trusted sink output; force sensitive
      bootstrap in the prepared connector, refuse a directly aliased readiness diagnostic sink and
      sanitize ordinary endpoint getter failures without swallowing control exceptions. Prove
      pre-provider/client refusal in existing non-terminal paths. All three private lanes accept
      `0472bcfab`; this does not enable terminal delivery or prove native handles, relay,
      restoration, SSH acceptance or the execution-wrapper finalization gate.
- [x] Compose a private same-identity Linux file read through one carrier attempt without staging,
      spool or lock creation. The implementation includes strict file-response collection and actual
      local Python 3.11 reads; the focused file, inline, terminal and import suite passes 404 cases.
      Native acceptance, stat-only operations, mutation, locking and FileAccess remain separate
      gates.
- [x] Bind private buffered command/script and file-read preparation to one explicit identity plan.
      The shared launcher selects direct delivery, non-interactive root sudo or fixed non-root
      demotion; the guest checks Linux real/effective/saved IDs and normalized groups before
      workload access. At `c3cebea5`, all three private review lanes are clean, including portable
      identity stubs and mutation-proven payload assertions. Account resolution, actual sudo/root
      transitions, native acceptance and production RunContext remain separate gates.
- [x] Resolve a core-bound destination account's IDs/groups through a private read-only helper and
      one carrier attempt. At `1688da5d`, all three private review lanes are clean and all 42 new
      account tests pass. Local Python 3.11 lookup and lookup-to-inline composition preserve the
      distinction between database membership and actual process credentials. This adds no public
      account selector, privilege transition, staging or permission enforcement; native acceptance
      and production composition remain separate gates.
- [ ] Accept the preparation/result and file-operation LLDs after private review and disposition of
      their helper/runtime, launch-evidence, cross-identity operation coordination, and platform
      prerequisites.
- [ ] Integrate shared same-invocation runtime admission into account and file-owner lookup, then
      file, inline and terminal preparation. Preserve complete prerequisite observations separately
      from carrier failures; never infer a missing interpreter from absent evidence. Prove bounded
      prefix forwarding, common loader imports, unchanged sensitive stdin, and clean selection
      refusals without staging, installation or an implicit preliminary probe. Native macOS proof
      and existing-guest bootstrap remain required, not satisfied by local selection fixtures.
- [x] Apply private runtime admission to both account lookup kinds. Bind destination OS explicitly,
      preserve first-existing selection and Darwin shim non-execution, check Python 3.11 and common
      loader imports in the same invocation, and keep prerequisite evidence separate from carrier
      facts and account results. Private review at `66aa55d8` is clean; synthetic faults execute the
      actual trampoline and detect removal of its checks. File, inline, terminal and native adoption
      remain part of the open integration gate above.
- [x] Apply the same private runtime admission to all seven file families and buffered inline
      execution. Seventeen file entrypoints and inline preparation now require `RuntimeSelection`;
      the identity transition encloses selection, prerequisite evidence is independent, and helper
      observations are absent without READY. All three private lanes are clean at `925cbdbf`.
      Terminal, native and production composition remain separate gates.
- [x] Retire the direct-runtime constructors and convert their remaining identity and bundle-sizing
      tests to admitted entrypoints. Private review at `02e8498a` confirms no remaining Python
      callers. The sibling SSH audit at `34a4eb71` also found no callers; complete runtime selector
      and identity prefixes remain in provider-size measurements.
- [x] Admit terminal preparation through the shared runtime selector with a bounded terminal-only
      control-record adapter. Prove LF/CRLF and existing uppercase-output-mode support under real
      local PTY settings without relaxing pipe parsing or releasing payload before raw-mode
      readiness. All three private lanes accept this terminal slice at `02e8498a`. Carrier terminal
      integration, transport-owned finalization and native proof remain separate acceptance gates.
- [x] Implement private revision-aware publication with explicit Create/Replace/Match conditions,
      bounded streaming from verified scratch, stat-only observations without old-content reads, and
      a content-bound post-publication revision. At `75aaaa5e`, all three private lanes are clean
      and the focused file suite passes 169 tests, including conflict, deadline and
      uncertain-publication behavior. External-writer atomicity and native acceptance are not
      claimed.
- [x] Implement the private read-only transaction-lock primitive with local contention, deadline,
      refusal and release tests. At `75aaaa5e`, all three private lanes are clean. The corrected
      post-acquisition deadline test is mutation-proven. Protected namespace setup, ordinary/admin
      sharing and native macOS acceptance remain separate gates.
- [x] Validate finite nonnegative relative budgets at the file request boundary before deriving a
      guest-local expiry. Keep deadline and post-cleanup evidence independent of the removed
      destination-lock prerequisite. All seven private decoders reject wrong-type, non-finite and
      negative budgets; local helper tests prove initial refusal before filesystem access and
      retained deadline/cleanup facts after late expiry where scratch debt exists. Native platform
      acceptance remains a separate gate.
- [x] Review and validate private exact-kind revision-bound object stat/removal and the Debian
      create-time lock setup. Keep setup idempotent without replacing a valid lock inode;
      distinguish local fixture evidence from privileged native bootstrap and ordinary/admin
      contention. All three private lanes are clean at `876355c7`; the file/Proxmox suite passes 305
      tests.
- [ ] Compose fixed inline file-operation bundles with one-stream compression and data-only scratch.
      Prove every final envelope against both complete Proxmox HTTP-body and workstation
      process-command bounds; no executable-helper staging fallback. The Proxmox compatibility floor
      remains 64 KiB even on providers accepting larger requests.
- [x] Review and integrate private held-object metadata, bounded inventory, search-only root
      traversal and read-only fixed lock-namespace composition. Preserve explicit partial/uncertain
      mutation facts and requested-depth completeness without claiming public FileAccess or native
      acceptance.
- [x] Deliver concrete stat/removal helper exchanges using the shared framing, identity check and
      fixed lock. Validate paths, revision/kind and relative time budget at the request boundary;
      prove no staging for stat and no replay after uncertain removal before extending the same
      composition to the remaining file operations.
- [x] Resolve metadata owner/group pairs through the fixed account helper without caller exec or
      identity transition. Preserve account lookup's independent full execution-identity result;
      prove that responses cannot cross the two operation kinds.
- [x] Bring private bounded reads under the fixed transaction lock and guest-local relative budget.
      One host call prepares and dispatches once; the guest materializes the snapshot and checks
      expiry while locked, then unlocks before emitting bytes. Private review at `2ad918bc` is
      clean; removing the final expiry check makes the absence and snapshot tests fail. Native
      acceptance and production FileAccess remain separate gates.
- [x] Deliver private locked inventory and metadata/ensure-directory exchanges. Validate complete
      inventory framing and bounded entries, preserve metadata partial/uncertain effects, and keep
      identity checks before lock and target access. All three private lanes are clean at
      `fef0045d`; the combined file/scratch selection passes 604 tests. Native acceptance and public
      FileAccess remain separate gates.
- [ ] Compose the remaining publication and streaming-transfer exchanges. Preserve exact-leaf
      requests for approved-root operations through core-owned parent decomposition, without
      granting parent/sibling authority. Bind those operations in the complete FileAccess surface
      before the additive RunContext gate, not as a forwarding layer over legacy files.
- [ ] Measure the complete publication helper family and carrier framing before accepting its
      delivery shape. At `5f5ef96d`, the standalone publication test fixture is 31,880 source
      characters, or 31,916 in a minimal Windows Python command. The actual demoting helper and SSH
      serialization produce 33,108 Windows command characters, exceeding 32,767. Dropping the unused
      read-protocol module while retaining framing reduces that fixture to 31,076; its dispatcher is
      still a test, not the production exchange. Select only actual family dependencies and prove
      complete Windows and Proxmox request bounds without an implicit executable-staging fallback.
- [x] Deliver private stage creation and exact-offset chunk exchanges using sensitive input,
      complete typed observations, original destination binding and the fixed transaction lock. All
      three private lanes are clean at `85d27904`, including post-cleanup expiry and exact chunk
      cleanup-debt binding. These two operations do not complete upload, recovery delivery,
      publication, public FileAccess or native acceptance.
- [x] Separate unverified scratch identity/length from final digest verification, permitting
      one-pass upload without rewinding the source or using a whole-file host buffer. Add
      cooperative acquisition/transfer expiry checks while retaining bounded exact cleanup after
      expiry. Private review at `fef0045d` is clean; mutation tests prove final digest verification
      and cleanup-only normalization remain necessary. This is a local primitive, not transfer
      delivery.
- [x] Add streaming from one held source inode into a private snapshot, including source-change
      refusal, deadline checks and exact cleanup evidence, before wiring snapshot/chunk/publication
      exchanges. The local primitive at `5b58e8f5` passes all three private review lanes with the
      pre-existing signal-atomic descriptor-bookkeeping limitation retained explicitly; this does
      not complete remote snapshot delivery or helper lifecycle acceptance.
- [x] Implement bounded immutable scratch ownership receipts, core-allocated tokens and
      identity-bound snapshot creation. Historical reconciliation recovers exact cleanup ownership,
      not ready content or publication authority. All three private lanes are clean at `5a9d3b8f`,
      including post-cleanup deadline checks and inherited-group handling. This is local evidence;
      remote reconciliation, dispatch ordering and publication-stage recovery remain open below.
- [ ] Settle and fault-test cleanup ownership when the first scratch/snapshot creation reply or
      publication-stage cleanup-debt reply is lost. Missing identity is not absence; do not recover
      by replaying creation or scanning a prefix. Prove the bounded immutable ownership-receipt
      candidate, including original-parent binding, interrupted receipt creation/removal and late
      requests. Every follow-on mutation must validate its still-existing operation receipt under
      the caller's operation ownership before creating any artifact; read-only snapshot chunks
      retain their existing unlocked exact-reference checks.
- [x] Deliver private stage reconciliation and exact cleanup through the fixed identity-bound
      helper. Accept complete historical cleanup ownership only, preserve missing-evidence
      uncertainty and exact failure debt, and check expiry before explicit cleanup mutation and
      after descriptor closure. All three private lanes are clean at `b0840a37`. This does not
      complete snapshot/publication recovery or prove earlier-request quiescence.
- [x] Admit the fixed Linux snapshot scratch parent independently of source-write authority. The
      selector creates and repairs nothing, refuses links and unsafe ownership/mode, and preserves
      deadline/descriptor handling. Local fixtures prove a non-root download from a read-only source
      parent, and a separate read-only host probe admits UID 0/mode 01777. Native VM/macOS selection
      and remote snapshot delivery remain unproved.
- [x] Deliver private snapshot creation, exact-range chunks, historical ownership reconciliation and
      cleanup through the fixed Linux helper. Verify binary data and ready/source revision agreement
      before exposing typed results; preserve exact known debt on expiry, reject substituted cleanup
      identities and phases, and treat missing source roots as absence without bypassing
      prerequisite or final deadline checks. Local Python 3.11 helper evidence and independent
      review at `9cdce7e6` establish this internal slice, not complete downloads, production
      operation coordination, FileAccess or native carrier acceptance.
- [x] Implement private publication-stage receipts and cleanup-only recovery beneath the original
      destination parent. Admit existing upload ownership before sibling creation, recover after
      payload removal, preserve known failures and exact debt through handled interruptions, and
      share record-only cleanup after observed publication. All three private lanes are clean at
      `86189dc2`; the combined local suite passes 11,902 tests with 13 skips. This does not complete
      remote publication delivery, upload composition, helper quiescence, FileAccess or native
      acceptance; complete carrier sizing remains an explicit gate above.
- [x] Deliver the private publication/reconciliation/cleanup family described in the file LLD. Bind
      all cleanup evidence to the original destination, token and stage reference; refuse
      substituted identities and paths. Preserve confirmed publication, recovered ownership or
      completed cleanup when only the final descriptor-closure deadline expires. Prove the actual
      production bundle's complete carrier sizing before accepting its delivery representation, then
      fault-test lost replies and partial observations through the exchange. This slice does not
      itself complete upload orchestration, FileAccess or production operation ownership.
- [x] Replace argv-embedded fixed file bundles with the reviewed bounded stdin-prefix delivery. Keep
      one fixed codec and one invocation, verify the core-fixed length/digest before decoding or
      executing the prefix, and leave request bytes solely to the operation parser. Exercise all
      existing file families, fragmented/short/corrupt prefixes, exact manifest boundaries,
      sensitive retention and complete carrier-size refusal. Do not claim native Windows/QGA
      acceptance from a locally serialized command or an invalid-request bootstrap probe.
- [ ] Jointly accept and prove the privately implemented carrier sink extension with the SSH owner
      before enabling its production use or exposing it through RunContext.

At transport `77063be1` plus SSH `3142af0a`, private local merge `d694a873` was conflict-free. Three
installed-OpenSSH file-delivery cases and one sensitive live-duplex case passed on Linux. The
snapshot-download fixture skipped because this host lacks its required root-owned mode-1777 `/tmp`;
the temporary merge's shared editable Python environment also could not run the isolated
fresh-process import check against that merge. The SSH owner
[confirmed the shared sink shape is stable](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-5827772124)
for the remaining transport composition. This is partial local acceptance evidence, not
supported-workstation or production FileAccess acceptance; the checkbox stays open.

- [x] Review and prove complete private upload under a borrowed core operation owner. Cover one
      durable claim across staging, finite source consumption, publication and ordered cleanup;
      preserve prerequisite refusals, failure-carried debt and uncertain completion without replay.
      All three private lanes accept `faf99365`. Corrections cover inactive settlement authority,
      lost refusal debt, exception-context disclosure, byte accounting, canonical option validation,
      helper deadlines and actual-dispatch admission. The final deterministic interruption
      regression detects the old reporting gap between durable ownership and local attempt-handle
      assignment. This is private composition, not production FileAccess or recovery acceptance.
- [x] Compose complete private JSON updates through the existing read/stat and upload helpers under
      one borrowed operation. Factor borrowed upload so a JSON call never releases and reacquires
      ownership between snapshot and publication. Validate source before target I/O, avoid old-byte
      reads for replace/skip, preserve all four strategies and retry only proved condition conflicts
      within eight attempts and the original deadline. No uncertain dispatch, retained cleanup debt
      or missing termination evidence permits replay. Public FileAccess/result conversion and
      production ownership remain separate required gates. All three private review lanes accept
      `9d97623c`. Corrections remove repeated interior validation and retain neutral
      existing-document validation failure for parser-capacity and caller-bound refusals; both
      refuse before publication in regression coverage.
- [x] Implement [owned download composition](file-operations-lld.md#owned-download-composition)
      through the existing snapshot exchanges and shared borrowed dispatch gate. Verify binary
      chunks, exact length/digest, empty and absent sources, short/stalled/failing sinks, one claim
      and original deadline, and retained cleanup/uncertainty facts without replay. This private
      stream coordinator does not complete local atomic publication or public FileAccess download;
      those remain required, including optional public size bounds and native host metadata proof.
      All three private lanes accept `4a53c39a`. Review corrected missing deadline facts across
      download, upload and JSON and removed duplicate control-flow branches. Admission-driven
      timeout regressions replace a reproduced startup-timing assumption; both fail against the old
      settlement behavior and pass under concurrent stress.
- [x] Implement a private Linux x86_64/aarch64 local download stage with create-only publication and
      explicit replace-existing selection. Require complete caller verification before publish,
      ordinary destination write authority, ordinary single-link objects and same-directory staging;
      preserve supported local access metadata or refuse before replacement, and retain exact
      cleanup and published-effect facts separately. Native local tests cover ACLs, metadata and
      failure boundaries. A directory-ancestry custody gate now refuses paths in which another local
      user could swap the verified stage name or displace its parent before publication. This is a
      host primitive, not public FileAccess download or non-Linux acceptance.
- [x] Compose that private Linux stage with the owned snapshot download under one deadline. Admit
      publication only after complete transfer verification and remote cleanup; retain independent
      remote, local publication, local cleanup and late-deadline facts, including on exceptional
      exits. This is still private Linux composition, not public FileAccess download or native
      non-Linux acceptance.
- [x] Implement private macOS and Windows local-download publishers with caller-private Create and
      held-file in-place Replace. Refuse unsupported objects and access metadata before mutation;
      retain uncertainty after local writes, truncation, flush, close or deadline failure. Separate
      ambiguous handle closes so they do not suppress unrelated cleanup. Portable macOS fault tests
      and Windows-marked tests exist; native filesystem/ACL acceptance remains unproved.
- [x] Compose the selected workstation publisher with the existing owned snapshot download under one
      deadline. Preserve remote verification and cleanup before local publication, unsupported host
      refusal before remote dispatch, and local publication/cleanup facts on exceptional exits. This
      is private host selection; native macOS/Windows acceptance and public result conversion remain
      required below.
- [x] Bind private `FileAccess.download()` to selected-host publication and typed result reduction.
      Require a concrete workstation path, create-only default and explicit Replace; support an
      optional positive byte bound, with None using the held snapshot protocol's representable size.
      Keep one serial borrow from retained-cleanup retry through stage construction, transfer,
      publication, result reduction and finalization. Retain unfinished facts before exceptional
      allocation and check interrupted call custody only after acquiring the borrow. All three
      private lanes accept `d764a3da2`; deterministic delayed-borrow regressions fail against the
      old custody boundary. Core teardown consumption, native workstation acceptance and production
      wiring remain open.
- [ ] Complete local download publication with create-only default and explicit replace-existing
      selection, as directed on 2026-10-05. Settle and prove workstation metadata/ACL handling,
      ordinary-file refusal, full-transfer verification and cleanup before publishing either form;
      never import guest ownership into the workstation or silently strip local metadata. Complete
      native supported-host acceptance and production delivery of the private typed reductions
      remain required, including propagation of timing failure alongside proved operation facts. The
      operator approved macOS/Windows in-place `Replace` after complete verification and remote
      cleanup: preserve supported local ACL/ownership semantics under ordinary authority, refuse
      unsupported cases before writing, and report possible partial destination bytes on later local
      failure. Linux may retain rename publication; no cross-platform atomic-replace guarantee is
      promised.
- [ ] Complete the additive-surface gates below before exporting or wiring production RunContext
      access. The models-only checkpoint is not additive-surface completion.

The owned-upload and terminal increment is privately reviewed at `faf99365`. Its complete local
suite passes 12,126 tests with 13 skips; Ruff/format and full mypy (1024 sources) pass. Final
focused reviews verify the interruption regression against the old assignment boundary, not merely
the new code's success. The publication bundle's current prefix is 36,372 bytes and the measured
complete long-path QGA body is 50,783 bytes, below the unchanged 65,536-byte bound. These are local
serialization and helper results, not native carrier acceptance. Production ownership/recovery,
complete FileAccess, terminal carrier integration, lifecycle and additive RunContext remain open.

The following JSON/native-binding increment is privately accepted at `9d97623c`. The passive
Proxmox/WSL2 hooks return actual delivery-account and runtime facts without constructing legacy
transports; new QGA delivery requires verified TLS and shares explicit CA selection with the
platform API. Review corrected unhandled CA-path expansion failures, misleading guide claims and
authored-prose test assertions. A full combined run exposed eight stale WSL bootstrap mock targets
after moving the production import to its call site; the correction preserves those tests' original
behavioral assertions. Complete factory/import independence and native execution remain pending, not
implied by the concrete hook tests.

The corrected `9d97623c` code passes the full local non-integration suite: 12,175 tests and 13
skips. Ruff lint/format and CI's full mypy selection (1029 sources) pass. These results cover local
helpers and workstation tests, not native platform acceptance. Download composition is the next
private file work unit; local publication, complete FileAccess and the production ownership gates
remain required.

The following download/resolver increment is privately accepted at `4a53c39a`. Its full local suite
passes 12,208 tests with 13 skips; Ruff/format and full mypy (1,033 sources) pass. File lint,
locked-SDD/rulesync, typer isolation and whitespace gates pass. Website validation passes 160 Python
and 103 Node tests plus both deterministic double-build comparisons. Private review observed one
flaky 100 ms timeout assumption despite a passing full suite; the final tests expire only after
accepted runtime admission, and the independent concurrent stress run passes all eight cases. An
optional duplicate test-only deadline-property patch remains acknowledged and non-gating. No native
acceptance is claimed. Local publication, production ownership/recovery, lifecycle and RunContext
remain required; the scoped interrupt-policy question is still open above.

The publication and account-runtime increment has clean project, complexity and generic correctness
reviews for the runtime at `66aa55d8`. The full local suite passes 12,050 tests with 13 skips;
Ruff/format, full mypy (1016 sources), file lint, locked-SDD, rulesync, typer isolation and
whitespace checks pass. Website validation passes 160 Python and 103 Node tests plus both
deterministic double-build comparisons. Final test-selection bookkeeping at `e3176f11` restores the
exact same tree. Pure prefix tests remain portable, while focused Windows CI selects the two
subprocess trampoline cases according to CONTRIBUTING. No live infrastructure was exercised.

Publication review corrected uncertain effects mislabeled as refusal, sensitive exception context,
an inode-reuse assumption in test cleanup, impossible reconciliation debt and secondary failures
overwriting prior mutation facts. Mutation tests detect removal of cleanup-progress binding,
reconciliation-shape checks and failure-preserving cleanup binding. Production helper success covers
Create, Replace and Match on the workstation interpreter and distribution Python 3.11; injected
fault helpers are identified separately. The final fixed publication prefix is 36,212 bytes with a
756-character bootstrap. Representative valid long-path publish/reconcile/cleanup requests across
direct/root/demoted delivery produce 48,561 to 48,881-byte complete QGA bodies and 1,872 to
1,982-character Windows SSH command strings. These are local serialization measurements, not native
acceptance or universal request-fit guarantees.

Account-runtime review corrected impossible Linux shim evidence and added tests that execute the
actual trampoline under synthetic version/import failure rather than merely emitting expected
records. Both guards are mutation-proven. The shared launcher preserves existing file/inline argv;
only the two account entrypoints adopt runtime admission in this increment. Representative complete
Windows SSH serialization of their shared account bundle measures 8,483 characters for Linux
selection and 8,533 for Darwin selection. Native macOS selection, real older interpreters, remaining
helper-family adoption and public diagnostic composition remain open.

Hosted run `35575344113` at `cbd9a66f` passed all gates except Windows and its aggregate gate: both
actual trampoline refusal tests observed UNKNOWN because Python text output translated the
protocol's LF to CRLF. At `f646b04d`, all three runtime records use binary ASCII output; parsing
remains strict. The subprocess fixture now reproduces Windows text translation on every host and
also covers READY followed by helper bytes. All three cases fail with the old writer restored.
Independent project and complexity reviews are clean, 71 focused tests pass, and the full suite
passes 12,051 tests with 13 skips. All static, documentation and website gates pass again, including
both deterministic builds. Representative account Windows command strings now measure 8,615/8,665
characters for Linux/Darwin selection. All hosted checks, including Windows, pass on `6d066728` in
[run 35576186411](https://github.com/WayfarerLabs/agentworks/actions/runs/35576186411). This CI
correction neither expands supported workload platforms nor consumes a public feedback round.

The file and inline admission increment has clean project, complexity and generic correctness
reviews at `925cbdbf`. Its full suite passes 12,062 tests with 13 skips; Ruff/format, full mypy
(1018 sources), file lint, locked-SDD and rulesync gates pass. Review fixed a Windows workstation
path leaking into synthetic Linux selection, narrowed optional observations explicitly in tests, and
reduced inline reader finalization to one call on normal and exceptional paths. The independent
project lane also exercised 42 Windows-selected tests with a synthetic Windows interpreter path.
This is portability evidence, not native Windows or provider acceptance.

Complete representative Windows SSH commands now measure 3,677 to 3,836 characters across the seven
file bundles and direct/root/demoted identity modes. QGA size guards continue to include the
complete runtime launcher and serialized request. Neither measurement proves native execution. The
read-only SSH audit at `34a4eb71` found 13 file exchanges requiring explicit selection and
optional-observation adaptation; SSH-owned code remains untouched. Terminal runtime composition,
complete file workflows, core ownership/recovery, lifecycle and additive RunContext remain open. No
public feedback round is consumed by this private increment.

Fixed file delivery has three clean private reviews at `72897a31`, with 11,958 full-suite passes and
13 skips. The six production bundles produce parsed invalid-request transcripts under distribution
Python 3.11. Successful operation tests separately cover every family: object/inventory use the
production bundle, read/metadata/stage add trusted test entrypoints, and snapshot also redirects its
scratch parent. These local measurements do not establish native SSH/QGA acceptance.

At that checkpoint, packaged prefixes measured 12,024 to 27,524 bytes. Complete Windows SSH command
strings measured 1,823 to 1,968 characters across the representative connection and all three
identity modes. Complete QGA JSON bodies with a synthetic 32 KiB ASCII manifest measured 45,691 to
61,336 bytes; that payload is a sizing fixture, not a valid-operation proof or a guarantee about
every escaped manifest. Oversized complete bodies refuse before wire access. Review removed a
duplicate stage-size test and collapsed fixture setup into one patch input without dropping fault
coverage. Publication delivery, joint SSH proof, module readiness and public FileAccess remain open.

The [operator ruling](frd.md#file-safety-and-guest-runtime-rulings) approves adding `python3` to
early guest provisioning, with helper code compatible with Bookworm's distribution Python. The
package addition is implemented; live provisioning and existing-VM native recovery must establish
availability on their actual paths. macOS platform hosts must provide preinstalled Python 3.11 or
newer; detect missing, unsupported, or Xcode-shim interpreters and report clean actionable errors
without implicit installation or an installation prompt. Prerequisite checks, file-only no-staging
readiness and exact direct-launch evidence retain their own implementation and proof obligations.

The same ruling bounds file safety to untrusted requests, conservative regular-file publication and
required access metadata, without containment of malicious target-user processes. Preserve safe
object checks and explicit trust assumptions, but do not build same-user namespace isolation to
close the earlier ancestor-rename/hard-link adversarial gate. Unsupported objects or metadata refuse
before publication; required workflows still need an implemented safe path or explicit disposition,
not silent omission.

The byte-endpoint work unit now includes borrowed live input, delivered-output retention, the shared
subprocess pump and buffered QGA sink delivery. Private review caught the post-exit pipe timer
incorrectly limiting temporary sink stalls. The correction keeps one accumulated collection budget
while pending sink delivery consumes the original operation deadline. The lexical provisioning tests
from the initial package increment were removed; package coverage and the separate live-provisioning
gate remain. These corrections do not establish production or joint SSH acceptance. Before
publication of the new types, the old SSH adapter must explicitly refuse unsupported I/O shapes
instead of silently interpreting them as EOF or discard. The SSH owner supplied standalone commit
`678e487d`, integrated here as `fc1c5310`. The integration tests exercise actual `LiveInput` and
`SinkOutput` values, including sensitivity and both sink-delivery modes, and verify refusal before
connection access or endpoint consumption.

At `354c7a17`, all three private code-review lanes are clean, and the corrected full local suite
reports 10,413 passed and 11 skipped. The focused execution/provisioning suite reports 496 passed
and four skipped. These are workstation tests, not live platform acceptance. Subsequent
combined-stream testing exposed a further gap: a pending sink paused accounting while the other pipe
could keep collecting. The scheduler now suspends all fresh reads during post-exit pending delivery,
then resumes the unchanged accumulated budget. Its correction received fresh review with the next
bounded work unit, rather than inheriting the earlier clean verdict.

The safe ancestor `a885ef5a` is published with only early guest Python provisioning and the local
PTY experiment, including the provisioning-test correction. Its execution package and RunContext are
unchanged from `e85e9f5c`. The exact publication pin passes 10,370 local tests with 11 skips and all
hosted checks. The branch retains that ancestor without changing the implementation tree. The
subsequent compatibility guard removes the prerequisite for publishing the new I/O types; progress
pushes remain distinct from a public review handoff or joint acceptance.

The [Darwin prerequisite candidate](preparation-lld.md#darwin-inline-prerequisite-candidate) keeps
runtime selection above carriers and checks interpreter compatibility inside the inline invocation.
It does not install Python, execute the known Xcode shim, or add a preliminary readiness probe.
Private file diagnostics distinguish missing, Xcode-shim and unsupported-version refusals with
Python 3.11-or-newer guidance. Inline execution still reduces these to generic preparation failure;
production macOS host diagnostics and native proof remain open. Its local executable experiment now
reuses the shared pump and fixed minimal environment; the separate private file snapshot primitive
implements bounded read-only observations. Their
[runtime](preparation-lld.md#darwin-inline-prerequisite-candidate) and
[filesystem](file-operations-lld.md#confinement-and-filesystem-mechanics) evidence descriptions keep
production composition, native platform acceptance, full mount handling and locking gates open.

At `b881942d`, project, complexity and generic correctness reviews are clean for the runtime
experiment, file snapshot primitive and combined-stream correction. Independent mutations prove that
the corrected representation tests detect disclosure of either snapshot bytes or its digest. The
runtime-identical `be168621` passes 10,470 local tests with 11 skips; the final corrected execution
suite passes 549 with four skips. Ruff, mypy, file lint, rulesync and locked-SDD checks pass. This
is private implementation evidence, not native platform acceptance or a completed additive
RunContext handoff.

At `6d089f67`, the private Linux publication primitive and retained exec-evidence experiment pass
project, complexity and generic correctness review. Publication uses caller-owned staging state,
preserves supported access metadata, refuses unsupported objects and retains exact cleanup debt;
acquisition and post-rename interruption regressions pass. Arbitrary asynchronous interruption and
complete helper ownership remain unproved. The same seven exec-evidence cases assert their complete
results under the current interpreter and distribution Python 3.11, without accepting a production
launcher or eager application-start claim. The exact head passes 10,517 non-integration tests with
11 skips; the execution suite passes 596 with four skips. All 33 publication cases also pass under
distribution Python 3.11. Ruff, mypy, file lint, typer isolation, rulesync, locked-SDD and website
gates pass. These remain private building blocks: FileAccess, remote helper delivery, locking,
platform acceptance and additive RunContext composition are not complete.

At `1a5958fa`, all three private review lanes are clean for the exact-child wait correction, the
SSH-owned compatibility guard and its shared-type integration tests, and the lifecycle
clarifications. Repeated review exposed two post-exit scheduling defects: a newly pending stream
could permit another fresh read in the same pass, and alternating pending streams could keep the
collection timer paused indefinitely. Separate mutation-tested regressions now cover both
transitions. Test callbacks no longer compete with the pump's reaper, and the external-reaper
fixture explicitly establishes ordering. The final head passes 10,541 non-integration tests with 12
skips; the full execution suite passes 620 with five skips. Ruff, formatting, mypy, file lint, typer
isolation, rulesync, locked-SDD and website gates pass. This is a draft implementation progress
push, not joint live-I/O acceptance or a completed public feedback/fix round. Launch-interruption,
native-platform, helper, lifecycle and production RunContext gates remain open.

At `e6860525`, all three private review lanes are clean for the public-launch experiment and its
bounded evidence record. The experiment no longer forces Python's private launch selector; it
observes the route and actual session creation under local CPython 3.12.13 and Debian 3.11.2. The
focused suite passes 81 tests with one skip. This head also gives the oversized live-input fixture
short parameter IDs: Windows CI at `4cf5261f` could not set pytest's environment variable for its
65,634-character generated test identifier. The input and assertions remain unchanged. All hosted
checks subsequently pass at `e6860525`, including Windows Python 3.13 and Linux Python
3.12/3.13/3.14; no production-launch or native-platform gate is closed by this test correction.

At `9c1993ef`, the private scratch-transfer primitive passes all three review lanes. It reopens
identity-bound objects, verifies bounded exact-offset transfers, and retains known cleanup debt.
Review corrected acquisition/interruption ownership, setgid inheritance ordering and interrupted
descriptor closing, and removed checks that did not strengthen the stated guarantees. All 29 scratch
cases pass on the current interpreter and distribution Python 3.11; the runtime head passes 10,572
non-integration tests with 12 skips. The corrected full execution suite passes 651 with five skips.
FileAccess, wire validation, remote delivery, concurrency composition and native-platform acceptance
remain open. The Lima resource-lifetime candidate at `8bac9560` and shared process-core design at
`367553fa` separately pass project and complexity review; neither claims an implemented supervisor
or destination helper. These remain draft progress increments, not public feedback/fix rounds or
readiness for production adoption.

At `c89a349d`, the process pump is extracted into the private standard-library-only `_process`
module, with carrier policy and report mapping retained in `carriers/_subprocess.py`. Standalone
execution proves binary input/output and exit handling without importing Agentworks, on the current
interpreter and distribution Python 3.11. The full suite at `2b22da86` passes 10,578 tests with 12
skips; the final simplification removes one redundant source-inspection test and passes all 656
execution cases with five skips, plus Ruff, formatting and strict mypy. Private review removed a
redundant type check that could silently discard an unexpected failure. These are reuse and local
process facts, not destination-helper or production-launch acceptance.

Windows CI at `b59bf286` exposed a timing assumption in the live-input early-close test: exit 23
could first be observed during cleanup, leaving completion legitimately unknown. The correction
keeps that conservative runtime behavior and adds deterministic coverage for both pre-cleanup exit
evidence and cleanup-only status. All hosted checks at `37a36aae`, including Windows Python 3.13 and
Linux Python 3.12/3.13/3.14, pass in
[run 35496253664](https://github.com/WayfarerLabs/agentworks/actions/runs/35496253664). The
[Darwin ownership investigation](prior-art-research.md#darwin-ownership-feasibility) separately
identifies a public-mechanism gap for generic MANAGED host jobs. The operation-coordination ruling
above subsequently selected platform-owned host lifecycle without weakening guest MANAGED.

SSH's implementation at `174187d2` includes transport `a885ef5a` and adopts the reviewed finite
subprocess pump. Its owner has separately supplied the buffered compatibility guard integrated here.
It still needs the extended shared I/O implementation and terminal preparation, plus production
target/trust composition. Pump adoption does not close the launch interruption gate. Next, settle
and jointly prove live source/sink reports and terminal preparation with a synchronized
payload-to-interactive handoff. A raw envelope through an unprepared PTY is not accepted. The
[same-terminal experiment](carrier-io-lld.md#same-terminal-preparation-experiment) separates remote
bootstrap feasibility from the remaining local client adapter proof. Full file/lifecycle
implementation is not a prerequisite for that bounded shared-boundary proof.

At `b1250da5`, the private Linux inline candidate delivers a fixed Python helper and a separate
bounded stdin manifest in one carrier attempt. It verifies the bound identity, separates script
source from application input with a memory file, and validates framed wait/output facts without
inferring application success or eager start. All three private review lanes are clean. Review
corrected false terminal evidence after contradictory output, portable identity fixtures and an
interpreter-path refusal, and removed duplicate parsing and unused size bookkeeping. The execution
suite passes 1,012 tests with five skips; the full non-integration suite passes 10,933 with 12
skips. Ruff, formatting, strict mypy and file lint pass.

A built-wheel check imports the candidate from the wheel, outside the checkout and without site
initialization, then executes its packaged helper on distribution Python 3.11. It verifies separate
script/stdin, all 256 stdout byte values, two binary stderr bytes and exact exit 255. Local
serialization for `/bin/true` with identity 1001 measures 63,277 fixed-source bytes, 63,391 argv
bytes including terminators, a 249-byte manifest and a 63,754-byte Proxmox JSON request body. These
are measurements of the current candidate, not accepted provider limits. Native delivery, eager
start, interruption ownership, staging, elevation, terminal preparation, lifecycle and production
RunContext remain open; the candidate is not a completed additive delivery or a public feedback/fix
round.

Hosted checks for `f768ade0` passed except Windows in
[run 35498733365](https://github.com/WayfarerLabs/agentworks/actions/runs/35498733365). Package-wide
import discovery exposed the guest module's eager POSIX account-database import. The fix at
`22511320` defers that import until guest default-shell lookup and adds a fresh-process regression
with the module unavailable; it does not skip the independence check. At `3b10267a`, the full local
non-integration suite passes 10,934 tests with 12 skips, with Ruff, formatting, strict mypy and file
lint passing. Hosted Windows confirmation subsequently passes in
[run 35499698325](https://github.com/WayfarerLabs/agentworks/actions/runs/35499698325) at
`616508bc`. That run's Linux 3.13 job exposes a test-only assumption: `pwd` was already loaded
before the import finder guard. The correction at `728b556d` marks the module unavailable
explicitly, preserving the regression without changing runtime behavior. All hosted checks
subsequently pass at `7641fa7f` in
[run 35501242049](https://github.com/WayfarerLabs/agentworks/actions/runs/35501242049), including
Windows Python 3.13 and Linux Python 3.12/3.13/3.14.

The fixed helper's packaged sources now use zlib compression before ASCII armoring; caller payload
remains in stdin. At `728b556d`, the minimal `/bin/true` request with identity 1001 measures 18,583
fixed-source bytes, 18,697 argv bytes including terminators, a 249-byte manifest and a 19,060-byte
Proxmox JSON body. The full local suite passes 10,934 tests with 12 skips, including the helper's
actual distribution-Python-3.11 execution cases. Ruff, formatting, strict mypy and file lint pass.
These are local delivery-size and compatibility facts, not native provider acceptance.

The [terminal input candidate](carrier-io-lld.md#private-terminal-input-adapter) now gives bootstrap
EOF a terminal-only handoff meaning, preserves preparation-owned readiness parsing and keeps
presentation above the carrier. Explicit input and output descriptors supply native terminal facts
without process-global stdio lookup. This remains a candidate for joint native proof with SSH, not
an enabled terminal mode or accepted platform evidence.

At `35c72e84`, Linux snapshot lookup uses `openat2` for every descendant open without a weaker
fallback, and private terminal preparation implements the nonce-bound two-gate source/collector and
one-shot Linux guest. The host side is workstation-neutral. Private review corrected inherited
Python signal dispositions and nonce transformation under restored terminal output modes, and
removed unsupported same-process guest reuse. All three private lanes are clean. The full local
suite passes 10,961 tests with 12 skips; Ruff, formatting, strict mypy, file lint, typer isolation,
rulesync, locked-SDD, 160 Python/103 Node website tests and both deterministic build comparisons
pass. Actual local Python 3.11 PTY and kernel lookup tests do not establish SSH/native workstation,
same-filesystem bind-mount or macOS acceptance. Complete files, lifecycle and additive RunContext
remain open; no public feedback/fix round is consumed.

The private no-staging file-read slice now composes the shared fixed-source packager, strict
file-response framing and the existing snapshot reader. Local composition drives the actual Proxmox
carrier against a fake provider and real helper subprocess; it is not native QGA evidence. Review
removed duplicate size/metadata and terminal bookkeeping, rejected undefined Linux mode bits, and
corrected retained collector/reader state on exceptions. A mutation-proven partial-record fixture at
`5e832bd7` covers reader cleanup. This does not promise secure erasure of transient Python locals or
asynchronous interruption atomicity.

Project, complexity and generic correctness reviews are clean at `7aa08d0b`; project and complexity
rechecks cover the test-only correction and final limitation wording. The corrective round is
private implementation work, not one of the three authorized public feedback/fix rounds.

At runtime pin `7aa08d0b`, the full local suite passes 11,009 tests with 12 skips. The final
test-only correction passes all 124 file tests. Ruff, formatting, CI-scoped mypy (933 sources), file
lint, typer isolation, rulesync, locked-SDD, 160 Python/103 Node website tests and both
deterministic build comparisons pass. The Python website run also emitted a server-thread
`BrokenPipeError` while returning success; no assertion failed. An extra mypy invocation over the
entire CLI directory, outside CI's scope, reports missing hatchling build-hook stubs; the required
`agentworks/ tests/` invocation passes. No dependency or unrelated website code was changed.

Hosted [run 35503399897](https://github.com/WayfarerLabs/agentworks/actions/runs/35503399897) at
`b0d62063` passes Windows and Linux 3.12/3.14, but the Linux 3.13 import test assumes its blocked
modules were not preloaded. The helper import succeeds; the test's final assertion fails. The
test-only correction at `ed19ee21` explicitly marks those modules unavailable, like the existing
terminal test. All three private review lanes are clean; negative import probes still fail, and the
four focused import tests pass. Runtime code is unchanged.

All hosted checks subsequently pass at `4f60e9c4` in
[run 35503894965](https://github.com/WayfarerLabs/agentworks/actions/runs/35503894965), including
Windows Python 3.13 and Linux Python 3.12/3.13/3.14.

At `c3cebea5`, explicit private helper identity plans pass all three review lanes after test-only
corrections. The full final non-integration suite passes 11,031 tests with 12 skips; Ruff,
formatting and CI-scoped mypy (936 sources) pass. File lint, typer isolation, rulesync and
locked-SDD checks pass. Website validation at the runtime-identical `400a634a` passes 160 Python and
103 Node tests plus both deterministic build comparisons; temporary build outputs were removed. The
shared carrier interface remains unchanged. Actual privilege transitions, target account resolution,
native acceptance, full files/lifecycle and additive RunContext remain open. No live infrastructure
was touched and no public feedback/fix round is consumed.

All hosted checks subsequently pass at `3ada8ff0` in
[run 35504674348](https://github.com/WayfarerLabs/agentworks/actions/runs/35504674348), including
Windows Python 3.13 and Linux Python 3.12/3.13/3.14.

The private account-discovery increment at `40a6bade` reads only the core-bound account's IDs/groups
under the delivery identity and feeds the existing identity plan. It uses a bounded one-shot JSON
reply, no staging or privilege change, and no public account selector. The developer's focused suite
passes 64 tests; execution tests pass 1,151 with six skips. Real distribution-Python-3.11 lookup and
lookup-to-inline composition run locally. The latter correctly refuses this container's differing
database and inherited group memberships. Private review and final gates were pending at that
integration pin. This does not close native identity-transition, Darwin acceptance or production
composition gates.

All three private lanes are subsequently clean at `1688da5d`. Review removed duplicate encoder
validation and a redundant result-construction check, deleted an unrelated directory assertion that
could not detect helper staging, and strengthened the account-payload assertion with a unique
substring canary. An actual argv-embedding mutation fails that assertion. The final full suite
passes 11,073 tests with 12 skips; Ruff, formatting and CI-scoped mypy (942 sources) pass. Website
gates at that pin pass 160 Python tests, 103 Node tests and both deterministic build comparisons.
Temporary build outputs were removed and no live infrastructure was touched. These private
corrections consume no public feedback/fix round.

All hosted checks subsequently pass at `aa82b8f2` in
[run 35506094459](https://github.com/WayfarerLabs/agentworks/actions/runs/35506094459), including
Windows Python 3.13 and Linux Python 3.12/3.13/3.14.

The file-mechanics increment is privately reviewed at `75aaaa5e`. Publication now accepts explicit
Create/Replace/Match conditions, uses metadata-only observations when no old-content match is
required, streams verified scratch in bounded chunks, and verifies a content-bound revision after
rename. The fixed read-only lock primitive proves local contention/refusal/release but does not
install its namespace or close cross-identity/native acceptance. Review removed a duplicate
post-publication reader and corrected a mistimed deadline test; deleting the post-acquisition check
now makes that test fail. All three private lanes are clean.

The final full suite at `e9cce152` passes 11,118 tests with 12 skips. Ruff, formatting and CI-scoped
mypy (945 sources) pass. The focused file suite passes 169 tests. File lint, typer isolation,
rulesync and locked-SDD checks pass. Website gates at `4c35aafe` pass 160 Python and 103 Node tests
plus both deterministic build comparisons; its local server emitted a BrokenPipeError without a
failed assertion. Temporary build output was removed. No live infrastructure was touched. Shared
namespace setup, complete file delivery, launch ownership, lifecycle, native proof and the additive
RunContext remain open; neither pending macOS decision is waived. These are private implementation
corrections, not a public feedback/fix round.

The next increment is privately reviewed at `876355c7`. Linux object stat/removal now binds exact
kind and revision, and shared new-guest bootstrap provisions the protected lock after Python
installation. Existing valid lock identity is retained; only new core-owned objects are finalized.
Setup failures report closed phase/kind/creation facts. Review corrected a post-open descriptor
leak, removed a redundant create-result flag and kept leaf validation at the future request
boundary. An ACL-fixture audit found unmapped `nobody` IDs, not missing filesystem support;
mapped-group fixtures execute the real inherited/existing ACL cases without skips.

The source bundler now compresses trusted modules together, and standalone publication executes
under Bookworm Python 3.11. The file design selects fixed operation-family inline bundles with
data-only scratch, avoiding executable installation. The Proxmox carrier also refuses a serialized
POST over 64 KiB before dispatch, including argv/framing/escaping. These changes do not prove the
final file dispatcher or native carrier request sizes. Shared SSH types are unchanged; Windows SSH
command size and complete native file delivery remain acceptance gates.

At `876355c7`, the final full local suite passes 11,177 tests with 12 skips; the file/Proxmox suite
passes 305 without skips. Ruff, formatting, CI-scoped mypy (951 sources), file lint and diff checks
pass. Typer isolation, rulesync and locked-SDD checks also pass in this increment. The unchanged
website code passes 160 Python and 103 Node tests and both deterministic build comparisons;
temporary build outputs were removed. No live infrastructure was touched. All three private review
lanes rechecked the correction pin. Native privileged setup, cross-identity contention, complete
file delivery, lifecycle and additive RunContext remain open; both macOS operator decisions are
still pending. No public review/test signal is raised and no public fix round is consumed.

The metadata/inventory increment is privately reviewed at `4e8921eb`. Linux metadata convergence
uses verified held-object procfs references, supports required directory set-group-ID modes and
preserves explicit completed versus uncertain mutation facts. Inventory returns complete results
within the requested depth with exact entry/name/encoded bounds. Shared file framing now has one
concrete codec, while read schemas remain operation-specific. Root and fixed lock-namespace walks
use path-only descriptors without unnecessary directory read authority; neither installs state.

All three independent lanes are clear on the correction pin. Review moved invalid directory-mode
refusal before target I/O, added a deadline check after final ACL verification, removed duplicate
inventory serialization and redundant metadata error reconstruction, and preserved handled-control
descriptor cleanup. Final local suite: 11,282 passed, 12 skipped. The combined file and Proxmox
request/trust/sink suite passes 448 without skips. Ruff, formatting, strict mypy (957 sources), file
lint, typer isolation, rulesync, locked-SDD and diff checks pass. Unchanged website code passes 160
Python and 103 Node tests plus both deterministic build comparisons. No live infrastructure was
touched. This is a private increment, not complete FileAccess or a public feedback round. The next
implementation step is the concrete stat/removal helper exchange; public composition, native and
cross-identity acceptance, lifecycle and additive RunContext remain open. Both macOS operator
decisions remain pending.

The stat/removal, metadata-ownership lookup and locked-read exchanges are privately reviewed at
`2ad918bc`. Object requests retain exact kind/revision conditions, closed relative budgets and
uncertain-removal evidence without replay. Ownership lookup returns only numeric owner/group IDs,
independently of execution identity. Read snapshots use the existing protected lock and emit after
unlocking; missing lock state refuses even when the target is absent. Neither read nor stat creates
prerequisite state. The full local suite at that pin passes 11,449 tests with 12 skips.

All three private lanes found no material issues at that pin. Corrections clear sensitive exception
chains, preserve Windows import-test selection and remove a Proxmox fixture's dependency on the
host's lock state. Shared identity decoding and one-pass account request parsing remove duplicate
checks. The subsequent cleanup at `b09e1212` deletes two unused account decoder wrappers and moves
their unchanged malformed-input cases to the real guest entry point. Public FileAccess, native
ordinary/elevated acceptance, lifecycle and additive RunContext remain open; this private work does
not consume a public feedback/fix round.

All three lanes also verified the narrow cleanup at `b09e1212`, and the full local suite again
passes 11,449 tests with 12 skips. Ruff, formatting and CI-scoped mypy (966 sources) pass. Website
gates pass 160 Python and 103 Node tests and both deterministic build comparisons. File lint, typer
isolation, rulesync, locked-SDD and diff checks pass. Generated test/build outputs were removed; no
live infrastructure was touched. Hosted checks pass at the preceding published `6edbd94c` in
[run 35511446645](https://github.com/WayfarerLabs/agentworks/actions/runs/35511446645); the new
progress publication still needs its own hosted confirmation.

The inventory/metadata exchanges and scratch-finalization increment are privately reviewed at
`fef0045d`. Inventory returns entries only after complete framing, length, digest, schema and
carrier-stream checks. Metadata and directory convergence preserve known partial versus uncertain
effects without replay. All four file guests share one concrete record writer. Review corrected
missing response-key handling and made compatibility cases select Python 3.11 explicitly.

Scratch creation now binds identity and length without requiring the final digest in advance;
verification establishes the ready reference. Cooperative expiry stops further acquisition and
transfer while retaining exact cleanup. An independent reproduction found that a slow successful
directory creation could otherwise be followed by data creation after expiry. The correction
prevents that new object, preserving only identity capture and mode normalization necessary for
cleanup. Mutation experiments establish the need for both final digest verification and that cleanup
normalization.

All three private lanes are clean at `fef0045d`. The final full local suite passes 11,595 tests with
12 skips; the file/scratch selection passes 604. Ruff, formatting and CI-scoped mypy (978 sources)
pass. File lint, typer isolation, rulesync, locked-SDD and diff checks pass. Unchanged website code
passes 160 Python and 103 Node tests and both deterministic build comparisons. A prior full run at
`14fb93b3` emitted multiprocessing resource-tracker warnings from a database test; the final run did
not, and no shared-memory files remained when checked. Owned test/build output was removed. No live
infrastructure was touched.

The bounded immutable ownership-receipt candidate has independent project and complexity review, not
implementation acceptance. Lost replies, late requests and partial receipt cleanup remain explicit
proof gates. Streaming source-to-scratch snapshots are now assigned for implementation;
transfer/publication exchanges, full FileAccess, lifecycle, native acceptance and additive
RunContext remain required. Shared SSH types are unchanged. Both macOS operator decisions remain
pending. This increment consumes no public feedback/fix round. Hosted checks passed at prior
published `63022be8`; fresh confirmation remains required after the next progress push.

The source-to-scratch snapshot increment is reviewed at `5b58e8f5`. It copies bounded chunks from
one held source, verifies length/EOF/digest and final source identity/metadata, and returns a ready
private copy plus the source revision. Initial absence is checked for expiry after descriptor
closure. Review reproduced and corrected skipped parent cleanup after a leaf-close interruption and
lost scratch cleanup debt when source closure interrupted a failed creation. An independent mutation
of final source verification fails four tests.

The generic lane also reproduced an existing traversal limitation: an asynchronous exception before
an intermediate ancestor close can leave that descriptor until helper exit. The same ordering
predates this increment. It remains inside the documented non-signal-atomic bookkeeping limit; blind
close retry is not added because interruption does not establish whether that numeric descriptor has
already been closed. Complete helper lifetime/interruption remains a production gate, not an
acceptance claim from these local primitives.

The final local suite at that runtime pin passes 11,617 tests with 12 skips. Ruff/format, CI-scoped
mypy (980 sources), file lint, typer isolation, locked-SDD, rulesync and diff checks pass. Website
gates pass 160 Python and 103 Node tests plus both deterministic build comparisons. Owned temporary
test/build output was removed. No live infrastructure was touched. All hosted checks pass at the
preceding published CI correction `46f5d948` in
[run 35517251196](https://github.com/WayfarerLabs/agentworks/actions/runs/35517251196); the snapshot
increment still needs fresh hosted confirmation after publication.

The focused systemd 252 source audit records helper acceptance separately from payload entry,
completion retention across unit collection and independent boundary-emptiness observation after
stop. The later private foreground candidate below implements those source-backed mechanics without
closing their native proof gates. Creation receipts are under implementation. Transfer/publication
exchanges, full FileAccess, native acceptance, complete lifecycle and additive RunContext remain
required. The macOS questions still pending at that checkpoint were resolved by the later
platform-owned-lifecycle ruling above. No public feedback/fix round is consumed by this increment.

The receipt increment is reviewed at `5a9d3b8f`. Core supplies a fresh token and execution identity
before staging or snapshot creation. The immutable receipt binds the closed operation and original
parent/object identities. Historical recovery permits cleanup only; active references separately
bind the original receipt inode. Recovery neither proves that a delayed request cannot still arrive
nor returns verified content. Remote exchange ordering and publication-stage recovery remain open.

Private review corrected reconciliation expiry after missing-name lookup and descriptor closure,
ensured the directory descriptor closes even when receipt closure interrupts, and removed redundant
receipt decoding. The lead reproduced a real inherited-group failure and verified its fix outside
the namespace sandbox. Both data and receipt inherit the parent's group before private directory
mode is finalized. An early data-open failure now cleans its exactly owned empty directory; unknown
objects remain untouched with explicit debt. Removing that normalization makes both new failure
tests fail. The future snapshot helper must check its bound execution identity before any source
access, including absence lookup; the local spool's receipt context is not that invocation boundary.

Final full local suite: 11,634 passed, 13 skipped. The extra skip is the sandbox's unavailable
alternate supplementary group; the committed regression passes separately outside that sandbox. The
independent correctness lane also reports 1,713 execution tests passed, six skipped, and a Python
3.11.2 receipt/transfer/reconciliation roundtrip. Ruff/format, CI-scoped mypy (982 sources), file
lint, typer isolation, locked-SDD, rulesync and diff checks pass. Unchanged website gates pass 160
Python and 103 Node tests plus both deterministic build comparisons. Owned temporary test/build
output was removed and absence verified; no live infrastructure was touched. Hosted checks pass at
the preceding snapshot increment `0e7a9edf` in
[run 35518292881](https://github.com/WayfarerLabs/agentworks/actions/runs/35518292881); this receipt
increment still needs hosted confirmation after publication.

The staging exchange is assigned separately against these receipt types. Full FileAccess, native
acceptance, launch ownership, lifecycle and additive RunContext remain required. Shared SSH types
are unchanged, and both macOS operator decisions remain pending. This progress push consumes no
public feedback/fix round; all three remain available for the completed PR.

The private stage increment is reviewed at `85d27904`. Creation and chunk requests use the original
destination binding, fixed lock and sensitive private protocol. Review corrected expiry after path
and lock cleanup, oversized pytest IDs on Windows, and returned chunk cleanup debt that could
conflict with the known active reference. Removing the debt-equality check makes all seven mismatch
cases fail. The three private lanes pass 86 stage tests, including real local Python 3.11 helpers.

The final full suite passes 11,720 tests with 13 skips. Ruff/format, CI-scoped mypy (991 sources),
file lint, typer isolation, locked-SDD, rulesync and diff checks pass. Website Node tests pass 103
cases and both deterministic double-builds match. One Python website run failed the unchanged
browser keyboard-hold launch witness; a complete rerun passed all 160 tests. This is an observed
intermittent gate failure, not a diagnosed or fixed website defect. Hosted receipt checks at
`9d8100b2` pass in
[run 35520474884](https://github.com/WayfarerLabs/agentworks/actions/runs/35520474884); the stage
increment requires fresh hosted confirmation. Remote recovery is the next file implementation; full
FileAccess, native acceptance, launch ownership, lifecycle and additive RunContext remain open.

The stage recovery and Linux scratch-root increment is privately reviewed at `b0840a37`. Lost
creation replies can recover complete historical cleanup ownership through the fixed helper, never
an active or ready content reference. Cleanup failures cannot introduce different debt. Missing
receipts remain uncertainty; delayed follow-on chunks refuse after receipt removal, but this does
not establish that an earlier original creation cannot arrive later.

Review corrected explicit cleanup after an expired path lookup and rejected historical replies with
missing identities or widened receipt modes. The typed result encoder no longer repeats those
external checks. Removing the incoming checks makes all ten malformed-history cases fail; removing
pre-cleanup admission mutates data despite a final refusal. Six real-helper deadline tests now
advance a controlled guest clock at the intended boundary, replacing a reproduced scheduling race.
The final correctness lane passes 147 focused tests, all 60 repeated deadline cases and 20,000
malformed-request probes. The complete file selection passes 736 tests. Root admission is local
Linux evidence only: namespace-sandbox `/tmp` has UID 65534 and correctly refuses, while the same
read-only probe outside that sandbox observes UID 0/mode 01777 and closes the admitted descriptor.

The final full suite at `b0840a37` passes 11,781 tests with 13 skips. Ruff/format, CI-scoped mypy
(993 sources), file lint, typer isolation, locked-SDD, rulesync and diff checks pass. Website gates
pass 160 Python and 103 Node tests and both deterministic double-build comparisons. Owned temporary
test/build output was removed and absence verified; no live infrastructure was touched.

All hosted checks pass at the prior stage publication `37d8150b` in
[run 35522218730](https://github.com/WayfarerLabs/agentworks/actions/runs/35522218730). The new
increment still needs hosted confirmation after publication. Snapshot/publication delivery, full
FileAccess, launch ownership, lifecycle, native acceptance and additive RunContext remain required.
The launch-owner candidate is being implemented separately; its design evidence is not production
acceptance. Shared SSH interfaces remain unchanged. The macOS decisions pending at that checkpoint
were subsequently disposed by the operation-coordination correction above: no blanket host lock
setup and no generic macOS MANAGED supervisor prerequisite for VM platforms. This progress increment
consumes no public feedback/fix round; all three remain available for the completed PR.

After the shared seam and LLD gates, the lead may charter bounded migration packages against one
pinned contract. The following is an assignment plan, not a claim that developers are allocated:

| Package                                       | Exclusive responsibility                                                                                                                                                            |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transport lead                                | Shared execution API/helpers/file policy, `capabilities/base.py`, composition boundaries, integration gates and final deletion.                                                     |
| SSH developer                                 | `execution/carriers/ssh/`, connection/trust migration and associated tests, currently in #832.                                                                                      |
| Optional harness/artifact migration developer | `harness_setup/`, harness setup/readiness invocation types, artifact publication/probes and harness plugin consumers; preserve domain behavior while replacing runner/file facades. |
| Optional session migration developer          | Session/tmux/console consumers and tests; preserve session/run identity, restart consent, runtime evidence and owned cleanup.                                                       |
| Optional platform/CLI migration developer     | Non-SSH adapters, VM exec/recovery, backup and workspace transfer consumers, with explicit per-file assignment before starting.                                                     |

Every charter names exact files and tests; overlapping files remain with the lead or are handed off
explicitly before another developer touches them. Separate working trees/branches share the pinned
contract, not a mutable working tree. Cross-package requests return to the lead; only the lead
integrates changes to common types and production composition. After additive delivery, migration
batches may land independently against its pinned contract. Temporary released coexistence is
explicitly authorized; final removal remains this effort's responsibility. Refresh the inventory at
each integration boundary and prohibit new legacy consumers.

## 1. Specify the small contract and proof charter

- [ ] Specify `PreparedInvocation`, the single input choice in `CarrierIO`, stream ownership,
      failure behavior and `CarrierReport`, with SSH implementation input. Done when transport
      publishes one candidate contract and acceptance matrix for both efforts to use, including
      single-attempt semantics and explicit treatment of unresolved feasibility questions.
- [ ] Record the OpenSSH 8.5 minimum's applicable binaries/locations and server compatibility in the
      SSH-owned design. Include workstation, platform-host and provider-inner invocation sites; no
      version requirement may be silently assumed from another hop's client.
- [ ] Confirm ownership: transport owns shared preparation/public outcomes and applying SSH policy
      in platform adapters and provisioning; SSH owns reusable connection/isolation policy, carrier
      delivery and trust/configuration migration. Remote Lima is the first consumer of SSH-backed
      platform access, not a concept inside SSH or a dependency for later platform consumers.
- [ ] Obtain a bounded proof/live-test charter naming isolated resources, tool versions, workload,
      cleanup and evidence. This artifact round does not run the proof or select live resources.

## 2. Prove the shared boundary before broad implementation

Transport and SSH contributors jointly produce one new-stack end-to-end buffered invocation slice.
Transport owns preparation, result interpretation and the proof harness; SSH owns its independent
connection/delivery portion. Useful existing code/tests may be copied, never called through legacy
execution modules. This is not a full file/job implementation or a preliminary legacy consolidation.

| Proof case                            | Evidence required to pass                                                                                                                                                                                                                                       |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Literal execution and shell bootstrap | Empty/quoted/special-character argv arrives intact; fixed interpreters and explicit user-shell selection, env/cwd and elevation have intentional behavior. Record supported account-shell combinations and startup-hook effects separately from payload policy. |
| Source and finite input               | Script source is not consumed as application stdin; finite bytes survive exactly and absent input is EOF. Sensitive-input cases prove suppression in results, diagnostics and owned artifacts.                                                                  |
| Guest streams                         | Ordinary-input cases preserve binary stdout/stderr, separately from carrier diagnostics, including NUL and newline-sensitive bytes. Do not require returned sensitive output as proof of byte fidelity.                                                         |
| Outcomes and observation              | Exits 0/1/255, lost contact and interrupted observation retain the facts actually known. A 255/drop may remain ambiguous; interruption reaches operation cleanup. No replay or inferred guest termination.                                                      |
| Readiness                             | A bounded invocation works without helper installation, staging or spooling, and without requested profile initialization. Demonstrate guest-stream behavior on this path too; account hooks are the separately documented carrier prerequisite.                |
| I/O ownership and failure             | Focused stream tests prove EOF, borrowed-stream lifetime, short writes, bounded flow control/cancellation and safe source/sink failure. Failure cannot become success, silent discard or confirmed remote cancellation.                                         |
| Non-SSH shape                         | A bounded Proxmox QGA case exercises the same request/report contract, finite input, output limits and no-staging readiness. A fake alone does not establish live feasibility or supported-version coverage.                                                    |

- [x] Complete the proof matrix with observed evidence and a pinned candidate contract. A failed or
      unresolved shell/input/stream boundary blocks broad parallel implementation; revise the seam
      and repeat the affected cases. If the needed mechanism changes scope, return to the operator.
      This gate does not claim exact SSH exit/drop classification or full platform acceptance.

This completion covers the defined buffered candidate: finite bytes/EOF, capture/discard and the
authorized connection identity. The
[acceptance disposition](proof-lld.md#joint-buffered-proof-acceptance-2026-09-17) records each row's
applicable evidence. Optional live/terminal modes, scoped elevation and arbitrary startup hooks are
not advertised or accepted by this slice. The specification and reconciliation tasks remain
responsible for the broader interface before parallel implementation; neither this checkbox nor the
PoC merge enables production use.

## 3. Reconcile designs and publish the implementation boundary

- [ ] Incorporate proof findings into this SDD and have the SSH owner reconcile #796's independent
      carrier design against the same proven contract. #796 supersedes #757; no legacy consolidation
      precedes the rebuild. Each effort edits only its own artifacts.
- [ ] Review and publish proof-informed design revisions before broad parallel work. Record the
      transport-owned contract revision and evidence both efforts will build against. Publishing the
      initial baseline in #795 does not satisfy this post-proof gate.
- [ ] Complete execution/context and file/job LLDs: shell/startup combinations, no-staging
      readiness, bootstrap tools, bounded transfer/path policy, jobs/cancellation/retention and
      stale records. Preserve macOS host jobs before guest creation. Establish remaining
      Proxmox/WSL2 feasibility under an authorized live-test charter; the small proof does not stand
      in for these checks.
- [ ] Before production enablement, implement and validate owned workload lifecycle and explicit
      cancellation, including ordinary descendants, stale/reused process identity, disconnected
      observation, bounded cleanup and truthful confirmation or uncertainty. Keep local waiting
      deadlines distinct from guest lifetime. The operator deferred this from the PoC only.
- [ ] Complete the file LLD for R7: whole-file publication, JSON merge semantics, ownership/mode and
      security metadata preservation, bounded inventory, directories/conditional removal,
      concurrency and uncertain results. Preserve the shipped four settings strategies and JSON
      literal-null behavior rather than adopting RFC 7396 deletion implicitly. Keep TOML and
      generated-section transforms in their domain, backed by snapshots/conditional publication.
      Enumerate tools available during native bootstrap and on supported platform hosts; prove
      destination-side confinement rather than relying on a preflight path check. Define trusted
      ancestors/mounts, private staging, database operation ownership, root creation and fail-closed
      behavior.
- [ ] Inventory intended mutation destinations and actions for harness configuration, `/opt`
      provisioning, `/run` session objects, recovery and platform hosts. Review execution-bearing
      content and select explicit core allowlist entries, trusted dynamic-root resolution and
      recipient subsets for post-removal activation. Consumers choose the appropriate file API
      rather than broad parent intents or public-exec workarounds to avoid a future restriction.
- [ ] Finalize execution/file interfaces and separately granted actions/profiles, including the core
      file ceiling, with production enforcement deferred until legacy removal. Keep registration
      requests, user consent, a general plugin policy evaluator and hostile in-process plugin
      isolation out of scope. Map FRD R11's future workflows to tests.
- [ ] Reconcile #796's pinned transport reference with the final reviewed contract and later
      file-only slice. SSH owns its artifact edits; shared file semantics and cutover stay here.

## 4. Build the independent stacks in parallel

- [ ] Establish exact child-status ownership wherever a native wait becomes execution evidence.
      CPython can substitute zero after ignored `SIGCHLD` or another reaper consumes the status; the
      helper and workstation observers must refuse missing evidence rather than report that
      synthetic value. Validate the retrospective completion candidate independently from eager
      launch acknowledgment and preserve uncertainty for unproved signal termination.
- [ ] Establish local process ownership through launch interruption on supported workstation hosts.
      A real SIGINT probe on Linux CPython 3.12.13 left a child alive when `Popen` construction was
      interrupted before returning its handle. Shared pump extraction and the existing SSH copy do
      not satisfy this gate. Prove cleanup or obtain an explicit contract disposition before
      claiming production interruption conformance; see the
      [process-startup evidence](prior-art-research.md#local-process-startup-and-interruption).

The transport lead owns shared profiles, supervisor lifecycle and session adoption. Migration
delegates consume this implementation rather than building another launcher; SSH owns delivery,
connection and trust only. Before broader lifecycle implementation, complete these additional gates:

- [ ] Review unified execution/file access and independent invocation, observation, I/O, lifetime,
      protection and identity dimensions. Approve typed shell constants, exact additive profile
      grants and refusal without downgrade. The [lifecycle design](execution-lifecycle-lld.md)
      supplies the proposal, not implementation proof.
- [ ] Reconcile the complete #770 requirements, threat model, acceptance cases and exclusions using
      the [preserved input](inputs/session-cgroups-frd-2c406948.md), not the routing index as a
      replacement FRD. Ownership is assigned to transport and #770 is closed; carry the accepted
      text into its designated requirements home. The closed source head is `2c406948`, matching the
      preserved snapshot; closing the PR does not complete requirements reconciliation.
- [x] Implement a bounded private Linux foreground MANAGED candidate for native proof. The fixed
      `systemd-run --pipe --wait` service runs under the explicit target identity, creates one
      delegated child cgroup before workload execution, inherits the carrier byte streams directly,
      and separately reports helper completion, payload evidence, service completion and observed
      child-boundary emptiness. Preparation is atomically one-shot and carries no replay, public
      job, RunContext or production-factory surface.
- [x] Implement the private durable managed-run reservation and receipt-reconciliation kernel. A
      fresh run is stored and its unit is derived before dispatch; possible dispatch commits before
      the supplied one-shot launch boundary; ambiguous launch is never replayed; exact receipts and
      exact protected-namespace absence after `NOT_SENT` reconcile idempotently. Persist only
      bounded non-secret target/incarnation/boot, workload, shell, profile, owner/lifetime, protocol
      and launch evidence. Application, cleanup, output retention and disposal evidence remain later
      gates, to be introduced with real producers and consumers. This adds no supervisor/carrier
      wiring, lease, stop/disposal, public job/RunContext surface or session backfill.
- [x] Define and test the private bounded target-side managed-job fact protocol. Canonical
      version-one launch, main-process wait, separate stdout/stderr end and positive boundary-empty
      facts carry exact run/unit and launch-receipt digest binding. One closed-stream disposition
      distinguishes complete/truncated capture, discard and sensitivity suppression, with retained
      length and digest; missing facts remain unknown. The LLD specifies immutable create-once facts
      in a protected boot-local per-run directory and sequences the first service slice to
      independent lifetime. Store, target service, operation lease/cleanup, requested-policy
      persistence/comparison, target-producer parity, carrier proof, public jobs and live lifecycle
      validation remain open.
- [x] Extract the canonical managed-job v1 byte schema into one Python 3.11 stdlib module that
      reuses the portable identity helper, and route the host typed adapter through it. Exact-source
      bundle parity covers launch, both wait outcomes, all four stream dispositions and
      boundary-empty facts. The target producer/service, requested-policy persistence, protected
      store, cgroup/systemd launch, carrier proof and live validation remain open.
- [x] Persist the first consuming service's requested output policy beside the managed-run
      reservation without changing the version-one launch receipt. Store capture, discard or
      sensitivity suppression plus the capture ceiling; validate old/private rows and malformed
      database values fail closed. Keep application bytes and secrets out of the database.
- [x] Implement the Python 3.11-compatible protected target store under
      `/run/agentworks/managed-runs-v1`: root-only fixed assets, bounded capture spools and
      same-directory create-once fact publication. Prove partial stages are never facts, existing
      canonical facts reconcile idempotently, conflicts refuse, closed-output reads require the
      matching stream-end fact and strange filesystem objects fail closed. Add no file lock, mutable
      shared status document, arbitrary path surface or automatic expiry.
- [x] Implement and privately prove the exact-source Python 3.11 target-controller core for Linux
      `MANAGED` plus `INDEPENDENT`. It consumes the fixed request, requires root and the exact
      derived delegated service-cgroup membership, gates one child through placement and verified
      groups/GID/UID, normalizes its signal state, publishes launch before readiness, and then
      handles finite input, bounded capture/discard, normal-wait evidence and independently bounded
      cleanup facts. Pre-exec failure, every signaled death and unproved cleanup remain unknown. At
      `c0b5ba4c`, project, complexity and independent correctness lanes are clean; exact-source
      Python 3.11 probes and 113 focused reviewer tests pass. This checkpoint does not construct or
      launch the transient service, prove a live cgroup or identity transition, expose a carrier
      exchange, or establish production target evidence.
- [ ] Complete the first fixed target controller service for Linux `MANAGED` plus `INDEPENDENT`. Use
      one root transient notify service and a delegated child workload cgroup; place the gated
      child, apply and verify its exact groups/GID/UID, publish launch and readiness, then release
      caller code. Concurrently drain both streams and wait for the main child. On main termination
      start descendant cleanup while draining continues. Publish wait only for close-on-exec-proved
      application entry followed by normal exit; preserve exit 126 and leave setup/exec failure or
      any signaled death unknown in this slice. Publish each stream end only after EOF, and publish
      boundary-empty only after `populated 0`. Preserve unknown facts on controller death,
      uninterruptible tasks and every unproved cleanup path. Refuse `OPERATION`, live streams,
      terminal I/O and arbitrary systemd properties before dispatch.
- [x] Implement the bounded private carrier-neutral `observe` and closed `read-output` exchanges
      over the protected store. The exact-source Python 3.11 target helper and host collector bind
      one canonical launch, return only present fixed facts, and admit output only from a validated
      stream-end and closed capture spool. Missing end remains unknown; discard and suppression are
      known unavailable; malformed or incomplete carrier evidence promotes no facts. At
      `4a1209ec079c9510cc671300aebc390a12c0ad25`, the manifest-only result control removes duplicate
      status and length claims; 64 focused tests, Ruff and targeted mypy pass. This checkpoint does
      not implement `start`, `stop` or `dispose`, host reservation/output-policy reduction, live
      target identity rereads or SSH/QGA production proof.
- [x] Implement the private fixed Linux `MANAGED` plus `INDEPENDENT` start exchange through combined
      code checkpoint `10fb0f0fac34234e1938681885988c6dc816c35e`. It preflights the exact reserved
      run, request, output policy, identity/runtime and carrier structure before durable
      possible-dispatch, then stages only five protected assets and attempts one closed transient
      root notify service. ASCII-armored canonical request framing preserves binary source/stdin; an
      exact launch fact confirms a durable receipt even when systemd client acknowledgement is lost.
      Acknowledgement additionally requires complete trusted helper/carrier evidence and client exit
      zero. The pure `Carrier.validate` seam refuses deterministic direct-envelope incompatibility
      without an attempt and is repeated by `execute`. Buffered SSH's pure refusal guard is
      owner-authored at `fad2ede68dc75d1af90d26e6e4bc848756da4eab`, integrated as
      `10fb0f0fac34234e1938681885988c6dc816c35e`; #832's expanded carrier must retain or adapt its
      truthful no-op validator. The transport slice's exact Python 3.11 source proof, 261 focused
      tests, Ruff, targeted mypy and documentation gates pass. Protocol asset bounds do not
      guarantee every carrier's smaller direct envelope; large-request staging/fallback, live
      systemd/cgroup and SSH/QGA proof, current target-marker/boot rereads, stop/dispose and public
      composition remain open.
- [x] Implement and privately prove the fixed managed-stop request, controller handling and
      carrier-neutral exchange through `8b99c784843da6152591afe4508198131000b7a0`. One separate
      protected empty `request-stop` leaf publishes only after exact launch validation; accepted
      intent remains distinct from positive boundary emptiness. The controller gives only its exact
      main child that has not yet been reaped one fixed grace interval, enters the existing
      whole-cgroup cleanup on anchor exit or grace expiry, and does not extend grace on retry. The
      guest observation remains finite for an unbounded caller deadline. A helper failure remains
      unknown after dispatch because publication may already be visible; post-publication
      observation faults preserve accepted intent, while only validated empty-boundary evidence
      proves termination. The 106 focused and 358 broader managed-execution tests, Ruff, targeted
      mypy, the full file-lint suite and diff check pass. This slice adds no process enumeration,
      mutable stop document, `execution_runs` stop state, generic admission lock, direct
      systemd-stop claim, disposal/retention policy or public composition. Hermetic proof does not
      establish live systemd/cgroup, current target-marker/boot or SSH/QGA behavior.
- [x] Implement and privately prove exact terminal disposal with a boot-local retry tombstone
      through `4292a4a8001633045c67c18ab87a3691a081bc39`. Require the exact launch, boundary
      emptiness and both closed stream ends before publishing `disposal` by hard-linking the
      validated immutable launch and syncing the directory. Validate capture spools structurally
      before deletion; their bytes, lengths and digests do not authorize release. Validate
      recognized crash stages by fixed name, owner, type, mode and link shape rather than content.
      First tighten controller ordering so it settles optional wait before boundary publication;
      both stream ends may remain later and independent, but the complete disposal predicate permits
      no later controller fact publication. Prove the late-wait race and simultaneous exact receipt
      retries, including a delayed retry that must not create a new stage after receipt-only
      success. After durable commitment, remove only validated fixed run artifacts and recognized
      private stages through the held directory descriptor, sync and report disposed only when the
      one-link receipt is the sole survivor. Exact retries resume partial cleanup; missing launch
      plus receipt proves nothing; strange entries and mismatched receipts authorize no deletion.
      Make target publishers refuse an observed receipt, while leaving distinct-operation
      serialization to the core operation claim rather than adding a file lock. Add no automatic
      retention interval, forced release, generic deletion API, mutable status or database disposal
      field. The 451 managed-execution tests, Ruff, formatting, mypy across 252 execution sources,
      the full file-lint suite and diff check pass. Host exchange proof covers pure preflight,
      deadline refusal, helper and carrier uncertainty, malformed or stale receipts, complete
      disposed/not-ready results and suppression of the launch receipt from representation.
      Filesystem proof covers symlinks, directories, FIFOs, unsafe modes, unexplained hard links,
      malformed bounded request and stop finals, mismatched receipts, failed receipt durability
      barriers, partial cleanup and simultaneous exact retries. Hermetic proof does not establish
      production ownership, current target/boot rereads or live SSH/QGA behavior.
- [ ] Add the fixed carrier-neutral `start`, `observe`, closed `read-output`, `stop` and `dispose`
      exchange over the target store. The private hermetic exchange now implements all five
      operations, revalidates the exact target/run/unit/launch digest on every later action, never
      replays start after possible dispatch and exposes no arbitrary command, unit, path or property
      control. This item remains open until the same exchange is proved through SSH and QGA before
      public factory, job or RunContext exposure.
- [x] Add private exact-VM operation custody around one already reserved independent managed start.
      The version-one `managed-start` obligation persists only the run ID. Preflight precedes
      admission, the run row records possible dispatch before the borrowed carrier starts, and one
      settled attempt with a confirmed receipt resolves only temporary start custody. Missing
      receipt, uncertain dispatch and interrupted control flow after arming retain the core claim;
      pre-arming refusal releases the unused borrow and resolves any installed row. The resource
      owner and managed-run row own the continuing job. Production binding, recovery, current
      marker/boot rereads and later job actions remain separate gates.
- [x] Add a private adapter from finite caller values to a managed request. It preserves literal
      argv, separate script source and stdin, the exact supplied shell selection, bounded
      environment and output policy, and suppresses output retention for sensitive input. It
      validates through the existing request codec before returning, with a pre-encoding source
      bound. This is not a start API or production caller: the first start caller must obtain the
      target account's actual shell for `USER_DEFAULT`, preflight the carrier before reserving the
      exact run ID, then bind the reservation to owned launch custody. The adapter alone does not
      prove that reservation is residue-free on refusal or establish SSH/QGA and live lifecycle
      behavior.
- [x] Add a private fixed guest observation of the destination account's default shell after the
      selected workload identity transition. The helper verifies actual UID/GID/groups first,
      reports only a supported sh/bash executable or typed refusal, and checks the configured path
      is a regular executable object. A nonce-bound 256-byte response is separate from the bounded
      identity-bearing request, runtime readiness and carrier facts. The managed controller still
      rechecks that same account shell before application launch, so a changed default refuses
      instead of substituting an interpreter. This hermetic and local Python 3.11 proof does not
      establish live sudo/demotion, production owner composition, or SSH/QGA behavior.
- [x] Move private managed-start request and carrier preflight before run reservation. Preparation
      binds a planned run identity, exact specification, output policy, raw carrier and deadline;
      the owned start consumes it once only after matching the reserved row. A canceled token drops
      its retained request input, and a failed reservation can discard it. Known expiry, carrier
      mismatch and owner closure refuse before arming; expiry after effect admission remains
      conservative. The 52 focused start tests, Ruff and targeted mypy pass. This mechanism does not
      yet supply the production caller that prepares, reserves and launches under a live target
      binding, nor recovery or SSH/QGA proof.

The private bound-start composition at `51e3a190b` accepts already composed exact-VM target facts,
identity plans, an already-held VM owner, carrier, runtime and deadline. It checks request shape,
resolves `USER_DEFAULT` through an owned workload-shell observation, prepares the raw carrier,
reserves one independent resource-owned run and hands it to the owned start. Local tests cover
success, invalid request and carrier refusal before reservation, uncertain launch custody, wrong
binding, sensitive input and shell-observation uncertainty. Corrections through `f66336d15` keep the
original interruption when shell-borrow release fails, derive workload identity from its plan, and
check deadline expiry again before reservation. The focused and neighboring selection passes 70
tests; Ruff and targeted mypy pass. A refusal after reservation may retain an exact `RESERVED` row
as a one-shot tombstone. The caller owns its run ID, inspects it after every escaping failure, does
not retry start with that ID, and retains owner custody if obligation or inspection state is
uncertain. An armed obligation can coexist with a `RESERVED` row; do not delete such a row or invent
a possible-dispatch timestamp. The existing armed-obligation regression covers that gap. This does
not constitute a production caller: the selected route must stay held and the provider locator must
be revalidated at the launch boundary. Interrupted-start recovery, later job actions, native SSH/QGA
proof and the complete RunContext surface remain open.

At private checkpoint `18aa1f4c2`, host preflight binds an observed VM guest identity and verifies
its derived boot fence against the planned run before reservation. The fixed Python 3.11 guest
helper rereads the protected marker, kernel boot and PID 1 start time before opening the run store
or invoking systemd, and refuses mismatch or unsafe evidence without publishing a launch fact. The
focused proof is hermetic. A production caller must still hold and revalidate the selected provider
route and locator, compose the exact target, and prove live SSH/QGA delivery; this guest fence alone
does not satisfy those gates.

At private checkpoint `0c1523e4e`, one WSL2 owned-operation object shares selected route, durable
hold, guest-target preparation and explicit settled release between the existing download and a new
managed-start caller. The caller passes the exact target and guest to the bound start, retains its
supplied run ID, and does not automatically end the WSL hold when the independent start returns.
Exact hold release can precede whole-owner closure while managed-start custody remains unresolved. A
follow-up correction compares a fresh locator, selected connection and runtime, and confirms the
locator again immediately before bound-start composition. The focused proof is local;
post-composition route changes, live WSL2/systemd behavior, recovery and public RunContext delivery
remain open at that checkpoint.

At private checkpoint `378b295cd`, an optional WSL2 route callback reaches the existing managed
start admission boundary. It runs after the core managed-start obligation is durably armed but
before the run records possible dispatch and before carrier delivery. A valid route difference is
`CHANGED`; unavailable, invalid or late evidence is `UNCONFIRMED`; exceptional observation keeps the
original `BaseException` with custody attached as cause. A refusal makes no managed carrier attempt
and leaves the exact run `RESERVED` and one-shot, with owner custody retained. Focused hermetic
proof does not bind locator observation atomically to later carrier delivery, or establish live
WSL2/systemd, recovery, production caller or public RunContext behavior.

At private checkpoint `ec36d7887`, managed observe and read-output require a canonical observed VM
guest identity. Host request preparation checks VM target kind and derived boot before carrier
dispatch; the fixed Python 3.11 guest helper rereads the protected marker, kernel boot and PID 1
start time before opening the managed store. Missing, unsafe or mismatched guest evidence refuses
without reading run facts or output. This closes one later-action guest fence hermetically, not a
production route or host-side JobAccess view. Stop and dispose still need equivalent current-guest
checks; live SSH/QGA delivery, recovery and public RunContext remain open.

At private checkpoint `d560ce3f2`, a host-bound read-only observation adapter admits only one exact
persisted independent VM run under a caller-held VM operation owner. It derives the expected launch
from the row and borrows custody for one guest-fenced attempt. The result reports the raw candidate
and whether uncertain delivery requires retaining the owner; escaping control flow preserves its
original exception with custody facts attached. The caller still owns route freshness, row
reconciliation, output-policy reduction and owner release. This is neither a production JobAccess
nor a public RunContext surface. Equivalent host-bound read-output, stop and dispose, recovery and
live SSH/QGA proof remain open.

At private checkpoint `9e578dbbb`, the closed stop and disposal requests also require a canonical
observed VM guest identity. Host encoding checks the exact launch's VM kind and derived boot before
carrier validation or dispatch. Both fixed Python 3.11 guest helpers reread the protected marker,
kernel boot and PID 1 start time before opening the managed store; missing, unsafe or mismatched
evidence returns fixed refusal without stop publication or deletion. Isolated bundle tests now
include the VM-identity modules required by their shared protocol imports. The first focused
stop/disposal run passed 94 tests; a redundant rerun exhausted scratch space while creating a large
test fixture, not on a code assertion. Host-bound action composition, current provider route,
recovery, native SSH/QGA proof and public JobAccess remain open.

At private checkpoint `c975c4f1b`, one host-bound closed-stream read shares the exact persisted VM
run admission and borrowed custody path with observation. It reduces the raw candidate against the
persisted output policy: complete or truncated capture requires a validated stream end and retained
bytes within the reserved prefix bound; discard and sensitivity suppression require matching
no-output end facts. Missing, mismatched, invalid or uncertain evidence is not accepted. The raw
candidate is internal evidence, never a caller-facing output view; the adapter does not mutate the
run row or release the caller's owner. Route freshness, job-state reconciliation, host-bound stop
and disposal, recovery, live SSH/QGA proof and public JobAccess remain open. Focused hermetic tests
pass, not native integration.

The next host-side stop slice will use an adapter-owned `managed-stop` lifecycle obligation under
the same exact VM claim. It will admit only a reconciled launch receipt, arm the obligation before
the one carrier attempt, and resolve temporary dispatch custody on positively proved no delivery
with settled coordination, or a complete, validated `ACCEPTED` or `TERMINATED` response with settled
delivery. `ACCEPTED` proves durable target intent, not job termination; only `boundary-empty` proves
the latter. Unknown or interrupted delivery retains the claim and exact-run recovery identity. This
does not add a mutable database stop state or make an uncertain stop retry safe before old-dispatch
drain is proved. Host-bound disposal and recovery are separate later gates.

At private checkpoint `617295760`, the owned stop adapter implements that one-attempt ruling. It
shares exact independent-VM row preflight with observe/read-output, requires `RECEIPT_CONFIRMED`,
installs a canonical run-ID-only `managed-stop` obligation, and arms it after pure stop-exchange
validation but before carrier dispatch. Settled `NOT_SENT` or complete validated
`ACCEPTED`/`TERMINATED` resolves temporary custody; unknown/failed delivery and interrupted arming
retain the obligation and owner without replay. The row remains unchanged, and acceptance remains
distinct from termination. The narrow local selection passed 21 tests, Ruff and mypy. Recovery
takeover, production route freshness, host-bound disposal, live SSH/QGA proof and public JobAccess
remain open.

Correction `9f4a8d50f` removes the redundant stop-specific arm callback: the borrowed carrier's
existing attempt admission already durably arms the supplied obligation after validation and before
delivery. Start and stop now share one canonical run-ID obligation codec while keeping distinct
obligation kinds. The corrected narrow selection passed 17 tests, Ruff and mypy.

The shared later-action preflight does not itself prove the guest marker used in the target's
incarnation fingerprint: it has no provider locator or persisted marker input. Production callers
must compare the observed guest marker with the persisted VM marker, compose the exact target from
the selected locator and that same marker before observe, read-output or stop admission, then
revalidate route freshness at dispatch. Matching the derived boot alone is not a production
target-binding proof.

The following private host-disposal slice will use a distinct `managed-dispose` obligation with the
same canonical run-ID recovery payload and exact VM claim. It requires a reconciled launch receipt
but delegates terminal predicate and receipt-only proof to the fixed target helper. Positively
proved no delivery with settled coordination, or a complete validated `NOT_READY` with settled
carrier delivery, resolves this one-attempt obligation without claiming disposal; complete validated
`DISPOSED` resolves it with exact receipt proof. Every incomplete, failed, interrupted or ambiguous
possible effect retains custody. Guest idempotence does not authorize host replay until prior
dispatch drain is proved. Explicit retention-release authorization, production route composition,
recovery, native SSH/QGA proof and public JobAccess remain separate gates.

At private checkpoint `19209cfd0`, the host-bound disposal adapter implements that one-attempt
custody rule without changing the persisted run. It shares exact independent-VM row preflight,
requires `RECEIPT_CONFIRMED`, and durably arms a distinct run-ID-only `managed-dispose` obligation
before carrier dispatch. Settled no-delivery, complete validated `NOT_READY`, or complete validated
`DISPOSED` resolves the temporary obligation; all ambiguous possible effects retain it, and an
escaping control exception retains its original identity. Eighteen focused hermetic tests pass, with
Ruff and mypy clean; the neighboring disposal, stop and observation selection passes 126 tests. This
does not establish explicit release authorization, production route freshness, recovery takeover,
native delivery or public JobAccess.

- [x] Connect the private independent-job lifecycle from a persisted `POSSIBLE_DISPATCH` row:
      observe one exact, validated launch receipt, reconcile it idempotently using historical
      `UNKNOWN` dispatch, then exercise read-output, stop and disposal without relaunch. Keep
      ordinary observation read-only. Invalid or incomplete observations do not move the row, and
      post-exchange control failure preserves its original exception with custody facts. The
      connected test uses a scripted possible-dispatch row and later-action carriers; it does not
      prove a real lost start acknowledgment or settle an earlier start obligation, close retained
      ownership, establish production route freshness, or provide crash-recovery takeover, polling
      wait, OPERATION lifetime, public JobAccess, or native SSH/QGA acceptance.

- [x] Add one private managed-result collection attempt after `RECEIPT_CONFIRMED`, reducing an exact
      validated wait fact, both policy-admitted closed streams, positive boundary-empty and current
      attempt custody into the existing `ExecutionResult`. Historical launch dispatch remains
      unknown; no launch receipt alone proves application entry. Do not infer resolution of an
      earlier start obligation from this result. Integrate the collector into the scripted
      independent-job lifecycle, with deadline, truncation, suppression, missing-fact and
      interrupted-read tests. Polling wait, production route ownership, native acceptance and public
      JobAccess remain separate gates.

The one-shot collector is a private checkpoint, not `wait` or a public job result API. A scripted
possible-dispatch row is reconciled before collecting in the connected lifecycle test. It does not
prove an actual lost start acknowledgment or settle the earlier start obligation. An independent
review found the deadline-between-exchanges case; the correction returns partial evidence only when
a later read's pure admission refuses after expiry, while interrupted admitted reads retain
aggregate custody. The managed selection passes 622 tests, full mypy passes 1,181 files, and Ruff
and whitespace checks pass. Live SSH/QGA and production route tests remain open.

- [x] Add a private bounded wait above the one-shot collector. Poll only clean, settled observations
      whose missing facts may progress, keeping the original finite deadline and exact VM owner;
      never replay the launch or retry uncertain delivery, invalid output, truncation or a fully
      observed terminal outcome. A nonzero main-process exit may still need boundary and stream-end
      evidence. A distinct pre-borrow deadline refusal, including at the next poll's admission,
      returns the latest partial evidence when available; an unrelated validation failure still
      escapes with accumulated custody. This private wait does not establish production route
      freshness, recovery takeover, OPERATION lifetime, native SSH/QGA acceptance or public
      JobAccess.

- [x] Generate one core-owned VM instance marker before new-VM provider dispatch, retain it on the
      provisional row and install that same non-secret 32-lowercase-hex value through every shared
      create bootstrap. Migration 41 leaves legacy rows NULL; ordinary existing-VM operations never
      write a marker. Bootstrap uses `/var/lib/agentworks/instance-id`, replaces only an ordinary
      regular leaf and refuses symlinks, non-regular leaves and multiply-linked leaves. Explicit
      adoption and production target composition remain later gates; the private probe and codec
      checkpoint below do not complete them.
- [x] Keep Lima marker delivery creation-only: its retained `mode: system` bootstrap omits the
      marker, while create streams the fixed installer through `limactl shell` after create/start
      and inside rollback. Later Lima start/restart never installs or repairs it. Isolated installer
      tests prove conservative leaf handling and mode changes; live platform proof of target root
      ownership and delivery remains a later integration gate.
- [x] Introduce and prove the vm-platform v2 provider-locator observation hook before managed target
      composition supports third-party platforms. The hook returns one bounded opaque provider token
      or explicit unavailable, never provider fields, persistence or marker evidence. It is a hard
      contract cutover. The reviewed dispositions are: AWS EC2 returns its account, region and live
      instance ID; Azure VM returns its exact live ARM resource ID; GCP GCE returns project, zone
      and numeric instance ID; WSL2 returns the Windows machine GUID, current-user SID and exact WSL
      registration GUID. Those positive SDK/process observations derive deadline-aware best-effort
      timeouts and reject a successful late result, but do not claim hard preemption of provider
      I/O. Lima is unavailable because its reusable instance name/path lacks a stable placement-host
      namespace and incarnation; Proxmox is unavailable because its VMID/node lacks a stable cluster
      namespace. Those unavailable results are honest checkpoint outputs, not permission to ship a
      critical or recovery operation without identity: production cutover must establish the
      required namespace or alternative proof, or prove the operation does not require this locator.
      No operation silently downgrades. `ProvisionRequest.instance_marker` remains creation
      evidence; this hook alone does not establish the private guest observation/composition
      checkpoint below or third-party managed target identity.
- [ ] Settle Proxmox restore/rollback incarnation policy before production target or recovery
      cutover. Implement the new-stack locator observation for private proof first; source
      implementation is not native acceptance. A live candidate combines the verified configured
      authority namespace, VMID and nonzero current `vmgenid`, with the node retained only as a
      route. The independent marker/full guest checks still apply. Version-pinned official source
      shows ordinary enabled-ID create, clone, restore and rollback generation; it does not prove
      native observation or a disabled/absent-ID policy. Prove the selected policy on PVE 8 and 9
      with the intended restricted token and `VM.Audit`, `current=1`, recreation, default/unique
      restore, disk/RAM rollback, clone, stop/start, migration and VMID reuse. Settle explicit
      adoption/alternative handling for missing, disabled or manually preserved IDs without
      synthetic identity or readiness repair. Do not impose a requirement for a cluster identifier
      that cannot be copied or infer positive generation from configured addresses. The
      [prior-art finding](prior-art-research.md) is response input, not native acceptance or
      recovery cutover by itself.

The Proxmox observation source uses the exact configured HTTPS origin as its operator-owned
namespace. It performs no DNS alias discovery or host/port/IPv6 spelling normalization; a changed
configured spelling requires explicit re-adoption. Node, token identity/secret and CA path are not
incarnation components. A versioned, unambiguously framed tuple of origin, VMID and normalized
nonzero hyphenated UUID is hashed into a bounded opaque token. This avoids imposing the locator's
size limit on the origin or exposing provider fields through a new public shape.

Read only current configuration through one fixed body-free `GET /config?current=1`, reusing the
verified, deadline-owned worker. After an attempted lookup, unavailable provider delivery/envelope
raises a sanitized typed provider failure; successfully observed config with missing, disabled, nil
or malformed generation raises a typed state refusal. It does not become
`ProviderLocatorUnavailable`, target-absence evidence, a synthetic UUID or a readiness repair. The
shared unavailable result remains a deliberate platform inability without an attempted lookup. This
response sequences source before its native proof so the proof can exercise the implementation; it
does not enable a production target/RunContext, settle old debts or complete the preceding gate.

- [ ] Complete and prove Linux supervisor launch through SSH and native QGA: protected identity,
      secret/source delivery, privilege changes, foreground wait, independent launch, output
      retention and terminal evidence. No workload code runs before boundary entry.
- [ ] Prove lost-acknowledgment reconciliation, wait timeout versus stop, observer-loss cleanup,
      runtime-anchor death, concurrent forks, stale identity and independently verified emptiness.
      Settle the OPERATION liveness/lease protocol before offering target-side cleanup.
- [ ] Implement the
      [operation lifetime path](execution-lifecycle-lld.md#operation-lifetime-implementation-path)
      with its first controller consumer: fixed guest-clock observation, exact launch/boot-bound
      bounded lease control, initial-expiry admission, monotonic renewal and irreversible expiry
      through the existing stop kernel. Preserve independent jobs and refusal at the host boundary
      until the keeper and aggregate lifecycle are delivered; no disconnected protocol scaffold is a
      completed slice. The private guest implementation now supplies that protocol and its
      controller consumer, including packed Python 3.11/3.12 real-child tests under a synthetic
      process-group boundary. The corrected managed/bootstrap selection passes 823 cases with 27
      existing fork warnings. It preserves refused host OPERATION admission. The 10,000-byte
      workload fixture fits the unchanged 65,536-byte Proxmox HTTP envelope: 64,955 bytes for
      independent and 65,319 for private operation requests, leaving only 581 and 217 bytes
      respectively. Larger metadata or workloads still face pure carrier validation; this does not
      establish a general native request size or native delivery proof. First-party service source
      is derived with the existing docstring compaction, including service admission, rather than
      bundling every original source byte verbatim. Keeper, recovery and native gates remain open.

All three independent private lanes reproduced a delayed-record-read expiry gap. The controller now
keeps its early expiry refusal, samples the guest clock again after successful or failed store
reads, and checks remembered expiry before accepting renewal. It reuses the store's validated
binding instead of repeating codec work. The wire boundary remains unchanged. Before correction, the
new deterministic selection fails six cases and passes two at `1357eefc5`; the corrected selection
includes nine new cases, including early expiry without a read and no revival after closure. The
earlier full lead suite passes 15,696 cases with 49 skips at that initial pin. Its next full run
passes 15,705 cases with 49 skips at `839177660`, before the following release correction; neither
result is evidence for a later pin.

Re-review reproduced the analogous initial-admission gap with real children: the final expiry check
preceded the protected-store stop-intent read. The check now follows that read immediately before
affirmative gate release. Both packed Python 3.11/3.12 regressions fail at the old pin by observing
actual body markers; corrected cases prove no entry, retained launch evidence and exact setup-child
reaping. Delayed stop intent still closes the gate through the existing cleanup path. This removes
avoidable intervening I/O, not arbitrary CPU-preemption races.

All three independent whole-unit lanes clear `a2d27446c`: project passes 322 focused cases,
complexity passes 848 and generic correctness passes 897, each with exit 0 in its own tree. Both
original delayed-read probes now pass. The complexity lane also restores the old release ordering
and observes both packed Python regressions fail before restoring the reviewed source. The complete
lead suite at that pin passes 15,709 tests with 49 skips and 27 existing fork warnings, exit 0. Full
Ruff/format (1,285 files), strict mypy (1,249 sources), exact CI typer isolation, file quality,
locked-SDD checks against fresh main `cea5e8523`, Rulesync and whitespace all exit 0. Website Python
(160 tests), Node (103 tests), all four builds and both deterministic comparisons also exit 0. These
are local source and protocol results, not native cgroup, SSH or QGA acceptance. The keeper,
recovery, complete RunContext and full-scope public feedback round remain open.

The latest hosted Windows failure stopped at the migration fixture's five-second child-commit
barrier before its database assertions. The private fixture correction uses generous finite
coordination and reaps the child on parent failure while preserving all schema refusal assertions
and product deadlines. A local delayed-spawn probe reproduces the old barrier failure and passes the
corrected fixture; it does not establish the original hosted failure's cause or native Windows
acceptance. Fresh hosted verification remains required.

The bounded-close core is integrated privately from worker `0a1068545`. All three independent lanes
clear that whole source unit against `a2d27446c`: project passes 172 cases with one skip and three
real-child control-identity probes; complexity passes 133 cases with one skip and reproduces the
original interruption failure by restoring the old lookup; generic correctness passes 678 cases with
one skip. Each exits 0 at the final pin. The worker passes 469 cases with one skip, 294 bundled
guest/bootstrap cases (including distribution Python 3.11), Ruff/format and strict mypy (1,250
sources). Corrections remove unused retained-status bookkeeping and preserve the immutable first
cleanup observation when a later natural-exit cleanup is pending. These prove the private core, not
caller-held carrier custody, Proxmox/keeper integration, native acceptance or full-scope completion.
The matching permanent execution documentation now distinguishes first-close evidence from later
cleanup settlement and retains caller-owned descriptors through pending construction.

The next coupled source unit adds mandatory raw carrier custody, existing ordinary/recovery attempt
storage, bound helper typing and explicit pre-target provider-query storage. Shared worker
`726ed8526` passes 750 focused cases with one skip, including actual distribution Python 3.11, and
scoped static gates; buffered worker `d8f033727` passes 233 cases with five skips and scoped static
gates. Those are their own pinned results, not evidence for the complete integrated head. The lead's
seven new ownership cases include actual delayed-constructor children under ordinary, recovery and
pre-target workflows. Local cleanup leaves remote debt intact. A private composition finding moved
the local-settlement guard into the positive remote-termination branch so unknown remote completion
still records remote uncertainty while local cleanup is pending. Production-only strict typing
passes 590 sources; test-fixture migration, full gates, whole-unit private reviews and native
acceptance remain open. No public completion or new RunContext enablement is claimed.

- [ ] Compose the core-owned keeper as one admitted support effect without relaxing ordinary owner
      serialization. Prove renewal while ordinary work holds its borrow, close/takeover races, one
      in-flight exchange, exact uncertainty retention and drain before release/disposal. Bind it to
      the actual owning operation, not a borrowed view or a wait deadline. Deliver recovery's fresh
      same-boot clock plus fixed-window ceiling without per-renewal database payload writes; initial
      start and renewal both need the post-clock generation fence. Keep native SSH/QGA, controller
      death, partition, suspend/boot and body-admission proof open until measured.
- [ ] Prove separate keeper delivery and exchange custody alongside an ordinary borrow, without
      background access to thread-affine database/context dependencies. Account for Proxmox's raw
      worker construction before its cleanup guard and unbounded post-kill `communicate()`; delivery
      timeout alone proves neither local drain nor guest-helper termination. Reuse the preceding
      launch/interruption gate and preserve exact route/guest fencing.
- [ ] Implement mandatory caller-held local delivery custody through the carrier and provider-read
      paths. Retain the existing native owner before admission, at most one unsettled worker per
      attempt; pending or lost ownership refuses further exchanges. Keep reports as observations and
      original exceptions unchanged. The existing operation attempt, keeper and pre-target workflow
      retain their own storage. Add bounded close and a non-dispatch settlement path after borrow
      handoff, with remote effects still independently retained. Prove delayed construction,
      kill/reap failure, explicit cleanup retry, natural exit, external-reaper uncertainty, all
      temporary Proxmox consumers and aggregate owner-release refusal. Do not silently add bounded
      return to guest source-descriptor or unmigrated terminal consumers.
- [ ] Reconcile #770's historical escape/relaunch proposal against the later exclusion of malicious
      target-user containment. Deliver DIRECT/MANAGED without a CONTAINED profile or
      per-run-user/jail implementation; retain future profile extensibility. Prove trusted
      socket-membership identity and ordinary lifecycle cases within that stated boundary.
- [ ] Resolve platform-owned macOS host workflows, Debian/kernel/systemd floors, WSL2 power lifetime
      and no-staging recovery. Required workflows block delivery when their guarantees cannot be
      met; no profile downgrade or fabricated platform equivalence. Use the
      [observable proof criteria](execution-lifecycle-lld.md#delivery-sequence-and-proof-criteria)
      and [reported test-bed gaps](prior-art-research.md#lifecycle-test-bed-gaps). For macOS hosts,
      prove the platform's actual start/status/stop and disconnected-operation recovery, not a
      generic Linux-equivalent MANAGED profile. Workstation SSH evidence is insufficient; report
      cleanup uncertainty without dropping required platform operations.
- [ ] Before advertising `INDEPENDENT` on a VM platform that can idle-stop, prove a recoverable
      resource-owned availability hold covering the active job beyond its initiating command and
      surviving observer loss. WSL2's current command-scoped hold is not this proof. Bind the hold
      to exact VM/job identity, retain uncertainty on controller loss or boot change, and release it
      only after terminal cleanup evidence. Refuse `INDEPENDENT` on that platform until proved; test
      explicit VM stop/reboot and terminal-record observation separately. A no-op hold is valid only
      where platform lifecycle evidence proves idle shutdown cannot end active work.
- [ ] Prove a distribution-scoped WSL2 boot fence before production managed-run adoption. The kernel
      boot UUID alone survives a distribution stop and restart inside the same utility VM. The
      private guest probe now combines that UUID with PID 1 start ticks; validate stable identity
      within one running distribution and changed identity across real stop/restart on the supported
      WSL2 bed, including lost-hold recovery. A live hold prevents ordinary idle shutdown but does
      not substitute for a restart fence after controller loss. Keep ambiguous or unavailable epoch
      evidence unknown and never treat the old run as present merely because the kernel UUID agrees.
- [ ] Prove the placement-host VM-resource lifetime separately from provisioning completion. For
      Lima, use supported platform lifecycle operations and prove readiness, disconnect recovery,
      stop and rollback for the supported drivers. A foreground anchor is an option to justify for a
      concrete workflow, not a required replacement for Lima's runtime. Do not treat numeric PID
      records as authority to kill unrelated host work.
- [x] Prove WSL2 host-client and guest-anchor cleanup independently before wiring a production
      platform hold. A Windows Job Object or observed `wsl.exe` exit proves only host-client
      cleanup. The candidate must obtain a guest-ready acknowledgment, retain an exact guest process
      identity, request cooperative exit, and verify that identity is absent after ordinary release
      and controller hard death while unrelated guest work survives. Production platform-hold and
      recovery composition remain separate gates.
- [x] Build the private WSL2 platform-hold ledger adapter under an already acquired exact-VM owner.
      Give each hold its own pre-effect canonical bounded payload and nonce, capture exact Windows
      controller creation identity, mark possible effect before one guest-anchor dispatch, publish
      boot-aware READY identity, and resolve ordinary release only on never-creation or settled
      local resources plus independently confirmed exact guest absence. Keep the owner available for
      child borrows during nested holds and retain uncertain coordination for explicit recovery.
- [x] Implement the private Windows controller observer for one validated persisted PID and creation
      time in native ticks under a finite deadline. A pinned process handle proves an exact live or
      exited process, or PID reuse; only a complete process snapshot can prove absence after open
      failure. Ambiguous API results and deadline expiry stay unknown. This is controller evidence
      only, without dispatch drain, guest absence, recovery factory or production wiring claims.
- [x] Build a private ordinary-release guest observer with a fixed no-staging Python query bound to
      guest boot, distribution init, PID and process start. Retain its native WSL client before
      dispatch and through uncertain cleanup; require complete nonce-bound output, exact zero exit,
      live deadline and local settlement before accepting absence. Compose it with a real
      SQLite-backed owner and two independent hold obligations in portable tests. This does not
      establish controller-death recovery, service-side dispatch drain or native WSL acceptance.
- [ ] Prove WSLService-side dispatch drain after controller death independently of Windows client
      Job cleanup. In a controlled delayed-delivery case, hold an admitted `CreateLxProcess` before
      guest creation, lose its controller, run a later absence observation, then release the delayed
      launch: the claim must stay unresolved. Establish which acknowledged launches can be recovered
      safely, and retain pre-`READY` ambiguity unless a stronger fence is proved. Distribution
      termination alone is not a drain proof or permission to disrupt unrelated work.
- [ ] Implement recovery handling for the versioned one-way guest-query admission marker. A later
      controller must retain any marked obligation unless every earlier admitted query can be
      accounted for or an independent service-side dispatch drain is proved; a fresh absence query
      alone cannot discharge it. Preserve the ordinary controller's ability to resolve on one
      complete exact absence result without claiming that the marker proves drain.
- [ ] Implement production WSL2 hold adoption and recovery: exact preparation discovery after a
      crash, controller-absence and dispatch-drain proof, a production exact epoch-bound guest
      observer, recovery factory, activation and platform wiring, RunContext integration, and live
      proof.
- [x] Compose one private ordinary WSL2 VM DOWNLOAD under a single exact-VM owner. Start the
      platform hold, prepare the target, require the resolved guest marker, boot ID and PID-1 start
      ticks to match the hold's durable READY evidence before file dispatch, then release only after
      settled file custody, exact guest-anchor absence and resolved lifecycle obligations. A settled
      missing source is a refusal, not retained work; uncertain file or hold outcomes retain the
      claim. This portable composition proof does not establish native locator-to-registration
      binding, WSLService drain, crash recovery, a production factory or RunContext access.
- [x] Add a private selected-WSL2-platform path for that ordinary DOWNLOAD. Observe the current
      registration and resolve the platform-owned native route before VM ownership, then start the
      hold under that owner and use selected-platform preparation with the held locator. Refuse
      changed registration, route or runtime before file dispatch, retaining the claim if exact hold
      absence is uncertain. This establishes portable file-dispatch gating, not atomic registration
      observation, pre-probe route stability, native WSL behavior, WSLService drain or a production
      factory.
- [x] Replace the private selected-platform preparer's internal binding resolution with one
      caller-owned binding and a locator observed before resolution. Confirm the locator before and
      after the guest probe, and use the same core-owned WSL2 carrier for probing and file dispatch.
      Prove replacement during resolution, changed registration around probing, isolation from
      plugin-route mutation and uncertain custody in focused tests and private review. This
      supersedes only the earlier checkpoint's internal re-resolution, not its completed historical
      record or the native and production gates above.
- [x] Require the private WSL2 operation wrapper to consume one exact VM owner acquired by its
      caller before route selection. Share that owner across the hold, preparation, file dispatch
      and managed start; read lifecycle facts through the owner and leave whole-operation sealing,
      resolution and release to core. Exact-owner and cross-database regression tests cover both
      wrapper entry points. This remains private composition, not production activation ownership.

The earlier private WSL checkpoints' route-before-ownership ordering and wrapper-level owner release
are superseded by this caller-owned arrangement. Their checked boxes record historical proofs, not
the production order or release authority.

The private WSL2 hold-recovery checkpoint at `15f8a936` consumes one exact persisted obligation
after generic takeover. It resolves a registered row without dispatch or a post-`READY` row only
after exact old-controller absence and a new, fully settled exact guest-absence query. Missing
`READY`, an earlier one-way query marker and uncertain observation retain the claim. Its
caller-retained observer preserves native-client cleanup custody after an unaccounted query; a
complete `PRESENT` result permits another query only within that same recovery controller, while a
complete `ABSENT` result permits exact ledger-resolution retry without another query. Portable
SQLite process-loss tests and focused static checks pass. This is not the production factory,
activation/RunContext wiring, native WSLService drain proof or live Windows/WSL2 acceptance; the
three gates above remain open.

The private WSL2 ownership candidate is implemented in the isolated proof branch. The caller owns an
inert lifecycle object before `start`; one native owner retains the WSL client, process/pipe handles
and Job Object handle across startup and cleanup interruption. Its snapshot keeps client exit,
client-handle closure, Job assignment and Job-handle closure independent, invalidates pre-dispatch
certainty before native effects. After an unaccounted guest query, ordinary release may retry local
settlement but refuses a second query or absence claim. A complete exact `PRESENT` response may be
queried again without replaying the anchor. The native adapter selects creation-time Job membership
and explicit handle inheritance with no weaker fallback. Portable tests execute the fixed helper
against Linux procfs and inject native API failures; native-Windows cases are present for Job
membership, handle confinement, descendant cleanup and abrupt controller death without invoking WSL.
Hosted Windows 2025 with Python 3.13.15 passes all 496 selected cases with 39 skips at `dca96813` in
[run 35710387854](https://github.com/WayfarerLabs/agentworks/actions/runs/35710387854). That
establishes the synthetic host-client cases, including abrupt controller death while unrelated work
survives.

The live Tier 2 Windows/WSL2 round at transport `f13f48e5`, composed with SSH `3cf322f9`, closes the
guest-anchor proof above. Windows Server 2022 with WSL 2.7.14.0 drove Ubuntu 24.04 under the bound
root account. Real `wsl.exe` preserved all 13 tricky literal arguments, binary stdout/stderr, finite
NUL input and EOF; bounded output and sensitive-input suppression also held. The measured nonzero
statuses confirmed that WSL cannot distinguish normal exit from the same numeric signal, so the
carrier correctly retains those values as observation failures rather than typed completion.
Ordinary release and forced controller death both removed the acknowledged exact guest PID/start
identity, independently observed through another `wsl.exe` process, while unrelated guest and
Windows work survived. The Windows client, guest anchor and helper residue were zero before the bed
was torn down. The composed non-integration suite passed 12,979 tests with 21 skips and every
static, documentation, Rulesync and website gate passed. This evidence does not wire the production
WSL platform hold, recovery factory, target identity or RunContext surface; those remain open.

The next native Windows/WSL2 experiment at transport `333b17bc` examined whether a read-only WSL UNC
file handle could hold a running distribution without a guest command. The
[full integration report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)
covers Windows Server 2022 build 20348.5622, WSL 2.7.14.0, a fresh Ubuntu 24.04 target, an
independent running control distribution, and complete tester cleanup. The official probe result is
`UNKNOWN`: its 30-second cold-open cap could not observe the measured 80-second `\\wsl$` refusal,
and its `/etc/os-release` path was a symlink that the WSL share could not open. The tester
separately sampled the stopped target throughout the cold open: `\\wsl$` refused after 80 seconds
without starting it; `\\wsl.localhost` refused after 40 seconds, also without starting it. With the
target already running, a supplementary run used the regular `/usr/lib/os-release` file and the
probe's otherwise unchanged cases. Against a 15.75-second no-handle distribution lifetime, one
handle retained it for 180 seconds; a second case closed the first of two handles, then the
remaining handle retained it on its own for 180 seconds. It stopped after normal close, last-holder
close, and abrupt holder death, while force-termination stopped the exact target and the unrelated
control survived. The 60-second utility-VM idle time was distinct from the roughly 15.6-second
target distribution idle time, especially with the unrelated distribution running.

This is bounded hold-feasibility evidence, not an official probe pass or a dispatch-drain proof. A
local synthetic review case also demonstrated a false-positive cold result: the probe observed
distribution state only after the open attempt and holder close, so a distribution that started and
stopped during that interval could yield `PASS`. The disposable probe and its dedicated tests were
removed rather than expanded into a second process-observation framework; the full integration
report preserves the observation record. The production WSL hold adoption, exact guest observation,
dispatch drain, recovery factory and live acceptance checkbox above remain open.

The durable launch checkpoint is privately accepted at `ead879ce`. Project, complexity and
independent correctness reviews are clean. Review removed the dormant application, cleanup and
disposal fields, the redundant stored unit name and a forwarding service object; the final schema
records only facts the checkpoint can produce and consume. Reviewer runs pass 121 and 177 relevant
execution, migration, backup and migration-safety tests. Independent experiments also confirm
cross-connection commit visibility before the launch boundary, one dispatch under concurrent launch,
exact idempotent reconciliation, malformed-state refusal and no replay after either terminal launch
outcome. No native destination or production workflow was exercised by this private review.

The checked private candidate is local mechanism evidence only. Its source harness executes the real
bundled supervisor against a test-local cgroup-filesystem shim and deliberately avoids detached
payloads. It does not prove Debian systemd 252+ delegation, root control to non-root service
identity, descriptor inheritance through `systemd-run`, real `cgroup.kill`, detached-descendant
cleanup, boundary removal timing, `waitpid` failure, carrier interruption or any SSH/QGA behavior.
Those observations remain required by the unchecked native and lifecycle gates above; a failed
native case blocks the MANAGED profile rather than selecting DIRECT or weakening the result.

The first native round composed transport `126c12a4` with SSH `f6af80cc` without changing either
branch. Bookworm systemd 252 and Trixie systemd 257 over SSH, plus a Bookworm guest through PVE 8
QGA, proved target UID/GID/groups, workload placement before caller code, byte-exact binary streams,
payload exit 23, no staging, truthful interrupted observation and cleanup of a real detached
descendant. Independent checks found no remaining transient unit, workload cgroup or process. PVE 9
was unavailable because every nested-virtualization-capable machine type tried in the authorized
zone was capacity-exhausted; it remains a required coverage cell rather than inferred evidence.

That round also found two candidate defects. Terminal/lifecycle `complete` depended on whether a
normal exiting payload happened to accept all offered stdin before its wait became observable, and
failed-to-start transient units accumulated in systemd's failed set. The corrections at `32751d5d`
retain post-wait helper I/O failures separately from terminal/lifecycle completion and use fixed
`--collect` for foreground unit cleanup. They do not weaken the later overall-success reduction or
replace durable job records. A second native round must repeat immediate exit, delayed exit and
partial-input-reader cases through SSH and QGA, prove failed-start collection on systemd 252 and a
newer release, and retry PVE 9. Native asymmetric stream failure and `waitpid` fault injection also
remain unmeasured; independent launch, terminal mode, reconnect-capable jobs and production
composition remain under the broader unchecked gates above.

The second native round composed transport `ae1ce293` with SSH `b57b45df`; composed head `73cdc437`
had CLI tree `77a52a47`, which SSH later adopted unchanged. It directly exercised SSH on
Bookworm/systemd 252 and Trixie/systemd 257, PVE 8 QGA on Bookworm, and PVE 9 QGA on Trixie.
Immediate-exit, delayed-exit, partial-reader and full-reader cases all retained terminal lifecycle
completion independently from their input fact. Four failed identity launches per cell left no
failed unit, collected unit or workload cgroup, while successful and exit-23 runs retained their
wait, stream and empty-boundary evidence. The round also re-proved target identity and groups,
pre-payload cgroup placement, detached-descendant cleanup, binary streams, boundary-forgery
resistance, interrupted observation without replay, self-cleanup and no staging. The composed
non-integration suite passed 12,708 tests with 13 skips and every static, documentation, rulesync
and website gate passed. Provider, guest unit, cgroup, process and scratch residue were
independently zero before teardown.

This closes the PVE 9 coverage cell and verifies both first-round defects. It does not complete the
unchecked lifecycle or production-composition gates. Native asymmetric stdout-only collection
failure and `waitpid` fault injection remain unmeasured, as do terminal mode, reconnect-capable
jobs, production RunContext composition, migration and cutover. The public feedback/fix round closed
cleanly at this pin; no further head change was requested by its test or complexity evidence.

- [ ] SSH effort builds `execution/carriers/ssh/` and its connection/trust migration. Transport
      builds common execution, scoped context delivery, files/jobs and other adapters, and applies
      SSH policy in platform-host/Lima/provisioning paths. Test reusable host composition separately
      from Lima commands; preserve one SSH implementation in the new stack.
- [ ] Integrate against the agreed contract and run the new-stack tests with legacy modules
      unavailable, including indirect imports and plugin initialization. No production old/new
      selector, shared legacy runner, or duplicate mutation dispatch is allowed.
- [ ] Specify and validate SSH state transition, concurrent-writer ownership and rollback evidence.
      Preserve configuration and complete trust records without importing old execution code.
- [ ] Deliver a shared file-only vertical slice through SSH and native QGA, without caller
      command/job calls: whole-file installation, privileged JSON merge preserving unrelated keys,
      approved directory creation/metadata and conditional removal. Add a session-owned stale-socket
      case; tmux creates sockets and no FIFO creation is required. Keep the initial small carrier
      proof intact; this later slice gates broader file-consumer migration, not the independent SSH
      build. Restricted test composition can withhold execution; production permissions remain
      inactive.
- [ ] Prove file correctness from first use and the future permission boundary in isolated tests:
      default denial, exact-file/subtree scopes, root versus parent authority, prefix
      collisions/traversal, links and concurrent substitution, confined extraction, forbidden
      metadata/removal, and inability to widen grants. Cover attempted helper redirection through
      caller environment, PATH or working directory, cooperating writers, external-writer limits,
      malformed JSON, special-object refusal, sensitive diagnostics, partial transfer/cleanup and
      uncertain publication. No runtime fallback may expose commands to the file-only caller;
      unavailable safe mechanics block acceptance. Record live target/platform evidence under an
      authorized charter. Adapt 0.19.0's boundary, settings, generated-section/ACL,
      publication-checkpoint and native-inventory tests to new delivery; copying tests does not
      establish the stronger race/concurrency promises by itself.

### Target identity composition checkpoint, 2026-09-21

Private project and complexity reviews of the design at `9822a26a` found no material or optional
findings. Markdown formatting, structure, spelling, whitespace and locked-SDD checks pass. This is
design evidence only; the implementation has its separate code evidence below.

- [x] Implement the private owned target identity composer described in the preparation LLD. Observe
      delivery/workload/root account facts as needed under one borrowed owner and deadline; produce
      only a supported direct, sudo-root or root-demotion plan. Keep account, termination, deadline
      and unresolved-ownership evidence distinct. Test refusals, interrupted observation, normal and
      abnormal completion, and actual helper composition without claiming native sudo or demotion
      acceptance. Public RunContext and production activation wiring remain separate required gates,
      not completed by this private increment.

The private composer and shared fixed-helper admission adapter are reviewed at `0c433730` by all
three lanes, with no material findings remaining. The correction preserves expiry after abnormal or
no-send completion without replacing its primary failure, and makes account tests collectable on
Windows. All 35 focused cases pass; removing the expiry correction makes its four new regressions
fail. Native identity transition acceptance is not implied. The optional suggestion to narrow the
Windows marker is acknowledged; the small composition module remains selected as a unit.

The full local suite passes 12,243 tests with 13 skips. Ruff lint/format, full mypy (1035 sources),
file lint, locked-SDD/rulesync, typer isolation and whitespace gates pass. Website validation passes
160 Python tests, 103 Node tests and both deterministic build comparisons. An initial run used a
long workspace fixture directory whose inherited default ACL/setgid and Unix-socket path limits
caused 32 failures; concurrent fixture deletion also interrupted the website repository scan. The
same pre-correction code passed all 12,239 tests with 13 skips under a short `/tmp` fixture root,
and the final corrected run above uses that placement. No production checks were weakened to make
those environment-dependent failures pass. No live infrastructure was exercised.

Hosted run `35596902203` passed Linux 3.12/3.13/3.14 and the non-test gates, but Windows reported
460 passes, 39 skips and two fixture errors. Its environment-variable limit rejected pytest's
autogenerated ID containing the 32,768-character invalid account name. The correction at `bac7bd44`
uses short explicit IDs, preserving all inputs, assertions and Windows coverage. Private project and
complexity reviews are clean; all 35 identity tests pass locally. All hosted checks pass in run
`35598300799`, including Windows 3.13 and Linux 3.12/3.13/3.14.

### Paired private target plans, 2026-09-24

- [x] Compose a private ordinary plan and an optional elevated plan in one target identity
      preparation call. The ordinary path must succeed before elevation is considered. Reuse the
      same deadline, owner borrow and account observations; look up root only when the caller
      explicitly includes elevation and non-root delivery requires sudo. Keep production target
      composition, permissions and native transition acceptance open.

### Managed VM target identity checkpoint, 2026-09-21

- [x] Implement the private version-one VM incarnation codec and pure managed-target composer. The
      codec binds the exact UTF-8 provider locator framing to the validated persisted instance
      marker, while the composer verifies the guest marker and keeps the canonical boot UUID outside
      the incarnation hash. Unavailable locators, legacy NULL markers, malformed values and marker
      mismatches fail closed; ordinary composition never creates, adopts or persists a marker.
- [x] Implement the private bounded Linux guest-identity probe. It makes one no-replay carrier
      attempt with closed stdin, admits the pinned Python runtime, reads only the fixed instance
      marker and boot-ID paths, validates the root-owned marker path and returns a nonce-bound typed
      observation separately from carrier facts. Isolated tests cover unsafe leaves, malformed
      values, incomplete streams, stderr, oversize, wrong responses, pre-expired deadlines and
      buffer clearing.
- [x] Compose the private VM target preparation under an already-acquired exact-VM operation owner
      and one finite deadline. Refuse mismatched ownership, unavailable locators, legacy NULL or
      malformed markers and expired budgets before borrowing or dispatching; use one serialized
      helper attempt; retain the typed guest result; record it before settlement; reject carrier
      failures, abnormal termination, runtime refusal, invalid observations and identity mismatch;
      and retain ownership after ambiguous dispatch or interrupted control flow. This function does
      not acquire or close the owner, activate a VM or route, look up a platform, adopt/persist
      identity, or expose a production target. Replace exact scope equality with the future
      core-owned hierarchy coverage predicate when #377's admission model lands.
- [x] Compose a private selected-platform preparation seam under that same owner and deadline.
      Static owner, platform-site, marker and finite-budget checks precede platform I/O. One serial
      borrow spans locator observation, native binding resolution and the guest attempt. Preserve
      typed locator-unavailable refusal, validate plugin results and deadline after each platform
      call, then delegate one guest attempt through the shared preparer. Return the validated
      passive binding only with successful preparation after an equal second locator observation.
      Valid unequal locators indicate change; unavailable or invalid confirmation remains
      unconfirmed. This detects cooperative replacement at preparation time; production later use
      still needs a locator-bound platform hold and binding. Malicious or engineered A-B-A host
      behavior is outside this checkpoint's threat scope. Release failure suppresses target and
      binding with an uncertain control fact retaining guest evidence. Hermetic tests cover refusal
      ordering, malformed returns, platform exceptions, late observations, exact input forwarding
      and one borrowed success. WSL2 is the first current positive locator and native binding pair;
      Proxmox remains unavailable and SSH-backed cloud/Lima bindings remain for #832 or later. This
      checkpoint does not activate or hold a VM, open a route, acquire or close the outer owner,
      reserve a run, establish live WSL/SSH/QGA behavior, supply Proxmox/Lima locator alternatives,
      prove recovery drain, adopt identity or expose RunContext/public access.
- [ ] Complete the target-identity gate with production/platform composition and live carrier proof.
      No explicit adoption workflow, locator-unavailable alternative or public RunContext claim is
      complete at this checkpoint.

### File values and owned composition checkpoint

- [x] Implement the frozen public file values and opaque revision conversion described in the file
      LLD. Reuse the existing versioned revision schema, validate consistent metadata and
      content-bound read results, and preserve payload privacy and portable module imports. These
      values do not establish remote evidence or activate permissions.
- [x] Compose bounded read, stat, inventory, conditional removal and metadata operations under one
      borrowed operation owner and deadline. Metadata name resolution and mutation share that
      borrow; record observations before settlement and retain ownership on unresolved effects or
      coordination failure. Preserve partial mutation separately from deadline and termination.
- [ ] Bind these primitives into the complete public FileAccess surface with typed error reduction,
      exact-operation path confinement and safe target/phase diagnostics. Complete production
      ownership, local download publication, directory transfer and native acceptance remain
      required; these private building blocks do not close those gates.
- [ ] Resolve lifecycle-ledger capacity before migrating high-volume artifact publication. Keep the
      current 128-row bound and commit-without-reply evidence model; do not prune resolved rows
      without a separate durable tombstone protocol or merely raise the limit. Prove that one
      bounded logical directory/package operation can publish many physical members under one
      obligation, including the current maximum supported package, or introduce an explicit lower
      product limit before production adoption. Per-file public calls must not make a supported
      artifact package fail only because its owning command accumulated resolved rows.

The 2026-09-24 inventory found a generic capture ceiling of 4,096 regular-file members in
`package_sources.py`, while the native harness inventory probe caps 512 entries including implied
directories for Claude, Codex and Grok. There is no single maximum shared by all harness
integrations. Current artifact publication iterates files and checkpoints each confirmed change;
mapping those calls one-for-one to retained lifecycle rows would exhaust the 128-row bound for a
supported large package.

The [serial package candidate](file-operations-lld.md#serial-package-capacity-candidate) uses one
row for the current child across preflight and mutation. A private originating-path
`FileOperation.upload_package` increment now uses one borrow and row for up to 4,096 upload members,
persists each child index and token before dispatch, and waits for a caller-supplied durable
checkpoint before preparing the next child. A real 129-member test stops after a checkpoint commits
but loses its reply; a synthetic 4,096-member test exercises the ledger bound without claiming 4,096
native file writes. The tests also cover a lost child-CAS reply, an unconfirmed CAS, a later invalid
member and control-flow interruption. This is not yet a process-loss recovery proof or a complete
package API: upload-wide helper drain, takeover, preflight reads, retirement, application checkpoint
integration and production artifact callers remain open. The capacity checkbox stays unchecked.

- [x] Extend the private upload helpers and pre-bound `FileOperation` upload/package paths to carry
      one exact existing gate through stage, publication, reconciliation and cleanup. Validate its
      Linux VM scope, effective UID, deterministic path and derived boot; persist it in the current
      child row before dispatch. Real local gate, stale-generation, failure-cleanup, package-child
      and near-limit payload tests pass. The optional path without a gate remains for legacy
      callers; this checkpoint does not establish package gate setup, production composition or
      process-loss recovery.
- [x] Admit private single-upload gate setup under the same row, borrow and token as the upload.
      Hold the source before registration, publish the bound gate before staging, and retain custody
      across lost setup, publication and promotion. Setup-only takeover uses non-creating inspection
      and settles only that control-state obligation, never the upload. The durable codec now
      refuses download as well as upload setup/bound rows whose guest-derived boot differs from the
      managed target. Local fixture and codec tests pass; native delivery and production composition
      remain open.
- [x] Extend private setup custody to package-upload child zero without adding another row or losing
      its index, token or application checkpoint. Publish the binding before preparing the first
      child; a lost setup reply may settle only the setup-only row after takeover and exact
      non-creating inspection. Local tests cover ordering, lost reply, lost binding publication and
      wrong guest. This does not resume a partly completed package after process loss or prove
      native carrier delivery and drain.
- [x] Add a private fence-only takeover for the exact current bound package child. Persist and reuse
      one proposed gate generation in the same possible-effect row, dispatch advance, and publish a
      confirmed binding without changing its index, token, path or cleanup facts. Local tests cover
      stale delayed effects, lost replies, interruption, row changes and absent/replaced gates. The
      child remains unresolved: this does not replay it, reconcile the application checkpoint, prove
      native mutation-path coverage or establish predecessor-controller and other non-gated effect
      custody.
- [ ] Require the gate in production upload composition, including verified raw guest marker and
      selected provider locator, without fallback to requests lacking a gate. Prove generation
      advance waits for old gated effects and rejects delayed old-generation requests before
      mutation across WSL, SSH and QGA; then bind package-child recovery and application checkpoint
      reconciliation. Neither the local DOWNLOAD helper journal nor a caller-asserted drain value
      proves upload takeover.

The production artifact publisher currently preflights destinations, then retires and publishes
members with a per-file ownership callback. Agent and workspace creation can call that publisher in
`buffered` mode before their resource row exists; in that mode `run_setup` does not persist those
callbacks, and agent creation writes the final state only after remote setup. The private package
callback's durability precondition therefore cannot be met by forwarding the existing callback
unchanged. Migration must establish an operation-owned pending creation/checkpoint record before
remote artifact effects, or another explicit durable ownership path, and reconcile it on takeover.
This is part of the activation-before-RunContext owner seam, not an excuse to weaken the batch gate.

- [ ] Replace buffered agent/workspace artifact creation with a durable per-member ownership
      checkpoint before adopting the new batch path. Prove interrupted creation can recover the
      exact current child without assuming the final agent/workspace row was committed.

- [ ] Resolve the public filesystem-root edge before claiming complete path coverage: the current
      nonempty parent/leaf helper contract cannot address `/` itself. The operator has been asked
      whether to exclude root targets initially or support read-only root stat/inventory. Keep
      ordinary public reads on snapshot/chunk delivery, inline reads on the separately bound
      no-staging path, and coexistence confinement separate from deferred permission activation.
- [ ] Move serial-borrow lifetime to the core file boundary so returned and exceptional private
      outcomes are retained before public reduction or relinquishment. Prove shared user/admin views
      cannot lose cleanup responsibility, and distinguish unresolved remote effects from
      proved-inert cleanup debt rather than using `requires_owner_retention` as a blanket claim
      release rule. Keep the coordinator free of file protocols and a second file lock.
- [x] Add concrete private result reducers for stat, inventory, removal, metadata, download,
      memory-read, upload and JSON outcomes. Preserve closed diagnostic facts, known destination
      change and partial/uncertain mutation evidence without exposing private outcome objects.
      Generic carrier failures do not imply connectivity loss. These reducers neither manage custody
      nor complete public FileAccess; upload/download exchange-detail retention and production
      integration remain required.
- [x] Retain the first primary download failure's exchange phase, dispatch and carrier failure. Keep
      helper failure details coherent with that primary failure while later cleanup updates
      independent cleanup/effect facts. Local sink failures and controls without a carrier report do
      not invent exchange evidence. Public reducer consumption remains a separate step.
- [x] Accept named `NewMetadata` in private upload, JSON and core custody composition. Resolve
      actual creation ownership before staging under the existing borrow and deadline, recording
      resolution before settlement. Replace/Match and existing-target JSON paths do not look up
      unused names. Retain first-primary upload exchange and publication failure details while
      preserving independent later cleanup facts. Numeric metadata stays at the fixed publication
      boundary; reducer integration and production FileAccess remain required.
- [x] Consume retained primary transfer and ownership-lookup facts in the private reducers. Preserve
      specific carrier failures independently from mutation uncertainty and unknown runtime
      evidence, expose a distinct removal phase and make confirmed changes visible in failure
      guidance. Remove duplicate private completion checks while preserving independent deadline
      refusal. This projection remains separate from public FileAccess wiring and durable recovery.
- [x] Preserve JSON failure provenance across nested uploads and direct retry work. Delegate phase
      and reason only when the terminal failure is the current child upload; retain direct carrier
      dispatch/failure facts and never let a stale publication conflict mask a later read failure.
      Production-path tests cover missing creation ownership, initial stat/read deadline loss and a
      conflict followed by a retry-read deadline. This remains a private reduction correction, not
      completion of the public FileAccess or recovery gates.
- [x] Preserve file-result chronology and known effects independently from later host state. A
      helper-confirmed change or no-change raises an ordinary typed failure with that exact effect
      when later termination or coordination is incomplete; helper refusal precedes a deadline
      sampled after its response. Record deadline expiry at actual upload/download cleanup entry as
      cleanup rather than transfer. Retained stat/inventory carrier failures precede later generic
      custody state. Direct JSON observation retains carrier failures even after normal process
      termination, and phase-less object refusals use the caller's observation/removal phase. Real
      producer-path tests retain custody and no-replay behavior.
- [x] Change private upload, download, JSON, memory-read and single-file compositions to accept a
      caller-owned `OperationBorrow` without acquiring or closing it. Nested compositions reuse the
      same borrow. Focused lifetime tests cover normal, invalid and exceptional results and prove
      that an unresolved attempt still prevents owner release after borrow closure. This
      prerequisite does not complete core outcome custody, durable recovery or production
      FileAccess.
- [x] Implement concrete private download custody under the existing operation owner. Attach
      validated working state before dispatch, retain original carrier/binding and unfinished
      outcomes before borrow release, and keep multiple obligations without retaining sinks in
      completed records. Tests cover pre-dispatch refusal, overlap, close/admission interleaving,
      identity-keyed record removal, exceptional outcome capture and allocation/retention failures.
      This download-only path does not complete shared public views, other file families, durable
      recovery or the outer claim-release gate.
- [x] Route the private memory-read adapter through shared `FileOperation` custody. Retain the
      original download outcome before byte/result allocation, preserve original control identity,
      and discard partial memory buffers. Fault tests prove inert cleanup debt survives failed
      result allocation and complete-download allocation failure does not strand a borrow. No
      compatibility bridge, new claim, public view or durable recovery is introduced.
- [x] Extend private `FileOperation` custody to uploads and JSON updates. Attach validated parent
      and nested upload state before dispatch, preserve exact original exceptional facts before
      borrow release, and retain child token/state when fact construction fails. Completed custody
      omits source streams and JSON bytes. Local tests cover all JSON strategies, no-op, overlap,
      multiple cleanup obligations, uncertain publication and failed capture; public reduction,
      other file families and durable recovery remain incomplete.
- [x] Extend private core custody to stat, bounded inventory, conditional removal, metadata and
      directory convergence. Attach original bindings and working state before dispatch, capture
      outcomes before borrow release, and retain lookup evidence before metadata settlement under
      the same borrow. Exchanges validate requests before dispatch; metadata validates options
      before lookup. Local tests exercise real helper operations, lookup refusal, partial metadata,
      overlap, failed fact construction and failed retention. Remove the unused private inline-read
      wrapper; ordinary reads still use snapshot/download. Public reduction, durable recovery,
      production ownership and RunContext remain separate gates.
- [ ] Give each logical file call one adapter-owned lifecycle obligation that also provides its
      carrier-dispatch admission. Use a caller-retained identifier for exact registration retry;
      persist the managed target fence, confined location, helper identity/runtime and scratch token
      before dispatch; update the same row with exact retained cleanup facts before releasing
      in-memory custody. JSON must keep one parent row and publish each nested upload token before
      that child can dispatch. Never persist file/JSON contents, source streams, credentials or
      replay material. Prove clean resolution, cleanup-only retention, before/after-commit database
      interruption and process loss around response and handoff.
- [ ] Complete the durable recovery handoff for file work, including pre-dispatch reconciliation
      identity and exact cleanup binding without storing payload contents. Test process loss before
      response, after response and during handoff. A surviving claim without recovery facts is not
      completion evidence; do not close the production ownership gate with in-memory retention
      alone. Use a distinct restricted recovery-dispatch object rather than ordinary borrow mode.
      Revalidate the exact generation, obligation state, payload revision and bytes before every
      attempt; share the serial-use guard; and keep registration, initial effect admission, generic
      publication and automatic resolution unreachable. Adapter drain evidence must cover every
      outstanding dispatch for the obligation across all earlier generations. For DOWNLOAD, expose
      only reconciliation and exact cleanup, persist newly discovered cleanup debt before cleanup
      dispatch, independently observe local helper termination and refuse while a helper survives
      controller loss. Treat this local spawned-process proof as substrate evidence only, not SSH,
      QGA or native production acceptance.

The 2026-09-24 local DOWNLOAD proof now originates its spawned-controller crash cases through
`FileOperation.download()` rather than fabricating a possible-effect row. A test-only carrier reads
the installed row before dispatch; expected and actual helper journal records must match its
persisted token in every recovery generation. The tests retain completed-helper cleanup and
surviving-helper refusal. The test-only journal does not prove native SSH/QGA dispatch drain.

- [x] Prove the shared guest effect-fence mechanism locally through DOWNLOAD first. Persist one gate
      binding in the existing `file-call` row, carry the verified raw guest epoch, initialize the
      gate before effect admission, and have the snapshot helper hold the same-inode Linux `flock`
      through effects and cleanup while SQLite stores generations in short rollback transactions.
      Prove that reading and closing another descriptor for the gate inode cannot release the effect
      lock. Exercise deadline expiry, active-helper takeover, delayed old requests, lost advance
      replies, replaced or missing state, and incomplete cleanup with a spawned controller. Use an
      isolated local mount fixture; this does not prove the selected `/run` setup, other helper
      families or native carrier dispatch. Do not create gate state in no-write, no-state readiness.

The private `f5fc93fb` DOWNLOAD checkpoint is **not accepted** as this proof. Its fixed-helper
controller-crash tests and durable binding are useful, but project review reproduced a competing
advance while the old helper's SQLite transaction remained open after a same-inode source descriptor
closed. The replacement design and both independent reviews select a single-file Linux flock effect
lock with SQLite used only for short transactional generation storage. No code from that private
checkpoint has been published on #833 or claimed as production recovery. Fixed-helper interrupted
cleanup, recovery of recovery, native routes and gate setup remain unproved.

The corrected private mechanism is composed at `b953eff7`. It binds the gate inode in the durable
DOWNLOAD obligation, holds Linux flock across fixed-helper effects after closing the SQLite
connection, and reproduces the original same-inode source-close race with a real spawned helper.
Project and complexity reviews found the final correction clean; the combined tree passes 201
focused file-gate, snapshot, recovery and obligation tests. At that checkpoint, fixed-helper
interrupted cleanup and recovery of recovery were still unproved. This is local lock-lifetime
evidence, not production drain evidence.

The shared fixed-helper bundle correction at `725514b9` removes docstrings and regenerates only
trusted file-helper source before the existing BZ2/base64 delivery. It restores complete local QGA
serialization fit after the snapshot gate dependency enlarged that family: the largest measured body
is 63,442 bytes with a synthetic 32 KiB ASCII manifest and demoted identity, leaving 2,094 bytes
below the unchanged 65,536-byte limit. The six family and three identity-mode size tests, valid
helper and fixture execution on controller Python 3.12 and distribution Python 3.11, and the
combined 252 fixed-delivery, effect-gate, snapshot, recovery and obligation tests pass. The margin
is not a guarantee for arbitrary future manifest escaping or bundle growth. Native SSH/QGA
acceptance and the open production effect-fence gates above remain separate.

The reviewed A-to-B-to-C local proof at `b06699bb` now interrupts B's real fixed-helper cleanup
after it unlinks snapshot data while retaining the gate lock, then kills B's controller. C cannot
advance while that helper lives; after its exact recorded exit, C advances, refuses stale B cleanup,
reconciles the same persisted debt and completes exact cleanup without a second scratch owner. The
test harness bounds controller and orphan-helper teardown, signals only a recorded matching process
identity and reports incomplete dispatch/identity evidence. Complexity review accepted the scenario
and process-descriptor teardown; project review confirmed the final journal-accounting correction.
The combined focused selection passes 82 tests, and the non-integration execution directory passes
3,464 tests with 17 skips and one deselection. This closes the local DOWNLOAD checkbox only.
Production setup/advance dispatch, other helper families, native routes, guest namespace lifetime
and a production drain-evidence producer remain unproved.

- [ ] Prove the restricted
      [guest-side file-helper effect fence](file-operations-lld.md#production-file-helper-effect-fence-candidate)
      before treating adapter drain evidence as production. First carry the verified raw VM guest
      marker, kernel boot ID and PID 1 start time from target preparation into file custody and its
      durable obligation; the current managed-target hash/derived boot UUID cannot reconstruct them.
      Give platform hosts a separate concrete identity/epoch proof, not VM marker inference. Bind a
      stable gate instance and generation to the durable `file-call` row before effect dispatch;
      make each effect-bearing fixed helper validate and hold the gate through its effects; advance
      it on takeover using exact compare-and-swap. Cover lost advance replies, late old dispatch,
      active effects, stale advance, repeated recovery, missing/replaced gate state, target restart,
      and each effective user/elevated identity through real SSH, QGA and WSL paths. Preserve
      ownership on uncertain identity or fence state. This is a documented residual remote-dispatch
      race, not a revival of blanket file-object locks or privileged macOS host setup. The local
      helper journal is not a substitute. Prove core Linux guest setup and lifetime of the
      `/run/agentworks/file-gates-v1` namespace, its local mount and common visibility across all
      helper routes before native acceptance; never substitute account-home or `/dev/shm` silently.
      Treat confirmed generation advance as guest-file-effect fencing, not proof that the remote
      helper exited or that an old controller stopped writing a local download sink. The production
      recovery evidence and takeover path must separately account for predecessor-controller
      liveness or fence its non-gated effects before whole-operation resolution and claim release.
      Keep the public FileAccess and ownership release gates open until this and the concrete
      recovery handoff are proved.

The reviewed setup design selects one deterministic, epoch-bound path and exclusive creation per
managed scope/effective identity. A lost setup reply is reconciled by non-creating inspection of the
complete gate under flock, not by replacing it or trusting the request's guest identity. Setup and
inspection must independently observe the live guest epoch; only initial setup may retry exclusive
creation when no durable binding exists. A durable binding instead makes absent or replaced state a
retained uncertainty. This is a design ruling, not production guest setup/inspection,
selected-carrier dispatch, `/run` provisioning or native acceptance. The first production vertical
must still sequence setup, durable proposal, confirmed advance and DOWNLOAD dispatch on the same
`file-call` row, then fence recovery before producing drain evidence.

For a lost `SETUP` reply, a recovery owner may settle only a post-takeover, still-setup-only
`file-call` row after positive exact non-creating `INSPECT`. Durable publication must precede
snapshot dispatch, so the predecessor cannot begin DOWNLOAD after takeover; delayed setup only
adopts the retained gate. Preserve that gate and UID namespace for the epoch. An absent or
incomplete inspection retains custody, and a bound row instead requires the full effect fence and
cleanup recovery. Prove this narrow path locally and through each native route before enabling it;
do not mistake it for whole-operation resolution or remote helper-exit evidence.

The private setup-only recovery adapter at `3b95a068e` now rebinds a post-takeover exact row,
refuses a cross-scope or already-bound target, and resolves only that obligation after complete
positive `INSPECT`. Missing and incomplete observations retain custody. An interruption after local
attempt settlement no longer strands an active recovery dispatch; the same correction covers the
existing DOWNLOAD adapter. A local held-delivery test completes old `SETUP`, takes over and inspects
while its carrier result is still withheld, then confirms the stale controller cannot publish or
start the snapshot. The file test directory passes 1,332 tests, and project, correctness and
complexity re-reviews of the adapter report no remaining finding. This is local proof, not a
production takeover factory, native delayed-helper test, native route acceptance, namespace-lifetime
proof or whole-owner release.

Setup is itself a remote control-state mutation, so the current possible-dispatch rule applies
before its first attempt. The first production composition must arm one `file-call` row with a setup
descriptor and deterministic identity/path under the existing serial borrow, then publish the
discovered gate binding to that row before file effects. `FileOperation.download()` currently
installs its row after receiving a gate binding, so the admission and handoff must move earlier;
adding only a setup call before `download()` would leave it untracked. Allocate the download token
before setup, reserve all later binding, proposal and recovery growth within the 8,192-byte payload
cap, and keep existing version-1 rows byte-for-byte valid. A new optional setup descriptor may
remain version 1 only if older readers refuse it safely and old downloads without a gate never
acquire setup meaning. Do not add a second generic row or a phase enum.

The private local setup/inspection substrate at `1040c8d1` validates identity and independently
observes the live guest epoch before exclusive creation. Inspection opens only existing state under
the shared flock and discovers the complete instance, generation and inode without advancing it;
missing, incomplete or unsafe state refuses. The 87 focused gate, recovery and fixed-delivery tests
pass, including successful setup/inspection on distribution Python 3.11. After this growth, the
snapshot family's synthetic maximum-manifest QGA body measures 63,970 bytes, leaving 1,566 below the
unchanged 65,536-byte compatibility limit. This is not native acceptance or a guarantee about future
bundle growth. End-to-end setup/inspection deadlines, canonical path provisioning, selected carrier
exchanges and recovery integration remain open.

The private local deadline correction at `82e702f17` refuses expired setup before guest observation
or exclusive creation and before committing a generation; deterministic tests also confirm that an
incomplete inode remains after later expiry. It does not interrupt blocked guest observation,
filesystem or SQLite calls, or prove quiescence after a lost carrier response. The private bounded
gate-control exchange described in the LLD is still needed for selected-route production use.

The private fixed gate-control helper at `8e8c67953` now supplies one non-replayed selected-carrier
exchange for setup, non-creating inspection and generation advance. It forwards the finite setup
deadline to the local primitive and treats complete runtime-output loss after dispatched setup or
advance as explicit uncertainty. The combined gate and fixed-delivery selection passes 85 tests; the
representative complete demoted-identity QGA body is 47,023 bytes with a 32 KiB manifest, below the
unchanged 65,536-byte limit. The helper executes under distribution Python 3.11. These are local
protocol and delivery checks only: the production obligation handoff, guest namespace, native route
visibility, helper quiescence and recovery integration remain open.

The private numeric-bound correction at `69cc281d4` limits gate UIDs to Linux's 32-bit range and
device/inode identities to unsigned 64-bit values. This makes their encoded growth finite for the
future 8,192-byte admission reserve. At that checkpoint, a short canonical gate path and exact
reserve calculation remained open. The gate, recovery and fixed-delivery focused selection passed
109 tests.

The private setup codec at `2447a1220` now persists only the exact bounded path and raw guest triple
in the version-1 `file-call` row. A setup descriptor and exact binding cannot coexist. Its admission
check encodes a maximal bound binding with proposal and reserves the existing DOWNLOAD recovery
growth before any possible setup dispatch. Old version-1 rows remain byte-for-byte valid; setup-only
rows are refused by ordinary snapshot recovery. The combined obligation, gate, recovery and
fixed-delivery selection passes 148 tests. This does not create or dispatch the production row,
prove the raw guest belongs to the managed target, provision `/run`, or provide native route and
helper-lifetime evidence.

The private acknowledged-setup custody slice at `a9d87ff65` now attaches one DOWNLOAD call before
gate SETUP, uses its borrowed fixed-helper carrier for possible-dispatch admission, publishes the
exact returned binding on the same `file-call` row, and starts the snapshot with the same borrow and
token. It retains the attached call after uncertain registration, setup, publication or promotion;
proven `NOT_SENT` and pre-registration refusal release unused custody. The scoped selection passes
55 tests, with Ruff, targeted mypy and diff checks green. Independent project and complexity reviews
found no blocker for this narrow slice. It handles only acknowledged first-time exclusive setup:
existing-gate inspection/adoption, lost-response reconciliation, selected-route guest proof,
root-owned anchors and target-owned `/run/agentworks/file-gates-v1/<euid>` provisioning each boot,
native helper quiescence, and production recovery remain open. A target-user file helper cannot
create that namespace; core must establish the private UID directory through a trusted guest setup
path before target-UID gate setup, without elevating the file operation itself.

The private repeat-use correction at `bf8fdad41` closes that normal existing-gate gap. `SETUP` still
creates with `O_EXCL`; only `EEXIST` adopts an existing complete matching gate through the
non-creating, flock-held inspection under the same identity and deadline. Unsafe, incomplete or
mismatched gate files remain in place and refuse; standalone `INSPECT` remains for recovery after a
lost response. Focused gate/helper/download tests, Ruff, format, targeted mypy and diff checks pass,
with independent project and complexity reviews finding no blocker. This is not lost-response
reconciliation, selected-route proof, namespace provisioning, native quiescence or production
recovery, all of which remain open.

The private WSL2 owned-download composition at `6e16d979b` now constructs the setup descriptor from
the exact guest identity returned by selected-route target preparation only after that guest matches
the durable READY hold epoch. It carries the managed target and requested effective UID through the
same selected carrier/runtime into the one-row gate setup and download. Separate tests cover lost
setup and later snapshot responses without releasing the owner or hold. The 19 focused WSL2 tests,
Ruff, targeted mypy and diff checks pass, and independent reviews found no remaining finding. This
path at that checkpoint still lacked a guest gate namespace provisioner, production RunContext
factory, native Windows/WSL2 delivery-drain evidence and production recovery; portable fixture
success does not establish a live WSL2 pass.

The private shared Debian bootstrap at `fc5004cbf` now installs non-cleaning boot-time `d` rules for
protected root-owned `/run/agentworks` anchors and target-owned `0700` root/admin UID directories,
then applies them after admin account creation. Its immediate replay refuses unexpected existing
directory metadata; boot-time `systemd-tmpfiles` may restore directory metadata, but the rules never
descend into or clean gate database files. A focused generated-shell test passes on Linux, including
replay preservation and unsafe-ancestor refusal. The guest-side validator at `92e995092` separately
rejects invalid gate names, symlinked/unsafe ancestors, absent or unowned UID directories and ACL
attributes before setup, inspection, advance or hold. The maximum synthetic snapshot QGA request is
64,582 bytes against the fixed 65,536-byte limit, leaving only 954 bytes; further bundle growth
requires renewed fit proof. These private pieces do not establish native boot/replay behavior or the
common namespace mount across SSH, QGA and WSL2. Managed agent UIDs arise after VM bootstrap, so
account creation/reinit must install/apply persistent per-UID rules; account deletion and UID reuse
must preserve outstanding gate obligations before retirement. That lifecycle, a production
RunContext factory, native delivery drain and recovery remain open.

- [ ] Complete the managed-agent gate namespace lifecycle before admitting agent-UID file effects.
      Install/apply a persistent target-owned UID directory rule at account creation or reinit and
      prove reconstruction after reboot. Account deletion and UID reuse must coordinate with the
      same VM operation ownership used by file effects and refuse while an old helper or retained
      gate obligation can still act; only a proved quiescent path may retire the rule and directory.
      The existing `native_mutation_guard` serializes legacy native setup separately and is not
      evidence of that `OperationOwner` exclusion. Cover interrupted provisioning, repeated reinit,
      retained work, deletion and numeric UID reuse without repairing or replacing a live gate file.

The account lifecycle audit found the shared create/reinit seam in `create_agent_on_vm`, after
`useradd` or account convergence and before agent-level SSH. Standalone deletion currently continues
after a best-effort remote cleanup failure and cannot establish safe UID retirement. Complete the
owned VM lifecycle boundary first, then provision and retire the gate namespace with those callers;
do not add an unused standalone ensure/retire API. Use a separate persistent `systemd-tmpfiles` rule
per managed UID rather than rewriting bootstrap's root/admin rule file, which bootstrap replaces on
replay. Retire the rule and directory only after the old helper and gate obligations are proved
quiescent, before freeing the numeric UID. Neither the existing local mutation guard nor `pkill`
alone provides that proof.

The private bound-row correction now checks the deterministic VM/UID/guest gate path at the
`file-call` codec boundary as well as the setup-only path. A substituted but syntactically valid
gate name refuses during payload construction or decode. Focused obligation and spawned recovery
tests pass 62/62; this does not supply native drain evidence or finish recovery orchestration.

- [ ] Use the owned snapshot/chunk download for general in-memory reads, preserving caller byte
      bounds independently of QGA's single-response capacity. Keep the no-staging readiness read
      separately bound before dispatch; prove its complete encoded response fits the selected route.
      Do not retry a failed direct read through staging after uncertain observation.

The private no-staging read now calculates a conservative complete successful stdout bound from the
caller limit and asks the selected carrier to validate it before dispatch. Proxmox rejects a
declared bound above 1 MiB before POST, reserving response-envelope space under its 8 MiB HTTP
reader limit; WSL2's local sink remains streaming. The transport branch's buffered SSH adapter
refuses sink output, while #832's live SSH sink streams without a fixed capture ceiling. Local
boundary and refusal tests are not native QGA/PVE capture evidence. The unchecked item still
requires native maximum-response acceptance, an explicit readiness consumer and integration with the
production route selection; ordinary in-memory FileAccess reads continue through owned chunks.

- [x] Implement the private in-memory adapter over the owned snapshot/chunk download. Preserve the
      exact download outcome, expose bytes only after complete verified transfer and cleanup, and
      discard partial buffers on normal or exceptional exit. Local tests cover empty, multi-chunk,
      absent, bounded-refusal and failure paths; a synthetic chunk source exceeds one 8 MiB
      response. This does not complete public FileAccess, readiness response sizing or native
      acceptance.
- [ ] Accommodate the full bounded directory inventory in the native HTTP response reader, including
      framing and provider-envelope overhead. Keep one traversal and a finite response limit; prove
      local maximum-response acceptance and above-bound refusal, then obtain native PVE/QGA evidence
      before declaring inventory delivery accepted.
- [x] Raise the private HTTP response bound to 8 MiB and exercise an exact 4 MiB canonical inventory
      through real framing, the synthetic provider response reader, Proxmox sink delivery and the
      typed collector. Preserve refusal at the response bound plus one byte. Native PVE/QGA evidence
      remains required above; this local test is not native acceptance.

All three private review lanes accept code pin `54be0ba9` with no outstanding findings. Review
corrected a public directory-entry constructor that accepted paths outside the declared UTF-8
domain; the added invalid-path case now refuses safely while ordinary Unicode paths remain valid.
The optional redundant serialization guard and declaration-only assertions were removed. The
combined focused selection passes 188 tests. Restoring the old HTTP bound makes the maximum
inventory regression fail with observation loss, without decoded entries. The measured fixture has
4,096 entries, 4,194,304 canonical bytes and 5,651,792 provider-response bytes. These are synthetic
delivery measurements, not a native backend claim.

The final code pin passes the full local suite with 12,355 tests and 13 skips. Full Ruff
lint/format, mypy (1041 sources), file lint, locked-SDD/rulesync, typer isolation and whitespace
gates pass. Website gates pass 160 Python tests, 103 Node tests and both deterministic double-build
comparisons. No live infrastructure was exercised. This is a draft implementation increment, not a
public review/test handoff; all three authorized public feedback/fix rounds remain available.

Hosted run `35604348860` at `8aef59ae` passed every platform lane except Linux Python 3.13, where
the new file-value import-isolation test failed. Its setup imported `importlib.abc` before
installing the guard, transitively loading `pwd` and `grp` through Python 3.13's `pathlib`. The
correction uses a minimal finder without that setup dependency and continues to make the blocked
modules unavailable during the tested import. Production code is unchanged; the 35 file-value tests
pass locally on Python 3.12. Hosted Python 3.13 confirmation remains pending.

All three private lanes accept the memory-read and import-test correction at `2c3af751` without
material findings. Each passes the 73 memory-read, download and public-value tests. Fault injection
confirms that failed final byte allocation preserves the exact exception and clears the temporary
buffer; withholding partial bytes is independently mutation-tested. The updated handoff design
distinguishes cleanup responsibility from possible remote effects and leaves production and durable
recovery gates unchecked.

That pin passes the full local suite with 12,365 tests and 13 skips. Full Ruff/format, mypy (1043
sources), file lint, locked-SDD/rulesync, typer isolation and whitespace gates pass. Website gates
pass 160 Python tests, 103 Node tests and both deterministic double-build comparisons. No live
infrastructure was exercised. The draft remains in implementation, with all three public
feedback/fix rounds unused.

Hosted run `35606895496` at `368f5f0c` confirms the import-test correction on Linux Python 3.13; the
Linux 3.12 and 3.14 lanes also pass. Windows Python 3.13 instead fails during artifact-capture
cleanup with `WinError 32` on the temporary bare repository's `objects` directory. The one-byte
storage limit prevents initialization from returning successfully, so fetch is not reached. The
traceback does not identify the process holding the directory or prove that termination caused the
conflict.

The scoped CI correction checks storage after template-free Git initialization exits, while keeping
deadline and output checks active. Fetch and subsequent commands retain in-flight storage checks.
This removes size-driven interruption from the failing initialization path without retries or
suppressed cleanup errors. A synthetic slow-initializer regression proves that sequencing change,
not native Windows handle cleanup. Windows confirmation remains required; this is not a general
proof of descendant termination for interrupted Git operations.

All three private lanes accept the caller-owned borrow, recovery inventory and scoped CI correction
at `05fa75e9` without material or optional findings. Project and complexity lanes each pass 225
focused tests with 4 skips; the generic correctness lane passes 151 affected tests. A process-only
mutation restoring initialization's in-flight storage check makes its sequencing regression fail.
The combined execution/artifact selection passes 3,102 tests with 10 skips. The full local suite
passes 12,366 tests with 13 skips. Full Ruff/format, mypy (1043 sources), file lint,
locked-SDD/rulesync, typer isolation and whitespace gates pass. Website gates pass 160 Python tests,
103 Node tests and both deterministic double-build comparisons. No live infrastructure was
exercised. This remains draft implementation, not a public review or native acceptance handoff.

Hosted run `35610495863` at `8e586040` passes every required check, including Windows Python 3.13
and Linux Python 3.12/3.13/3.14. It verifies the scoped initialization change on that native Windows
run, not the identity of the earlier handle holder or freedom from every possible cleanup race.

All three private lanes accept concrete download custody and memory-read integration at `b867a572`
without outstanding findings. Review corrected loss of the original control exception when outcome
or fact allocation fails. The regression now interrupts a real download through its sink, records
the prior typed cause at allocation, and verifies that evidence outside the production exception
handler. Restoring the stale cause or recording the wrong cause makes both regression cases fail.
The focused custody/download/memory selection passes 50 tests. The earlier caller-owned-borrow
checkbox records its completed prerequisite; the later memory-custody checkbox records that
adapter's subsequent transition to `FileOperation`.

That code pin passes the full local suite with 12,378 tests and 13 skips. Full Ruff/format, mypy
(1045 sources), file lint, locked-SDD/rulesync, typer isolation and whitespace gates pass. Website
gates pass 160 Python tests, 103 Node tests and both deterministic double-build comparisons. No live
infrastructure was exercised. Hosted validation of this increment remains pending; the draft has no
review/test/merge signal and all three public feedback/fix rounds remain available.

Hosted run `35614666650` at `2f34621e` subsequently passes every required check, including Windows
Python 3.13 and Linux Python 3.12/3.13/3.14. This validates the selected workstation tests; it does
not establish native backend acceptance or complete the remaining public composition gates.

All three private lanes accept upload/JSON custody at `9d8ff26a` without outstanding findings.
Review removed a generic completion helper, duplicate source reference and redundant interior checks
while retaining the JSON child's required exceptional-fact provenance check. Negative mutations
prove that removing that child check or restoring a stale source-exception cause breaks the
corresponding regressions. The direct source cases verify their evidence outside the production
exception handler.

The final code pin passes 12,395 local tests with 13 skips. Full Ruff/format, mypy (1046 sources),
file lint, locked-SDD/rulesync, typer isolation and whitespace checks pass. One earlier local
website run failed its mouse-tap launch witness in
`test_phase4k_native_input_focus_departure_and_accessibility_contracts`. Three isolated reruns and a
complete rerun subsequently pass, with no website edits: 160 Python tests, 103 Node tests and both
deterministic double-build comparisons. The initial failure's cause is unproved; these results are
not a claimed fix. No live infrastructure was exercised. Hosted checks for the write-custody
increment remain pending; no public review/test/merge signal is raised.

Hosted run `35617821328` at `bb7ebb58` subsequently passes every required check, including Windows
Python 3.13 and Linux Python 3.12/3.13/3.14. No native backend acceptance is implied.

A read-only browser investigation at `9d8ff26a` reproduces the mouse-tap snapshot by delaying the
first animation frame by 300 ms. Native pointer events arrive and queue thrust, but the simulation's
existing 100 ms discontinuity rule discards the frame and the controller clears input before a
physics step. Four ordinary browser runs and one instrumented control pass; the delayed-frame
experiment reproduces the failure. The original failing run lacks event/frame evidence, so its cause
remains unproved. Extending the wait cannot restore an already discarded input edge. No website
source was changed: changing discontinuity behavior would require a separate behavior decision;
bounded event/queue/frame evidence is the next diagnostic step if this recurs.

All three private lanes accept the single-file custody increment at `95fb9db5` without outstanding
findings. Review removed five unused borrowed-call wrappers and redundant immediate-fact identity
checks. It also found and corrected two lower-exchange paths where failed uncertainty-fact
allocation replaced the original control exception. Negative mutations prove both the guarded
allocation and capture-before-borrow-close regressions. Project, complexity and independent
correctness selections pass 191, 164 and 203 tests respectively. The accompanying public
error-reduction design remains a proposal being implemented, not a completed public interface.

That code pin passes 12,406 local tests with 13 skips. Full Ruff/format, mypy (1047 sources), file
lint, locked-SDD/rulesync, typer isolation and whitespace checks pass. Website gates pass 160 Python
tests, 103 Node tests and both deterministic double-build comparisons. No live infrastructure was
exercised. Hosted validation remains pending, and this is still a draft implementation increment
without a public review/test/merge signal. All three authorized public feedback/fix rounds remain
available.

All three private lanes accept the durable file-call custody groundwork at `693e9e48` without
outstanding findings. Each logical file call now uses one adapter-owned lifecycle row for dispatch
admission and retained recovery facts. Initial admission reserves the maximum encoded growth of its
typed recovery state, and JSON publishes each child token and attempt on its parent row before that
child dispatches. Review corrections prevent local encoding failure, owner closure before durable
registration and oversized retained facts from stranding unrecorded custody. A typed refusal
distinguishes the proved pre-registration case from registration-started or commit-unknown failure;
the latter cases and cleanup failure retain custody conservatively. Payloads remain canonical,
bounded and free of file content, JSON content, credentials, streams and replay material.

The focused selection passes 95 tests, the combined file/operation/lifecycle selection passes 1,320
tests, and the full non-integration suite passes 12,875 tests with 21 skips. Ruff/format, mypy
across 1,090 sources, file lint, locked-SDD and Rulesync gates pass. No live infrastructure was
exercised. The broader checkbox remains open: production recovery takeover and actual process-loss
proof around before/after-commit registration, response and final handoff are not delivered by this
private checkpoint. It adds no RunContext surface, production factory or #377 hierarchy, and raises
no public review/test/merge signal.

The database takeover kernel is complete at `60a9ce8d`. One stable operation identifier retains the
ledger while a separate caller-retained generation rotates from an exact persisted predecessor and
seals the ledger atomically. Exact retry after commit without reply survives database reopen;
competing generations, fabricated predecessors, delayed repository transitions and later attempts
from an already-armed predecessor all fail stale. Recovery can rebind exact persisted obligations,
publish identity only for an effect that was already possible and resolve typed evidence. It cannot
borrow ordinary dispatch, retry registration as rebind, or admit or publish a previously registered
effect. Migration 43 rebuilds only the changed owner table and proves existing obligation payloads,
revisions, timestamps, foreign keys and cascades survive.

The subsequent pre-release schema consolidation supersedes that implementation detail: the shipping
sequence is migrations 39 (owners, claims and obligations), 40 (managed runs) and 41 (nullable VM
instance marker). No released database contains the former 39-44 sequence, so the consolidation does
not add an upgrade path or change the recovery contract described above.

The round-7 branch-specific schema guard was later superseded by the 2026-10-05 operator ruling:
remove that special guard without adding unconditional canonical-schema validation on ordinary open.
Retain known development-only databases pending separate deletion approval. The existing
released-v38 migration and interrupted-migration recovery remain required and separately tested.

Final project and correctness re-reviews are clean at `f7a2ecac`; the final complexity review's two
material simplifications are incorporated, and its optional duplicate wrapper check is removed at
`60a9ce8d`. The combined execution/database selection passes 2,947 tests with 14 skips. After that
last deletion, 251 focused tests and the full non-integration suite pass 12,892 tests with 21 skips.
Ruff, formatting, mypy across 1,090 source files, file lint, locked-SDD and Rulesync drift checks
pass. This closes only the durable database-generation portion of the open recovery checkbox above.
It does not prove predecessor dispatch drain, remote quiescence, process-loss adapter recovery,
production recovery factories, operation-root composition or RunContext delivery. The next private
vertical remains DOWNLOAD snapshot recovery behind an explicit adapter-owned drain fence; no test
binding may be presented as SSH, QGA or native production evidence.

### Buffered execution result checkpoint

- [x] Implement safe immutable application result values, honest wait/exit/signal precision and one
      checked-result error. Keep deadline expiry and owned cleanup independent of the primary
      failure; distinguish captured, delivered, discarded and suppressed output. Result values must
      not infer completion from raw carrier status.
- [x] Add the safe checked-error composition seam: core supplies logical target identity to the
      contextual checker, which derives closed execution phase/reason facts and preserves the exact
      immutable result in `CheckedExecutionError` and its standard `ErrorDetails`. The private
      inline checked reducer reduces once, returns success or raises that bound error, and may pass
      trusted helper observation phase only where the result phase is unknown. Result-only
      projection refuses to reconstruct discarded phase evidence, while known nonzero application
      status is application-status evidence. This adds no target, accessor or RunContext exposure.
- [ ] Bind that seam during complete target/reducer composition, as required by FRD R5. The private
      checker does not itself deliver the complete public error contract; that remains required
      before public ExecutionAccess and RunContext delivery.
- [x] Preserve a valid helper terminal transcript when only later carrier observation is lost, while
      retaining the carrier error. Missing terminal, wire corruption and post-terminal records must
      still prevent trusted terminal evidence.
- [x] Implement and verify the selected retrospective normal-completion producer/reducer.
      Eligibility is inline-only CPython 3.11 through 3.14, the range covered by the source and
      mechanism audit; the shared file-helper prerequisite remains Python 3.11 or newer. Outside the
      selected range, inline execution refuses before `LAUNCHING` rather than inventing stronger
      evidence. Eager start, signaled-entry ambiguity and native acceptance remain separate gates.
- [x] Bind the existing owner, inline producer and contextual reducer behind one private
      `ExecutionAccess.run` increment. Define the intended caller values for explicit protection,
      lifetime, finite input and bounded capture/discard now, while supporting only DIRECT plus
      OPERATION on the Linux inline helper. Refuse MANAGED, INDEPENDENT, non-Linux runtime,
      unsupported startup, unavailable elevation, invalid values and expired deadlines before owner
      custody or dispatch. Keep this class internal and unexported; it neither completes the
      unchecked target/reducer checkbox above nor permits a run-only public RunContext target.
- [ ] Remove the clean inline-call ledger-capacity limit before complete RunContext adoption. Use
      one exact execution-lifetime row with fresh serial borrows, explicit installation and arming,
      retained-effect handoff, and an admission-safe final resolution of only that row. Preserve
      bounds, exact reply-loss retry, unknown custody and takeover fencing; never reopen resolved
      rows or replay helpers. Demonstrate more than 128 actual clean commands/scripts with mixed
      file operations and shared ordinary/elevated views, validation refusal, NOT_SENT, nonzero
      application status, unknown/control interruption, lost registration/arming/handoff/resolution
      replies, unused/duplicate finish, post-finish refusal and concurrent finish. Integrate final
      resolution into outer native teardown before aggregate cleanup. Independently reviewed SQLite
      probes show the existing APIs can compose this shape, not implementation or native acceptance.

All three private lanes accept code pin `8ceb899a` with no material findings. Review corrected
acceptance of output/status dataclass extensions that added diagnostic fields to default result
representations, and nonempty byte subclasses that bypassed non-capture output validation. Three
exact child-type checks close those boundaries without a new abstraction. All 68 focused
result/observer tests pass. Removing context suppression exposes the synthetic caller-exception
canary; reverting terminal preservation breaks its regression test. The optional removal of the
explicit completed state check in `ok` was declined to keep the public success predicate locally
readable.

The final full local suite passes 12,291 tests with 13 skips. Ruff/format, full mypy (1037 sources),
file lint, locked-SDD/rulesync, typer isolation and whitespace checks pass. Website gates pass 160
Python tests, 103 Node tests and both deterministic double-build comparisons. No live infrastructure
was exercised. Production integration remains open: safe target/phase metadata, native evidence and
the complete public target/RunContext surface are not delivered by this checkpoint.

The retrospective producer/reducer gate is subsequently complete at `c161b431`. The helper admits
only Linux CPython 3.11 through 3.14 before launch, emits nonce-bound exact-child normal wait
evidence, and the reducer accepts completion only after trusted terminal framing and settled
custody. Private project and complexity review corrections keep unfinished archives payload-free
without weakening the immediate caller result. The independent correctness lane passes 365 focused
tests plus real `ExecutionOperation` to local-carrier probes for success, exit 255, signal,
sensitive-output suppression and overflow. Hosted Linux 3.12 through 3.14, Windows 3.13, static,
documentation and website checks pass. This evidence does not close eager-start, signaled-entry,
native-platform or public-composition gates.

The private DIRECT access checkpoint composes those mechanics without exposing RunContext. Local
tests exercise literal command and explicit-script execution, binary input, exit 0 and 255,
discard/suppression, checked diagnostics, exact deadline propagation, pre-dispatch refusals,
uncertain owner retention and fresh-process retirement-module independence. Preparation now occurs
once before borrowing the owner, so invalid requests do not transiently acquire child custody. This
is not MANAGED execution, a job API, live/terminal I/O, native-platform acceptance or complete
target composition. Its 4,096-byte per-stream capture default and maximum are private inline
checkpoint limits, not the proposed production 1 MiB per-stream default or bounded output spooling.

## 5. Add the complete new RunContext surface

The [2026-09-19 ruling](frd.md#operator-rulings-2026-09-19) authorizes three delivery stages, not
one all-callers cutover. PR #830 publishes this design only. The following implementation PR adds
the new surface; migration and removal follow in their own PRs. Permissions are groundwork until
removal: do not enforce new recipient grants or the successor core file ceiling, or rely on their
isolation, in coexistence releases. Operational safety and selected profile guarantees still apply.

The first checkbox below describes this section's delivery outcome, not the next construction step.
Complete target identity, execution/jobs, FileAccess, platform composition and whole-workflow
validation first through private composition seams. Only then expose the two passive RunContext
accessors as one complete additive surface. An accessor-only or run-only target is not an acceptable
intermediate public API.

- [ ] Add `admin_execution_target()` and `agent_execution_target()` to the existing RunContext,
      returning the new target without changing legacy accessors or callers. Use permanent names, no
      union target type, stack selector or forwarding adapter. Prove passive construction/access and
      composition-owned lifetime, no new effects on existing callers, and independent new-stack
      usability with legacy modules unavailable. Do not claim restricted recipient authority.
- [ ] Add platform-owned new-stack connection/provisioning composition without decoding opaque VM
      metadata in core or constructing legacy targets. Apply the
      [2026-09-21 factory inventory](migration-strategy.md#new-target-composition-inventory-2026-09-21):
      explicit carrier delivery identity, Proxmox CA-bundle/trust migration and import independence,
      WSL2 distribution/user binding, independent Lima delivery and explicit SSH placement-host
      endpoint/account/trust/OS facts. Preserve unchanged legacy hooks during coexistence; new
      factories cannot obtain their inputs through those hooks.
- [ ] Implement and prove the first independent native-binding hooks for Proxmox and WSL2 under the
      [core binding contract](execution-contract.md#core-native-binding). Preserve passive
      construction, actual delivery-account facts, explicit runtime selection and old-hook behavior.
      Proxmox uses the configured CA for both platform API and new QGA delivery, without accepting
      legacy verification bypass. Other platforms, provisioning-result integration and complete
      target identity composition remain part of the required gate above.
- [ ] Expose native binding resolution as an explicit preparation operation with a deadline, before
      passive target/RunContext construction. The remaining-platform inventory shows that cloud
      public-IP resolution requires provider reads; do not force stale metadata or hide those reads
      behind an accessor. Keep Proxmox/WSL2 resolution passive, keep route lifetime in core, and use
      already-observed create-time endpoint facts for the new provisioning result. Retire the
      initial private hook name without a compatibility alias before consumer adoption.
- [x] Rename the private native hook to `resolve_native_execution_binding` with a required deadline,
      retaining passive Proxmox/WSL2 resolution and unchanged legacy callers. Update focused tests
      and permanent developer teaching without adding an alias or claiming cloud/provider and
      create-time adoption. All three private lanes accept this correction at `4a53c39a`; the
      broader native composition gate above remains open.
- [ ] Prove fresh-process platform composition without retirement dependencies, not only calls to
      already-imported native hooks. The first-binding audit at `6149cf06` found that VM-platform
      package initialization imports Lima's legacy dependencies, while plugin initialization imports
      providers that still load legacy transports. Keep this initializer work with the complete
      platform/factory composition gate; concrete hook independence does not complete it. The
      [full registration audit](migration-strategy.md#full-registration-dependency-audit-2026-10-06)
      adds harness file/SSH import edges; include those in the fresh-process proof without routing
      any new workflow through old operations.

The private WSL2 import checkpoint at `acc1fdec` moves concrete built-ins into one explicit registry
and loads the legacy SSH error type only when command checks run. A fresh subprocess blocks the six
retirement roots before importing, constructs an ordinary WSL2 platform and resolves its recorded
distribution/user native binding. Both plugin-first and registry-first processes share the same
descriptor and registration dictionary; historical migration mapping stays fixed. The affected local
suite passed 1,041 tests, and private project, complexity and generic correctness reviews found no
material findings. This proves only the WSL2 construction/binding path on the local host;
plugin-package independence, complete platform composition and native Windows acceptance keep the
checkbox open.

Private registration worker `b8e65bc37` now localizes eager retired imports to the existing old
operations in nine platform/Tailscale/harness modules, without changing registry design or old
operation behavior. Plugin-first and registry-first fresh subprocesses block all six retirement
roots before actual shipped registration and passive Proxmox/WSL2 construction/binding, while
forbidding process/network effects. Existing provider/Lima tests patch the canonical old dependency
instead of its relocated incidental module global; all behavior assertions remain. The worker passes
2,317 focused/adjacent cases with four skips, 68 relocated-patch cases, both Windows-selected
fresh-process cases on Linux, 13-file typing/style, file quality and whitespace checks. Final
whole-unit reviews and complete lead gates at `7145e4c2a`, recorded above, include this registration
work. Native Windows, other-platform binding/trust/provisioning, actual new-stack workflows and the
complete additive RunContext remain open, so the broader factory checkbox is not completed.

- [ ] Validate complete provisioning, native recovery without Tailscale, plugin operations, files,
      backup, host provisioning/rollback and interactive attachment through the new surface. Cover
      required operations, optional refusal, sensitivity and supported workstation/platform
      versions. Resolve trust-state writer ownership before new production use. Missing evidence
      requires operator disposition, never a passing claim.

## 6. Migrate consumers in owned workflow batches

- [ ] Complete the
      [incident-derived behavior inventory](migration-strategy.md#incident-derived-behavior-inventory)
      before deleting legacy code/tests. Each entry records its old source/test, new owner and
      replacement regression, required workstation/platform evidence, and explicit disposition. ADR
      0020, Windows stdin conversion and Git-for-Windows toolchain assumptions are seed cases, not
      an exhaustive inventory or a claim of new-stack validation.
- [ ] Assign non-overlapping migration PRs for harness/artifacts, sessions/consoles and platform/CLI
      consumers as needed. Each batch records legacy call sites, permanent new accessors, owner,
      deliberate operation choices, intended grants/paths, regression/live evidence and surviving
      state disposition. Prove complete production workflows per batch, with no fallback or
      duplicate mutation. Keep unmigrated callers unchanged; freeze new legacy use.
- [ ] Complete the [migration inventory and cutover gates](migration-strategy.md): retain #789's
      historical recovery intent without integrating its closed branch, audit caller
      shells/identity/I/O/lifetimes/grants and approved filesystem destinations, migrate
      file-provisioning shell snippets to FileAccess where it expresses the operation, resolve
      surviving jobs, plugin compatibility, state migration and rollback. Every old entry point has
      a destination and removal point.
- [ ] Replace `NativeFiles`, `files.runner`, exposed staging slots and raw setup/readiness runners
      with RunContext access. Preserve artifact ownership/checkpoints, JSON/TOML settings behavior,
      generated-section surroundings/metadata, session/run identities and restart confirmation.
      Validate native inventory and identity discovery without caller exec in file-only operations.
      Keep genuinely executable harness CLI work behind command access, not a disguised file API.
- [ ] Migrate sessions and other jobs to the same supervisor. Preserve session UUID/run IDs,
      tmux/harness readiness, restart consent, legacy-run uncertainty and owned cleanup. Do not
      certify legacy detached descendants by moving only a surviving parent into a new cgroup.

- [ ] During sessions/console migration, replace the admin-owned multi-console agent-pane sudo path
      with an explicitly bound and proved cross-user execution path. The current three-plan identity
      composer does not support non-root delivery into a different non-root workload identity. Do
      not hide this gap with ambient sudo or expose native agent authority merely to make this
      consumer fit.

## 7. Remove legacy and activate permissions

- [ ] Require zero remaining legacy consumers, including direct/lazy imports and external plugin
      entry points under the reviewed compatibility policy. Complete the behavior inventory and
      surviving-job/state disposition before deleting their readers. Transport owns this final PR.
- [ ] Physically delete legacy accessors, factories, the retirement packages including
      `agentworks.native_files`, and temporary scaffolding. Prove installed-package startup and
      complete production workflows without them. Retain operator trust/configuration and cleanup
      evidence; replacing code never authorizes deleting that state.
- [ ] Activate explicit core recipient grants and the successor file allowlist only with legacy
      absent. Inventory all approved workflow actions/paths before activation, then prove restricted
      file-only, observe-only, elevation and exact-profile views, denial before effects and no
      indirect bypass. Keep operational failure, channel support and permission denial distinct.
      This is not registration/consent or an in-process plugin sandbox. If either removal or
      enforcement evidence fails, do not claim permission isolation or close the effort.

Each stage is independently green and updates permanent collateral to the behavior it actually
ships. Temporary coexistence has an explicit final removal owner and gate, not indefinite support
for two APIs. No checkbox above claims that the separately owned SSH work is done.

Future implementation satisfies FRD R1-R11 and promotes implemented contracts into permanent docs
with the code that makes them true. Closeout requires evidence-backed validation, complete
retirement and a truthful final plan before creating `locked.md`.
