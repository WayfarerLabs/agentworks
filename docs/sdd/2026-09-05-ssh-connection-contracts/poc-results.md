<!-- cspell:ignore tcsh -->

# SSH PoC Evidence

Status: The buffered proof has six measured live cells and a successful macOS fixture retest.
Existing reports cover the combined candidate through verified unchanged CLI and runtime trees.
Transport accepts the joint buffered PoC; final SSH reviews and gates are in progress. Production
readiness remains Phase 2.

## Revisions and delivery

- Governing merged transport design: #795, `857110df`.
- Current transport candidate: #826, `8b4d3690b88b51a096bf465e8f57ee3769e84a7a`.
- SSH implementation: #796. The PR handoff records the exact pushed head for each review.
- First live run: SSH and integrated SHA `1c32e4155c4a41304638d7637e1418459cc90092`, containing
  transport `a570a2de3b30ed754cca7195fb932af25488673e`; no merge or conflicts were necessary.
- Later live run: the same SSH input plus transport `d75c0bd3e699cfea3f5dcf40d2d34cb32c8bbe91`,
  integrated as `d3c300ba7e51c83ee53da4b76760451b33a49e98`, with no conflicts. This was a real
  merge, not the earlier ancestor relationship carried forward.
- Windows retest: SSH and integrated SHA `901d9614181da0fb209e9896c8e747a44118d123`, containing
  transport `d75c0bd3`; the tester rechecked ancestry and installed the candidate on Windows.
- Default-shell proof: SSH `901d9614` plus transport `6687ef88f2138c819600ff11fa777924f707d9d9`,
  integrated as `c188b32ea89ba0da1aadf07bda5461dd033009bf` without conflicts. Transport's
  intervening change was documentation only; this run required a merge.
- Revised-vector proof: SSH and integrated SHA `1ccc304b339a22baeb0df8ef5a7534a3c9e6f8ec`,
  containing transport `6617f6e6`. The tester rechecked ancestry, installed that tree and verified
  the renamed `Observation.reported_exit` field on each workstation. No merge or resolutions were
  needed.
- macOS fixture retest: SSH and integrated SHA `bc2a0711c4b9eb9a069c8df9ec9be512d9a28bae`,
  containing transport `e41a4482`. The tester rechecked ancestry and verified the installed fixture
  by content. Runtime is unchanged from the six-cell live proof at `1ccc304b`.
- Final documentation combination: transport `931ad8ef77d7e02751e7c2ae315d1466f3fd7f15` plus SSH
  `bc2a0711`, conflict-free tree `9522adc07a544eadb658bf12324080bf420204b3`. Its entire `cli/` tree,
  `3fae525bb53ef7bac860a1458bf41845322b3f0a`, equals tested `bc2a0711`; its runtime tree,
  `4405f771470a51b6b1c43fdd1363a7dadfd810a1`, equals live-tested `1ccc304b`. Both leads verified
  these comparisons. This is carry-forward evidence, not a rerun.

The operator explicitly directs the full SSH PoC and artifacts in #796, then full implementation,
migration and integration in a second PR under this SDD. There is no design-only merge. The tester
can combine pinned branches locally; record both inputs, integrated tree and installed revision. SSH
does not modify transport's contract or acceptance criteria.

## Proof charter and prerequisites

The operator assigned live setup and testing to their existing integration tester, with its own
inventory, credentials and budgets. The
[SSH handoff](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5708541462) and
[joint handoff](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709098942) scope
that charter to isolated fixture identity/trust, pinned installed inputs, shared vectors, bounded
fault workloads, safe evidence and independently verified cleanup. Carrier input and per-stream
capture limits bound payload resources; each reported deadline bounds local observation. The reports
below identify actual workstations, destinations, versions, workload sizes and cleanup outcomes.
Private inventory and spending ceilings remain in the tester's existing authorization and were not
independently audited or republished by this SSH session. These handoffs grant no new provisioning
authority.

The shared bootstrap uses Linux Bash, GNU tools and descriptors, with no guest Python, installed
helper or filesystem-staging dependency. The first report measures the prerequisites and unusable
temporary-directory vector; the later report measures Bash 5.1.16/coreutils 8.32 and independently
checks staging and cleanup. This resolves first-delivery dependency readiness for the measured Linux
candidate. It is not evidence that a complete Agentworks initialization ran on a Python-free image,
or that a macOS platform host supplies the same userspace. Platform-host composition remains
Phase 2.

## Published live evidence

The operator's integration tester supplied the
[first report](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5709203388) and the
[later combined-tree report](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5714595742).
Both installed the pinned combined package with Python 3.12.13 on aarch64 Linux. The earlier
[Muntz provenance correction](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5708775662)
explicitly withdraws its local-only report as the integration deliverable; that evidence remains
separate.

The following table records those earlier runs; the Windows failure and affected macOS coverage are
superseded by the later measurements below.

| Workstation                                        | Destination/platform                                      | Reported result                                                                                                                                     |
| -------------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| aarch64 Linux 6.1, OpenSSH 9.2p1                   | Remote Lima, AWS EC2 and Azure VM destinations, Debian 13 | All eight shared vectors and reported SSH fault/trust lanes passed.                                                                                 |
| macOS 26.3 arm64, OpenSSH 10.2p1                   | Remote Lima, Debian 13                                    | All shared vectors and reported lanes passed.                                                                                                       |
| aarch64 Linux                                      | Ubuntu 22.04.5, Bash 5.1.16, coreutils 8.32               | Shared vectors, byte preservation and early consumer closure passed at the declared Bash floor.                                                     |
| aarch64 Linux                                      | PVE 8.4.21 and 9.2.11 native QGA, Debian 13 guests        | Shared vectors and native fault/trust lanes passed; guest identity was root. Transport owns acceptance.                                             |
| Windows Server 2022, Python 3.12.14, OpenSSH 9.5p2 | Reachable SSH destination on the later combined tree      | Three carrier attempts expired at 30 seconds with unknown completion and empty captured streams; hand-run SSH succeeded. Mechanism not established. |

The first report measured strict refusal for wrong/empty trust and unauthorized identity without
mutating trust files. Binary bytes were preserved, explicit output limits retained partial output
and independent completion, large early-closed payload input succeeded, and sensitive canaries were
absent from sampled process arguments and reports. Destination userspace and account shells were
measured separately from workstation clients; consult the reports for each exact combination. No
staging artifacts were observed in the tested destinations.

The second report verified the updated shared input accounting, Bash floor and all-module
independence guard. Native TLS used the real cluster CA and certificate, with the matching loopback
SAN reached over an SSH forward because the trusted DNS route was unavailable. This is the reported
network-path qualification, not an SSH ruling on native trust acceptance.

### Windows resolution and later shell proof

The [Windows retest](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5715862216)
installed `901d9614` and verified its child-environment helper on the Windows bed, rather than
relying on a release version string. Windows Server 2022, Python 3.12.14 and
`C:\Windows\System32\OpenSSH\ssh.exe` (OpenSSH 9.5p2) reached a Debian 13 destination using explicit
fixture identity and strict trust.

Both the original Windows OpenSSH-parent context and a clean launch passed three smoke calls, all
eight shared vectors, exact binary delivery, output limits, 150,000-byte finite input, strict trust
refusal and sensitive suppression. Smoke calls completed in 0.8 to 1.0 seconds; the original context
had previously expired three times at 30 seconds. Only the SSH-parent context inherited private
descriptor state. These observations resolve that measured failure, not every Windows version or
launch arrangement. The tester disclosed and discarded an initial clean-context setup whose SYSTEM
account could not use the fixture identity; the successful rerun used an accessible identity and
independently verified authentication first.

The same head passed the affected Linux lanes against Debian 13 with Bash 5.2.37 and coreutils 9.7.
That report carried earlier macOS results forward, leaving the cross-platform pipe-drain change
unmeasured there until the subsequent retest below. A remote detached child's prompt return does not
test a workstation descendant retaining the local client's pipe handles; the dedicated local
regression covers that separate case.

The
[default-shell report](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5716019566)
measured `Shell.user_default()` through SSH and native QGA on PVE 9.2.11. Independent observations
established UID 1000 with `/usr/bin/bash` and UID 0 with `/bin/bash`, respectively. Both preserved
all 2048 input bytes, completed guest streams and reported zero without framing, bootstrap or
carrier failure despite `SHELL=/does/not/exist`. A separate `/bin/dash` default-shell account
reached the bootstrap's explicit refusal. This does not establish login/interactive startup, startup
hooks or the same new lane on PVE 8.

Account-shell refusal before bootstrap is different: measured tcsh/false accounts returned raw SSH
status 1 while captured output had a framing error. The shared harness rejected those calls. Raw
status alone cannot establish that bootstrap or application execution occurred, and suppressed
output intentionally supplies no framing proof. The current transport candidate corrects the shared
wording and strengthens the sensitive vector to require exit 37 after synthetic reflection, with a
regression rejecting a pre-bootstrap zero. This improves the controlled proof, not authentication
against arbitrary startup behavior. SSH consumes the renamed `reported_exit` observation without a
private framing oracle.

Both later runs measured 295 execution tests passed with four skips. The reports independently
verified destination removal, temporary identities/access and tester-file cleanup, no live test
tailnet nodes and empty VM inventory, plus provider state. The Windows bed was deallocated; the
native run destroyed its guest, revoked its token and stopped the bed. These remain attributed
tester observations.

### Revised sensitive vector and macOS retest

The
[current SSH report](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5717455178)
and [native companion](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5717456803)
measure `1ccc304b` with transport `6617f6e6`. All eight shared vectors passed in each of six cells:
Linux aarch64/OpenSSH 9.2p1 to Debian 13, macOS 26.3 arm64/OpenSSH 10.2p1 to Debian 13, Linux to
Debian 12, native QGA on PVE 9.2.11, and both Windows launch contexts. The revised sensitive case
reported exit 37, both streams suppressed, zero retained bytes and no carrier failure everywhere.
This is controlled-case evidence, not proof against arbitrary account startup behavior.

Windows Server 2022 used Python 3.12.14 and native OpenSSH 9.5p2. The SSH-parent context had private
descriptor state present and stdio mode absent; the clean context had both absent. Both preserved
binary bytes without CRLF translation, enforced output limits, refused elapsed deadlines before
dispatch and retained unknown completion for deadlines during work. The original authenticated
timeout remains resolved. Cleanup status 1 on Windows versus -9 on POSIX proves no earlier natural
exit.

Fresh macOS live measurements passed the vectors, byte preservation, output bounds, early stdin
closure and both deadline lanes. The local execution suite reported 254 passed, 45 skipped and one
failure: `test_agent_endpoint_is_explicit_and_must_be_a_socket` tried to bind a 133-byte path under
pytest's temporary directory, exceeding the measured 103-byte Unix-socket limit. Non-socket refusal
ran, but genuine-socket acceptance did not. The skips were not individually enumerated. The named
descendant-output and descendant-stdin regressions have no macOS skip, so the sole reported failure
supports those specific passes; it does not make the whole suite green. Round 3 corrects the fixture
using a short owned directory and retains both assertions. The later macOS retest below closes that
specific failure by measurement.

The Debian 12 cell measured Bash 5.2.15, coreutils 9.1, `/bin/bash` and UID 1000. It establishes
guest compatibility, with two qualifications: provisioning used `gcloud` outside Agentworks, and
initial host-key pinning used TOFU. It is not proof of Agentworks provisioning or independently
authenticated initial trust. The separate trust-refusal cells retain their own provenance.

Native execution remained UID 0 on PVE 9.2.11. PVE 8.4.21 did not receive the new vector in this
run. Transport's
[disposition](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5717481315) accepts
carry-forward of its unchanged delivery/TLS/bounds evidence while explicitly retaining that
measurement distinction. SSH does not declare native or combined acceptance.

The tester observed bootstrap/sleep counts of 10/2 at 30 seconds, 5/1 at 60 and 90 seconds, and 0/0
at 120 seconds after deadline lanes. These bounded workloads drained after finishing; no reaper or
cancellation guarantee follows. No carrier staging appeared in destination `/tmp`. Reported cleanup
removed destinations and the Bookworm instance, destroyed the native guest and revoked its token,
stopped both beds, removed Windows access/keys/files and disabled sshd, and removed macOS keys and
scratch. Provider-level checks independently verified teardown. This session did not perform those
live mutations.

### Final macOS fixture measurement

The [fixture report](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5718460281)
measures `bc2a0711` on macOS 26.3 arm64 with Python 3.12.13. The installed fixture's SHA-256 matches
the candidate. Both non-socket refusal and genuine-socket acceptance pass under the normal temporary
environment and a deliberately long, owned 165-byte pytest base. The socket path is 33 bytes against
the measured 103-byte host limit. The socket closes and the owned directory is removed with no
residue. The tester discarded an initially misquoted 15-byte base and reported only the corrected
long-base measurement as that regression's evidence.

The SSH suite passed 99 tests with six skips; the combined execution suite passed 255 with 45 skips
and no failures. The skips comprise 26 Linux descriptor/GNU-tool cases, ten Linux `/proc` fault
cases, two first-bootstrap cases, two shared-bootstrap cases, one Linux account-lookup case, two
Windows descriptor-metadata cases, one Windows child-environment case and one Windows process-status
case. Those 41 Linux-scoped and four Windows-scoped cases remain unmeasured on macOS. The local
descendant-pipe and process-ownership regressions carry no macOS skip and passed in this run.

The report explicitly carries the six live cells forward from unchanged runtime at `1ccc304b`. No VM
or bed was started for this retest. Tester scratch, the long base and socket directories were
removed and checked. The
[native delta report](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5718672799)
likewise carries native evidence forward through transport `931ad8ef`. Its native timings of 0.6
seconds for one call and 4.5 seconds for eight vectors are context, not a diagnosis of Windows CI
scheduling or the earlier, separately resolved Windows SSH descriptor failure.

## Lifetime and cleanup

Local observation expiry can leave the workload and bootstrap descendants running. The second report
narrows the first report's word "indefinitely": bounded work drains after its own completion;
unbounded work has no PoC reaper. The current transport
[contract](../2026-09-12-transport-improv/execution-contract.md#carrier-contract) records the
operator's accepted PoC deferral and forbids production use until shared ownership/cancellation is
implemented. SSH does not add an implicit kill or retry mechanism.

The second report supplies the earlier cleanup addendum: the GCP instance, disk and deny firewall
rule were removed and verified through provider listings; test guests were destroyed, tokens
revoked, certificate material removed, PVE beds stopped, and the Windows tester files/access removed
before the bed was deallocated. The incomplete GCP metadata refusal was reported separately as an
existing lifecycle gap. These are attributed tester observations, not cleanup performed by this SSH
session.

## Local validation history

Before the first live handoff, SSH's independent project, complexity and correctness lanes cleared
`1c32e415`; hosted Linux Python 3.12/3.13/3.14 and Windows Python 3.13 CI passed. The Windows
selection measured 255 passed and 16 skipped, using synthetic children and installed option parsing.
It did not establish real authenticated Windows SSH delivery, as the subsequent negative live report
demonstrates.

Local combined execution tests measured 260 passed/1 skipped, focused SSH tests 98 passed/1 skipped,
and the complete suite 10,181 passed/8 skipped. The peer's aarch64 environment measured 10,184
passed/8 skipped at the same input. The collection difference is unproven and neither count is
substituted for the other. The later tester reproduced 292 passed/1 skipped on its newer combined
execution tree. Exact new-head gate results belong in the next PR handoff.

SSH already had an isolated-process test that blocks the retirement set and executes the carrier,
`test_ssh_executes_in_fresh_process_without_legacy`. The published claim that no automated SSH guard
existed missed that test. Transport's newer all-module scan now provides broader shared coverage
too; both belong in combined validation.

## Feedback round 1

The full reports and complexity review were available before the first round began, more than an
hour after the handoff. The lead removed `review-requested` before changes and posted its
[critical reading](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5715563686).
Round 1 consumes one of the four authorized rounds; no later round starts without a full integration
report for the new handoff.

The round addresses policy assertions omitted or obscured by `ssh -G`, the natural-exit case with
undelivered bytes and an inherited stdin reader, duplicate SSH retention mapping, and documented
status-255/Windows-cleanup ambiguity. Explicit default options remain intentional policy across
supported clients. Historical completed plan pins retain their original meaning; this record is the
current dependency source. The dated main comparison remains until its Phase 2 migration risks have
actual dispositions.

### Windows investigation

The original Windows timeout was material. Its reported `local_status=1` can result from our local
kill/reap path; it does not prove a natural exit ignored by the pump. The later report explicitly
accepts that correction.

The investigation traced Windows OpenSSH's inherited private descriptor state, which can describe a
launching SSH process's handles instead of Python's newly created pipes. Hosted
[baseline job 105233338071](https://github.com/WayfarerLabs/agentworks/actions/runs/35230586276/job/105233338071)
at `fe22cce0` reproduced a failure with the real installed client: the local peer received its SSH
banner, but the contaminated-descriptor case returned DEADLINE instead of observed client refusal.
Clean and alternate stdio-state cases passed. The fixture uses an owned loopback peer and never
reaches authentication, so no operator credentials were used.

The correction filters the two OpenSSH-private variables from the Windows child environment only.
Ordinary variables and the parent remain intact, with case-insensitive matching. Corrected
[Windows CI](https://github.com/WayfarerLabs/agentworks/actions/runs/35231092083/job/105235070534)
passed 269 tests with 16 skips. All three private review lanes cleared `901d9614`; local full tests
passed 10,216 with 11 skips and execution tests passed 295 with four skips. The
[round-1 handoff](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5715778967)
records the complete green gate set. The authenticated retest above, separately from this local
evidence, closes the original measured Windows case.

## Feedback round 2

The [second round](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5716704189)
started after the full reports and review window, with the label removed before edits. It records
the Windows resolution, distinguishes raw channel completion from bootstrap/application evidence,
and consumes transport's
[reviewed harness correction](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5717038529).
The [critical reading](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5716400569)
also retains affected macOS coverage and other unmeasured cells as open evidence. Two of the four
authorized fix rounds remain after this round's handoff.

The [round-2 handoff](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5717218075)
records all three private reviews clear at `1ccc304b`, 10,217 local tests passed with 11 skipped,
296 execution tests passed with four skipped, and the complete green local/hosted gate set. Hosted
Windows measured 269 passed with 16 skipped. Those fixture results remain separate from the live
report above.

## Feedback round 3

The [third round](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5718050777)
started after the full reports and collection window, with `review-requested` removed before edits.
It accepts the new live evidence with the qualifications above and fixes only the SSH-owned socket
test's temporary path. The previous fixture failed locally under a deliberately long pytest base
directory; the corrected test passes under that same base and leaves no owned socket directory. This
Linux reproduction and correction do not substitute for the requested macOS retest. No carrier
runtime or shared contract changes are included.

The first round-3 candidate, `00ed8354`, passed all local gates and private reviews. Hosted Windows
[job 105297331787](https://github.com/WayfarerLabs/agentworks/actions/runs/35249288856/job/105297331787)
then failed transport's unchanged default-trust test: its five-second deadline expired after the
synthetic POST, before GET. The job reported 268 passed, 16 skipped and one failure; the other
hosted jobs passed. The requested diagnostic rerun was unavailable to this session's token, not a
second test outcome. Transport's
[owned correction](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5718274104)
gives that trust-test helper 30 seconds for two real worker starts and TLS/HTTP. Its controlled
delayed-start experiment supports timing sensitivity without proving the exact Windows cause. Trust
assertions, production deadlines and separate deadline-enforcement tests remain unchanged. SSH
consumes the reviewed correction and transport's evidence update by rebasing onto `e41a4482`.

The [round-3 handoff](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5718404715)
records all three private reviews clear at `bc2a0711`, 10,217 local tests passed with 11 skipped,
296 execution tests passed with four skipped, and the complete green local/hosted gate set.
[Hosted CI](https://github.com/WayfarerLabs/agentworks/actions/runs/35250844162) passed all jobs;
Windows measured 269 passed with 16 skipped and the default-trust test passed in 4.80 seconds. One
authorized fix round remained after this handoff. The earlier failed job remains part of the
evidence rather than being replaced by the green result.

## Feedback round 4

The [final round](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5719789643) began
after the complete reports and collection window, with the label removed before changes. It records
the macOS measurement, consumes transport's reviewed documentation and acceptance record, and
reconciles this SDD for the second implementation PR. The leads initially requested another tester
acknowledgment for the documentation combination; transport's
[correction](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5719760856) and the
independently verified tree equality above establish that the existing reports already cover it.
That additional gate was unnecessary, not a missing test or a requirement waiver. No new SSH runtime
change or live-test claim follows from this closeout.

## Joint acceptance and remaining scope

Transport's
[acceptance disposition](../2026-09-12-transport-improv/proof-lld.md#joint-buffered-proof-acceptance-2026-09-17)
maps every proof row to the existing reports and local ownership tests. The accepted buffered
candidate uses finite bytes/EOF, capture/discard and the explicit connection identity.
Account-default selection and unsupported-shell refusal were separately measured. Login/interactive
startup is refused; there is no identity-switch request or implicit elevation. Immutable input has
no borrowed caller stream. Neither carrier advertises live streams or terminals. These are
applicability decisions under the existing candidate, not omitted required cases or acceptance of
future APIs.

The acceptance record in transport `8b4d3690` adds documentation only. PVE 8's new sensitive vector
remains unmeasured, with transport's explicit bounded-proof carry-forward decision; Bookworm retains
its out-of-band provisioning and TOFU qualifications. WSL2, multi-node Proxmox, demoted native
identity, provider-inner policy and arbitrary account startup hooks remain unmeasured. Those facts
do not expand the accepted candidate's capabilities. Supported default-shell results establish only
the measured accounts. Guest drain is not cancellation.

The next work is the full implementation and migration in the second SSH PR under this SDD,
coordinated with transport's proof-informed design reconciliation and production gates.
Public-result interpretation, live/terminal ownership, files/jobs, shared workload cancellation,
platform-host and provider-inner composition, complete trust/configuration migration, production
enablement and legacy deletion are not completed by this PoC.

No new credentials, destinations, provisioning or network mutations are authorized by these reports.
The existing integration tester owns the authorized inventory and cleanup. This SDD stays unlocked;
the accepted joint buffered proof is not production readiness or completion of the full SDD.
