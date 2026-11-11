# Validation and acceptance audit

Date: 2026-11-10. Owner: the effort lead. Governing requirements: [frd.md](frd.md). Implementation
convergence:
[PR comment 6774724254](https://github.com/WayfarerLabs/agentworks/pull/885#issuecomment-6774724254).

## Evidence and attribution

- Initial implementation: `9b864f743d697f98bc9be716ceb0320f71c1a42c`, green
  [CI 42830730004](https://github.com/WayfarerLabs/agentworks/actions/runs/42830730004), and
  [integration report 6771656760](https://github.com/WayfarerLabs/agentworks/pull/885#issuecomment-6771656760).
  The report passed twelve gates with 11,654 tests and 11 skips. Live coverage was Linux to remote
  Lima/macOS and Windows Server 2025 to native WSL2. Its current-main conflicts required a fix.
- Converged implementation: `452eb0622520fcb816e6c9a0c6ff85f810ce5495`, containing main
  `1dd5a06dd330d947676fe130cc345fa93e64c797`, green
  [CI 42851513484](https://github.com/WayfarerLabs/agentworks/actions/runs/42851513484), clean
  [published delta review](https://github.com/WayfarerLabs/agentworks/pull/885#issuecomment-6772829295),
  and
  [integration report 6774690468](https://github.com/WayfarerLabs/agentworks/pull/885#issuecomment-6774690468).
  All twelve gates passed with 11,678 tests and 11 skips. Fresh live coverage was Linux and Windows
  Server 2025 workstations to remote Lima/macOS; Windows used PowerShell 5.1 and 7.6.9.
- Three private lanes were clean at `55d14026e`: project, complexity, and generic
  correctness/security. Their focused suites passed 213, 231, and 146 tests. The correctness lane
  also passed 21 isolated checks, including seven rekey failure/cancellation stages. The final
  plan-only delta was checked at `452eb0622`. These checks supplement live evidence; they are not
  native provider observations.

The old and new source comparisons preserve the initial migration, runtime-state controller, VM
runtime, family guard, and WSL keeper/bootstrap/transport evidence at its original head. The console
attach dependency cleanup is a separate reviewed change, exercised by the new campaign. Closeout
changes only SDD records; it adds no application or permanent-guide change.

## Requirements

| Requirement | Implementation, behavioral tests, and permanent guidance                                                                                                                           | Live evidence                                                                                                                                           |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1          | `db` constraints/migration 39 and per-kind intent services; `tests/db/test_desired_runtime_state.py`; desired-runtime-state and runnable-status guides                             | Initial real migration, explicit adoption and finite Shell exit retained intent; final pure setters                                                     |
| R2          | Lifecycle acceptance before execution, UUID checks and runtime-only cleanup; `tests/sessions/test_prepared_runtime.py`, runtime-state failure tests; lifecycle acceptance guidance | Both campaigns retained targets through parent shutdown, recovery and explicit child operations                                                         |
| R3          | Explicit selection and focused scopes in `runtime_state`; CLI and composition tests; command reference and focused recovery examples                                               | All six commands in the initial campaign, VM/fleet and focused recovery in both campaigns, healthy repeats                                              |
| R4          | VM preparation, session execution, console realization, then provider stops; composition/console tests; console independence guidance                                              | Final two-session/named-console restoration, stopped member absent, retained member order; initial console/member independence                          |
| R5          | VM/session identity fences, guarded console selection and domain observation; replacement, recovery and preview-race tests; safe recovery guidance                                 | Native guard contention, retained durable identities and healthy repeat; unknown/corrupt runtime faults retain unit coverage                            |
| R6          | Dry-run observation and typed per-resource results; runtime-state composition/failure/preview-race tests; preview/results guidance                                                 | Actual non-mutating dry runs; stopped-parent refusal with independent-family progress; final secret-barrier snapshots                                   |
| R7          | One Resolver, failed-gate memo and deferred execution; runtime-state composition, prepared operation, eager-secret and console-value-barrier tests; secret boundary guidance       | Initial and final mixed-fleet missing-input barriers; final healthy secret-backed no-op without input; selected repair cache and real PTY single prompt |
| R8          | Single migration cutover, nullable legacy workload intent, derived JSON compatibility; DB/restore/CLI tests; adoption and migration guidance                                       | Initial schema 38-to-39 data/native identity comparison and explicit adoption; no migration rerun claimed for the final delta                           |
| R9          | One PR, recorded stage budgets, independent reviews, green gates, published critical readings, this audit and final lock                                                           | Both completed exact-head reports, actual CLI recovery on both workstation axes, independently audited cleanup                                          |

Permanent behavior is documented in the
[desired runtime state guide](../../guides/desired-runtime-state.md), the runnable-status guide,
command reference, retained management guide, and orchestration README. It does not depend on this
SDD remaining in the tree.

## Acceptance criteria

The numbered rows correspond to the FRD. All rows have source and behavioral-test evidence above;
the live column states the additional native observations without implying all fault permutations
were driven live.

| AC  | Decisive result                                                                                                                                                                    |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Final Linux and Windows campaigns each recovered two desired-running sessions and a named console with ONE VM reconcile after a real provider stop/new boot                        |
| 2   | Both final healthy repeats preserved native/DB identities; composition tests also reject replacement on healthy paths                                                              |
| 3   | Initial finite Shell exit retained running intent; explicit session/console stops and stopped members remained absent across recovery                                              |
| 4   | Initial stopped parent blocked accepted running children without booting; explicit start/recovery retained child intent                                                            |
| 5   | Initial idle on-demand VM stayed off; focused child demand activated without retargeting; final Windows new-boot recovery used an on-demand parent                                 |
| 6   | Native console/member independence and final four-pane/member-order restoration; focused console tests forbid member-session expansion                                             |
| 7   | Domain observation, partial ownership, checkpoint and stale-identity tests refuse unknown/unsafe recovery; arbitrary native corruption was not injected                            |
| 8   | Actual initial three-kind dry runs preserved DB/native/lock snapshots with required workload input absent; preview race tests use real public deletion services                    |
| 9   | Initial and final missing-input fleet snapshots proved no deferred workload or VM-stop changes; final healthy no-op performed no unused workload lookup; real repair prompted once |
| 10  | Initial stopped-parent refusal preserved independent healthy-family recovery; composition tests cover failed infrastructure gates, contention and nonzero aggregate results        |
| 11  | Actual initial migration left workload intent null until adoption; pure setters and accepted start/stop adopt without guessing from liveness                                       |
| 12  | This PR contains code, permanent documentation, actual reports, green required CI and closeout artifacts; final closeout publication follows the plan                              |
| 13  | Initial externally stopped running-intent VM recovered with children still stopped; explicit childless activation also has a dedicated composition test                            |
| 14  | Initial focused session/console stop and recovery preserved sibling/member intent and identities; composition tests forbid scope expansion                                         |
| 15  | Isolated real CLI setters succeeded without configuration/provider access, retained runtime evidence and rejected wrong-kind states; final Windows setters preserved native state  |

## Coverage limits and cleanup

The native WSL race delayed acknowledgement consumption, not guest boot. Stop refused while the
acknowledgement reader held admission; after consumption, admission released before the interactive
body and stop succeeded. Stopped-intent diagnostic access created no keeper. Malformed, missing or
timed-out acknowledgement, cancellation and forced cleanup have host-sensitive behavioral tests;
they are not additional live guest-fault claims.

No fresh Windows 11, Windows real-TTY prompt, cloud/Proxmox recovery, infrastructure-key-failure
isolation, model conversation restoration, arbitrary unknown-runtime corruption, console staging
fault, or exhaustive cancellation-timing live pass is claimed. Source/controller tests cover the
relevant refusal, isolation, staging and cancellation paths. These limits do not waive the FRD's
contracts or replace the required real recovery, which both campaigns established. Existing WSL
resource-cap, hostname and installation/dependency qualifications remain attributed to the initial
report; this feature did not change global WSL configuration.

Both live campaigns independently audited supported fixture deletion, exact native births, provider
disks/directories or WSL registration, SQLite integrity/foreign keys, trust entries,
keypairs/declarations and post-unlink credential scans. The final campaign removed three test VMs
and children; recorded 14 Mac HostAgent/VZ births and 450 Linux client births were gone.
Supplied-key scans reported zero matches, including decoded archives, with zero exclusions. Standing
management tools, access and other lanes were preserved. The initial admission observation timeout
and a cleanup-helper assumption were corrected in the test harness with original evidence retained;
neither established a product defect.

## 2026-11-11 integration status

The operator-directed current-main integration follows the completed SDD-only closeout at
`ad4600df8`. It changes the combination of desired-state lifecycle with main's newer trust,
execution, artifact delivery and compatibility code. The reports above remain evidence about their
original tested heads. Combined-source gates and all three independent review lanes passed, with
12,325 tests and 34 skips in the corrected root run. The final delta at `101071778` is independently
clean. CI run 42947972175 passed all ten jobs at the handed-off `dbbbd31cc` head. Completed separate
integration report 6786419851 passed all twelve gates with 12,325 tests and 34 skips. Its actual
Linux and Windows Server 2025 workstation campaigns against remote Lima recovered desired sessions
and ordered named consoles after a new boot, retained identities on healthy repetition, and verified
missing-trust refusal, genuine host-key import, pure setters, provider/off-parent stops and native
artifact retention. The report's independent controller/SQLite probe reproduced the missing-trust
preview defect; the current corrective head requires fresh validation before convergence.

The report's model health attempt reached the first-party API through the installed Claude session
but failed with insufficient credit, exit 1 and zero reported cost. It proves no successful model
conversation, OAuth or interactive response. Independent cleanup removed its three Lima fixtures and
Windows campaign root, checked empty entity/event tables and integrity/foreign keys, and audited
native resources and captured process births. Linux/model credential scans and exported local
Windows evidence were clear; no remote Windows private-key scan is claimed. Offline tailnet metadata
may remain. Migration, WSL and cloud evidence above remains attributed to earlier heads.

Independent verification also reproduced an incoming-main host-key import metadata race:
`vms/manager/host_keys.py` retains a VM row/address, then writes trust by name without a family
guard or fresh identity check. A controller/SQLite proof used public VM deletion and replacement
through `Database.insert_vm`, after which import wrote the old key for the replacement. It does not
establish a full public-create/native campaign or a compromised ready VM. Importer and trust store
code are byte-identical to main `834eb7156`, and the conflict resolution introduces no new caller.
This is recorded for the SSH trust/import owner as an upstream discovery outside this follow-up; no
production fix or broader trust-import contract change is included.

## 2026-11-11 corrective candidate

The final authorized implementation feedback round adds preview's read-only trust prerequisite
before provider credentials/observation and a real Console preacceptance regression. The
operator-directed GCP correction dispatches native resume for suspended instances and waits for
known stopping/suspending transitions before deciding, preserving numeric identity checks and
operation verification. Behavioral tests cover request/operation identity, ownership replacement,
timeouts, cancellation and unchanged stored intent. All three private lanes were clean at
`98b957b40`, with independent focused runs of 184, 121 and 121 tests. Removal experiments confirmed
the new boundaries' sensitivity; the Console experiment did not reach an intent write because
provider configuration was incomplete after the early boundary was removed.

The complete non-overlapping root confirmation returned zero with 12,366 tests passed and 34 skipped
in 308.04 seconds. The earlier overlapping run's sole failure was a handover-cleanup timeout in
unchanged incoming-main execution conformance code. Its isolated rerun passed; the original cause is
unproven, and no source was changed for it. All twelve required local gates passed, including static
checks, file quality, rulesync, locked SDDs, typer isolation, 160 Python and 103 Node website tests
and both deterministic double-build variants. Exact final-head CI, ready publication and the new
separate integration report remain pending. No new live GCP result is established by unit tests or
earlier reports.

## Supplemental integration preflight

Round-two CI 42960165124 passed all ten jobs at `863c499aa`; all three final SDD delta reviews were
clean. Completed separate preflight 6787498628 compared that head with freshly fetched main
`07feb4a45` and reproduced four conflicts. It stopped before gates, wheel build, delegated review
and live testing, touching no integration operator state. It establishes no fresh native GCP or
combined teardown result. Earlier evidence retains its exact head attribution.

The operator authorized one supplemental round. Its main teardown integration and deterministic GCP
test-clock correction require new combined gates, three private lanes, exact-head CI, ready handoff
and the associated integration report. Those remain pending; publication alone does not complete
monitoring or disposition.
