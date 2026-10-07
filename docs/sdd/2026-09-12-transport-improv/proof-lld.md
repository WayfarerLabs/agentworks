# Transport Boundary Proof

Status: Joint buffered PoC accepted. Design reconciliation, broader interfaces and production gates
remain open.

## Delivery and ownership

The operator authorized transport-side PoC implementation after #795 merged. This is the
implementation vehicle for that work, not a design prerequisite for SSH to start. The SSH owner is
updating #796 and supplies the independent SSH contribution. Transport owns integration of the joint
proof, shared types, preparation, outcome interpretation and acceptance cases.

The merged [execution contract](execution-contract.md) remains the governing design. The first
implementation is deliberately the buffered subset. `carrier.py` is the executable candidate;
neither its existence nor green local tests closes the [proof matrix](plan.md). Proof findings amend
the candidate before broad parallel implementation.

Production factories, RunContext, consumers, connection/trust state and the old stack are unchanged.
There is no public old/new selector. File operations, jobs, live streaming, terminal attachment and
the complete permission-scoped public API are not part of this slice.

## Candidate carrier boundary

`PreparedInvocation` owns literal bootstrap argv, never stdin. The unused diagnostic-label field was
removed from the buffered candidate; no carrier consumed it. `CarrierIO` owns exactly one input, EOF
or immutable finite bytes, and capture or discard output. Its sensitivity is the union of request
and input sensitivity; an input marker cannot be cleared. Finite bytes need no borrowed stream or
close operation. The later live-source/sink shape remains unimplemented until bounded cancellation,
short-write and borrowed-stream tests establish it.

`Deadline` carries one monotonic expiry, or an explicit absence of a time limit. Carriers do not
renew the budget while polling. `CarrierReport` separates dispatch evidence, remote command-chain
completion, local process status, raw output provenance/completeness/retention and a closed failure
code. Payloads and captured bytes have no diagnostic representation; provider exception text is not
a diagnostic. Interruption propagates after local cleanup, never as successful execution or
confirmed cancellation.

The 2026-09-17 live run observed surviving bootstrap descendants and the ordinary guest workload
after local deadline expiry through both carriers. The operator accepted deferring guest
cancellation from this PoC. The implementation intentionally has no remote reaper or cancellation
handle; a caller must not retry on expiry as if the earlier work had stopped. Shared owned workload
lifecycle/cancellation remains a pre-production gate, separate from this local deadline.

The buffered candidate retains distinct input-delivery and output-collection failures, consumed by
the SSH pipe implementation. They are not remote exit classifications. Expected early consumer
closure needs to be distinguished from incomplete required delivery, and capture-limit exhaustion
retains its separate code. An I/O failure must not erase independently observed invocation
completion, promote incomplete streams to complete, or imply cancellation/replay permission.

An observed exit can belong to an account shell that refused before running the prepared bootstrap.
It does not independently establish bootstrap execution or a nested application's outcome. In
particular, an SSH local status of 255 remains ambiguous. Output parsing does not introduce an SSH
completion guarantee.

## Shared no-staging preparation experiment

The initial test lane is Linux. It investigates an explicit Bash bootstrap with base64 encoding,
using pipes and `/dev/fd` rather than temporary files or an installed guest helper. It does not
assume Python is available during native recovery or readiness. Bash, base64 and descriptor support
are prerequisites to measure, not claims about every supported target.

Bash 5.1 is the mechanism minimum for saving and waiting on each process-substitution PID;
[the Bash maintainer's explanation](https://lists.nongnu.org/archive/html/bug-bash/2024-07/msg00044.html)
distinguishes it from earlier, narrower wait behavior. Local tests measure Bash 5.2.15. The second
live report below separately measures Bash 5.1.16 with coreutils 8.32; compatibility is not inferred
from executable presence or that upstream explanation.

Application argv, script source, environment, directory and finite stdin travel in an ASCII input
envelope. The bootstrap argv is fixed implementation source, not caller source or secret values.
Application script source and application stdin use distinct descriptors. Fixed `sh`/`bash` and the
actual destination account's supported default shell are separate choices. Login/interactive startup
combinations require their own proof and must not be silently accepted.

The parent observes source/stdin producer termination and output encoders before returning. An
unexpected producer failure emits bootstrap-failure evidence and returns 125, even if the payload
exited zero; the report still records remote command-chain completion, not a nested exit oracle.
Early consumer closure may produce SIGPIPE and is allowed without claiming all input was consumed.
GNU env's `--default-signal=PIPE` is an explicit prerequisite of this experiment. Local fault
injection kills only decoders descended from the fixture's bootstrap and checks that partial
delivery cannot be reported as complete.

Guest stdout and stderr are separately armored into a shared record stream. Raw carrier stderr
remains diagnostic or mixed provenance. Framing validation, output bounds and end markers must
establish complete guest streams before interpreting them as such. Invalid or incomplete framing is
a failed proof, not an empty successful command. The envelope does not carry a new exit-status
oracle; the carrier's actual completion evidence describes only the remote command chain.

Sensitive execution suppresses workload output in the bootstrap and retained carrier output.
Suppression is not permission to retain raw sensitive frames for diagnostics. A suppressed stream
must be reported as suppressed rather than as a decoded empty guest stream. Suppressed or discarded
bytes provide no framing evidence. Neither raw zero nor absent retained output alone proves
application success. The eventual shared public-result interpreter must honor that distinction; this
internal buffered proof does not implement or waive it.

The sensitive conformance case reflects synthetic input/output and deliberately exits 37. Requiring
that distinctive exit alongside suppression rejects a shell that consumes input and exits zero
before bootstrap. A local process regression exercises that refusal. This is controlled-case proof,
not authentication against arbitrary shell startup behavior. Earlier live reports used the original
zero-exit vector; the final-candidate report below measures the strengthened case through both
carriers.

## Native adapter placement

The candidate native carrier lives at `execution/carriers/proxmox.py`. Importing the proposed
`plugins/proxmox/execution.py` location currently initializes `plugins/__init__.py`, which eagerly
imports and registers all installed plugins and reaches legacy execution modules. Making only
`proxmox/__init__.py` lazy would not establish independence.

For this proof, the independent carrier owns a small explicit PVE wire boundary and imports no
plugin package. It receives already-resolved connection inputs, performs no discovery at
construction, and is not wired into production. Moving provider-owned execution into the plugin
package remains part of the existing dependency/cutover gate after plugin initialization is
disentangled. No plugin registration redesign is smuggled into the proof.

PVE wraps QGA input/output in JSON strings. The shared ASCII envelope avoids claiming that a Python
UTF-8 re-encoding recovers arbitrary original guest bytes. Native output is still raw carrier data
until the shared decoder validates its framing. Provider and local capture limits remain explicit.
One dispatch is allowed; a lost acknowledgement or failed status observation never causes replay.

Each native HTTP request runs in an owned workstation-Python worker. Connection authority and
request contents arrive on its stdin, never argv. The parent applies the remaining total budget,
kills/reaps the worker on timeout or interruption, and never retries the request. This bounds DNS,
TLS and response observation, not just socket inactivity. It does not cancel an already-submitted
guest process. Redirects and environment proxies are disabled; TLS verification is mandatory. An
explicit cluster CA bundle provides an alternative to the connection type's default trust without
disabling hostname verification. Use a certificate-matching API hostname, not a server-name
override. The wire worker caps a response at 8 MiB, including the JSON envelope. This accommodates
the maximum bounded directory inventory after framing without adding paging or guest scratch. Native
acceptance must still prove the complete response through supported PVE/QGA versions. PVE input is
capped at 65,536 ASCII bytes; whole-request acceptance, including bootstrap argv, remains a
live-test measurement rather than a guessed limit.

Preparation caps the complete encoded input envelope at 262,144 bytes. Source, argv, environment,
cwd, framing and application stdin share that budget; neither encoded limit is a raw-stdin
allowance. A sh script containing `/bin/cat`, with no env or cwd, permits 196,554 raw stdin bytes at
preparation and 49,098 for native delivery. Literal `/bin/cat` argv instead permits 196,551/49,095
bytes. These examples were measured locally, not promised for other compositions. The final
envelope's byte length is the exact preflight quantity. Carrier rejection above its bound occurs
before dispatch; this PoC does not implement the production bounded-transfer/spooling mechanism
required by FRD R4.

## Test scope and evidence

Local tests may run bounded subprocess fixtures with synthetic data in session-owned temporary
directories. They neither use operator credentials nor access operator VM/configuration state. They
prove local mechanics only, not SSH delivery or live PVE behavior. Independence must also be tested
in a fresh process with the retirement modules unavailable; ordinary pytest startup imports legacy
fixtures and cannot stand in for that check.

The operator identifies the existing integration tester as the live-evidence lane. That tester can
combine pinned transport and SSH commits in a disposable local branch without merging either on
origin. Record both input SHAs, the resulting integration SHA, any conflict resolutions and the
installed revision before testing. A conflict that changes the shared contract returns to transport;
it is not a tester fix that bypasses review. Use the tester's authorized inventory and resource
budget, with explicit execution/elevation scope, bounded workloads and independent cleanup checks.
This session does not provision a separate bed or infer new create/delete permission.

`tests.execution.conformance.check_buffered_contract(carrier)` runs measured Linux vectors through
an explicitly supplied carrier. It resolves no credentials and opens no connection on import. Run
the same vectors for the SSH and QGA constructions, then their fault-injection and interruption
lanes. Report transport-only results separately from combined-tree evidence. An affected code or
contract change invalidates the corresponding prior observations and requires retesting.

The live reports below establish the measured native and SSH cells. The later reports close the
measured Windows failure and add explicit destination-account default-shell evidence, separately
from the eight fixed-interpreter vectors. The final-candidate report measures the strengthened
sensitive vector and affected macOS drain behavior. The subsequent macOS delta report verifies the
SSH-owned socket-fixture correction and a clean local suite. Broader shell/startup and
identity/elevation coverage remains unproven. Live I/O is not implemented by the finite-input slice
and cannot be enabled without its separate ownership proof.

### Joint buffered proof acceptance (2026-09-17)

Transport accepts the joint finite-input proof at transport `931ad8ef` and SSH `bc2a0711`. The
lead's disposable local combination is `2b1f1d39aff70ec2f0c148d2a9d34dedde9d39a5`, without conflict
resolutions. Its entire `cli/` tree is identical to the tested SSH candidate, and its
`cli/agentworks/` tree is identical to live-tested `1ccc304b`. The final transport delta from
`e41a4482` changes only this evidence record and the plan. Closeout documentation does not create
new runtime coverage or invalidate those comparisons.

The
[native delta disposition](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5718672799)
explicitly carries its evidence forward and says no live retest is warranted. The
[SSH/macOS report](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5718460281)
already covers the combined implementation and the affected fixture retest. Requesting a further
tester acknowledgment of a documentation-only local combination added no coverage; it is not an
acceptance gate. Four authorized feedback rounds are complete, with no unresolved material finding.

| Proof row                             | Applicable evidence and boundary                                                                                                                                                                                                                                                                                                                      |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Literal execution and shell bootstrap | Shared vectors preserve literal arguments, fixed interpreters, env and cwd. The lookup report measures account-default selection on SSH and QGA and unsupported-shell refusal. Execution uses the explicit connection identity, with no implicit sudo or demotion; this API exposes no identity-switch request. Login/interactive startup is refused. |
| Source and finite input               | Shared vectors separate script source from stdin, preserve finite bytes and establish EOF. The revised sensitive case reports exit 37 with no retained bytes in all six final live cells; local mutation tests reject suppression without payload execution.                                                                                          |
| Guest streams                         | Shared vectors and byte-range tests validate separate binary guest stdout/stderr framing. Raw carrier diagnostics remain distinct or mixed; discarded/suppressed output is not decoded guest-output evidence.                                                                                                                                         |
| Outcomes and observation              | Exits 0/1/255, bounds, deadlines and interrupted observation retain observed facts. Fault tests cover lost observation and local cleanup without replay. SSH 255/drop ambiguity and lack of remote cancellation remain explicit.                                                                                                                      |
| Readiness                             | Both carriers execute the shared bootstrap without helper installation or staging; reports record no observed carrier staging. Account-shell prerequisites are documented separately, not a guarantee against arbitrary startup hooks.                                                                                                                |
| I/O ownership and failure             | Immutable bytes/EOF have no borrowed stream lifetime. Local SSH tests cover duplex pressure, EOF, short writes, input/output failures and descendant-held pipes; native tests cover owned-worker interruption and kill/reap. Live-source/terminal ownership remains required before advertising those modes.                                          |
| Non-SSH shape                         | Real QGA exercises the same buffered boundary on PVE 8/9, including TLS, limits and readiness. The final exit-37 and account-default cases are newly measured on PVE 9 only; earlier unchanged-runtime PVE 8 evidence retains its original attribution.                                                                                               |

The transport-owned two-row
[applicability audit](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5718891752)
also received an independent project review. PVE 8's unmeasured revised vector is accepted as a
bounded-proof coverage limitation: the changed conformance assertion exercises shared bootstrap
logic, not a per-major runtime branch, and both majors already have delivery, TLS and bounds
evidence. This is not a fresh PVE 8 pass or a waiver of supported-version testing at production
acceptance. Bookworm's out-of-band provisioning and TOFU qualifications remain unchanged.

This acceptance lets both owners close their buffered PoC artifacts and reconcile their designs
against the proven candidate. It does not complete FRD R3 privilege isolation, public-result
interpretation, live I/O, files/jobs, workload cancellation, platform-host composition, production
enablement or legacy deletion. The next gate is proof-informed design publication under the same
artifact ownership, not unrestricted parallel implementation. Existing independent cleanup reports
cover the live runs; the final delta assessment created no infrastructure.

### First live integration report (2026-09-17)

The operator's integration-testing lane published reports on
[#826](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617) and
[#796](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5709203388). Installed input
was SSH `1c32e4155c4a41304638d7637e1418459cc90092`, containing transport `a570a2de` as an ancestor,
with Python 3.12.13 on aarch64 Linux. Transport `e3d93736` changed only tests/evidence, not shipped
code. Later trust changes require affected-case retesting; the old report is not evidence for
untested revised behavior.

| Measured scope                                                                                             | Reported observation                                                                                                                       |
| ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Linux workstation to native QGA, PVE 8.4.21 and 9.2.11, Debian 13 guests                                   | All eight shared vectors passed on both. Execution identity was root; no demotion was tested.                                              |
| Linux workstation to SSH destinations on remote Lima, AWS and Azure; macOS 26.3 workstation to remote Lima | All eight shared vectors passed. Clients were OpenSSH 9.2p1 and 10.2p1, respectively.                                                      |
| Native output bound                                                                                        | A 400 KB producer retained exactly 32,768 bytes with OUTPUT_LIMIT, incomplete output and observed completion 0.                            |
| Sensitive native and SSH calls                                                                             | Synthetic canaries absent from sampled destination/local argv and returned reports; retained bytes suppressed.                             |
| Native deadline on both PVE majors, and SSH deadline on Lima                                               | Six guest processes remained at 60 seconds after local return, then zero after explicit tester cleanup. No guest cancellation was claimed. |
| PVE response types                                                                                         | The live provider returned integer exited/truncation fields, validating normalization of both boolean and 0/1 wire shapes.                 |

The report also identifies CA trust, composition-dependent input limits and missing automated SSH
independence coverage. Those drive the first feedback round. The wider final cleanup/evidence
addendum is requested: the GCP instance was still provisioning at report time. GCP, Windows carrier
execution, WSL2, other guest shells, demoted QGA and multi-node Proxmox were not covered. No
test-bed network/certificate mutation or new provisioning authority is inferred from the report.

### Second live integration report (2026-09-17)

The integration-testing lane's
[complete retest](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5714595742)
combines transport `d75c0bd3e699cfea3f5dcf40d2d34cb32c8bbe91` and unchanged SSH
`1c32e4155c4a41304638d7637e1418459cc90092` at integrated `d3c300ba7e51c83ee53da4b76760451b33a49e98`,
without conflicts or manual resolutions. The tester reports the same tree and 292 passing execution
tests with one skip as the lead's local combined checkout. This is separate from the live
observations:

- All eight shared vectors passed on SSH Debian 13 and Ubuntu 22.04.5 destinations, and native
  Debian 13 guests on PVE 8.4.21 and 9.2.11. Ubuntu supplies measured Bash 5.1.16/coreutils 8.32
  evidence. Native execution remained root, not a demotion or permission-binding proof.
- Actual cluster CA and matching certificate name succeeded on both PVE majors. System trust without
  that CA, an unrelated CA, and a mismatched name each refused. The tester reached the real cluster
  API through an SSH forward using the certificate's loopback SAN because its named DNS route was
  stale. This proves the reported live TLS controls, not direct DNS reachability or multi-node
  behavior; no server-name override or verification bypass was used.
- Near-limit input, oversized refusal before dispatch, output truncation, already-elapsed deadlines,
  interrupted observation and sensitive suppression passed in all four cells. The measured raw-input
  sizes were composition-specific, not new universal limits.
- The all-modules independence check included the combined SSH package. The report supplied final
  cleanup evidence for guest processes and test resources, including the earlier GCP addendum. The
  unrelated incomplete GCE metadata cleanup issue was reported to the operator.
- Bounded workloads were observed finishing and draining their bootstrap trees after observation
  ended. This narrows the earlier report's indefinite-survival wording; it does not establish
  automatic cancellation, a reaper for unbounded work, or guaranteed cleanup for every workload.

Three real Windows Server 2022 carrier attempts at this combined tree exhausted their 30-second
deadlines with no completion, while manual SSH reached the target. Their `local_status=1` can arise
from deadline cleanup; it does not prove the client exited before the deadline. The mechanism is
unresolved in this report and belongs to the SSH owner. Hosted Windows tests do not establish real
SSH delivery. Any corrected SSH candidate requires reviewed, pinned combined-tree retesting before
joint proof acceptance. Windows success, WSL2, demoted QGA identity, additional account shells and
multi-node Proxmox are not implied by the passing cells.

### Windows resolution and destination-account lookup (2026-09-17)

The [SSH retest](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5715862216)
installed SSH `901d9614181da0fb209e9896c8e747a44118d123`, containing transport `d75c0bd3`, with 295
execution tests passing and four skipped. Windows Server 2022, Python 3.12.14 and installed OpenSSH
9.5p2 passed repeated authenticated smoke calls and all eight original vectors from both an
SSH-parent launch and a clean launch. Both measured contexts also preserved the full byte range,
enforced output bounds, delivered large finite input, refused wrong pinned keys and suppressed
sensitive output. The first clean-context harness attempt failed because its SYSTEM identity could
not use the fixture key; the corrected run is separately reported, not a controlled same-account
comparison. The original Windows timeout is resolved on these measured cells, not all versions or
launch arrangements.

The
[combined lookup report](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5716019566)
merges transport `6687ef88f2138c819600ff11fa777924f707d9d9` and SSH `901d9614` at
`c188b32ea89ba0da1aadf07bda5461dd033009bf`, without conflicts or resolutions; 295 execution tests
passed with four skipped. Transport's intervening change was documentation only. Independently
observed destination identities and account shells were UID 1000 with `/usr/bin/bash` over SSH on
Debian 13, and UID 0 with `/bin/bash` over native QGA on PVE 9.2.11. `Shell.user_default()`
preserved all 2048 input bytes with complete streams, no framing/bootstrap/carrier failure and
reported exit zero, despite `SHELL=/does/not/exist`. Neither account shell was changed for the
positive case. A separate `/bin/dash` account produced bootstrap refusal 125, confirming the
documented allowlist rather than support for that path. The new native lookup measurement covers PVE
9 only; earlier PVE 8/9 delivery and trust evidence remains attributed to its original run.

The SSH report also measured account-shell refusal before bootstrap. Captured output rejected the
missing framing; raw completion alone did not identify application execution. This prompted the
shared terminology and sensitive-vector correction above. Remote detached-child measurements are not
evidence for a workstation descendant retaining local client pipes; the SSH-owned local regression
covers that separate case. The earlier macOS whole-cell pass does not automatically cover changed
cross-platform drain code and needs affected-case retesting or explicit justification.

Both reports supply independent cleanup evidence for their guest processes and test resources. They
do not establish login/interactive startup, arbitrary account hooks, WSL2, QGA demotion, live
streams/terminals, Bookworm or multi-node behavior. Published requests for additional beds do not
authorize this session to provision them. Bounded observed guest drains remain distinct from
cancellation of unbounded work.

### Final-candidate live report (2026-09-17)

The [combined report](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5717455178)
and [native companion](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5717456803)
test transport `6617f6e6c025cdca76551fa44fe61e377da1df41` as an ancestor of SSH
`1ccc304b339a22baeb0df8ef5a7534a3c9e6f8ec`. The SSH head is the combined tree; there was no merge or
conflict resolution. The tester installed that tree and checked the new `reported_exit` feature on
the remote workstations. The local Linux execution suite passed 296 tests with four skips.

All eight current vectors passed in six cells: Linux aarch64 to Debian 13 and Debian 12 over SSH,
macOS 26.3 arm64 to Debian 13 over SSH, both Windows Server 2022 launch contexts, and native QGA to
a Debian 13 guest on PVE 9.2.11 as UID 0. Every sensitive case reported exit 37, no carrier failure,
explicit suppression and zero retained stdout/stderr. This closes the changed-vector measurement
gap, not the separate public-outcome or arbitrary-startup proof.

MacOS used OpenSSH 10.2p1 and freshly passed authenticated byte preservation, output bounds, early
input closure and deadlines. Its local execution suite reported 254 passed, 45 skipped and one
failed. The failure was `test_agent_endpoint_is_explicit_and_must_be_a_socket`: its 133-byte fixture
path exceeded the measured 103-byte Unix-socket limit. Binding failed before positive socket
validation; non-socket refusal had already passed. SSH owns the fixture correction and affected
macOS retest. This report does not establish a green macOS suite or coverage of its unnamed skips.
The named local output-descendant and input-descendant tests have no macOS skip and fall within the
reported passing cases; they are distinct from remote detached-child observations.

Windows used Python 3.12.14 and the installed system OpenSSH 9.5p2 client. Both the SSH-parent and
usable-identity clean launch passed the new vectors, exact bytes, bounds and deadlines. The earlier
authenticated timeout remains resolved. Local cleanup status 1 after expiry, versus -9 on the
measured POSIX workstations, does not identify an earlier natural client exit.

The Bookworm destination adds Debian 12, Bash 5.2.15 and coreutils 9.1 compatibility evidence. It
was provisioned outside Agentworks, and its key was initially pinned by trust on first use rather
than independently authenticated acquisition. It is not evidence of Agentworks provisioning or
strict initial trust. The earlier Ubuntu 22.04 Bash 5.1.16/coreutils 8.32 observation retains its
original provenance; a few measured releases do not prove every version above the minimum.

Native PVE 9 also preserved completion at the output cap, refused already-expired work before
dispatch, and retained sent-but-uncompleted evidence when observation expired. PVE 8 was not rebuilt
for this round. Its earlier delivery, TLS and bounds evidence carries forward for the unchanged
native runtime; the changed shared vector was measured on PVE 9, not newly measured on PVE 8.
Default-shell lookup retains the preceding report's SSH/PVE9 attribution. None of these results adds
WSL2, provider-inner policy, demotion, multi-node, arbitrary account hooks, live streams or
terminals.

On a shared SSH destination, the tester observed ten bootstrap processes and two guest sleeps at 30
seconds after deadline lanes, five and one at 60 and 90 seconds, and zero at 120 seconds. Earlier
native and Bookworm deadline work was also gone when checked. This is observed drain of bounded
work, not cancellation or a reaper. The reports record no carrier staging observed in destination
`/tmp`, and independent provider-level cleanup of destinations, native guest/token, stopped beds and
workstation access/scratch. No replay or new infrastructure authority follows from this evidence.

### Final macOS delta and unchanged-runtime evidence (2026-09-17)

The
[complete delta report](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5718460281)
tests SSH `bc2a0711c4b9eb9a069c8df9ec9be512d9a28bae`, containing transport
`e41a44827e8e283a4b9fe3369224ef41c42a95a5`. Both the tester and transport lead independently
verified this ancestry and an empty `cli/agentworks/` diff from measured `1ccc304b` to `bc2a0711`.
No merge or resolutions were needed. The six live carrier cells above carry forward on that
unchanged-runtime basis; they were not rerun. The lead's current combined Linux execution suite
passed 296 tests with four skips, with execution lint, format and type checks also passing.

On macOS 26.3 arm64 with Python 3.12.13, the tester verified installed fixture contents against the
candidate's checksum. The corrected fixture uses a private short-path temporary directory, closes
its socket before directory cleanup, and retains both non-socket refusal and genuine-socket
acceptance. Each assertion pair passed under the normal temporary environment and an intentionally
long, 165-byte pytest base path. The actual socket path remained 33 bytes, below the measured
103-byte limit, and independent cleanup checks found no owned directory residue. An earlier
misquoted setup produced a 15-character base path; the report explicitly excludes that attempt as
long-path evidence.

The macOS SSH-focused suite passed 99 tests with six skips. The combined execution suite passed 255
tests with 45 skips and zero failures, superseding the preceding 254/45/1 result. All 45 skips are
accounted for: 41 require Linux bootstrap, account-lookup or fault-injection facilities, and four
require Windows process/descriptor behavior. They remain unmeasured cases on macOS, not passes or
proof of complete workstation coverage. The named local descendant-pipe regressions are among the
255 passing tests, separately from remote detached work.

This delta used only tester-owned macOS scratch, verified removed; no VM or bed was created or
started. PVE 8's revised exit-37 vector is still not newly measured, and its earlier unchanged
runtime evidence keeps its original attribution. Bookworm's provisioning/trust qualifications and
all broader unmeasured scope above remain unchanged. This resolves the reported fixture regression,
not the full proof matrix, production workload lifecycle or cutover gates.

### Local fault-injection evidence (2026-09-17)

Windows CI later exposed a five-second safety timeout in the positive default-trust fixture, after
the endpoint received POST but before GET. The adjacent explicit-CA positive case took 4.38 seconds.
A controlled Linux experiment with 2.6 seconds added to each worker start reproduced the same
POST-only deadline result at five seconds; a 30-second budget completed POST and GET with exit zero.
This supports allowing scheduling time in the TLS correctness fixture, not a claim to have traced
the exact Windows slowdown. The fixture now allows 30 seconds without retries or weakened trust
assertions. Production deadlines and their separate enforcement tests are unchanged.

The first feedback round adds malformed source/stdin cases that inspect decoded output and an actual
fixture-file side effect, rather than looking for plaintext inside armored output. Removing either
initial validation step causes its regression to fail. New encoder fault cases pin the stdout/stderr
encoder by its input pipe, interrupt that owned process while the fixture payload is stopped, then
resume a successful payload. Removing either encoder wait causes the corresponding regression to
fail instead of accepting a successful helper exit.

During stress, opening process handles for every descendant before filtering produced error 22 on
short-lived candidates. The revised fixture matches the owned argv, pipe and required state before
opening a handle, then rechecks those criteria with the handle pinned. It does not suppress the
error; its exact kernel cause was not established. The implementation lane's final Python 3.12.13
and 3.14.7 runs each passed 200 producer faults, 200 encoder faults and 100 malformed-input cases,
1,000 total with no skips. This refines test selection only, not bootstrap runtime or guest
lifetime.

The same round replaces native `verify_tls=False` with explicit CA trust and mandatory hostname
checking. Local loopback HTTPS tests observe correct-CA/host success, wrong-CA/host refusal before
HTTP dispatch, safe missing/invalid bundle failure, normal default trust and no ambient-trust
fallback when an explicit bundle is selected. These are workstation TLS tests, not live cluster
acceptance. A combined-checkout mutation adding a legacy import to the SSH package fails the new
all-modules independence guard; the restored package passes.

### Earlier decoder-selection repair

Python 3.14 CI exposed a race in the original decoder-kill fixture. A 100-run local reproduction
selected the intended source decoder 61 times, a different decoder 38 times, and an already-exited
decoder once. In that last case the signal interrupted no source delivery and the helper correctly
returned zero, reproducing the CI assertion failure. Process traversal also observed disappearing
`/proc` entries. This evidence did not demonstrate a production bootstrap defect.

The repaired fixture pauses its own payload before further consumption, matches the producer's
stdout to the intended source/input pipe, and uses a process handle to stop, verify and signal the
same live process. It observes producer termination before resuming the payload. This replaces
timing-based target selection without weakening the failure/partial-stream assertions or adding a
production test hook. An unusually large pipe that can hold the entire fixture input is explicitly
reported as an unsupported fault-injection case, not silently passed.

The implementation lane measured 550 bounded cases each on Python 3.12.13 and 3.14.7: 400 targeted
producer failures across source/stdin and inherited signal variants, 100 early stdin closes, 25
early script exits and 25 sensitive early closes. All 1,100 cases passed; measured producer pipes
held 65,536 bytes and none of these runs skipped the fault. These are local Linux fixture results,
not SSH authentication, native QGA or live platform evidence.
