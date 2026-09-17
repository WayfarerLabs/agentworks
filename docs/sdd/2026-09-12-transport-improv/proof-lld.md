# Transport Boundary Proof

Status: Implementation in progress; not acceptance of the joint proof.

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
renew the budget while polling. `CarrierReport` separates dispatch evidence, prepared-invocation
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

An observed exit belongs to the prepared invocation. It does not independently establish a nested
application's outcome. In particular, an SSH local status of 255 remains ambiguous. Output parsing
does not introduce an SSH completion guarantee.

## Shared no-staging preparation experiment

The initial test lane is Linux. It investigates an explicit Bash bootstrap with base64 encoding,
using pipes and `/dev/fd` rather than temporary files or an installed guest helper. It does not
assume Python is available during native recovery or readiness. Bash, base64 and descriptor support
are prerequisites to measure, not claims about every supported target.

Bash 5.1 is the mechanism minimum for saving and waiting on each process-substitution PID;
[the Bash maintainer's explanation](https://lists.nongnu.org/archive/html/bug-bash/2024-07/msg00044.html)
distinguishes it from earlier, narrower wait behavior. Local tests measure Bash 5.2.15. Exact guest
versions and older-version execution remain separate evidence, not inferred from executable presence
or that upstream explanation.

Application argv, script source, environment, directory and finite stdin travel in an ASCII input
envelope. The bootstrap argv is fixed implementation source, not caller source or secret values.
Application script source and application stdin use distinct descriptors. Fixed `sh`/`bash` and the
actual destination account's supported default shell are separate choices. Login/interactive startup
combinations require their own proof and must not be silently accepted.

The parent observes source/stdin producer termination and output encoders before returning. An
unexpected producer failure emits bootstrap-failure evidence and returns 125, even if the payload
exited zero; the report still describes the prepared invocation, not a nested exit oracle. Early
consumer closure may produce SIGPIPE and is allowed without claiming all input was consumed. GNU
env's `--default-signal=PIPE` is an explicit prerequisite of this experiment. Local fault injection
kills only decoders descended from the fixture's bootstrap and checks that partial delivery cannot
be reported as complete.

Guest stdout and stderr are separately armored into a shared record stream. Raw carrier stderr
remains diagnostic or mixed provenance. Framing validation, output bounds and end markers must
establish complete guest streams before interpreting them as such. Invalid or incomplete framing is
a failed proof, not an empty successful command. The envelope does not carry a new exit-status
oracle; the carrier's actual completion evidence remains authoritative for its invocation.

Sensitive execution suppresses workload output in the bootstrap and retained carrier output.
Suppression is not permission to retain raw sensitive frames for diagnostics. A suppressed stream
must be reported as suppressed rather than as a decoded empty guest stream.

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
override. The wire worker caps a response at 4 MiB. PVE input is capped at 65,536 ASCII bytes;
whole-request acceptance, including bootstrap argv, remains a live-test measurement rather than a
guessed limit.

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

The first live report below establishes the measured native and SSH cells, not complete acceptance.
Supported tool versions and shell startup combinations, identity/elevation, remaining fault and
boundary cases, final cleanup evidence and the full joint matrix remain open. Live I/O is not
implemented by the finite-input slice and cannot be enabled without its separate ownership proof.

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

### Local fault-injection evidence (2026-09-17)

The first feedback round adds malformed source/stdin cases that inspect decoded output and an actual
fixture-file side effect, rather than looking for plaintext inside armored output. Removing either
prevalidation causes its regression to fail. New encoder fault cases pin the stdout/stderr encoder
by its input pipe, interrupt that owned process while the fixture payload is stopped, then resume a
successful payload. Removing either encoder wait causes the corresponding regression to fail instead
of accepting a successful helper exit.

During stress, opening process handles for every descendant before filtering produced EINVAL on
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
