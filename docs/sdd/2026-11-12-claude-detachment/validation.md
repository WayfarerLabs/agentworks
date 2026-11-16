# Validation record

Base: `2253a6819`. Implementation and substantive corrections: `c5d5e37dc`, `873722634`.

## Independent reviews

Project, generic correctness and complexity lanes independently reviewed the same pinned states. All
lanes reported no remaining material findings at `873722634`. Corrections included shorthand
normalization for instance-only fields, the configured launch environment, native process identity
verification, and accurate persistence documentation. Complexity review removed duplicate state and
protocol values. A final fixture correction ensures malformed registry records reach the parser
rather than being excluded by their filename.

## Checks

- Ruff lint and formatting pass across source and tests.
- Mypy passes with the CLI project's configuration across 1,034 source and test files.
- Prettier, Markdown lint and spelling pass.
- Rulesync drift, locked-SDD protection and typer isolation pass.
- Focused subprocess probe, configuration, integration and lifecycle tests pass. Refusal preserves
  the runtime and pin, while explicit adoption changes only the Claude namespace.
- An isolated-HOME real CLI run accepts an ordinary Claude template and rejects the same template
  declaring `force_uuid`, reporting its file and field path.
- Website Node tests and deterministic double builds pass for `/` and `/agentworks/`. The initial
  Python website run had one browser keyboard timing failure; its focused seven-test rerun passed.
- Full non-integration CLI run: 13,078 passed, 35 skipped, 15 nested test cases passed, exit 0.
  Final probe and integration regressions: 118 passed, including the command-validation state
  boundary and numeric malformed-record fixture correction.

## Limits

No production Claude session or cloud VM was restarted. Native transcript and registry handling was
exercised through the actual generated login-shell command against isolated temporary files and
process metadata. Native registry identity fields were checked against the locally installed Claude
Code 2.1.319 binary. Live handoffs under the incident's version mix were not reproduced.

Optional detection data remains best-effort. Missing Python or unreadable records retain transcript
existence behavior. Without a transcript, forced background adoption requires verifiable Linux
process identity; missing start-token or PID-domain evidence produces an explicit failure.

## Rebase and publication validation, 2026-11-13

The operator authorized a rebase onto latest main, a ready PR, and monitoring with up to three
feedback/fix rounds. The rebase onto `3ad615680` produced `ad23efdb0`; range-diff confirms the three
implementation commits are unchanged. New main changes include native MCP naming, startup access,
and Typer 0.29.1.

All three independent lanes cleared the rebased change. Their focused runs passed 250 tests with one
skipped, 223 tests, and 419 tests respectively. Ruff, Mypy over 1,038 files, repository lint,
rulesync, locked-SDD protection, typer isolation, Python website tests (160 passed), Node website
tests (103 passed), and both deterministic double builds pass. The fresh baseline full CLI run
passed 13,217 tests with 35 skipped and 15 nested cases. The later existing-session correction has
separate validation below.

A separate local tester drove actual Claude Code 2.1.319 through native print/create and
print/resume against the repository's loopback provider fixture. Both calls exited zero with the
same session UUID. The fixture observed the resumed request carrying the first synthetic reply and
both prompts; the native transcript grew from 257,257 to 261,837 bytes at the same path. The actual
generated Agentworks probe found it, and exact adoption selected that UUID while preserving
unrelated state. Seeded continuation evidence caused refusal and preserved the pin; explicitly
forcing the original UUID suppressed the seeded pointer. This detector exercise was not a genuine
native handoff.

The real CLI accepted an ordinary template, rejected direct and inherited `force_uuid` template
settings, and emitted the schema marker. No VM backend, production sessions, real credentials, or
paid inference were used. The fixture stopped; the lead independently observed connection refusal on
its port and absence of the temporary native home and workspace. Actual left-arrow detachment and
live parked-job recovery remain untested.

### Readiness audit correction

A subsequent operator-path audit found that existing sessions have no supported way to set or remove
an instance spec: `--spec` is creation-only, start/restart accept no spec, and `resource edit` edits
YAML declarations rather than live session state. Earlier mechanism-level review results do not
establish usable existing-session recovery. Ready publication was temporarily held until a supported
editing surface was implemented. Independent review classified this as necessary within the existing
recovery scope; the implementation uses the retain/replace/clear model on named session
start/restart. Unlike agent reinit's empty-string alias, this session surface requires a JSON object
and clears only with `{}`. The operator's pending preference can still steer this choice. The
unmerged local lock is removed while this requirement is open.

### Existing-session recovery completion

`fd094a35c` added named start/restart `--spec`, with candidate validation before observation or
consent, persistence after consent, identity checks surrounding that interval, and one VM-family
guard reused through runtime composition. The CLI supports complete replacement, explicit `{}`
clearing, and omitted-option retention. It rejects bulk replacement. Guides and the command
reference describe setting and removing `force_uuid` while preserving other desired fields.

Private review corrections require valid JSON rather than treating an empty string as a clear, and
reject conflicting `force_uuid`/force-new intent before accepting any overlay or runtime target. The
optional local launch-config preflight keeps this policy in the Claude integration. All three review
lanes cleared `ae99f67ca`; their independent focused runs passed 118, 118 and 138 tests. The lead's
policy regressions passed 123 tests and Mypy passed across 1,041 files.

An independent real-CLI run at `ae99f67ca` passed 13 cases for set/retain/replace/clear and invalid
requests, including empty/whitespace input and both force-new conflicts. Invalid requests preserved
complete session snapshots. A stopped VM row without an address on unsupported WSL2 supplied an
intentional local reconciliation barrier, so accepted configuration changes returned failure after
saving desired state. No actual VM, transport or provider executable was used; all tripwires
remained untouched. The final instance-state query was empty, and the lead independently checked
absence of the owned home/baseline state. This proves configuration acceptance and refusal
boundaries, not guest runtime convergence. The corrected implementation's full final suite passed:
13,257 passed, 35 skipped and 15 nested test cases passed, exit 0. Its first run exposed an older
structural CLI test that explicitly prohibited the new start/restart option. Updating that contract
assertion required no production-code change; the focused 16-test run and complete rerun passed.

Final Ruff lint/format, configured Mypy, repository lint, rulesync drift, locked-SDD protection and
Typer isolation pass. Website Python and Node runs passed 160 and 103 tests respectively, and fresh
deterministic double builds passed for both site bases. The code reviewed and exercised by the three
lanes and local tester is `ae99f67ca`; subsequent closeout changes update only the existing CLI
option-placement test and SDD records. The PR handoff carries the SDD label and monitoring
allowance. Publication, CI and PR-level review remain operating duties tracked in the session goal
and PR comments, rather than implementation-plan completion claims.

## PR feedback round 1, 2026-11-13

[PR 905](https://github.com/WayfarerLabs/agentworks/pull/905) was handed off ready at 19:52:58 UTC,
head `5991ce9c9`, with all CI checks subsequently passing. The complete independent PR-level review
passed 584 targeted tests and found no material issue, plus one optional warning-text assertion to
remove. The published complexity review arrived during collection. The operator's existing allowance
covers this first round, begun at 20:53:09 UTC after the one-hour collection interval; draft state
was confirmed before any head changes. Main remained `3ad615680`.

Implementation `835861d1d` clarifies that forced addressing disables detection until removed, and
that absent history may simply mean no first prompt was submitted. The required suppression and
ambiguous-history policies remain intact. It removes the unused JSON Schema extension and mapping
scope hook while retaining the annotation, nested declaration enforcement, bounded tail fast path
and both independently necessary preflight checks. The reference-marker consumer rule is restored.

An isolated Claude Code 2.1.319 interactive UI reached its empty prompt through ordinary theme,
confirmation of the public fixture key, security notice and workspace trust. The native registry
held one matching interactive session with no parked link, while two observations found zero exact
or other project transcripts and zero provider requests. Normal exit returned 0 with no transcript.
Native resume of the same UUID returned 1 because no conversation existed. This proves successful
startup does not establish native history; no managed-guest restart was exercised. The native
processes exited, and the lead independently observed connection refusal on port 43625 and absence
of the owned home/workspace. No real credentials, paid requests or operator state were used.

Adversarial experiments verified the first session check prevents observing a replacement selected
before guard acquisition, while the transaction check protects the later preparation interval.
Direct harness start also needs its own force-new preflight even with existing transcript evidence.
New regressions cover both, malformed host probe output, and real Claude conversation/native-probe
failure stages. Warning frequency remains behavioral coverage; the authored-text comparison is gone.
A measured 11,850,142-byte transcript with a continuation in its tail required only 131,072
transcript bytes on that fast path, justifying its retention for this case. An ordinary restart
without a pointer, or a buried continuation, still requires a full scan after the tail read. No
supported guest predating Python 3.9 was found: the managed release registry is Debian 12/13, with
distro Python installed by initialization.

The isolated real CLI accepted an ordinary template, rejected direct and inherited forced UUIDs, and
rendered valid session-template schema after removal of the unused extension. Its temporary home was
independently confirmed absent. Focused tests passed 212 cases; Mypy passed across 1,041 files. The
complete suite passed 13,269 tests, with 35 skipped and 15 nested test cases, exit 0. All three
private lanes cleared implementation `835861d1d`; each independently passed 212 affected tests. The
complexity lane deleted the early session check, direct-start conflict check and host-output
validation together: all 12 corresponding regressions failed. Restoring the exact source passed
all 12. Ruff lint/format, configured Mypy, file lint, rulesync drift, locked-SDD protection and
Typer isolation pass. Website Python/Node runs passed 160/103 tests, and both deterministic double
builds passed.

The initial ready head's separate integration-test lane withdrew without starting when the PR
returned to draft; that report supplies no merge clearance. Its next ready transition starts a fresh
cycle. Final closeout review, pushed-head handoff, CI, external validation and required team
approval remain recorded by the PR workflow; these local results do not claim those duties finished.

## PR feedback round 2, 2026-11-13

The next review confirmed closure of both earlier material findings and identified one candidate
omission at `f7be90d23`. A parked-job candidate prevented the fallback scan from discovering a
different continuation buried before the transcript tail. Both versions refused launch and preserved
the pin, but the recovery hint omitted the buried continuation UUID. Independent actual login-shell
probes reproduced tail-plus-parked (two candidates), buried-only (one), and buried-plus-parked
(incorrectly one). The minimum correction returned both in the last case without changing the other
results or the one-roundtrip contract.

The one-hour collection minimum elapsed at 22:09:40 UTC. No additional integration findings or
status response had been published after the status check at 22:01; external validation remains
pending rather than being counted as clearance. Draft state was confirmed at 22:10:06 before edits.
This is round 2 of the existing three-round allowance. Main remained `3ad615680`.

Implementation `1d92957f3` removes only the parked-candidate condition from the fallback decision.
The existing independence test now covers tail and buried continuations; it produced one pass and
one failure against the prior source, then passed after correction. Extra cases cover a
string-valued parked-job list and a forced background conversation with no transcript emitting only
the force warning. A leftover mapping local is removed. The accepted warning policy and FRD are
unchanged.

The architecture and prior-art notes now state the actual cost boundary: a tail continuation can
avoid reading the full transcript, while ordinary history and buried continuations require a full
scan in addition to the initial tail read. The retained path is bounded in memory; no claim is made
that it saves reads on ordinary pointer-free restarts.

Focused tests passed 216 cases; Mypy passed across 1,041 files. The complete suite passed 13,273
tests, with 35 skipped and 15 nested test cases, exit 0. All three private source lanes cleared
`1d92957f3`, independently passing 216 affected tests each. Reintroducing the scan shortcut,
removing the list check and bypassing the forced guard made all four added cases fail; restoring the
source passed all four.

Ruff lint/format, repository lint, rulesync drift, locked-SDD protection and Typer isolation pass.
Website Python and Node runs passed 160/103 tests, and both deterministic double builds passed.
Final closeout review, pushed-head handoff, CI, external validation and required team approval are
tracked by the PR workflow. These local results do not supply external integration clearance.
