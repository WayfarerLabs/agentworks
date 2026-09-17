# SSH PoC Evidence

Status: Live Linux/macOS evidence exists. Windows delivery has a reported failure under
investigation. Combined acceptance and production integration remain open.

## Revisions and delivery

- Governing merged transport design: #795, `857110df`.
- Current transport candidate: #826, `d75c0bd3e699cfea3f5dcf40d2d34cb32c8bbe91`.
- SSH implementation: #796. The PR handoff records the exact pushed head for each review.
- First live run: SSH and integrated SHA `1c32e4155c4a41304638d7637e1418459cc90092`, containing
  transport `a570a2de3b30ed754cca7195fb932af25488673e`; no merge or conflicts were necessary.
- Later live run: the same SSH input plus current transport above, integrated as
  `d3c300ba7e51c83ee53da4b76760451b33a49e98`, with no conflicts. This was a real merge, not the
  earlier ancestor relationship carried forward.

The operator explicitly directs the full SSH PoC and artifacts in #796, then full implementation,
migration and integration in a second PR under this SDD. There is no design-only merge. The tester
can combine pinned branches locally; record both inputs, integrated tree and installed revision. SSH
does not modify transport's contract or acceptance criteria.

## Published live evidence

The operator's integration tester supplied the
[first report](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5709203388) and the
[later combined-tree report](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5714595742).
Both installed the pinned combined package with Python 3.12.13 on aarch64 Linux. The earlier
[Muntz provenance correction](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5708775662)
explicitly withdraws its local-only report as the integration deliverable; that evidence remains
separate.

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

The real Windows timeout remains material. Its reported `local_status=1` can result from our local
kill/reap path; it does not prove a natural exit ignored by the pump.

The investigation traced Windows OpenSSH's inherited private descriptor state, which can describe a
launching SSH process's handles instead of Python's newly created pipes. Hosted
[baseline job 105233338071](https://github.com/WayfarerLabs/agentworks/actions/runs/35230586276/job/105233338071)
at `fe22cce0` reproduced a failure with the real installed client: the local peer received its SSH
banner, but the contaminated-descriptor case returned DEADLINE instead of observed client refusal.
Clean and alternate stdio-state cases passed. The fixture uses an owned loopback peer and never
reaches authentication, so no operator credentials were used.

The correction filters the two OpenSSH-private variables from the Windows child environment only.
Ordinary variables and the parent remain intact, with case-insensitive matching. Corrected native CI
and independent review must verify the same regression; the next full live report must establish
whether this fixes the original authenticated Windows case. Source plausibility and the local-peer
reproduction alone do not close that case.

## Outstanding acceptance

Windows delivery, unexercised workstation/platform combinations, non-Bash account-shell behavior,
provider-inner client policy, demoted native identity, and the remainder of transport's matrix need
measured evidence or explicit disposition. WSL2 and multi-node Proxmox were not exercised. Live
streams and terminals remain unavailable in the buffered candidate; required PoC cases cannot be
silently deferred to Phase 2. Transport owns joint acceptance.

No new credentials, destinations, provisioning or network mutations are authorized by these reports.
The existing integration tester owns the authorized inventory and cleanup. This SDD stays unlocked,
and no production-readiness or completed joint-proof claim is made.
