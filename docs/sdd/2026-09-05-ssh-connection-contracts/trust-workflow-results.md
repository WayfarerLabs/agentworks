# Trust Workflow Proof Evidence

This SSH-owned record tracks the composed trust proof for Phase 2. It supplies no new requirements
and does not replace the [plan](plan.md) or production creation/publication acceptance.

## Installed-client fixture composition

The developer's `9500cf4eea8646de42778d76f2d28e7547f0b791` adds four integration-marked cases. The
existing enrollment server is extracted into one ordinary owner shared by enrollment and maintenance
tests, without replacing the separate byte-delivery fixture. Its explicit server policy disables
user SSH RC execution. The cases remain Linux loopback fixtures with generated keys and fixture
creation provenance, not production provider bindings.

The composed sequence enrolls a creation candidate, publishes that candidate with complete
applicable policy through the actual maintenance CLI, and executes strictly through a managed
reference. It then blocks admission, fails a partial refresh, verifies learned-key and
prior-generation retention, repairs explicitly and reconnects. The same carrier observes fresh
policy at every step. Command markers distinguish refused effects from successful execution.
Separate cases use real hashed known-hosts records with the selected alias/nondefault port and
refuse a different lookup identity. Source bytes/timestamps, candidate metadata and partial
publication evidence are checked separately.

Project, complexity and generic private source reviews clear the exact developer pin. The developer
records **98 non-integration tests passed with 1 skip**, four native cases collected, full
Ruff/format, strict mypy (1,225 sources) and file quality passed. The lead integrates the unit at
`a5c6407bf70faeac79c63381f5d2b60b432abc8d`, independently passes **71 affected non-integration tests
with 1 skip**, collects all four new cases, and passes full Ruff/format and strict mypy. The
different focused counts reflect different selections, not native execution.

**Native cases have not run.** Collection starts no server and generates no credentials. These
results do not establish real hashed matching, actual authentication offers, native macOS/Windows
trust custody, genuine provider creation receipts, production publication/writer coexistence,
rollback or additive RunContext use. Those remain required gates. The later rebase onto transport
`833f1c280cc67f8d9b8f71f2e229ab69d20f7df5` carries this unit as `a975ce65f`; fresh combined
validation follows the next actual resize/native-primitive integration batch.

## Authentication offers and native fixture custody

The developer's `c335e81ce603c25392dca7af0a28fb2bd12d1d18` supplies six Linux integration cases.
They compare full key algorithm/blob observations from the owned server's authentication packet log.
They cover inherited-agent exclusion, explicit selection of one public identity from a multi-key
agent, matching versus stale sibling public keys and refusal to offer an automatic sibling
certificate. Positive observed offers distinguish actual authentication from acceptance alone;
unrecognized log syntax fails observation rather than becoming an absent-offer claim.

The source-pure fixture worker enters passively and admits native work through an explicit guarded
start. One retained worker owns each server/agent construction, borrowed lifetime and cleanup.
Synthetic interruption cases cover delayed startup, construction, completion waiting and cleanup
ordering. These helpers create no native resources at import or collection. Native execution
requires a tester-owned account/home or independently verified startup-free configured shell;
overriding HOME alone does not isolate OpenSSH's account-derived configuration.

All three independent source lanes clear the combined private pin
`fedaf2e8e9339d5212405f2d0ae69956fc67975e`, including these fixtures, installed POSIX terminal
composition and bounded Windows diagnostics. The lead measures **415 non-integration SSH tests
passed with 6 skips**, full Ruff/format (1,283 files), strict mypy (1,246 sources), selected Windows
typing and file quality, all exit 0. Six authentication and two POSIX terminal cases collect without
execution. A subsequent optional cleanup removes an unnecessary terminal fixture grouping marker.
Rebase onto transport `75a59e15a` at `60c1fa3843c6ad45433da92ffba83349a8b1a968` preserves the entire
SSH source/test tree after that cleanup. Fresh combined gates follow that dependency change.

The complete suite at that rebased pin passes 14,801 non-integration tests with 51 skips (exit 0).
Full Ruff/format, strict mypy (1,251 sources), typer isolation, file quality, locked-SDD, Rulesync
and website gates also exit 0. The [terminal record](terminal-results.md) preserves the exact gate
scope and the separately failed native Windows run requiring new diagnostic evidence.

**The new native cases have not run.** No default account-configuration isolation, physical keyboard
behavior, successful native offers, provider creation/publication, writer coexistence, rollback or
RunContext acceptance is inferred from their source review or synthetic gates.

## Isolated CA, KRL and source-rollback fixtures

The developer's `8634ef40cf459654042d43b727c4a2c0c2a08525` completes two new Linux
integration-marked fixtures. The CA case changes applicable authority policy while retaining the
served replacement certificate. It distinguishes a source-file update from an explicit managed
refresh, then checks that source rollback, stale writer revision and import over an existing bundle
do not discard the retained complete policy. The KRL case publishes a learned creation candidate,
revokes the actual served host key, preserves the previous snapshot before refresh, retains blocked
admission after a failed update and repairs with the complete retained KRL and learned key.
Restoring the old source bytes and timestamps does not restore an earlier managed generation.

Every application attempt runs in a fresh isolated interpreter with retirement modules unavailable.
Each refused invocation has a unique effect marker and is issued once. Refusal requires the complete
expected upstream client sequence, status 255, the conservative observation failure and no
application output. KRL evidence additionally matches the served key's SHA256 fingerprint and
selected policy path. Marker absence alone or a generic 255 cannot substitute for evidence of the
expected cause. Production carrier behavior still does not parse client diagnostics.

Private review corrected the original generic-status classification and destructive controller
timeout. The fixture now retains its admitted controller through cooperative interruption and SSH
settlement; it does not kill that controller or its process group while borrowed fixture resources
remain in use. Its 15-second observation expiry remains a failure even after a late successful exit.
Cleanup has no finite completion guarantee; the owning test process or job supplies the hang bound.
Controller output is discarded. A bounded report retains only validated classification, pass
results, byte counts and retirement-import observations, excluding raw client diagnostics and key
contents.

The separate source-pure probe suite passes 25 cases at the developer pin. It exercises unrelated or
incomplete refusal evidence, incorrect KRL fingerprints/paths, malformed and oversized metadata,
privacy limits, late success, interruption faults and borrowed-resource settlement ordering. The
developer's wider synthetic selection passes 122 cases with 1 skip; native collection finds both new
cases without executing them. All three independent project, complexity and generic source lanes
clear combined pin `c0d1beadbc0448a330b2542b4aa2967ec435938e`. Its rebase onto transport
`0be8850447277adf8fce68bb6c887288251dfe67` changes no SSH source or test bytes relative to the
resolved pre-rebase pin `e989974a6`.

The lead's complete local CLI suite at that combined code/test pin passes **15,173 non-integration
tests with 51 skips and 27 warnings**, exit 0 in 256.68 seconds. Full Ruff lint/format (1,295
files), strict mypy (1,258 sources), selected Windows fixture typing, typer isolation, file quality,
locked-SDD, Rulesync and whitespace checks exit 0 at the reviewed tree. Website validation passes
160 Python and 103 Node tests, four builds and both deterministic comparisons, all exit 0. The
owning suite has settled; an independent same-user process cwd/descriptor scan finds no references
before removal of its exact scratch root. These checks do not execute the integration-marked cases.

**Both new native fixtures have not run.** Source review and synthetic evidence do not establish
installed-client CA/KRL acceptance, physical or supported-workstation behavior, genuine provider
creation/publication, concurrent production writers, persisted-config rollback or additive
RunContext delivery. Their owned loopback server and fixture creation identity cannot stand in for
those production gates. No completed checkbox or public final-product feedback round follows from
this increment.

## Local persisted-configuration restoration

The bounded regression `cli/tests/execution/carriers/ssh/test_config_rollback.py`, added on base
`e38f5fcff35bd85d099129fdbe5dd8480017c007`, uses isolated persisted TOML and the actual config and
managed-trust APIs. Two cases load the original configuration, deliberately add `[operator.ssh]`,
import complete multi-file policy, then refresh with an additional retained learned-key fixture and
updated binary revocation bytes. One case also fails a refresh with an unavailable revocation file.
The fixtures are opaque custody inputs; no installed client interprets them and no enrollment or
provider creation is claimed.

Restoring the original TOML bytes restores all legacy operator fields and leaves new settings
absent. Both cases reopen config and the explicitly retained bundle in a fresh interpreter that
refuses retired SSH, transport, runner and database imports. Reopening preserves the entire managed
file snapshot, including the current manifest, earlier generations, learned-key/revocation bytes and
partial failed-update evidence. The active case admits the newer complete generation; the failed
case remains blocked. A stale writer using the initial generation refuses without changing state.
Re-adding settings for the same bundle preserves current policy; the blocked case requires explicit
complete-policy forward repair. Original legacy identity, aliases and trust files remain unchanged
throughout.

The developer measured **2 new cases passed**, then **168 focused non-integration tests passed with
1 skip**, both exit 0. The focused selection includes the new regression, config settings, managed
trust, interruptions, enrollment and migration probe tests. Changed-test Ruff lint/format and strict
scoped mypy (one source file) pass, as do the two affected documents' pinned Prettier 3.8.3,
markdownlint and spelling checks. These are local API/persistence observations on Linux, not native
Windows or macOS evidence.

**All previously pending native fixtures still have not run.** This increment establishes only the
local persisted-configuration/state boundary described in the
[configuration LLD](configuration-lld.md). It does not establish actual old/new delivery,
installed-client CA/KRL acceptance, production writer ownership/coexistence, genuine
creation/publication, production rollback or additive RunContext use. Transport composition and
operator-owned native acceptance remain open; no completion checkbox is changed.

### Private review correction: partial publication custody

Review of `265c08b21ae30366272e458c7c054cc7844a4362` found that capturing the bundle snapshot only
after a failed refresh did not prove a partial generation existed. The regression now identifies new
generation directories created by that failed refresh and requires retained copies of every complete
known-host input before capturing the snapshot. The existing restoration and forward-repair checks
then prove those partial bytes survive. Redundant pre-restoration checks of the initially admitted
generation were removed; the dedicated managed-trust regression already covers them.

A temporary isolated test plugin deleted only the failed publication directory when the missing
revocation source raised, then propagated the error. The original regression passed both cases (exit
0); the corrected regression failed at the new partial-evidence assertion in the blocked case, with
the active case passing (exit 1). Production trust source hashes remained unchanged. Without the
mutation, the corrected focused selection again passed **168 tests with 1 skip**, exit 0.
Changed-test Ruff lint/format, strict scoped mypy and both documents' pinned formatting, Markdown
and spelling checks pass. The original measurements above remain historical evidence; this
correction adds the missing custody guard and does not expand the local acceptance scope.

All three independent private source lanes clear corrected combined pin
`81271915ea9487c1d68957734dd2f0e2d14123a7`. The lead's first integrated pin
`ee3da60ee35f5ea01dfc7042f420a941af07c0de` passes the full suite: **15,770 non-integration tests, 51
skips and 27 warnings**, exit 0 in 240.82 seconds. After the assertion/deletion correction, the
lead's focused selection passes **168 tests with 1 skip**, exit 0. Final full Ruff/format (1,315
files), strict mypy (1,278 sources), scoped Windows typing (one file with silent imports), exact CI
typer isolation, locked-SDD and Rulesync checks pass. Fresh website checks pass 160 Python and 103
Node tests, four builds and both deterministic comparisons. The corrected focused run is not a
second full-suite run. Settled fixture/output roots are independently checked before removal; logs
remain retained. These reviews and gates concern this private persistence increment, not full PR
approval or native/production acceptance.

## Native Linux CA and KRL observations

The two migration test bodies described above now have scoped native observations at
`a920eceb368dcfd031939015939d3d6501ea494f`. This supersedes their earlier unexecuted status for
Linux OpenSSH 9.2 only. A tester invoked the unchanged original test functions against sequential,
fixture-owned loopback servers using the actual SSH carrier and managed-trust implementation. Each
application attempt used its original isolated interpreter with retirement imports blocked; all
eleven observations confirmed the expected checkout's SSH, trust and shared process modules. No
production result, assertion, application command or operation deadline was substituted.

The first CA attempt stopped before server startup: the generated signing key had mode `0640`, which
OpenSSH refused. All four original child handles settled; the failure remains retained. A separately
reviewed fixture preparation correction set the three owned private keys to `0600` before signing,
then one fresh invocation passed both original test bodies. The scratch runner's SHA256 is
`43aa27010686f9fa3283e14f242af459837029b4ff63ed1a8c61a9add104c53b`. The helper now explicitly
restricts each newly generated private key immediately after generation, before signing or server
use. That source correction is separate from the native observation pin.

- CA policy: two complete unknown-CA refusals followed by two accepted commands; explicit refresh,
  retained generations, source bytes/timestamp rollback, stale writer and re-import refusal all
  passed. The application marker contained exactly `xx`; no refusal marker existed.
- KRL policy: two accepted commands, three blocked admissions and two native refusals identifying
  the actual served key and selected KRL. Learned trust, failed partial-update evidence, blocking
  across source rollback and complete forward repair were retained. The marker contained exactly
  `xx`; no refusal marker existed.

The installed client was OpenSSH 9.2p1 and server OpenSSH 9.2 on Linux arm64, Python 3.12.13. The
tester used only owned keys and loopback listeners, an environment limited to PATH/LANG, and
effective server configuration with PAM and user SSH RC disabled. An owned `ZDOTDIR` startup hook
positively confirmed isolated shell startup before each body. No operator home, configuration,
database, credentials, account, service, VM or cloud resource was changed.

Cleanup evidence has an explicit limit. Thirty parent-owned original process handles had closed
pipes, known exits and independent same-execution `ECHILD` observations. Both listeners rebound; the
lead independently confirmed both disposable fixture paths were absent. All original product
custody-close assertions passed. However, the isolated controller's profile observer missed SSH
constructors launched through raw `_thread` threads. Its empty child lists do not prove those
children absent. The original receipt's broad `native_settled=true` fields remain preserved but are
qualified by a separate record stating that independent SSH grandchild and aggregate cleanup are
unmeasured. No native retry was performed to improve that observation.

These results establish the recorded native CA/KRL behaviors, not live CA reload, minimum-client or
macOS/Windows coverage, genuine provider creation, production writer coexistence, persisted
configuration rollback or usable RunContext delivery. Full R1-R5 and integrated acceptance remain
open; no completion checkbox, SDD lock or retirement claim follows.

## Native Linux authentication offers

All six original authentication-offer cases now pass at `526b6e9b400aece54ec03152bc870738081f016e`
through installed Linux OpenSSH 9.2 servers and agents. This supersedes their earlier unexecuted
status for this client/workstation only. The test bodies, full wire algorithm/key comparisons,
operation deadlines and native owners were unchanged. The fixture's eighteen generated private keys
were measured as `0600` without a permission overlay, exercising the source correction recorded
above.

The inherited-agent cases and rejected explicit-agent case each observed one query and no signed
attempt. The authorized explicit-agent, matching/stale sibling and sibling-certificate cases each
observed one query and one signed attempt. Every case offered exactly the selected ordinary key; no
unrelated key or certificate was offered. The stale sibling's second call refused before dispatch
and emitted no additional native client or packet. Each agent held both specified keys, checked by
the original inventory assertion; successful authentication alone was not the verdict.

Each server used the independently verified owned startup guard described above. Its positive
startup proof log was preserved separately, then the actual owned diagnostic descriptor and file
were reset to offset/size zero before the original case. Complete case packet logs were not
filtered. The scratch runner's SHA256 is
`b2541ad574c4caf11fc9c8421c1c711751eb261b40a392b3f27cbdd79839bdc4`.

A transparent recorder retained original process objects across raw threads. All 73 direct native
handles had known statuses, closed pipes and independent same-execution `ECHILD` observations; this
includes the actual carrier clients. All six original custody closes returned true, listeners
rebound, and exact owned agent sockets/roots and disposable fixture paths were absent. The lead
independently audited the receipts and path absence. Server-internal descendants were not audited;
the earlier CA/KRL cleanup gap is not retroactively closed by this separate run.

The new-path interpreter refused six retired import roots and recorded the expected checkout's
SSH/client/connection/shared-process origins. No operator state or credentials were accessed and no
native retry occurred. Default/system configuration placement, other platform agents, minimum
OpenSSH 8.5, macOS/Windows, provider hops and complete RunContext/R1-R5 acceptance remain open.

## Native Linux trust maintenance and hashed lookup

The four original trust-workflow cases now pass at `526b6e9b400aece54ec03152bc870738081f016e`, using
the actual in-process CLI app and installed Linux OpenSSH 9.2. This supersedes their earlier
unexecuted status for this workstation/client. Both ordinary and alias workflows retained
enrollment/recovery evidence, refused ordinary use of an unpublished creation receipt, published
complete learned/original policy and KRL, blocked use, retained a failed partial refresh, repaired
explicitly and reconnected. Each application marker contained exactly `xx`. The two real
hashed-policy cases accepted only the selected alias/port lookup; a different lookup returned native
status 255 without another marker effect, leaving each marker exactly `x` and the policy
bytes/timestamps unchanged.

The eight original CLI invocations were observed without replacing their app or commands: each
workflow's refresh, block, deliberately failed refresh and repair returned respectively zero, zero,
`StateError` with exit one, and zero. All original assertions, finite deadlines, native owners,
retained generations and partial learned-policy checks remained active. The runner's SHA256 is
`60374fe65fa75a9f497d7a9109ffb102a4a8a00b397e857c2f5c9c5de9e13404`.

Private HOME/XDG directories and state guards were installed before CLI imports. The bounded Python
audit observed no operator/default configuration, database-service or legacy-delivery attempts; the
private CLI root retained only its four empty initial directories. CLI registration imports were
allowed during coexistence. This is not a whole-CLI import-independence or kernel audit. Function
profiling does not cover arbitrary raw-thread calls; the transparent native launch-stack recorder
separately covers the actual observed raw-thread SSH constructions.

Each server's positive startup proof used a separate owned host pin, preserving the original
managed/hashed test policy. The owned shell-startup guard, loopback-only listening and disabled PAM
and user SSH RC were verified before each original body. The twelve generated private keys were
already `0600`, with no permission overlay. All 62 original direct native handles had known exits,
closed pipes and same-execution independent `ECHILD` observations. Original custody closes
succeeded, all four listeners rebound and disposable fixtures were removed; the lead independently
audited those receipts, fixture absence and the empty private CLI directories.

No retries or source changes occurred in this native run. Server descendants, the external `agw`
entrypoint, genuine provider creation, production writer coexistence, persisted operator-config
rollback, other workstation/client versions and full RunContext delivery remain unproved. These
local fixture observations do not close full R1-R5 or integrated acceptance.

## External trust-maintenance CLI entrypoint

A separate eight-command observation at public `1cfdd832c6f92289ae41f646ece978b61aaa8502` now
exercises the actual `cli/.venv/bin/agw` executable on Linux arm64 with Python 3.12. This closes the
external-entrypoint gap only for the explicit owned-path file-state operations measured here. The
prior in-process/native observations retain their original scope and pins. The inputs are opaque
policy and binary revocation snapshots; no OpenSSH process parses them in this observation.

Import, describe, complete refresh, block, missing-source refresh failure, stale-generation refusal,
complete forward repair and final describe produce external exits `0,0,0,0,1,1,0,0`. Every fresh
console process positively records the reviewed observer hash, actual checkout module origins, one
real entrypoint call and the expected maintenance function. Both failures reach the actual
entrypoint as `StateError` and become `SystemExit(1)`. No app, handler or result is substituted; no
prose wording is asserted.

Independent file checks verify complete source/revocation bytes and manifest hashes, three distinct
successful generations, blocked current policy after the failed refresh and retained partial copies
of both known-host inputs. Stale refusal preserves the recorded bundle file hashes and timestamps,
excluding `.lock` and directory metadata. Forward repair unblocks a new complete generation while
retaining every previous generation and partial publication byte and timestamp. Original sources
remain unchanged.

The reviewed runner's SHA256 is `4a337363f15749935c509a4fbaa1fdeae78a215e5ea5d035b32de4df18c283bc`;
its additive observer is `666d626123a40116a129c6a10fe1a32d03c122ad9eb14a933224bc2216bcf0d3`. The
complete signed report is `e2eed89ce6f2b9962040281b7ff227cda7024aac71b40f696ec3449c27c0ced1`, and
the original aggregate receipt is
`f88604621997407d43fb47d00f5706b3ad7a2304e36947c73ec171a693dca1eb`. The lead fully reads both
records, checks every individual observer receipt against the aggregate and independently confirms
fixture absence and four empty private CLI directories.

All eight original console process handles have known exits, closed pipes and independent
same-execution `ECHILD` observations before disposable-state removal. No timeout, signal, retry,
SSH/keygen/server/agent process or network endpoint occurs. Private HOME/XDG directories precede app
startup; bounded Python audit/profile hooks observe no guarded operator/default-config, database,
socket or extra-subprocess attempts. These hooks do not establish complete kernel or arbitrary
raw-thread coverage, and allowed CLI registration imports do not establish whole-app independence.

This observation supplies no native SSH trust interpretation, production creation or writer
ownership/coexistence, operator rollback, other platform, RunContext or full R1-R5 acceptance. The
full implementation and retirement gates remain open.
