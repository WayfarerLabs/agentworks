# SSH Transport Composition Evidence

Historical and current integration observations are scoped to their exact source pins. This record
does not replace the SSH plan, shared transport contracts or full implementation acceptance.

Earlier records retain their original observations. New entries supersede only the gates they
actually measure.

## Deferred native terminal fixture uses the actual carrier

Fixture increment `37c7840c28386d7d26e3ba59202318299c5994c5` starts from published SSH
`62c1e432bea2cf5d925f761722b2af67e1b9ca4e`. Its three changed files are SSH test fixtures and their
README; production, shared transport, workflows and contracts are unchanged. The existing two
explicitly selected Linux integration cases now call actual `SSHCarrier.execute` with the original
prepared invocation, operation deadline and delivery custody. They observe the launched client argv
independently of terminal selection, require `-tt`, and exclude literal payload canaries. The
positive guest exits 37; the assertions require SENT, `ExitStatus(code=37)`, local status 37 and no
failure. Missing runtime must still withhold payload, keys and application entry; a normal selector
exit is not application execution, and unobserved completion stays UNKNOWN.

The fixture previously deleted its directory even after failure or uncertain guest absence. It now
retains failed or uncertain directories, including private fixture keys and diagnostics, for
separately authorized inspection and cleanup. Successful deletion follows the existing positive
custody, client, descriptor, server and applicable guest checks. No new cleanup owner, signal or
retry is added. Two controlled entry-failure cases require the original exception identity and
retained binary evidence while forbidding subprocess and PTY admission. They prove that boundary,
not late native cleanup or every interruption point.

The developer's own frozen environment passes both controlled cases in 0.05 seconds and collects
both native cases in 0.04 seconds without executing them. Root independently passes the controlled
cases in 0.04 seconds and collects the native cases in 0.03 seconds; same-process receipts verify
all 166 and 164 first-party origins respectively belong to root's composition checkout. Full Ruff,
formatting of 1,600 files, typing of 1,562 sources and pinned README checks pass. The developer's
initial empty Markdown-library invocation is invalid coverage; the corrected actual CLI checks one
file with zero errors. Original failed evidence remains retained.

All three independent source reviews find no material defect, and root fully reads their signed
reports. Complexity independently passes the two controlled cases, then removes only the retention
guard in memory: both cases fail at retained-file reads after preserving the original exception. Its
initial scratch compiler error occurs before pytest and supplies no negative proof; the actual two
failure traces and corrected driver remain preserved and are read by root.

No native campaign runs in this increment. This bed's earlier owned sshd attempt failed remote PTY
ownership with GID 5 and error 22 (invalid argument); its current group map still exposes only one
mapped group. No account, group, namespace, service or permission workaround is attempted. A
suitable bed and separately authorized startup-isolation/cleanup charter remain necessary.
Collection and controlled cases do not prove native exit 37, resize, runtime refusal or complete
lifetime/restoration behavior. macOS, Windows owner/input delivery, shared sanitation, production
factories, both usable RunContext paths and full R1-R5 acceptance stay open. No completed plan
block, lock, feedback round or review signal changes.

## Complete hosted result at published POSIX head

[CI run 39333022476](https://github.com/WayfarerLabs/agentworks/actions/runs/39333022476) tests SSH
`62c1e432` through actual merge `8f87f07b59a5d48cf89c20968974efc589f38f82` into transport
`284c128d`. Root reads originating checkout, interpreter, command and results for all four Python
jobs, plus every complete failure trace. Each Linux selection has one failure, 21,994 passes, 310
skips and 51 warnings: Python 3.12.3 takes 604.96 seconds, 3.13.16 takes 506.80 seconds and 3.14.8
takes 505.69 seconds. All fail only the previously routed shared
`test_buffered_ssh_refuses_terminal_before_connection_access` assertion that terminal capability is
false on POSIX, before connection or native access. The shared fixture remains unchanged by SSH.
These hosted selections do not explain or waive the 22 local host-fixture failures below.

Windows Server 2025, Python 3.13.16 passes 2,195 cases, skips 80 and reports 25 warnings in 1,482.17
seconds with the exact `windows and not integration` selection. This is that pin's hosted result,
not native terminal acceptance or an attribution of earlier managed-start failures. Static Python,
file quality, locked-SDD and Rulesync jobs pass. Website fails one of 160 Python tests in 92.863
seconds: Chromium never exposes its DevTools endpoint before page assertions; subsequent Node and
build steps are not reached. Root reads the complete failure, original command and fixture source;
website/workflow inputs equal protected main. The cause remains unknown. No retry, timeout change or
unrelated Website fix is applied. The complete run concludes failure; source reviews and other green
results do not make it green.

## Current POSIX terminal leaf composition

Working join `5c15f9fa1939ff542c172e95d0f915769aa440cc` combines transport
`1936202d18341edc5dcc3310a886f15a2df40393` with SSH port `09e6a56f809cf9ff588ef4c7fef3977ccb90c438`.
The owner correction changes only the stale manifest-reference field expectation and removes its
adjacent authored-summary wording assertion. Original failures remain preserved. This correction
does not clear the other host-fixture failures.

The port changes exactly eight SSH-owned files. Its six Python diffs match the previously reviewed
POSIX unit; README wording reflects the current working candidate. It accepts TerminalInput
passively on POSIX, advertises that internal capability, selects the existing terminal argv and
lazily routes to the retained relay. It changes no shared type, owner, helper, readiness parser or
presentation policy. Current enrollment-key, causal-custody and drain fixtures remain intact.

The developer's own frozen environment reports 656 passes, eleven expected Windows-host skips, 40
authored integration cases excluded and one failure in 40.56 seconds. All sixteen synthetic
composition cases pass, using actual shared preparation with an owned synthetic client rather than
installed SSH. Scoped style/typing and both pinned README checks pass. Root fully reads the complete
patch, developer handoff, original failure and selection/environment/cleanup receipts. Independent
review of evaluated pin `24674afe09a9487e42bbd661386ee91b594fa8f4` finds no material defect in all
three lanes; root fully reads their complete signed reports. The project's own frozen environment
reports 101 passes, two expected Windows skips, four excluded integration cases and the same shared
failure in 8.81 seconds. Complexity reports 37 passes and that failure; forcing terminal argv off in
memory makes the real-preparation composition fail. These are synthetic checks, not installed SSH or
native workstation acceptance.

The complete local selection at that pin returns exit one: 23 failures, 22,206 passes, 78 skips and
51 warnings in 412.99 seconds. Twenty-two failures remain in host-fixture families; the other is the
shared terminal-capability assertion below. The corrected manifest-reference case now passes. Root
reads every failure's assertion/error record and the complete shared refusal trace, retaining the
original full output and failed fixture views. The observed host failures do not establish one cause
for all cases. No test is skipped, overridden or retried to obtain green results.

All fifteen static/website entries pass, including strict typing across 1,561 sources, 160 Python
and 103 Node tests, four builds and two deterministic comparisons. Canonical file quality passes
after correcting one spelling issue in this record and formatting the updated progress documents.
The original failed checks remain retained. No production or test source changes after the full run.

The open shared test `test_buffered_ssh_refuses_terminal_before_connection_access` still assumes
terminal capability is false on every platform. The
[complete failure and owner consideration](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6278181267)
record the required capability-qualified refusal without changing or skipping that shared test. This
evaluation precedes the candidate's publication; public SSH was at D7048. Internal POSIX enablement
is real, but it supplies no native macOS/Windows proof, complete Linux fault/lifetime acceptance,
shared sanitation, public RunContext composition or full R1-R5 acceptance. Windows terminal
implementation remains required. No completed SSH plan block, review signal, public round, native
admission or lock changes.

## Configured SSH platform-host declaration composition

Private join `11fcf786a6fbbefe24196cecb2baa62b02b17474` incorporates transport
`224aca065eb0ade807ba686b7563bffc4d0f02e7`. Production CLI and the transport SDD match the owner;
only the three prior SSH fixtures differ in CLI files. Shared carrier interfaces and public SSH
delivery modes are unchanged. All three independent source reviews find no material defect; root
fully reads their complete signed reports. This is source clearance, not validation clearance.

Evaluated runtime/test pin `38a6cf35eb6b60012528403a6bc07c01a907dab1` returns exit one in the
complete local selection: 23 failures, 22,190 passes, 78 skips and 51 warnings in 408.45 seconds.
Twenty-two failures belong to host-fixture families, including the new real local-helper case at
preliminary command preparation before file transfer. Root reads all 23 assertion/error records and
both new complete traces; original failed views and full output remain retained. The prior decoded
identity/mismatch and differing NSS/inherited groups support a bed constraint without proving every
case's cause. No override, skip or retry-to-green is applied.

The other failure is the stale manifest-reference expectation at
`tests/manifests/test_reference.py:668`: it expects only `mode` and `host`, while the new SSH
placement correctly exposes `access` too. This source matches the owner and the required update
belongs there. The
[complete additional failure and reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6278021594)
are public; SSH changes no transport test or renderer here.

The project lane's own frozen environment passes 61 synthetic/configuration/fault cases,
deliberately deselecting only the actual local-helper case. Complexity's own environment reports 63
passes and one preliminary-command failure, with both scoped clock variants and the original
budget/expiry checks passing. Generic correctness is source-only. These selections do not replace
the red full suite. All 15 static/website entries pass: Ruff/format, strict typing across 1,560
sources, canonical file quality, Rulesync, locked-SDD, exact CI Typer isolation, 160 Python and 103
Node tests, four builds and two deterministic comparisons.

A separate fresh-process passive check installs the existing six retirement-root blockers before any
first-party import, imports the new host module and constructs only its explicit access model. It
exits zero with 235 verified own-checkout module origins and no forbidden modules loaded. The
context is never entered, so this proves no database, helper, SSH or native workflow. Its exact
script and receipt remain retained separately from the test counts.

The optional complete Lima placement access declaration supplies a literal endpoint, login,
Linux/Darwin runtime and explicit shared host coordination key. It reuses existing operator SSH
policy and the ordinary host context. The key coordinates declared resources without proving
physical identity. Legacy Lima destinations, readiness and production methods remain unchanged;
declaration presence selects neither a stack nor fallback. Core supplies the required trusted file
root, preserving existing descendant mount confinement. Actual Remote Lima adoption, provisioning,
rollback, native SSH/macOS and both usable production RunContext paths remain open.

The owner reports 168 host/file/VM-context cases, 129 disposal/read cases and strict typing across
1,560 sources, with all three source lanes and both final-document lanes clear. These are attributed
owner results, not this composition's validation. Its real ordinary local-helper case exercises
command and file content beneath the selected root with synthetic SSH delivery. The current bed's
previously observed identity/group mismatch remains a validation constraint, without an override.

The isolated fault-fixture correction at `5978c6d4f` fixes monotonic observation only inside the two
binding-reinstall cases. Assertions, shared fixtures, markers and production deadlines remain. The
owner's controlled 5.25-second comparison establishes sensitivity to elapsed fixture time, not the
exact original hosted delay. Original D9 failures remain in the following record; fresh hosted
verification of this correction is separate.

Public SSH head `d7048eaf` finishes CI run `39327127783` with failure through actual merge
`e081257528a3070ed26b4a56961272c4844f3f73` into D9. All three Linux jobs pass 21,919 tests each,
with 310 skips and 51 warnings, in 879.44/790.55/731.70 seconds for Python 3.12/3.13/3.14. Windows
Python 3.13.16 reports one failure, 2,194 passes, 80 skips and 25 warnings in 1,227.84 seconds:
`test_checked_managed_nonzero_retains_exact_contextual_result[run]` gets an unacknowledged managed
start before its intended checked-result assertion. The retained control fact preserves the
originating operation. Its exact cause remains unproved and a source/log diagnostic is separate; it
is not the previous keeper-drain or disposal fixture.

The Website Python step fails one of 160 tests during Chromium DevTools endpoint discovery, before
page assertions; later Node/build steps are not reached. Website/workflow bytes match protected
main, without establishing the startup failure's cause. The
[complete original failure](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6277709046)
is retained. No rerun, timeout change or unrelated website fix is made here; earlier local passes do
not clear it. Root reads all originating checkout/interpreter/results and every complete failed
trace for this run.

Owner run `39327927020`, actual merge `18a99f19f9d0997ebeb3fe2c89cc21539fe43693`, independently
reports the same stale manifest-reference failure on Linux Python 3.13.16: one failure, 21,978
passes, 310 skips and 51 warnings in 762.38 seconds. Root reads its originating
source/interpreter/result and complete failure trace. Its other Linux jobs also fail only that
expectation; Windows independently passes 2,195 tests, with 80 skips and 25 warnings in 1,049.85
seconds. Root reads all four originating checkout/interpreter/results and all three Linux failures.
This red owner run neither accepts the SSH joint head nor explains D7048's distinct Windows failure.

Newer owner run `39329774986` tests `1936202d` through actual merge
`4a516b45579d6331142c18da7f724af1004c4c1b`. Linux Python 3.12/3.13/3.14 each pass 21,979 tests, with
310 skips and 51 warnings, in 863.40/777.13/494.09 seconds. Root reads their originating
checkout/interpreter/command/results. Windows Python 3.13.16 passes 2,195 tests, with 80 skips and
25 warnings in 1,079.89 seconds. Root reads its originating checkout/interpreter/command/result; the
run finishes green. These owner results do not replace the red current SSH composition or supply
native terminal evidence.

The SSH-owned Windows keyboard decision document at `3ccc0404` has project and complexity reviews
clear, including the accepted units-versus-bytes clarification. Root fully reads both complete
reviews, both final-pin attestations and the author's correction/quality evidence. It maps existing
comparison cells to unresolved choices and preserves required physical-key/relay proof. It selects
no decoder, production owner or terminal activation. This private batch adds no completed SSH plan
block, native probe admission, review signal, public feedback round or implementation acceptance.

## Ordinary host context and completed-file bookkeeping

Private runtime/test composition `b595937509014eb1e91f631761c98d9f96e384c5` incorporates published
transport `d9cc9d88243f57417443e64fa80f8dfe306edfb2`, including its preceding host-context increment
`1eed4f5c7`. Production CLI and the complete transport SDD match the owner. Only the three prior SSH
fixtures differ in CLI files; no shared carrier or SSH public delivery-mode change is made. All
three source reviews find no material defect; this is not validation clearance.

The complete local selection at evaluated runtime/test pin
`c7436cc9db1329c5c71e8c7624f456e810fdd3a6` returns exit one: 22,132 passes, 21 failures, 78 skips
and 51 warnings in 414.19 seconds. All failures belong to the new host fixture families. Sixteen
completed-call cases stop at the preliminary command, two identity assertions show different groups,
the JSON-debt case gets file refusal, and two upload cases stop before successful completion or find
the expected published file absent. These do not exercise their intended positive retry branches.
Original failed views, every failure's assertion/error lines and the full log remain retained.

An independently owned project-review environment reports 13 passes and 17 failures in its focused
selection. One additional diagnostic remains failed at the same preliminary command, with an actual
decoded FailureFact of phase identity and code mismatch. Its requested UID/GID are 1021/1041 and its
NSS-derived groups differ from the parent's inherited groups `{1041,65534}`. The parent UID/GID
match, and the helper requires exact kernel identity. This supports the group-mismatch explanation
without directly observing guest kernel metadata or establishing every failing case's cause. No
account/group change, identity override, substitute helper, skip or retry-to-green is applied.

Complexity's own selections report 30 passes and 21 host-related failures; all twelve simulated
native/canonical VM closeout cases pass. Replacing only the completed-file bookkeeping method with
an in-memory no-op makes both otherwise-passing VM stat cases fail during original cleanup retry:
the retained file borrow blocks dependent execution-lifetime resolution. Root fully reads all three
signed source reviews, the diagnostic driver/result and the complete deletion-control output. The
generic structured name `selected-host` is an optional diagnostic-context question for the owner,
not a custody or scope defect. Prior D1 is taken by the owner, as described below.

Ruff/format, strict typing across 1,557 sources, Rulesync, locked-SDD and exact CI Typer isolation
pass. Website checks pass 160 Python and 103 Node tests, four builds and both deterministic
comparisons. Initial canonical file quality fails only the SSH plan's Markdown reference wrap;
Prettier and spelling pass. The documentation-only correction passes repeated canonical file
quality; the original failed views remain. Neither this correction nor source review makes the local
suite green. Fresh joint-head verification and applicable positive host coverage remain separate
gates.

The private ordinary platform-host context claims its explicit PLATFORM_HOST scope before actual
account discovery. It composes existing command/file views with selected accounts, elevation,
runtime, trusted root and deadline, preserving incoming RunContext views. Retained partial
construction and original control carry same-process cleanup authority. It does not enable
configured host adoption, Remote Lima, host MANAGED execution or automatic host recovery.

Host and VM closeout now settle completed file bookkeeping before resolving dependent execution
lifetime debt, after actual helper/delivery closure. The retry uses each original captured outcome
and borrow. Missing outcomes, uncertain effects, outstanding attempts and JSON publication debt
remain held. It neither recaptures outcomes nor republishes debt, replays helpers, reruns transforms
or repeats file mutations. An already-committed borrow release can complete teardown while
preserving the original exception and cause. The owner also removes the optional test-only capture
decoder identified by the preceding complexity review; production encoding and exact retained
payload comparison remain.

The original Windows run at owner `1eed4f5c7`, CI merge `7d3001a258f019ae18ecf90b9e5d30281a51be4a`,
reports 2,195 passes, 80 skips, 25 warnings and four collection errors in 1,137.63 seconds.
Linux-only fixture globals construct a Linux runtime from a Windows interpreter path before the skip
marker applies. Production POSIX-path validation correctly refuses. The correction moves runtime and
Unix identity discovery into Linux-only execution. Owner run `39323682634`, actual CI merge
`9ec49099344e191ce23dbfd3cba8971e5893a393` into base `cea5e8523`, has no collection errors. Linux
3.12/3.13/3.14 each report 21,919 passes, 310 skips and 51 warnings in 871.07, 775.41 and 771.76
seconds respectively. These are owner-head results, not fresh SSH joint-head verification.

That owner run still fails on Windows 3.13.16: two failures, 2,193 passes, 80 skips and 25 warnings
in 1,135.08 seconds. Both parameters of
`test_prior_action_binding_reinstall_rechecks_takeover_and_closing_before_local_handoff` fail in
`lose_response`, before the intended binding-reinstall branches. The expected unknown disposal is
actually `disposed=False`, failure DEADLINE, with `deadline_exceeded=True`. The exact time-consuming
phase is unobserved. Original checkout/interpreter/result portions and both complete failure traces
are retained; no rerun, timeout extension or owner-source correction is made by SSH.

That failure is distinct from SSH `d6268311`, CI merge `9c7020c10b25ad43258ff393f43f7cd8b1ea8887`
into owner `ff19ce4e0`: its Windows run reports one failure, 2,194 passes, 80 skips and 25 warnings
in 1,270.04 seconds. The first managed start in
`test_all_keeper_drains_attempted_before_any_guarded_finish[True]` fails before the intended cleanup
assertions. The retained log does not expose which clean-start predicate failed. The independent
source/log diagnostic confirms that fixture setup precedes the fresh five-second start budget and
that cached bundle-prefix access does not rebuild bundles. Its exact cause remains unattributed; the
separate green owner run does not refute it. Original logs and the complete signed diagnostic are
[preserved publicly](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6276864836).

An owned Linux SSH host-context probe is prepared but explicitly disabled. It uses the actual
returned RunContext target, ordinary account/runtime preparation, one DIRECT command, stat and a
bounded snapshot read. Four synthetic environment fields cover issue #845's percent-encoded JSON,
literal percent tokens, quotes, backslashes, Unicode, multiline controls and an empty value. The
same command echoes exact framed values plus 512 binary input bytes, for 794 planned stdout bytes
under the unchanged 4,096-byte limit. This is source/data preparation, not a native result or the
full fourteen-case/client/platform matrix.

The reviewed driver has SHA256 `cf2636e1598ec1461c741dcb904ca490e654ef8e8ff652e9f9ad4e37d25a43f4`;
its complete signed effect map is
`1b91ed4640fafc64ed6c945b55ae5af60e7c62ba039a673d68e805e865c41781`. Root fully reads the original
driver/map and exact environment-only refinement, compiles without invocation and independently
checks synthetic framing. Earlier reviewed versions remain byte-identical in retained evidence. The
real NSS/process-group mismatch may cause a correct helper refusal on this bed; no identity
override, fallback or acceptance claim follows. A corrected source pin, quality clearance and final
driver admission remain prerequisites. No native/helper/database effects have occurred.

All completed SSH plan blocks and the migration/retirement sequence remain unchanged. Genuine
creation/trust publication, writer coexistence/rollback, configured factories, both usable
RunContext paths, terminal activation and R1-R5 acceptance remain open. This private composition
starts no new feedback round, ready/review-requested signal, SDD lock or retirement step.

## Finite DIRECT capture and ordinary host-file composition

Private runtime/test composition `65d1987c5b2a89def8941a6bd304e2d676c0cdfb` incorporates published
transport `ff19ce4e05616bddf4cce8a04eaba65451c15dac` without conflicts. Production CLI and the
complete transport SDD match that owner. Only the reviewed SSH drain, generated-key permission and
causal terminal-custody fixtures differ in CLI files; no SSH carrier interface changes.

The transport increment adds finite Linux VM DIRECT capture beyond 4,096 bytes per stream through
owned verified scratch. Actual wait/status remains separate from output publication and artifact
cleanup debt. Ordinary host files bind the exact PLATFORM_HOST operation scope with version-7
custody, rather than a fabricated VM identity. Automatic host file recovery remains refused. Fresh
process fixtures block retirement modules before actual shipped binding registration/resolution;
provider and tool observations remain synthetic. Full host-context composition and Remote Lima
adoption, provisioning and rollback are still open.

The foreground-budget fixture now fixes its test-local monotonic observation, as its neighboring
composition fixture already does. Requested three/five-second values, exact shared deadline
identity, policy-selection count and cleanup/expiry assertions remain. This separates scripted
budget-sharing proof from host scheduling; it neither changes product deadlines nor identifies the
exact older hosted delay. The original red run and its measurements remain in the historical record.

Evaluated runtime/test pin `48c5a9e643246364c4fcd9a8d382d3e66249981a` passes the complete local
selection: 22,085 tests, 78 skips and 51 fork warnings in 398.23 seconds, exit zero. Ruff/format,
strict typing across 1,554 sources, canonical file quality, Rulesync, locked-SDD and exact CI Typer
isolation pass. Website checks pass 160 Python and 103 Node tests, four builds and both
deterministic comparisons. Production CLI, the complete transport SDD and all 25 prior completed SSH
plan blocks retain their independently checked boundaries.

All three independent source reviews have no material findings and are fully read. Complexity's
optional D1 identifies the stored-capture decoder's current test-only callers. The lead verifies
that production compares retained encoded payload directly; this simplification remains with
transport, without modifying owner source or blocking the bounded adoption. Removing only the
seven-line all-resource closing sweep in the review experiment causes both expected ordering
failures; its necessity is measured, not inferred from a passing suite alone.

Previous hosted runs remain attributed to their original source and merge pins. Fresh combined-head
hosted verification remains separate. Earlier native forwarding and lifetime probes below remain
scoped to `33b6a412`; no new native or supported-platform result is claimed for this increment.
Production creation/trust publication, writer coexistence/rollback, full terminal delivery, both
usable RunContext paths and R1-R5 acceptance remain open. This working composition starts no new
public feedback round, review-requested/ready edge, SDD lock or retirement step.

## Latest transport increment composition

Joint pin `442c067ccc80409a5b6ed5e372ab4cc3398cc6fb` incorporates published transport
`08b5ce9d1dd0be71bdee7f4df18d9ce4dbd628e5` without conflicts. Production CLI source and the entire
transport SDD exactly match that owner head. Its finite Linux VM DIRECT preparation uses the shared
stage before application dispatch; the existing SSH byte path needs no additional staging protocol.
All three independent integration reviews clear this composition and preserve completed plan
records. Their focused checks include actual passive SSH validation of staged request shapes;
privileged admission in packed fixtures remains injected.

The complete local non-integration selection passes 21,971 tests with 78 skips and 51 fork warnings
in 408.48 seconds. The formerly failing managed-admission case now passes in the full run after the
owner's reviewed peer-publication correction. Ruff, format, strict typing across 1,541 sources,
canonical file quality, Rulesync, locked-SDD and Typer isolation pass. Older failed views remain
preserved. The refreshed private terminal composition `983eb5a38301fdb353be19ad8f642cc0139cebd9`
passes all 578 SSH cases with 12 skips; its SSH production bytes equal reviewed `e9442d6a8`. Native
observations remain attributed to their actual joint pin `09dff3511`.

[Hosted verification](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6255846632)
also clears earlier source `72a841d83`: Linux 3.12/3.13/3.14 each pass 21,707 tests with 308 skips;
Windows 3.13 passes 2,182 with 80 skips. The synthetic Windows client module includes all three
supplied input corrections. Those checks do not prove installed OpenSSH or terminal workflows, and
do not clear fresh hosted verification of this newer transport composition. Full R1-R5 acceptance,
both usable RunContext paths and the existing retirement sequence remain open.

## Retained managed-read custody composition

Private join `0815f86b5568998f0cf50a6ed8bb689785638bf3` integrates published transport
`c0298ba16f945ca4725145cefc39f3a6282acfaf`. Production CLI and the entire transport SDD match the
owner. Managed observation, output, result/wait and uncertain-start reconciliation use the retained
ExecutionOperation; the unused alternate controller is removed. All three independent integration
reports are clear and fully read. The combined managed/SSH selection passes 2,355 tests with 12
skips; full Ruff/format, strict typing across 1,541 sources and canonical file quality pass.

Private runtime/test pin `1c901c857e89ede043de211c12710396069e78ba` adds the reviewed SSH fixture
permission correction and native CA/KRL record. Its production CLI and transport SDD still exactly
match the owner; only the SSH drain and enrollment fixtures differ in CLI files. The permission and
CA/KRL unit has all three private reviews clear, plus 34 focused non-integration tests and targeted
source/document checks passing. Later authentication and trust-CLI evidence has both independent
document reviews clear. None of these reviews is full transport or implementation acceptance.

## Combined test-harness calibration

The first full run at `1c901c857` reports 327 failures, 21,505 passes, 78 skips, 51 fork warnings
and 161 setup errors in 310.95 seconds. The lead placed its temporary fixtures under a long
workspace path instead of the usual temporary filesystem. That root is 122 bytes before worker/test
suffixes, inherits setgid and default/access ACLs, and creates a gate namespace with mode `02770`.
Native socket binding refuses the path length; private-mode and gate checks refuse the inherited
permissions, as designed. Dropped-identity traversal also encounters the workspace boundary.

The unchanged socket test reproduces the path refusal, then passes under a short owned `/tmp` root.
A controlled selection of socket, private-directory and upload-gate cases passes all three under
that filesystem in 1.36 seconds. No production code, test assertion or operation deadline is
changed. The complete failed run, runner, calibration receipts and original errors remain retained.
The full run with short, exclusively owned temporary storage and no inherited ACL/setgid passes
21,972 tests with 78 skips and 51 fork warnings in 313.13 seconds, exit zero. Its runtime/test pin
remains `1c901c857`; the failed run is not relabeled as success.

## Hosted and native limits

Owner run `39173844606` is green at source `c0298ba16`, CI merge `5262de6` into main. Its
originating Windows result passes 2,182 tests with 80 skips and 25 warnings in 1,096.46 seconds.
This does not resolve the earlier joint source `a920eceb` run `39171980967`: that run's Windows
foreground-budget fixture observes after 3.592 seconds under its original requested three-second
deadline. The exact gap remains unexplained and transport-owned; the fixture is unchanged by this
owner increment. The
[critical reading](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6256323832)
keeps both originating results and their different source/base pins separate.

The [trust workflow record](trust-workflow-results.md) adds twelve scoped native Linux cases: CA/KRL
policy behavior at `a920eceb` with explicit owned-key preparation, then authentication offers and
actual in-process CLI/hashed lookup at `526b6e9b4` without that permission overlay. Their exact
pins, measured effects, original child observations and cleanup limits remain separate. In
particular, the earlier CA/KRL observer's missed SSH grandchildren are still unmeasured. These
fixture observations supply neither genuine provider creation nor production writer coexistence,
persisted operator rollback, supported-workstation delivery or usable RunContext acceptance. Public
terminal activation, full R1-R5 and the later migration/retirement sequence remain open.

## Causal terminal custody fixture correction

Fresh hosted run `39177029566` evaluates SSH `1cfdd832c6f92289ae41f646ece978b61aaa8502` through CI
merge `fc7851504b8cb86ae7fb81ac890e0565798d3d6f` into owner `c0298ba16`. Linux Python 3.13 and 3.14
each pass 21,739 tests with 309 skips and 51 warnings. Python 3.12 reports one failure, 21,738
passes, 309 skips and 51 warnings: the SSH held-terminal cleanup/interruption fixture takes 1.070233
seconds against its aggregate elapsed-under-one-second assertion. Its original control identity,
cause and uncertainty-note checks already passed. The Windows job is still running when this
correction is evaluated; no result or cancellation is inferred.

The timer includes native child startup and the caller's separate finite cleanup observation. The
exact hosted delay remains unattributed. Correction `54855b53f3cf3096d54bb3ffc72c4fd76b6c9412`
records exit from the selected held boundary in a finally-set event. Caller return must occur while
the release gate and boundary-exit event are both unset. The original six variants, request and
cleanup deadlines, native owners, exception/status checks, descriptor retention and original
worker/child/terminal cleanup assertions remain. No production behavior or timeout changes.

The developer and independent project reviewer each pass all thirteen module cases. A controlled
1.05-second construction delay fails the original timer and passes the revised fixture. Premature
release and waiting past the unchanged three-second hold each fail the new causal assertion.
Independent deletion testing confirms that removing only the boundary-exit check lets the latter
violation escape detection. Original local child, worker, custody, PTY and retaining interpreter
settlement are measured in those same executions. The lead fully reads all developer and review
reports plus the original control receipts; all three private source lanes clear the composition.

Evaluated runtime/test pin `a2e903d44008cfb7f7907b08b0fa309dad73d966` combines this fixture and the
separately reviewed external CLI evidence. Its complete local suite passes 21,972 tests with 78
skips and 51 warnings in 322.88 seconds. Full Ruff/format, strict typing across 1,541 sources,
canonical file quality, Rulesync, locked-SDD and exact CI Typer isolation pass. Production CLI and
the entire transport SDD remain byte-equal to owner `c0298ba16`. This working increment preserves
all original hosted failures and does not resolve the older transport-owned Windows budget gap,
activate the private terminal path or close full implementation/RunContext acceptance.

The originating Windows Python 3.13 job subsequently passes 2,182 tests with 80 skips and 25
warnings in 1,530.27 seconds at that same `1cfdd832`/`fc7851504` source/merge. This does not resolve
the earlier `a920eceb` budget-gap cause. The run retains its Linux 3.12 failure; fresh hosted
verification of the causal correction remains separate.

## Native Linux caller-held forwarding

All eight original installed-forwarding cases pass once at public
`33b6a4129620ca274b4457f3125d92b48dff22aa`, using Linux arm64, Python 3.12.13 and OpenSSH client
9.2p1/server 9.2. This supplies current caller-held component observations after the earlier native
proof at `8c3c4e5b`; it does not transfer old evidence to a new source or accept the whole
forwarding fault matrix. Original bodies, fixtures, operands, assertions and five-second operation
deadlines remain unchanged.

The cases transfer exact binary bytes, refuse occupied first and second listener orderings,
distinguish readiness from destination health, refuse IPv6/IPv4 partial success and preserve
trust/identity/forced-command refusal with listener release. IPv6 runs without a skip. Every case
admits and retains the same caller-held native session owner through settlement; none measures
before-owner refusal. Readiness is observed only for binary transfer and destination-health cases.
Expected failure statuses are 255 or one; successful held-session final status minus nine records
explicit cleanup, never natural application completion zero.

Each fresh owned loopback server has effective PAM/user-SSH-RC refusal, a positively checked owned
shell-startup guard and a separate startup trust pin. All sixteen private keys are measured `0600`
without a permission overlay. Actual checkout origins are positively gated before effects, six
retired execution roots are unavailable, and original fixture/helper hashes match public source. The
fully read signed report has SHA256
`2ae7a5b5a3179bb9c424e4e03edbfb106f20771a7dec169d5c33d44c9ade5b81`; its original aggregate is
`813989b79237384567939874353b2b388fb18be3b20c98b286935137fbd1903d`. The reviewed runner is
`dc3a97685af78f9c1014d02c9bb5a6dcbfa7e59b5af623e069308fc9c3891ecc`.

All eight original coordinator and delivery closes settle before server teardown. Fifty-six direct
original process handles have known statuses, closed pipes and independent same-execution `ECHILD`
observations. Nineteen independent endpoint rebinds cover all requested forwarding endpoints and
owned server ports. The lead checks every individual receipt against the aggregate, source hashes
and original child records, then independently confirms all eight exact disposable paths absent. No
retry or unexpected failure occurs; no source or operator state changes.

Cleanup is immediately positive in every case. Native pending construction, interrupted or failed
cleanup, concurrent close and retry faults remain unmeasured, as do server-internal descendants.
These Linux fixture observations supply neither other supported clients/platforms nor genuine
provider/RunContext/writer integration. Full R1-R5, terminal activation and retirement gates remain
open.

## Native Linux forwarding lifetime fault probes

Two separate controlled probes pass once at public `33b6a4129620ca274b4457f3125d92b48dff22aa`, using
actual Linux OpenSSH 9.2 clients and the same caller-held forwarding implementation. These inject a
post-construction return hold or a published drain hold; they are not genuine OS constructor
failures or failed native cleanup/retry. No fake client, owner, status or close result is supplied.
The eight original forwarding bodies above are not rerun or changed.

The constructor-return probe propagates the original deadline error while the real client remains
live, its valid pipes retained by the pending constructor and its owner's pipes unpublished. The
other probe holds the exact published native pipe tuple, sends SIGINT only to its current process
under the unchanged default Python handler, and traces the original exception in the actual
readiness wait. That exact KeyboardInterrupt object reaches the caller with its original retention
note.

Both callers return while their measured boundary is still held; finally-set exit events and expiry
flags forbid a fixture timeout from becoming pending-success evidence. A fresh finite close actually
returns false, the original child/pipes remain live/open and valid, the same owner/storage stay
retained, and replacement process admission refuses. Explicitly releasing only the fixture hold then
permits fresh close of that exact same resource and owner. This supplies no remote-no-effect claim.

The fully read signed report has SHA256
`77820a36ad6f6091f2b3c8fbb2f78dc79f941dbaa5154b4174baee70585f27f5`; its aggregate is
`a91891de5105b8ea4ba0d56a46f085651934314c053f2a592949c229f132176f`. The reviewed runner is
`527c3b5f35d59b371fbf5e8aeb93882a8df7eee3f52dc4597a01fce22efd6c68`. Source origins and hashes, owned
startup isolation and four private keys at `0600` are checked before native use, without a
permission overlay. Product timeouts and original caller cleanup remain unchanged.

All fourteen original direct process handles have known exits, closed pipes and independent
same-execution `ECHILD` observations. Four requested endpoint rebinds pass. Both original caller and
delivery stores settle before server teardown; the lead verifies individual receipts, source hashes
and original primary traces, then independently confirms both exact fixture paths absent. Final
forwarding/server status minus nine records cleanup, never natural completion zero. No unexpected
failure, observation expiry or retry occurs.

Genuine OS construction failure, actual native cleanup failure/retry, before-owner refusal,
concurrent close, other platforms/controls and server descendants remain unmeasured. Genuine
provider/RunContext/writer integration, full R1-R5 and retirement acceptance remain open. The
earlier eight-case record retains its own immediately positive cleanup scope; these later controlled
probes do not retroactively enlarge it.
