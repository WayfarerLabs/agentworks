# SSH PoC Evidence

Status: Buffered implementation and 98 local SSH tests pass; one native Windows status test is
selected for Windows CI. Joint live acceptance is pending.

## Revisions and delivery

- Governing merged transport design: #795, `857110df`.
- Transport candidate, preparation and shared vectors: #826,
  `81e5f6c3141c3988df8fe36bd0460a6fffd8c10e`.
- SSH implementation: #796, the commit carrying this record and subsequent reviewed corrections. The
  PR handoff records the exact pushed head; live reports must record their tested SSH commit.
- The operator explicitly confirmed that #796 carries design and the entire SSH PoC. The later
  implementation PR completes this same SDD. Earlier feedback suggesting a separate design merge is
  disposed by that direction.

PR #796 is stacked on the actual transport code dependency. The operator's tester may combine both
branches locally without either merging to main. Record transport SHA, SSH SHA, integrated SHA,
installation revision and any conflict resolutions in each live report. The candidate contract
remains transport-owned.

## Evidence boundary

The implementation uses explicit isolated policy and the shared buffered candidate without legacy
execution imports. Local tests exercise synthetic children, real local pipes, actual shared
preparation and its existing vectors. Installed OpenSSH option parsing is offline. Test-created
files are temporary synthetic fixtures; no operator credentials, trust state or live targets are
used. The [LLD](ssh-lld.md) and [test handoff](../../../cli/tests/execution/carriers/ssh/README.md)
describe the measured surfaces and reproducible commands.

Local toolchain observed: Linux workstation, Python 3.12.13, OpenSSH 9.2p1 Debian client. This is
not a server inventory or a live compatibility result. macOS/Windows execution, minimum-version live
behavior, platform-host and provider-inner clients, server/authentication combinations and
initial-image prerequisites remain unmeasured here.

## Local validation and review

At implementation commit `6cf987ca`, the complete non-integration Python suite passed with 10,167
passed and 7 skipped. Ruff lint/format, mypy, typer isolation, locked-SDD checks, rulesync drift,
Python/Node website tests and deterministic builds at both site bases passed locally.

Independent project and correctness reviewers found one material issue: positive Windows client
crash statuses were incorrectly interpreted as guest completion. The correction limits established
POSIX completion to 0 through 254, preserves other local statuses as unknown, and adds three
portable mapping regressions plus a native Windows process-status test. The complexity review found
no blocking complexity; its repeated path-validation simplification and stale checkpoint sentence
were corrected. After those changes, the focused SSH suite passes 98 tests with the one native
Windows case skipped on Linux. All three lanes re-reviewed `e57e02e4` with no outstanding material
findings. The full corrected suite passed with 10,170 passed and 8 skipped in 137.32 seconds. The PR
handoff records subsequent evidence-only commits and hosted results.

The repository file-lint gate on this pinned base fails solely on inherited transport-owned files at
`81e5f6c3`: Prettier reports `cli/agentworks/execution/README.md` and
`cli/tests/execution/README.md`, and cspell reports the dataclass conversion helper name twice in
the latter. Markdown lint passes. These files are unchanged by SSH. Transport subsequently fixed
them at `5b9977de`; SSH has not imported that update yet.

## Coordination hold

Transport #826 was rebased and advanced to `5b9977de3411f55828c6e1203ca4da00520ee877` during SSH
validation. In addition to bootstrap corrections and the README fixes, it removed `Failure.INPUT`
and `Failure.OUTPUT` from the shared candidate. SSH uses those values to distinguish pipe failures.
The current SSH head still pins `81e5f6c3`; combining it directly with the new transport candidate
requires disposition of that incompatibility. The SSH lead recommends retaining both values and has
escalated to the operator before changing the shared contract or silently remapping evidence. The PR
remains draft without `review-requested` while that coordination is pending. No authorized external
feedback/fix round has started.

## Outstanding acceptance

The operator's integration tester owns the authorized live inventory, credentials, workload budget
and cleanup checks. No live report exists in this record yet. Transport must evaluate the combined
proof against its [authoritative plan](../2026-09-12-transport-improv/plan.md), including native
delivery and cases beyond the current buffered candidate. An unsupported or missing case is a gap,
not permission to defer a required PoC case to Phase 2.

Record reports and dispositions here when available. Until then, the Phase 1 live-evidence and
acceptance checkboxes remain open; no lockfile or production-readiness claim is made.
