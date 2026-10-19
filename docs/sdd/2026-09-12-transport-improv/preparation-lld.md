# Invocation Preparation and Public Results

- Status: Production design with private implementation increments; not enabled for production
- Governing requirements: [FRD](frd.md), especially R2-R5, R8-R11
- Existing boundaries: [execution contract](execution-contract.md), [proof](proof-lld.md), and
  [lifecycle](execution-lifecycle-lld.md)

## Scope and decisions

This design owns foreground invocation values, application preparation, the framed evidence that
turns a carrier observation into an application result, and checked versus unchecked result
behavior. It sits above the unchanged call shape of `Carrier.execute(...)`. It does not redesign an
SSH connection, add a carrier-specific completion oracle, or own job identity, supervisor lifetime,
stop, file publication, or RunContext composition.

The [execution contract](execution-contract.md) owns invocation, stream separation, outcome,
sensitivity and no-replay semantics. This LLD supplies their preparation mechanism. Preparation may
use inline delivery or private staging. Readiness binds inline-only preparation and therefore never
stages, spools, launches a supervisor, or requests startup files.

The new package remains disconnected from production until the plan's additive-surface gates pass.
The [delivery-stage contract](execution-contract.md#delivery-stages-and-permission-activation)
distinguishes immediate operational safety from deferred recipient permission activation.

## Facts at the current boundary

The proof supplies literal bootstrap argv, finite input owned by `CarrierIO`, and framed stream
evidence. Its `CarrierReport.completion` still describes the remote command chain, which can stop in
an SSH account shell before bootstrap, while sensitive/discard currently removes every carrier byte.
Its 256 KiB envelope and QGA's smaller measured limit are proof bounds, not production large-input
support. It also lacks startup modes, shared identity change, spooling, and public results. Raw
zero, stream-end markers, and absent retained bytes therefore never become public application
success.

## Public invocation values

The invocation values and their boundary validation live in
[`agentworks.execution.models`](../../../cli/agentworks/execution/models.py). The first increment
replaces free-text shell names and factory constructors with three explicit enum members and places
startup flags on `Script`. This intentionally changes the internal preparation API, not the buffered
carrier ABI, and does not enable production use.

The current increment deliberately does not validate text encoding or NUL in these constructors.
Preparation must reject NUL and strict-UTF-8 encoding failures for every command argument and script
source before input is consumed or remote effects begin. Constructor-level semantic validation can
be reconsidered later only as a separately compatible public-value change.

Transport-owned proof code and tests import the values from `models` directly. `preparation.py`
imports them normally, so the SSH-owned fresh-process test's existing
`from agentworks.execution.preparation import Command` continues to work incidentally until its
owner updates that import. There are no deprecated aliases, `Shell.fixed()`, `Shell.user_default()`,
or compatibility constructors. The package root does not re-export the values as a supported public
API until the complete new surface passes its gates.

The target retains the contract's concise
`run(Command | Script, *, profile, stdin, output, sudo, env, cwd, sensitive, deadline, check)`
shape. `Input.eof()` is the default. `Input.bytes(data, sensitive=False)` accepts immutable bytes,
including empty bytes, and always ends in EOF. `Input.sensitive(data)` cannot be downgraded.
`Output.capture(max_bytes)` is the buffered mode in this LLD; `Output.discard()` retains no
application bytes. Live streams and terminals keep their separate ownership gate.

The effective sensitivity bit is the union of request-wide sensitivity, input sensitivity, and the
bound environment's value provenance. No content scanning attempts to discover secrets. A caller may
add sensitivity and may not clear a bound sensitive value.

## Preparation pipeline

`ExecutionAccess.run` delegates to one private pipeline. Each transition produces an immutable value
and consumes the same monotonic `Deadline`:

```text
public request
  -> validate invocation and options
  -> bind final identity and effective sensitivity
  -> select the known preparation substrate
  -> compile manifest, bootstrap argv and frame decoder
  -> deliver large private inputs when required
  -> call Carrier.execute exactly once for the application launch
  -> interpret validated frames plus CarrierReport
  -> retrieve bounded spooled output when required
  -> attempt owned cleanup
  -> return or check the ExecutionResult
```

Validation occurs before local input is consumed and before remote effects. It rejects invalid text,
relative or empty working directories, invalid environment names, protected helper names,
unsupported shell/startup combinations, incompatible I/O/lifetime/profile choices, expired
lifetimes, and an already-expired deadline. Authorization checks become an earlier target-layer
transition only when the removal gate activates them.

The pipeline receives a final composed environment, not unresolved config or secrets. Helper
processes start with a fixed minimal environment. The payload starts from the composed map rather
than inheriting the delivery process's `PATH`, loader variables, locale, or shell-control variables.
Protected Agentworks keys have already been composed by the owning operation. Login or interactive
startup may intentionally modify that payload environment according to the selected shell.

The working directory is applied after the final identity is established and before the selected
program or shell starts. A missing or inaccessible directory is a preparation failure with no
application-start claim. Literal commands receive no application shell and execute argv directly.

## Identity and elevation

An execution target binds a workload account and a carrier delivery account. Neither is caller
selectable. Each invocation uses one of three private identity plans:

| Delivery and request                | Private plan                                                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Delivery already uses workload user | Enter the trusted bootstrap directly and verify the expected UID and primary/supplementary groups.           |
| Workload user requests granted root | Enter through fixed `sudo -n --user=#0 --` bootstrap argv, then verify root identity before workload access. |
| Provider delivery starts as root    | Resolve the bound account and enter it with a fixed demotion helper, then verify its UID/groups.             |

<!-- cspell:ignore setpriv -->

The demotion candidate on Debian is `/usr/bin/setpriv` with an exact resolved UID, primary GID,
explicit supplementary groups, and cleared inheritable/ambient capabilities. A fixed `env -i`
launcher establishes the helper environment after the transition. It does not ask `setpriv` to
derive a shell or environment from the account. The account name and expected identity are trusted
target data, never public request fields. Numeric identity metadata may appear in the fixed wrapper
argv; workload source, paths, environment and stdin may not. If `setpriv` availability or group
behavior cannot be established on every bootstrap image, the implementation must select and prove a
different shared launcher before QGA demotion is enabled. It may not leave an ordinary call as root.

Each private identity plan binds the transition and expected UID/GID/groups for both execution and
file helpers. The manifest carries the expectation, not a request to select privileges. A root-sudo
plan requires a root expectation, and a demotion plan requires a non-root expectation; invalid
combinations refuse before delivery. The destination verifies identity before accessing workload
paths or launching the application, including Linux real, effective and saved IDs and normalized
group membership. Delivery selection does not infer a fallback or grant permission. Capability
bounding-set restrictions and `no_new_privs` belong to the independently selected protection
profile, not ordinary account selection. This candidate still requires actual root-demotion, sudo
and native-platform proof before production use.

Core resolves the bound workload account before constructing its identity plan. A private, fixed
read-only helper queries the destination account database under the existing delivery identity; it
does not need an identity transition to discover the transition's inputs. The account name travels
in sensitive stdin, never as executable source. Only UID, primary GID and normalized supplementary
groups return. No password, account description, home or shell is exported by this lookup. Shell
selection remains after the final identity transition.

The lookup uses one carrier attempt and a bounded nonce-bound JSON request/reply, not an application
stream protocol. Missing accounts and lookup/runtime refusals remain distinct from malformed or
incomplete observation. Noise, reflection, duplicate fields, extra content, oversized metadata or
incomplete streams must not yield an identity. Discovery neither grants authority nor certifies the
identity of a later process: the selected helper still checks its actual IDs/groups before workload
access. There is no account cache, automatic replay, staging, privilege change or public account
selector in this operation. Shared same-invocation admission selects and checks the Python runtime.

The same fixed lookup family supplies a separate metadata-owner/group operation. It resolves only
the requested UID and GID; it does not run the execution-account supplementary-group lookup or
change the helper's credentials. Its result kind is bound to its request, so file ownership cannot
be mistaken for a complete execution identity. File metadata selection and execution elevation
remain independent choices.

### Owned target identity preparation

Core prepares a mandatory ordinary identity plan and, when composition explicitly includes
elevation, an optional elevated plan before constructing a passive execution or file view. Its
inputs are the carrier, delivery account, bound workload account, elevation-inclusion choice,
runtime selection, deadline and already-acquired operation owner. An optional numeric guest
bootstrap binds an already-prepared root entry and full observed guest for body admission. These are
private composition inputs, not public request fields or enforcement of the future permission model.
Separate direct-login routes remain possible. Native composition can instead prepare an explicitly
selected agent account over the admin delivery route without exposing an elevated agent API.

The composer resolves delivery and workload accounts and must produce the ordinary plan before it
considers elevation. An unsupported ordinary transition fails without a root lookup. When elevation
is included and non-root delivery requires sudo, the same preparation resolves root through the
fixed account helper under the same serial borrow. Identical account names reuse only that
preparation's observation; there is no persistent account cache. Every exchange uses the same
deadline and arms the owner immediately before actual carrier dispatch. Prepared status requires a
usable ordinary plan and successful completion of every included elevation lookup. A failed or
uncertain elevated lookup may retain the already-derived ordinary plan as evidence, but the overall
preparation is not `PREPARED` and no access view may be constructed from it. Incomplete observation,
abnormal completion or uncertainty never supplies a usable plan. Record the observations and
deadline facts separately from whether preparation succeeded.

The ordinary plan selects direct entry when delivery already uses the bound identity, or exact
demotion when delivery has UID zero and the workload does not. An included elevated plan selects
direct entry from root delivery or fixed sudo entry from the bound non-root workload identity.
Non-root delivery through a different non-root workload identity requires the explicitly supplied
numeric guest bootstrap with a usable fixed sudo root entry. Without it, the transition refuses;
there is no inferred sudo authority or fallback. That body's plan remains demotion, while the
bootstrap supplies root entry and drops to the exact body credentials before loading workload code.
This adds no persisted identity mode. Omitting elevated access still omits its root lookup and
elevated plan, even when the core bootstrap uses root for admission. Account lookup is not proof
that sudo or demotion will succeed; the final helper still verifies its actual identity before
accessing workload data.

Paired native views share one operation owner, file operation, execution operation, carrier, numeric
guest bootstrap and deadline. Core selects the agent from its recorded row and checks VM membership
before platform effects; descriptive context scope does not select the agent. Both identity
preparations remain in aggregate custody. Failed or uncertain agent preparation cannot publish a
partial pair. Sharing operation state avoids a second cleanup lifecycle; each view still supplies
its own body plan, and only the admin view includes an elevated plan. Job observation/control in
this private composition is operation/resource- and VM-bound, not recipient-account authorization.
Recipient checks on existing job references remain required at permission activation; paired views
do not supply that isolation during coexistence.

The existing carrier admission adapter is shared by account preparation and file workflows. It owns
dispatch and termination evidence only; callers own their protocol facts. A lookup without
termination proof retains unresolved ownership, while a proved terminal refusal may relinquish the
borrow without closing the operation owner. Preparation neither releases the enclosing claim nor
activates a route, retries a lookup, launches a workload or constructs a legacy transport.

The sessions/console migration must separately disposition the existing admin-owned multi-console
pane that enters an agent account with sudo. It is a real cross-user consumer, not a reason to infer
agent authority for every native admin target. The explicit bootstrap-bound preparation path is
available to that migration, but does not itself migrate the consumer or prove native credential
transitions.

### Sudo bootstrap

The sudo wrapper consumes the same manifest from stdin after privilege change. Source, input, and
environment never appear in sudo argv or environment assignments. A failure before the inner
bootstrap's first trusted frame cannot be classified as sudo refusal from raw nonzero status or
stderr: account shell, sudo, and bootstrap startup remain indistinguishable. It produces an unknown
application outcome unless a separately proved trusted phase mechanism establishes more. There is no
password prompt or fallback to non-elevated execution.

`Shell.USER_DEFAULT` is resolved after this identity transition with the destination account
database. It therefore selects root's shell for an elevated request and the demoted user's shell for
provider-root delivery. The resolved absolute path is recorded in the private invocation plan before
application start. Only the supported sh/bash paths are accepted initially; a configured zsh, fish,
missing path, or non-executable object is an explicit interpreter refusal, not substitution. Managed
shell lookup on a numeric-bound operation uses the same root entry, exact body credentials and full
guest checkpoint as other fixed helpers. A bootstrap-bound agent lookup must not attempt ordinary
demotion directly from the admin delivery account.

The bootstrap emits its identity-verified frame only after the transition and checks succeed. That
frame proves what the trusted helper observed, not hostile same-user isolation. Guest root and a
malicious workload remain outside this API boundary.

## Shell and startup policy

The Linux preparation substrate maps the typed choice after final identity selection:

| Shell          | Non-login, non-interactive                           | Login, non-interactive        | Non-login, interactive   | Login, interactive               |
| -------------- | ---------------------------------------------------- | ----------------------------- | ------------------------ | -------------------------------- |
| `SH`           | `/bin/sh SOURCE_FD`                                  | `/bin/sh -l SOURCE_FD`        | `/bin/sh -i SOURCE_FD`   | `/bin/sh -l -i SOURCE_FD`        |
| `BASH`         | `/bin/bash --noprofile --norc SOURCE_FD`             | `/bin/bash --login SOURCE_FD` | `/bin/bash -i SOURCE_FD` | `/bin/bash --login -i SOURCE_FD` |
| `USER_DEFAULT` | Use the matching row for the resolved supported path | Same                          | Same                     | Same                             |

`SOURCE_FD` is an inherited descriptor, not a command string or temporary pathname supplied by the
caller. Application stdin is a separate descriptor installed as fd 0. Reading stdin in the script
therefore never consumes source. A startup file selected by login/interactive semantics can read fd
0, change environment or directory, print output, or fail before the script body. Those are explicit
effects of requesting startup, not carrier behavior. The result records only the start and wait
evidence actually established; it does not pretend to distinguish a profile failure from the script
body unless the trusted bootstrap observed an exec failure before shell entry.

For the non-login, non-interactive default, preparation removes `ENV`, `BASH_ENV`, `SHELLOPTS`,
`BASHOPTS`, `BASH_XTRACEFD`, exported helper functions, and the private `_agw_` namespace before
starting the application. The fixed Bash path adds `--noprofile --norc`. `SH` uses the supported
system `/bin/sh` behavior and must be proved on each supported guest release. A PTY does not alter
any row. Interactive without a PTY is permitted only after its separate shell behavior is proven; it
may report the shell's normal lack-of-job-control diagnostics.

Literal commands never use this table. Internal staging, supervisor, and file control commands also
use fixed helper interpreters; they never inherit `USER_DEFAULT`, login, interactive, payload PATH,
or loader settings.

## Application evidence protocol

Preparation assigns a fresh 128-bit invocation nonce before any dispatch. The nonce is not a secret
and may appear in fixed bootstrap argv. It associates records with this attempt and prevents stale
or incidental output from being accepted. It is not authentication against a malicious destination
account, which can inspect its processes and forge its own output.

The trusted bootstrap writes newline-delimited ASCII records to its control descriptor. Each record
contains the protocol version, nonce, strictly increasing sequence number, record kind, decoded
length, and canonical base64 body. Records have a fixed 8 KiB encoded maximum; binary application
data is split into smaller frames. Length, alphabet, sequence, singleton, phase, and terminal-order
violations make the transcript invalid. There is no transcript-wide checksum: the nonce, sequence,
closed grammar, and strict terminal ordering already identify and delimit the attempt, while a hash
would not authenticate a same-user helper. Stream digests remain solely to verify reconstructed
stream bytes.

The version-one production record kinds are:

| Kind         | Meaning                                                                                                        |
| ------------ | -------------------------------------------------------------------------------------------------------------- |
| `LAUNCHING`  | The helper is committing its one launch attempt. This does not prove successful exec.                          |
| `STARTED`    | A mechanism with proved exec acknowledgment observed successful application entry.                             |
| `STDOUT`     | Captured launch-chain bytes, promoted to application output only with application evidence.                    |
| `STDERR`     | Captured launch-chain bytes, promoted to application output only with application evidence.                    |
| `STREAM_END` | One stream ended, with total retained length, digest, and caller-bound truncation flag.                        |
| `WAITED`     | The launcher's observed wait fact and its precision: wait code, exact exit, or exact signal.                   |
| `FAILED`     | A closed non-sensitive trusted phase and code, with no payload or provider prose.                              |
| `FINISHED`   | The one terminal record, after the launcher and output handlers have been waited. Not application proof alone. |

`LAUNCHING` is never mapped to `ApplicationState.STARTED`. A direct shell substrate may emit
`STARTED` only after a proved close-on-exec acknowledgment or equivalent. Death between `LAUNCHING`
and that acknowledgment therefore remains `UNKNOWN`, even if the application may have run. The
managed supervisor may later supply an exact start fact through the same schema.

The no-staging Linux shell experiment launches through fixed GNU `/usr/bin/env --` and records the
shell's wait code. GNU env's reserved launcher codes can help test hypotheses, but cannot form the
production contract: 125 through 127 collide with legitimate application exits, and shell encodings
from 128 through 255 cannot distinguish a pre-exec signal from an application exit. A public
invocation must support every application exit value, including 255, without downgrading a completed
application to `UNKNOWN`. The shell experiment therefore remains non-production; no reserved-code
subset is an accepted completion rule. The fixed Python helper uses the separately selected
retrospective rule below.

Even after start is proved, a direct shell wait code does not distinguish explicit exit 143 from
signal 15. It is represented as `WaitCode(143)`, never `ExitCode(143)` or `Signal(15)`. A managed
supervisor may provide exact exit/signal precision. This is evidence precision, not a
carrier-specific result type.

`FINISHED` is accepted only once, after the required wait and stream-end records, with no subsequent
record. It is strict terminal evidence that the trusted helper reached that phase, not a
self-authenticating checksum. Application completion additionally requires either proved eager entry
or the selected retrospective normal-completion evidence below. Valid application evidence can
survive later carrier observation loss, while raw carrier completion without it never proves
bootstrap or application completion. A missing, duplicate, out-of-order, or post-terminal record is
a protocol failure. Frames captured before application proof are not returned as application
stdout/stderr.

Eager application `STARTED`, live application-output promotion and signaled-child attribution
require independent successful-exec evidence. A managed supervisor may acknowledge its exact launch
and return a `JobRef` before application completion without claiming application `STARTED` or entry
into a script body. Readiness cannot install or stage a helper to manufacture either kind of
acknowledgment. Buffered direct execution does not claim eager start: it may establish only the
selected retrospective normal completion below, without reducing exit-value coverage or acquiring a
synthetic `STARTED` record.

### Selected retrospective completion

The fixed Python-helper path separates evidence of completed execution from an eager launch
acknowledgment. On the audited CPython 3.11 through 3.14 native fork/exec family, with no pre-exec
callback, an intact exec-error channel and one reaper, an actual normal wait for the exact child
establishes entry and completion retrospectively for every exit value from 0 through 255. It does
not emit a synthetic earlier `STARTED`.

This evidence rule is limited to the inline execution helper. That helper admits it only on CPython
3.11 through 3.14 and emits a trusted pre-launch runtime refusal outside that range. The shared
runtime selector and file helpers retain their broader Python 3.11-or-newer prerequisite; this is
not a global interpreter cap. A future Python minor, alternative implementation or changed launch
mechanism remains usable only for operations whose proof does not depend on this inference until its
direct-execution path is separately audited.

The retained [exec-evidence experiment](../../../cli/tests/execution/exec_evidence_probe.py) uses
public launch controls: `shell=False`, `close_fds=True`, `preexec_fn=None` and
`start_new_session=True`. A delegating observer records the actual native fork/exec route without
forcing its selection, and child code verifies that the new process owns its session. Both checks
pass on local CPython 3.12.13 and Debian CPython 3.11.2. The new session is a candidate ownership
boundary, not descendant containment. The closed cases also cover all 256 exit values, missing wait
evidence, invalid launch inputs and the counterexamples below on both interpreters. Source audit
covers the released CPython 3.11 through 3.14 launch predicates, while the production helper itself
supplies framing, source/input transfer and the single-reaper exact-wait path. This does not
guarantee future interpreters. Application cancellation and native carrier acceptance remain open.

The helper must obtain the actual native wait status. `Popen.wait()` and `poll()` can substitute
zero when child status is unavailable; neither is sufficient evidence. Ignored `SIGCHLD`, a
competing reaper, a pre-exec callback, a broken error channel or a different spawn implementation
invalidates the inference. The proof must isolate those cases and verify the selected interpreter
path rather than infer it from Python's minimum version. The inline-only runtime guard is the closed
eligibility decision for this initial implementation. See the
[runtime research](prior-art-research.md#local-mechanism-evidence).

A signaled child without independent entry evidence stays unknown. For scripts, the exact normal
wait proves completion of the selected interpreter, not entry into the first script-body command.
Detached managed launch requires acknowledgment of the exact supervised launch, not proof of
application entry. Eager application `STARTED` reporting and live application-output promotion still
require independent successful-exec evidence. Buffered capture may keep bounded bytes private until
completion evidence is established. The shell experiment and its wait-code result gain no stronger
meaning from the selected Python-helper rule.

Application stdout and stderr can never inject control records because the bootstrap encodes them
through dedicated descriptors. Raw account-shell output before bootstrap is not framed application
output. The decoder discards it without retaining a diagnostic copy. This is robust against
accidental hooks, not hostile same-user forgery.

## Required carrier I/O extension

The workstation pump and destination Python helper share one private standard-library process core,
not independently maintained copies of pipe scheduling and wait handling. Carrier adapters retain
carrier types, sensitivity/retention policy and report mapping. The shared core owns exact status,
bounded duplex progress, short writes, post-exit collection and known-process cleanup. Extraction
must preserve the existing Windows handle-backed behavior and all host pump tests; a POSIX guest
consumer does not narrow the workstation contract.

The inline helper may embed that exact packaged source as fixed bootstrap code. This is distinct
from application source: caller argv, script, environment, cwd and input remain data on stdin and
are never interpolated into helper code. Source composition must not install files or import an
Agentworks installation on the destination. Its final encoded request, including fixed argv and JSON
escaping, needs the same native whole-request proof as the manifest. The private QGA carrier checks
both `input-data` and the complete serialized request; a fitting local fixture is not native
acceptance. Neither extraction nor embedding resolves the separately recorded launch-interruption or
application-entry gates.

### Default-deny local launch ownership

The selected implementation candidate gives one private owner construction and exact cleanup
responsibility without changing process-global signal handling. A raw standard-library thread starts
with only a small admission cell. Before the caller observes successful startup, it cannot receive
command data, environment, cwd, inherited descriptors or borrowed streams. An interrupted or
ambiguous startup terminally cancels admission and never retries. Such an inert bootstrap may finish
later without performing operation work.

After successful startup, the caller may publish one immutable launch request within its cleanup
guard. The owner retains the actual child through construction, status observation and final
cleanup, including the interval before PID/readiness publication. Caller-side pumping preserves the
existing byte-endpoint semantics. Before requesting final cleanup, the caller stops using the
internal pipes; it propagates its first control-flow exception only after the owner has completed
cleanup or reported the existing explicit incomplete-cleanup outcome. This is the required behavior,
not yet established for arbitrary asynchronous interruption by the implementation below. Repeated
interrupts must not release ownership or dispatch again. Prepared source descriptors remain
caller-owned and close in an outer `finally` after the carrier/helper has relinquished them.

The terminal observation proves the owner has relinquished operation resources, not that the native
thread has finished its final return instructions. No background pump or task may touch a borrowed
endpoint after return. The control protocol needs justified synchronization and bounded storage, not
an executor, transfer registry or an assumption that arbitrary blocking callbacks can be canceled.
Raw-thread exceptions must not leak request data through the interpreter's default exception hook.
Preserve existing exact wait evidence, cleanup-induced exit distinctions and external-reaper
uncertainty; thread ownership is not descendant containment or remote cancellation.

The private shared pump implements a candidate with lock-mediated publication and a caller-side
cleanup guard. Local tests inject interruption before native start returns, between request
publication and admission, immediately after admission, during pumping and during cleanup. The
inline helper also closes its prepared source descriptor across emission or launch failure. These
checks extend the
[local admission experiment](prior-art-research.md#local-process-startup-and-interruption), not its
platform scope. A separate real SIGINT experiment at entry to the cleanup loop leaves a live child
and open pipes before fixture cleanup: the caller can escape before requesting the owner's stop.
Nested Python guards relocate that asynchronous boundary rather than close it. Selecting an
application-wide interrupt policy or a separate supervisor protocol requires an explicit design
decision; neither is supplied or implicitly authorized by this candidate. This gap remains a
production gate. Native Windows/macOS behavior, interpreter shutdown, repeated startup cancellation
and the final bundled-helper size remain acceptance gates. The existing OS process-creation caveat
still applies; no new hard real-time bound is promised.

The application-boundary inventory at `43c04a06` found no existing deferred-interrupt facility to
reuse. `cli/_entry.py` translates escaped `KeyboardInterrupt` into exit 130; RunContext carries no
cancellation policy, and orchestration uses ordinary context unwinding. Neither prevents an
interrupt from skipping cleanup entry. Provider rollback paths deliberately allow a second Ctrl-C to
abandon cleanup, and `vms/manager/tailscale.py` installs its own handlers for port forwarding.
Changing the entire CLI's signal policy would therefore change existing behavior beyond the new
execution stack. The fixed inline guest helper runs in a separate interpreter and would not inherit
a workstation CLI policy.

A scoped interrupt policy for new execution and its fixed helper is proposed for operator
discussion, not selected or implemented. Its design must cover handler ownership/restoration,
ordinary interruption and repeated interrupts, and the held-process consumer without changing legacy
provisioning rollback implicitly. It must distinguish requesting cleanup from proving it and retain
explicit incomplete-cleanup facts. Forced interpreter termination cannot acquire a cleanup guarantee
from a Python signal handler. The production gate above remains open.

### Shared ownership for held local processes

SSH forwarding needs the same child owner without the run-to-completion byte pump. Transport owns
one private `LocalProcessOwner` seam, constructed before dispatch, with one immutable launch
request, once-only admission, immutable observations and explicit close after pipe use ends.
`run_owned_process` and the SSH forwarding resource consume that same implementation. This is a
factoring of local process ownership, not a second job backend or descendant supervisor.

The seam encapsulates inert bootstrap startup, request admission, ambiguous-start cancellation and
settlement. A never-admitted request is distinct from a started process's terminal observation; an
inert canceled bootstrap need not publish a process terminal record. Natural exit is observable
while pipes remain borrowed and stdin remains open. Closing only stdin is input EOF, not a request
to kill or a manufactured input failure. A status first obtained during cleanup remains local
cleanup evidence, not natural-exit evidence.

SSH retains its readiness protocol and I/O drainer. On close it first stops and joins every pipe
user, then relinquishes those pipes to the common owner and waits for settlement. Relinquishment
does not ask a running drainer to stop: it asserts that pipe use has already ended. Repeated close
returns the same settled facts without another kill or reap. Immutable snapshots support observers;
launch and relinquishment have one caller-side owner, not a new general concurrent-control API.

POSIX PTY geometry updates do not themselves notify a child merely borrowing that PTY as stdin. The
shared owner therefore supplies one fixed resize-notification operation, not a general signal API.
`notify_resize` accepts a finite local process deadline and uses the existing retained worker and
exact-status observation path. Its single request slot distinguishes pending cancellation from an
admitted native call. A pending request can be denied on expiry, interruption, close, observed exit
or lost ownership. Once admitted, expiry reports `UNKNOWN` while the same worker retains the request
through signal or terminal settlement; an occupied slot refuses another request. Native polling and
signaling occur outside the publication condition lock.

`NOT_SENT` means no signal call was made or the kernel reported that the child was absent.
`REQUESTED` means the local kernel accepted SIGWINCH, not that SSH transmitted a window change or
the remote application observed one. Signal failure remains separate from process status and cleanup
evidence. Unsupported hosts refuse without dispatch. The adapter still owns geometry, terminal
endpoints and restoration. Actual SSH and native workstation delivery remain proof gates.

This seam must not be advertised as fixing the independent cleanup-entry SIGINT gap above. Shared
ownership removes forwarding's direct `Popen`-before-owner construction gap; global interrupt
policy, native workstation proof and final production acceptance remain separate gates.

### Inline Python implementation slice

The next private implementation composes the shared process core with one fixed Linux helper and the
production evidence grammar. It does not replace the accepted buffered proof, expose RunContext or
accept application completion before the remaining evidence gates pass. Its first executable slice
uses the already-selected identity, non-login/non-interactive commands and scripts, finite input,
and bounded capture or suppression. Elevation, startup modes, live/terminal input, staged transfer
and managed lifetime keep their separate implementation and proof obligations.

The private record spelling is:

<!-- cspell:ignore AGWE -->

```text
AGWE1 <nonce> <sequence> <kind> <decoded_length> <canonical_base64>\n
```

The nonce is 32 lowercase hexadecimal digits. Sequence numbers start at zero and are contiguous,
with a signed 64-bit positive maximum. Decimal fields have no sign or redundant leading zeroes. The
whole record, including its newline, is at most 8 KiB; each decoded body is at most 4 KiB. The
existing eight record kinds are unchanged. Control bodies use closed schemas; stream bodies are
bytes. Encoding/framing does not itself establish phase validity or application completion.

The standard-library-only `_evidence_wire` codec is shared by the fixed helper and the workstation.
Its incremental reader keeps at most one bounded record and sends validated frames to a trusted
first-party consumer, never a plugin callback. Raw hook output and other nonces are discarded.
Records are recognized only at stream or line start, not as substrings of diagnostic text. The
helper writes a separating newline before its first record, so a hook without a trailing newline
does not contaminate that record. A matching tag embedded in a raw diagnostic line is discarded with
that line. Malformed records associated with this nonce latch a safe closed error and disable
further frame delivery while input continues to drain. Finalization detects an incomplete matching
record. The next layer owns phase/order rules, stream accounting, sensitivity and
application-evidence interpretation; the codec neither accumulates an unbounded transcript nor
returns success.

For Linux script source, the candidate creates an anonymous memory-backed descriptor with
`os.memfd_create`, writes the bounded source, rewinds it and passes it explicitly to the selected
shell through `/proc/self/fd/N`. Application input still uses fd 0. No guest-directory entry is
created, and the source text appears in neither argv nor the helper environment. This is a Linux
implementation of the source-descriptor contract, not a portable Darwin mechanism. The parent owns
and closes the descriptor; the process core borrows passed descriptors without modifying their
parent-side flags. No sealing or same-user isolation claim is added.

The core exposes only the public process-launch controls this consumer needs: working directory,
explicit passed descriptors, and new-session creation. Existing workstation carrier defaults stay
unchanged. The helper selects `shell=False`, `close_fds=True`, `preexec_fn=None` and
`start_new_session=True`, matching the audited native launch candidate. A session is not a cgroup
and cannot supply MANAGED cleanup. Normal wait observations remain distinct from eager start proof;
no synthetic `STARTED` record is emitted.

The first local source-descriptor experiment ran on distribution CPython 3.11.2 and Linux
6.1.0-52-arm64. Both `/bin/sh` and `/bin/bash` received 16,640 binary stdin bytes independently of
source, returned separate exact binary streams and exited 255. This measures local descriptor
mechanics only, not carrier delivery, native guest availability, identity transition, or operation
lifetime. The helper must not turn the workstation observation deadline into an implicit guest
runtime limit. Its production lifetime policy still requires the separate target-side owner.

Sensitive and discard modes require the pending [carrier I/O candidate](carrier-io-lld.md), or an
equivalent jointly accepted seam. The method signature stays:

```python
carrier.execute(invocation, *, io=carrier_io, deadline=deadline)
```

After SSH consultation, preparation supplies `SinkOutput(stdout, stderr, require_live=False)`. Each
destination is a bounded borrowed sink endpoint. The stdout endpoint is the transport-owned
incremental frame decoder; the stderr endpoint is a bounded raw-discarding diagnostic sink. A QGA
carrier may deliver its buffered response to the same endpoints after polling. `require_live=False`
therefore says where output is delivered, not that the channel streams it live. Public direct
streaming separately uses `require_live=True` and requires `ChannelFeatures.live_stdio` before
dispatch.

```python
output = SinkOutput(
    stdout=frame_decoder,
    stderr=diagnostic_discard,
    require_live=False,
)
```

Carriers only pump raw chunks and report transport-level dispatch, local status, raw completion, and
I/O failure. They do not parse frames, infer application status, or manufacture stream precision.
Preparation owns the decoder and borrows it to the carrier through the sink endpoint. Writes are
non-blocking and bounded, consume the same deadline, and stop before `execute` returns or raises. A
sink failure maps to `Failure.OUTPUT` with partial carrier evidence and no replay.

For sink delivery, a carrier never retains raw stdout/stderr bytes in `CarrierReport`, even for
ordinary calls. It transiently forwards them and reports disposition `DELIVERED`, not `STREAMED`,
because buffered QGA delivery is valid. The decoder incrementally keeps at most one 8 KiB record
plus bounded control state. For ordinary capture it retains only decoded application frames,
optionally in an owned 0600 workstation spool. For sensitive or discarded output it rejects any
application data frame, retains only schema-validated control facts, and drops raw hook/reflection
and mixed carrier stderr bytes. Thus a startup hook that echoes secret stdin cannot place that text
in a result, exception, log, or carrier report.

This changes the current meaning of `CarrierIO.sensitive`, whose proof implementation suppresses all
bytes before a decoder can inspect them. The production sink mode must forward bytes transiently
while forbidding raw retention. Existing `Capture`/`Discard` proof behavior can remain until the
joint carrier change lands, but it cannot implement public results. The SSH and QGA lanes must run
the same sink/decoder conformance tests before this seam is accepted.

## Inline and staged delivery

The public contract has no provider-size switch. Preparation chooses one of two private mechanisms
without changing invocation meaning.

### Inline path

The inline manifest is canonical ASCII with base64 fields. It carries literal argv or source,
composed environment, cwd, shell/startup selection, identity expectation, finite stdin, output
policy, and nonce. Source and stdin decode into separate descriptors. The complete encoded manifest
has one conservative transport-owned limit proven across every required carrier; 32 KiB is the
candidate, not an accepted constant until QGA whole-request testing confirms it.

The bootstrap argv contains only fixed helper source, non-sensitive protocol constants, a trusted
runtime path selected by target composition when needed, and the nonce. It contains no application
command argument, source, environment value, cwd, or stdin bytes. Inline delivery installs no guest
file. The measured buffered proof uses no Python or Agentworks installation; the production helper
may use the explicitly approved early guest Python prerequisite, subject to Bookworm compatibility
and a new no-staging proof. Readiness never installs it.

### Private staging path

If the manifest or requested capture exceeds the inline bound, normal execution allocates an
operation-owned 0700 scratch directory under a core-selected root. The caller cannot name or retain
it. Source and stdin remain separate fields of the canonical request. A staged fixed helper may
store its exact packaged prefix and canonical request together as one prepared input stream in a
0600 object. Its trusted bootstrap independently verifies the code-prefix length and hash before
loading modules; request bytes are never code. Ordinary captured streams use separate 0600 objects.
Sensitive objects may exist only for the active operation and are removed on every observed terminal
path; uncertain cleanup is reported and remains owner debt rather than being declared absent.

The private transfer substrate is below both public execution and FileAccess. It supports only
create-owned-scratch, write-at-known-offset, verify length/digest, read-bounded-range, and
remove-owned-scratch. It accepts no caller destination, shell fragment, callback, ownership, or
mode. File helper asset delivery must consume this same substrate, bounds, and integrity protocol;
it does not define a second numbered-part bootstrap or an independent chunk size. File upload may
also reuse the substrate without exposing command access. Neither use grants the recipient public
`run`.

The private `_scratch.py` increment implements these destination-side object operations beneath a
borrowed parent descriptor, including exact duplicate comparison, whole-object verification and
identity-bound cleanup debt. `_scratch_wire.py` carries strict active references, verified
references and cleanup debt. The private stage family already carries begin, exact-offset chunk,
ownership reconciliation and cleanup over a carrier. Its file adapter also binds destination
context; generic helper preparation must use core-fixed parent selection and existing execution
custody rather than construct a public FileAccess or file-call obligation. Known acquisition facts
survive handled creation interruptions, but Python bookkeeping is not signal-atomic and filesystem
calls have no hard interruption bound. Native macOS, complete execution composition and
whole-request QGA bounds remain separate gates.

The current stage wire uses a conservative 12 KiB raw chunk bound, below the scratch primitive's 24
KiB ceiling, with an exact offset, total expected length, chunk SHA-256 and whole-object SHA-256. A
lost chunk acknowledgement permits another control request only after the owned scratch identity and
fixed range make the write demonstrably idempotent. Blind append, application relaunch, or fallback
to another carrier is forbidden. Final whole-object verification precedes `LAUNCHING` or execution
of a delivered file-helper asset. This shared delivery mechanism does not establish runtime
availability: the approved early guest Python prerequisite still needs implementation and
phase-specific proof. macOS hosts must supply the separately approved preinstalled compatible
interpreter.

### DIRECT prepared-input integration order

Connect normal DIRECT execution to the existing prepared-input stage through a Linux VM vertical
slice with finite input and a finite execution deadline first. This is implementation ordering, not
a reduction of the complete target surface. Retain the actual stage on the original
`ExecutionOperation` before its first effect and use the existing token-only obligation, bounded
chunks, verified loader and cleanup controller. The staged object contains the exact packaged helper
prefix followed by the canonical request; source and application input remain separate data fields.
It is not a second application launch or a second general transfer mechanism.

The outer loader enters through the existing root/full-guest checkpoint. After whole-object
verification, its fixed first-party bootstrap re-enters the existing numeric credential admission
for the selected workload identity before loading the application helper. A root-owned input stage
does not authorize running the workload or manipulating its files as root. Bind identity and helper
source in core preparation, never by compiling manifest contents. Keep activation closure distinct
from application completion and preserve the actual activation delivery until closure is proved.
Keep helper custody and prepared-input cleanup as separate outcome facts. A later stage-cleanup
failure retains the owner and prevents cleanup confirmation without erasing independently known
application status or verified output. Reserve normal execution admission across chunk delivery and
transfer that reservation to the actual activation call; another execution helper must not enter
between chunks. This is operation-local admission, not a new cross-file-operation lock. Releasing
that reservation never discards retained stage or activation cleanup custody.

A separate fixed prepared-input entry may accept the larger canonical request. The small inline
entry and its no-staging readiness limits stay unchanged. Choose the delivery before application
dispatch; a size refusal may select staging, but uncertain dispatch never permits another launch.
Validate the complete staged activation and every chunk against the existing provider envelope,
including the Proxmox 65,536-byte whole-POST floor and 10,000-byte stdin case.

The existing loader consumes a finite activation request to EOF and borrows the staged file as
stdin. That does not establish staged live-input support: a live source cannot be consumed to EOF
for activation, nor replaced with the staged file's EOF. Complete the separate finite control,
prepared-request and application-input descriptor handoff before enabling that combination.
Platform-host/Darwin staging, explicit unbounded budgets, captured-output spooling and
parent/recovery continuations also remain required before the complete additive surface is accepted.
Do not invent a finite execution timeout, require host administrator setup, or generalize the entire
recovery binding merely to claim this first slice complete.

### Late managed-start control

Bulk transfer must precede the initial OPERATION lease sample. Source and stdin can each be 16 MiB;
thousands of bounded transfer requests cannot consume that 60-second admission window. Do not
increase the lease, renew it before settled start, lower the input limits or authorize a stale
sample. The generic staged loader remains opaque to managed leases.

The private managed-start codec uses one version-two format for both inline and staged delivery: an
immutable canonical body followed by a fixed 512-byte dispatch-control slot, using the existing
`MAX_LEASE_BYTES`. The slot contains the existing canonical lease JSON padded with ASCII spaces, or
padded literal `null` for independent lifetime. It needs no extra wrapper, version or base64 layer.
The lease's closed schema has fixed quoting/escaping within one bound operation; space padding makes
its total and complete HTTP-input length independent of clock digit count. Lifetime is fixed before
preparation; the two lifetime modes need not have identical quote counts.

The body contains the ordinary exact request assets without a lease. Reuse body-only validation and
the existing complete-control lease headroom before any staging effect; a near-limit lease-free
control alone does not prove that the reconstructed full control fits. Recalculate the outer request
bound too. A body-only representation never authorizes dispatch. The complete decoder rejects any
lease already in the body, separates the exact final slot, validates canonical lease or null,
reconstructs the control asset and uses the existing full request validator. There is no second
staged grammar or compatibility decoder for the unreleased version-one start wire. This codec is
implemented; staging orchestration and actual late sampling remain to be connected.

Declare scratch length as packaged prefix plus immutable body plus that fixed slot. Transfer only
the prefix/body before initial keeper sampling, stopping exactly at the slot offset without writing
placeholder padding. After sampling, freeze one actual tail and append it in one existing bounded
range. Hash it exactly once. No previously written range changes; a partial or uncertain append
retains debt rather than permitting overwrite, resampling or activation replay. The small activation
then uses the same whole-object loader and original bootstrap. Guest lease checks before and after
request publication remain required: tail delivery, verification, decoding and service admission
still consume the post-sample window.

This ordering is unfinished implementation work. Prove stable body/total/envelope sizes, maximum
control headroom, a real transfer lasting beyond one lease window followed by a fresh sample, wrong
or missing tails, expired admission, exact launch binding, independent null control and uncertain
append/activation/cleanup. Tokens precede creation, and owner/debt lifetime remains independent of
lease validity and helper observation. Neither a timeout nor the loader's remaining allowance proves
remote closure.

The private stage owner uses a separate token-only cleanup row, registered and marked possible
before BEGIN. It does not overload the immutable managed-start/run obligation or retain payloads,
active references or activation digests in that row. Settling an ordinary helper borrow does not
resolve scratch debt. Chunk progress and active loading remain same-process facts; process-loss
reconciliation may recover only historical cleanup authority. Missing or invalid acquisition
receipts remain uncertainty, not absence or permission to recreate a stage.

Production composition must retain the stage before effects and preserve actual helper delivery
custody for drain. A cleanup-only continuation may use a fresh finite deadline after preparation
expires, but routing must receive that fresh budget too. Interrupted bookkeeping needs exact local
reconciliation without another helper effect. These parent and recovery continuations remain open;
the stage primitive alone does not complete late-lease ordering or native activation.

### Captured output retrieval

Captured stdout and stderr spool separately. Each spool keeps at most `max_bytes + 1`; helper
readers drain the remainder so a caller retention bound does not send SIGPIPE to the workload. The
extra byte proves caller-bound truncation. Completion frames contain safe sizes and digests, then
the workstation retrieves fixed ranges under the original deadline. A timeout after a conclusive
`WAITED`/`FINISHED` pair can retain known application completion with incomplete output. Provider
output limits therefore reduce one observation chunk, not the application's supported output size.

The default capture bound is 1 MiB per stream. It is a caller-visible retention bound, not a
provider maximum. A caller can deliberately choose a larger finite bound. The implementation may
spool locally to avoid holding the selected bound during transfer, but constructing the final public
`bytes` allocates at most that caller-selected amount. Workflows expecting unbounded data use
FileAccess rather than stdout.

#### DIRECT capture composition

Use the existing bounded process capture first, then publish its retained bytes through the existing
scratch primitive before emitting terminal helper evidence. The process pump already retains only
the selected limit plus one byte while draining the rest. This avoids introducing a mutable,
unknown-length scratch format: after child observation, each retained prefix has a known exact
length and digest. Create its ordinary owned scratch object with that length, write bounded chunks
and obtain the existing verified ready reference. The extra in-memory byte proves truncation and
need not be stored. No padding to the caller's maximum, managed-run store, second process owner, or
generic stream-storage framework is needed.

Create these artifacts as the selected workload identity, not through an implicit administrative
file authority. Reuse the core-selected scratch parent and the existing identity, type, link,
receipt and whole-object checks. Fixed helper code chooses names from core-generated tokens;
application source, arguments and file paths never choose them. Discarded or suppressed output
creates no capture objects. The first bounded implementation may retain captured prefixes in helper
memory until publication, as the existing inline helper does. This is bounded by the selected
capture policy, not by provider output capacity. Streaming capture can replace that implementation
later without changing the output or ownership contract.

Publication failure after child observation is a separate normal outcome. This output mode emits one
closed record per stream: either a ready reference bound to its retained-prefix facts, or an
explicit unavailable-capture record with those facts and that token's creation disposition
(`NOT_ENTERED` or `POSSIBLE`). Set `POSSIBLE` before entering scratch creation; only a creation
never entered may report `NOT_ENTERED`. An unavailable record grants no ready reference, verified
local bytes or complete public output. The mode's grammar accepts the actual wait after both
records, then the ordinary failure and terminal records. Thus a handled create/write/verify failure
can retain known application status instead of making scratch publication a prerequisite to
reporting the child's wait. Preserve an earlier process failure; otherwise publication failure
reports `OUTPUT`. Cleanup debt remains independent. If only one stream publishes, retain and
retrieve its ready prefix without inventing the other stream's storage. Lost or interrupted terminal
evidence still cannot establish application completion or a token's non-creation.

The parent retains one exact cleanup binding for the two capture tokens before application dispatch.
It includes the selected target/runtime/workload identity, not application bytes. Creation occurs
inside the single fixed application helper, so the parent must also retain its actual delivery and
closure until capture creation has stopped. A returned ready reference permits exact cleanup; a
token by itself grants only the existing historical reconciliation path. Missing receipts remain
uncertainty. A fixed helper may explicitly attest that a particular creation was never entered, but
missing output or an unavailable reply is not such evidence. Keep this debt separate from helper
custody and known application completion, using the same distinction as prepared-input cleanup.

After ordinary admission stops, aggregate closing may reconcile and remove only these already-owned
capture tokens; it cannot reopen an ordinary borrow or admit new work. Prepared-input and capture
controllers retain their own exact row and protocol checks. They may share the small host-side
mechanism for retaining and observing a closing helper's actual delivery, not a generic resource
cleanup state machine. Aggregate cleanup must not enter another helper while an earlier closing
helper retains unsettled custody. Artifact-only cleanup uncertainty remains separate and need not
prevent other safe cleanup once actual helper closure is proved.

Represent closed remote stream evidence separately from locally retrieved bytes. A nonce-bound
capture reference must agree with its stream-end retained length, digest, retention and truncation
facts. The observer must not require nonexistent inline data frames for that explicit output mode,
nor weaken validation of the existing inline capture/delivery modes. A trusted terminal wait can
establish application completion while capture retrieval is still incomplete. It cannot manufacture
verified local bytes. Each bounded retrieval validates the exact ready object and range; the host
checks ordering, coverage and the final retained length/digest before marking capture complete.
Preserve verified prefixes and known status on later retrieval failure, with explicit incomplete
output and the original failure precedence. Never patch bytes into an observation whose count/hash
still describe different evidence.

Select the fixed capture helper and validate complete activation/read envelopes before effects. Keep
readiness on its small unstaged entry. Host observation, range retrieval and cleanup use the
original selected deadline; expiry grants neither replay nor implicit guest cancellation. After that
budget, retain exact cleanup custody for a separately admitted fresh cleanup continuation. The
existing finite Linux VM input slice is the first composition target. Workload-identity scratch
delivery, token cleanup/recovery, host/Darwin and live/unbounded combinations still require their
own end-to-end proof before the production capture default or full surface is accepted.

#### Host finite prepared input and capture

The next buffered host composition covers Linux and Darwin, finite source/stdin, a finite execution
budget and an explicit finite capture bound. Reuse the existing stage, verified fixed-prefix loader,
capture publication, snapshot ranges and retained cleanup objects. Do not add an SSH-only output
collector, another spool protocol, or a larger provisional inline ceiling. Ordinary inline and
sensitive/discard behavior remain available without forcing scratch creation.

Separate prepared-delivery authority from its current VM-only representation. Preserve the existing
version-one VM cleanup record and numeric root/guest bootstrap unchanged. Add a host-only
version-three record containing the exact owned PLATFORM_HOST scope, actual entry identity plan,
runtime selection and token. Version two remains reserved for the existing in-flight recovery
wrapper. Host records contain no fabricated VM incarnation, boot, guest bootstrap or effect gate.
Their codec rejects mixed shapes, version relabeling and incompatible runtime/identity data before
effects, with the same bounded ledger envelope. Capture reuses that binding plus its existing
workload plan and two distinct stream tokens; child/debt updates retain the host format.

For a host, stage and activation run under the execution call's actual selected ordinary/elevated
plan, not an implicitly privileged root path. Use existing Linux `/tmp` or Darwin `/private/tmp`
scratch admission and identity checks. Adapt the fixed loader to consume the verified held input
under that host identity and the original trusted packaged prefix. It never compiles request bytes
or uses a VM check as an invented host fence. Preserve complete envelope validation, original
nonce/digest/length and one-shot activation, including separate source and sensitive stdin.

Host capture uses the existing bounded process prefix and per-stream publication after actual child
observation, then existing snapshot reads and cleanup under the workload identity. Preserve known
wait/status on publication or retrieval failure, verified prefixes, suppression and per-token
creation uncertainty. All exchanges use the original operation deadline; separately admitted fresh
cleanup uses its supplied budget. Retain actual stage, capture, helper and delivery custody before
effects, and settle closing helpers and completed-call bookkeeping before dependent cleanup or
lifecycle resolution. No receipt or local SSH exit substitutes for remote helper closure.

Keep automatic process-loss recovery refused for these host records before rebinding or dispatch.
Same-process closing uses the exact original retained objects; a missing historical receipt is not
absence or permission to activate again. Native host continuity and predecessor-drain proof remain
separate requirements for later recovery, not prerequisites manufactured by this format.

Prove large separate source/stdin and capture through the host context, Linux ordinary/elevated
selection, Darwin codec/runtime/loader behavior, maximum controls, truncation, suppression, partial
publication/retrieval and fresh cleanup without replay. Local Darwin-shaped fixtures are not native
macOS evidence. Native SSH/macOS validation, host configuration/adoption and the full
surface/default change remain open until their own acceptance. Live and explicitly unbounded staging
retain separate gates.

#### Closed MANAGED capture ranges

Use the existing managed run store and observation owner for closed captures, not a second scratch
store or file-access grant. One `READ_OUTPUT` helper retrieves one explicit byte range, capped at
256 KiB. The helper checks the protected spool's complete length and digest while retaining only the
requested intersection of bounded read chunks. It emits no successful result until that whole check
passes. The response includes the original launch and stream-end facts plus the returned offset,
length and range digest. The collector checks those bindings and retains no more than the requested
page. Its complete encoded stdout requirement includes framing, facts and the runtime prefix; the
provider's capacity must be sufficient before dispatch.

`read_output` may return fewer bytes than its positive `max_bytes` bound when a page reaches this
transport ceiling. Its next cursor is the requested cursor plus the returned byte count;
retained-prefix EOF is true when that next cursor equals the verified total retained length. An
exact EOF cursor succeeds with empty data; a cursor beyond the verified prefix remains a validation
error. Discarded and suppressed streams remain distinct from empty captured streams and retain no
output payload.

Full result collection reads successive pages under the original deadline. Each page has its own
ordinary helper borrow and outcome, matched against the exact stream-end fact first observed. Record
every attempt before reducing its output; stop on failure, unresolved custody or expiry. Keep
independently established application completion and any verified prefix in a normally returned
result when later retrieval fails. Escaping controls keep their original propagation and custody
cause; this does not introduce payload-carrying exceptions. A successful page alone never
establishes complete output. There is no paging wrapper that claims several dispatches as one,
renewed deadline, uncertain replay or new persistent read session.

Repeated whole-spool hashing deliberately avoids indexes or caches. At the private 16 MiB retained
ceiling, full retrieval needs 64 pages and scans 1 GiB per stream; a 1 MiB capture needs four pages
and scans 4 MiB. Native usable completion within the caller's budget still requires proof. This
closed-output work does not finish DIRECT spooling, the production capture default, active job
output, helper staging or readiness's no-staging guarantee.

## Readiness and minimal substrate

The composition root binds a private `inline_only=True` constraint to preflight/runup readiness
targets. It is not a public performance flag. This constraint:

- rejects MANAGED launch, private scratch, output spooling, and an over-limit manifest;
- rejects `login=True` or `interactive=True`;
- uses EOF or finite inline stdin and a small finite capture bound;
- runs no install, target realization, helper upload, or implicit probe before the invocation; and
- returns a typed refusal before dispatch when the request cannot remain inline.

This proves that Agentworks preparation adds no guest write. It does not prove an arbitrary caller's
command is read-only, and it cannot prevent pre-existing SSH account hooks from running before the
bootstrap.

The Linux inline helper's current measured prerequisites are Bash 5.1+, GNU `env` and `base64`,
`getent`, `id`, and `/dev/fd`. The staged Linux path additionally proposes `mktemp`, `chmod`, `dd`,
`head`, `cat`, `wc`, `sha256sum`, and `rm`; sudo or setpriv is conditional on the identity plan.
Every path is fixed by the substrate, not PATH lookup. Python, systemd, Tailscale, and an installed
Agentworks helper are not prerequisites of that measured buffered proof. The
[guest-runtime ruling](frd.md#file-safety-and-guest-runtime-rulings) allows early Python for the
production design, but availability and no-staging invocation must be proved before enabling it.

Darwin platform-host preparation uses the same manifest, frames, and result interpreter with
preinstalled Python 3.11 or newer. Interpreter selection and validation belong to shared
preparation, not an SSH carrier branch. Detect a missing interpreter, unsupported version, and the
system-default Xcode shim separately. Do not execute a known shim to discover whether it prompts for
developer-tool installation. Report the host and selected path, the prerequisite failure, and the
remedy without a traceback, implicit installation, or fallback to another identity or route. An
execution/connection failure is not proof that Python is absent. Prove these cases on macOS,
including absence of an installation prompt and no readiness writes; no detection algorithm is
accepted by this paragraph.

The current Linux bootstrap cannot be relabeled Darwin-compatible: account lookup and descriptor,
metadata, and process APIs differ. The Darwin helper must prove the supported platform mechanics
without creating a circular bootstrap dependency. Until that proof passes, production platform-host
scripts and managed jobs remain blocked rather than downgraded.

The private buffered DIRECT inline candidate now has a Darwin implementation. It keeps the common
manifest, framed evidence and result interpreter, but checks identity at fixed fresh-exec entry and
uses an independently owned `/dev/fd` source pipe rather than Linux anonymous-memory descriptors.
The non-blocking writer supports bounded cancellation; uncertain thread or descriptor closure
retains actual custody, preserves application stream/wait facts and withholds `FINISHED` and
successful helper exit. Linux saved-ID and source checks remain unchanged. Worker `c9bd8f1dd`
reports 96 focused and 546 adjacent passes, including packed current/Python 3.11 checks, plus strict
typing across 1,507 sources. These are local fixtures, not native Darwin evidence. Independent
combined review, native macOS proof and broader platform-host identity/files/context composition
remain open; Darwin MANAGED and live foreground calls are still refused.

### Darwin inline prerequisite candidate

Runtime selection belongs to shared preparation and target composition, not SSH. The candidate uses
one explicitly bound absolute interpreter path, or the fixed candidates `/opt/homebrew/bin/python3`
and `/usr/local/bin/python3` in that order. These cover the documented Homebrew prefixes and
Python.org installer links. A custom installation requires an explicit bound path; this candidate
adds no public configuration field or PATH discovery.

An explicit path is the sole candidate. Otherwise, the first existing directory entry wins,
including a broken symlink. Missing entries allow selection of the next fixed path; an unusable,
unsupported, or shim selection does not trigger fallback. This makes a stale installation an
actionable failure rather than silently changing the runtime. No interpreter is executed during
selection.

A fixed, non-login system shell checks the selected object for regular-file and executable status,
then compares its device/inode identity with `/usr/bin/python3` using `test -ef`. The comparison
rejects direct selection and symlink or hard-link aliases without executing the system shim or
implementing a path-resolution framework. It does not identify an arbitrary copied shim or malicious
wrapper; the selected installation and target account are trusted prerequisites. Native macOS proof
must establish the chosen shell's identity-test behavior. If no independent candidate exists, the
presence of the known system path gives a shim-only diagnostic; otherwise the diagnostic is missing
Python. Never invoke `xcrun`, `xcode-select`, a package manager, or the shim to classify that state.

Selection and helper entry share one carrier invocation. The selected interpreter runs fixed source
with `-I -S -B`: isolate Python environment/user-site settings, skip site initialization, and
prevent bytecode writes. A small trampoline checks the version before entering the Python 3.11
helper in the same process. Python 3.4 introduced `-I`; an older interpreter that rejects the
startup flags cannot emit the prerequisite response and follows the observation-failure path. This
is not a preliminary readiness probe or staging operation. A valid nonce-bound prerequisite response
reports a closed failure category. A carrier failure alone establishes no prerequisite category;
missing, truncated or invalid prerequisite responses remain unknown, never evidence of an absent
interpreter. Preserve a complete valid prerequisite record already received even if stdin delivery
or later observation also fails. It reports what this invocation observed, not helper quiescence or
permission to retry. Diagnostics use the bound host, established selected path, failure category and
remedy, not raw interpreter or account-shell output.

This remains an implementation candidate. Acceptance needs Intel and Apple Silicon macOS evidence
for independent installations, explicit paths, shim aliases, broken selections, old interpreters, no
developer-tools installation prompt, no readiness writes, and interrupted observation. Primary
sources and their limits are recorded in
[runtime prior art](prior-art-research.md#macos-python-prerequisite).

The local [runtime experiment](../../../cli/tests/execution/runtime_prerequisite_probe.py) uses the
shared bounded subprocess pump and a fixed minimal environment. Its fixture paths exercise actual
POSIX object/alias checks without touching a Mac or executing a system shim. Local Python 3.11.2
establishes the positive trampoline path; the unsupported-version response is synthetic, not an
observation of Python 3.9 or 3.10. No production selector, configuration field or helper wiring is
enabled by these tests. The shared pump's process-construction interruption gap also remains open.

### Shared runtime admission

Promote the candidate into shared preparation, with target composition explicitly selecting Linux or
Darwin rather than inferring the destination from the workstation. Linux uses `/usr/bin/python3`
unless an explicit absolute path is bound; Darwin retains the candidate rules above. Only Darwin
treats the system Python path as a shim. Neither choice adds a public configuration field or an
implicit preliminary probe.

The old-compatible trampoline checks the Python version and the fixed common bundle-loader imports
(`base64`, `bz2`, `hashlib`, `json`, `os`, `sys`, `types`) before entering helper source. This is
loader admission, not proof of every operation-specific prerequisite. Account-database support,
filesystem features and launch mechanisms retain their existing operation-level checks. Selection
and admission leave stdin untouched; helper entry preserves the original nonce argument.

One bounded nonce-bound prerequisite record precedes the family transcript. For pipe I/O, a shared
prefix sink consumes that record and, only on readiness, forwards subsequent bytes unchanged to the
existing family sink, including partial writes and temporary sink stalls. A refusal never enters
that collector; trailing bytes after refusal invalidate the prerequisite transcript. Closed
observations are ready, missing, shim, unusable, unsupported version, missing required modules and
unknown, with the selected path derived from a core-bound candidate index rather than guest
diagnostic text.

Each exchange keeps this prerequisite observation beside unchanged carrier facts. Its operation
observation is absent when admission did not occur; readiness itself is not application start or
operation success. Complete refusal evidence can coexist with incomplete input delivery. The current
pump may stop collection on an input failure before receiving any refusal, so this design does not
promise a specific diagnosis for every failed invocation. It adds no refusal-time stdin drain, retry
or ownership release. Explicit EOF readiness is separate acceptance evidence, not a cache or
permission for a later invocation to skip admission.

Begin integration with the two private account lookups, then adopt the same boundary in the file,
inline and terminal paths. Their distinct operation protocols and evidence rules remain unchanged.
Native macOS acceptance and existing-guest bootstrap remain separate gates.

The private account implementation now uses this boundary for both lookup kinds. Its local tests
exercise Linux selection, real Python 3.11 lookup and fixture-based Darwin selection, not native
macOS acceptance. The seven file exchange families also adopt it, applying the selected identity
transition around the selector and keeping absent file observations separate from prerequisite
refusal. Buffered inline preparation also requires explicit runtime selection, placing prerequisite
observation before its unchanged helper framing and retaining one-attempt semantics. The file and
inline implementations have completed private review. Private terminal preparation now composes
runtime admission as described below and is under review; carrier integration remains open. The
original runtime experiment above remains separate evidence rather than production dispatch.

Terminal admission cannot simply reuse the pipe prefix unchanged. A local Linux `openpty` experiment
at `75400d16` ran the actual runtime selector with a fixed no-op helper: the process exited zero,
but the terminal line discipline changed the binary-written READY record's LF to CRLF before the
terminal helper could configure raw mode. The binary-writer correction for Windows does not prevent
this separate transformation. Terminal composition must prove prerequisite observation before its
payload gate under actual initial terminal settings. Keep the non-terminal parser strict and
application bytes unchanged after handoff; do not normalize the guest output stream to make a
readiness fixture pass. This is a local mechanism finding, not native terminal acceptance.

Further local PTY checks at `d95a5c47` observed the same selector emit LF with output processing
disabled, CRLF under default settings, and an entirely uppercase CRLF record with `OLCUC` enabled.
The existing terminal handoff tests already require uppercase-output-mode support. Extend the
existing readiness sink with one bounded prerequisite phase: suppress setup noise, recognize the
bound nonce, and accept only the exact canonical or entirely uppercase control-record spelling with
LF or CRLF. Normalize that record alone before the shared closed decoder. A malformed, oversized or
truncated nonce-bound candidate fails closed; a later record cannot repair it. Reuse one generated
nonce, lowercase for runtime admission and uppercase for the existing terminal markers.

Runtime readiness only advances to waiting for payload readiness. Bootstrap bytes remain withheld
until the guest helper has configured raw mode and supplied its existing payload-ready marker.
Preserve split/coalesced records, downstream partial writes and stalls, and exact application bytes
after handoff. A complete terminal refusal is an immutable observed control-record fact and stops
the endpoint without releasing bootstrap bytes. It does not claim the pipe collector's whole-stream
validation: terminal setup noise is already permitted, and immediate refusal prevents observing all
later output. Later terminal failure or temporary-buffer disposal must not erase that prerequisite
fact.

Preparation remains separate from carrier integration. The transport-owned execution wrapper must
finalize readiness when the carrier attempt ends, including a truncated pre-handoff stream; the SSH
carrier does not parse these control records. The inspected SSH snapshot `34a4eb71` has no terminal
endpoint or production caller of this preparation helper. Candidate tests therefore cannot stand in
for the joint terminal implementation and native acceptance gates.

### Private Linux guest bootstrap packaging

Hardened guests require a narrow system-fact boundary without running ordinary file effects as root.
The private numeric bootstrap uses explicit root entry only to open the fixed read-only init-stat
descriptor, then sets and verifies the actual target's numeric credentials and capabilities before
consuming stdin or loading operation code. It permits only system Python on Linux; platform hosts do
not select this path.

`RootGuestProgram` selects inline fixed source or one fixed stdin prefix. After establishing the
selected fixed delivery, its loader installs only the canonical guest-identity pair. The bootstrap
binds a fresh bounded reader from the held descriptor and requires the full expected guest before
loading remaining trusted modules in dependency order and entering the family body once. The
packager supplies the canonical pair even where a family lacks it, rejects duplicate modules, and
rejects unknown local dependencies in that early pair before dispatch. The identity module is not
reloaded. The outer descriptor cleanup encloses every phase; application exec cannot inherit the
descriptor. Runtime readiness is not this guest checkpoint, and errors after the checkpoint retain
body-failure semantics rather than becoming admission refusals.

The ordinary file bundle keeps host response parsing, gate control and materialized reads outside
guest families that do not consume them. Trusted fixed root source uses the existing compaction and
compressed ASCII delivery; there is no additional stdin codec, duplicate observer prefix, staged
executable or automatic fallback. Carrier preflight still counts actual aggregate input, argv,
quoting and provider serialization. A valid protocol request can exceed a carrier's envelope and
must refuse before provider dispatch; its decoder ceiling is not a delivery guarantee.

Early fixed guest operations use a separate named-account admission leaf because they precede
numeric preparation and full guest observation. It resolves the core-bound account through the
system account database, shares the fixed descriptor and exact credential transition, and loads its
fixed first-party body only after admission. The body receives a fresh bounded init reader and owns
stdin. Failure never substitutes root for the intended non-root account. A non-root target loses the
bootstrap's root credentials; an explicit target resolving to UID 0 retains its intended authority
without a demotion or confinement claim. This does not make the numeric path's mandatory full guest
optional or expose arbitrary plugin source through a privileged entry.

These are private implementation increments, not production adoption. Ordinary helper and later
service entry still need production guest fencing and durable cleanup composition. Native post-drop
init reads, credential transitions and complete workflows remain acceptance gates.

The private adoption separates the initial fixed guest-facts route from ordinary delivery. WSL binds
that route to root in the same selected distribution and executable, while the named bootstrap
selects the configured body account. The ordinary binding remains the admin carrier. Initial
preparation wraps the selected probe carrier in the existing operation borrow; it does not execute
directly, replay or fall back to the ordinary route. A loader-only canonical identity bundle binds
the held reader before entering its existing `main` once. Hold and query consumers admit the runtime
record separately from READY or query evidence. New hold payloads distinguish launch and body
accounts; old payloads retain their original same-user semantics and version during recovery. This
sequencing does not close the native or full production-fencing gates.

Later numeric helper preparation binds the verified full guest and the prepared root-entry plan in
one core-owned context, independently of each request's body identity. The existing admin carrier
can use its fixed non-interactive sudo entry; root delivery can use direct entry. Neither choice
changes ordinary delivery or a plugin's exposed authorization. The numeric bootstrap then switches
to the selected body's exact credentials before loading its family. Gate-control, snapshot, staging,
publication, read, inventory, object and metadata exchanges accept this private context and select
matching fixed argv and stdin prefixes, refusing competing guest facts before dispatch. All family
entries, including reconciliation and cleanup, forward the context. Canonical reader binding also
preserves each body's fresh effect checks rather than replacing them with cached evidence.
Production VM composition must bind the context once in operation-owned state and retain it through
all continuations and recovery. Optional context-free private candidates are not a production VM
fallback. Buffered inline preparation now accepts the same explicit context through the existing
INLINE root program, with no stdin prefix or manifest change. The full guest checkpoint precedes
body entry and request consumption; existing identity and exact-wait evidence remain required.
Missing terminal evidence after bootstrap refusal leaves the application unknown. Private execution
state now requires the exact selected target and validates its owner scope and bootstrap boot before
forwarding that context into preparation. Body identity remains call-specific. The private WSL2
native factory requires both prepared identity plans and constructs one bootstrap from the elevated
plan and actual full guest. File and buffered execution state share that immutable context; missing
preparation refuses before exposing views. Fresh recovery, other platform composition and the
complete outer managed-helper boundary still need adoption. The fixed service controller entry
separately uses numeric admission and the INLINE loader before controller modules or store effects,
with its exact controller identity and full guest transported as bounded argv data. Its admission
tests are simulated; native controller and later workload-child admission remain unproved. These
private compositions do not prove native credential transitions or complete production RunContext
delivery.

## Public result and check behavior

`ExecutionResult` carries the shared carrier `Dispatch` value, application state, optional
application status, separate `ExecutionOutput` values, an optional `ExecutionFailure`, and whether
owned cleanup was confirmed. An independent `deadline_exceeded` flag preserves expiry even when a
different failure is primary. It does not compress those facts into a return code.

`ApplicationState` is `NOT_STARTED`, `STARTED`, `COMPLETED`, or `UNKNOWN`. `NOT_STARTED` needs
positive evidence, such as local refusal before dispatch or a trusted pre-launch `FAILED` frame.
`STARTED` needs proved eager exec acknowledgment. `COMPLETED` needs a terminal transcript plus
either proved eager entry or the selected fixed-helper retrospective normal-completion evidence.
`UNKNOWN` covers every gap, including `LAUNCHING` alone, a signaled child without independent entry
evidence and every direct wait outside the selected rule. `status` exists only for `COMPLETED` and
is the honest union `WaitCode | ExitCode | Signal`; the shell experiment produces only `WaitCode`,
while the selected inline helper produces exact `ExitCode` for its normal waits.

`ExecutionOutput` contains retained bytes, `complete`, and the shared `Retention` value
`CAPTURED`/`DELIVERED`/`DISCARDED`/`SUPPRESSED`. Delivered output records acceptance by the
requested sink, not captured bytes or durable storage. Intentional discard or suppression does not
claim empty guest output and does not by itself make a completed zero result fail. Captured overflow
returns the prefix up to the caller bound with `complete=False` and `failure=OUTPUT_LIMIT`. A
transfer or frame failure similarly leaves partial bytes explicitly incomplete.

`ExecutionFailure` is a closed transport-neutral fact such as `PREPARATION`, `DELIVERY`, `DEADLINE`,
`OBSERVATION`, `PROTOCOL`, `INPUT`, `OUTPUT`, `OUTPUT_LIMIT`, or `CLEANUP`. Safe phase and target
metadata may accompany it. Provider exception text, raw account output, payload values, remote
scratch paths, and credentials never do.

`result.ok` is true only when application completion is known, the exact exit or wait code is zero,
no operational failure affects the requested semantics, the deadline has not expired, owned cleanup
is confirmed, and requested captured or delivered output is complete. It is never true for `STARTED`
or `UNKNOWN`, and no unknown status is converted to zero.

Request validation, unavailable optional features, expired target lifetime, control-flow
interruption, and eventually activated authorization denials raise regardless of `check`. After an
attempt begins:

- `check=False` returns `ExecutionResult`, including known nonzero exit, timeout, delivery failure,
  protocol failure, partial output, and uncertain dispatch.
- `check=True` returns only an `ok` result. Otherwise it raises one checked-execution error carrying
  the exact same safe immutable result.
- `KeyboardInterrupt` and cancellation of the local caller propagate after bounded local cleanup;
  neither is converted to a checked-execution error or guest cancellation claim.

The minimal error addition is one `CheckedExecutionError(ExternalError)` with a `.result` attribute.
The result's structured facts, rather than a growing exception subclass matrix, distinguish known
guest failure from uncertainty and incomplete output. The lead approves this base after inspecting
the CLI's external-error handler and ordinary traceback logging: the exception message is safe fixed
prose and the result contains no raw provider exception. `ExecutionResult.check()` is an
intentionally context-free convenience: it returns the same instance when `ok`, otherwise raises
this error with implicit exception-context rendering suppressed. The target's `check=True` path
instead uses the contextual checker or its private reducer seam, which provides a safe logical
target identity and only a privately proved phase refinement. `check=False` returns the result
unchanged. Python may retain an active caller exception as implicit context; suppression prevents
its ordinary traceback rendering, not object retention. Renderers use safe result fields for detail,
never output bytes by default. These result values do not supply evidence: producer and reducer
acceptance remain separate implementation gates.

## Reuse by supervisor and file helpers

The supervisor consumes the same validated invocation manifest, identity plan, sensitivity, and
frame decoder. Its private launch control substitutes durable result/output storage for the direct
bootstrap, but must emit the same application evidence schema. `run(..., profile=MANAGED)` can call
private launch and wait without a public `start` grant. This LLD does not select unit identity,
lease, stop, retention, or stale-reference mechanics owned by the lifecycle design.

File helpers consume only the private scratch transfer and framed control-result mechanisms. A
packaged helper asset is another operation-owned object delivered through the same fixed-offset
chunks and whole-object verification, not a file-specific bootstrap protocol. File operation schemas
accept bytes, paths already authorized by their own layer, and fixed action enums, never `Command`,
`Script`, public argv, or callbacks. Internal helper execution is authorized as part of the public
file action. It neither exposes `ExecutionAccess` nor requires a recipient `run` grant. File policy
composition follows the
[file LLD](file-operations-lld.md#immediate-mechanics-versus-deferred-permission-activation).

## Implementation slices and focused tests

The production implementation proceeds in reviewable slices without wiring RunContext early:

1. Add `models.py`, move `Command`/`Script`/`Shell`, update all transport-owned proof imports and
   constructors, and ask the SSH owner to update its one fresh-process import. The internal
   preparation API changes; buffered carrier runtime behavior and types do not.
2. Add result models and pure frame encoder/decoder tests. Malformed, reordered, duplicate,
   oversized, wrong-nonce, missing-terminal, truncated, and post-terminal records must never produce
   completion. A valid `FINISHED` has no transcript checksum and still requires the preceding start,
   wait, and stream evidence.
3. After explicit SSH consultation, add the accepted sink output to `CarrierIO`; prove both carriers
   only pump bytes, retain no raw sink-mode data, stop using endpoints before return, and preserve
   partial carrier evidence on decoder failure/interruption.
4. Replace the proof bootstrap with the production Linux inline protocol. Prove literal argv,
   source/stdin separation, all byte values, env/cwd, identity verification, shell startup, exact
   required start acknowledgment, wait 0/1/125/126/127/143/255 precision, bootstrap failure, and
   account-hook noise.
5. Add private staging and spooling. Prove boundary-minus/at/plus envelope and chunk sizes, large
   source and finite stdin, exact offsets, short writes, lost chunk acknowledgment, whole digests,
   output larger than a provider response, caller-bound truncation, partial retrieval, and owned
   cleanup without application replay. Exercise the identical transfer implementation and settled
   chunk bound for file helper asset delivery; no second numbered-part/bootstrap path is permitted.
6. Add target-level unchecked/checked interpretation. Mutate each evidence input independently so
   raw zero, raw 255, stream ends, suppression, discard, or `STARTED` alone cannot become success.
7. Run independence tests with every retirement module unavailable before any production wiring.

Sensitive tests use synthetic canaries and inspect argv, environment, reports, exceptions, logger
records, local spools, and guest scratch after cleanup. They require a distinctive nonzero terminal
result so suppression without payload execution fails. Tests assert structured facts and behavior,
not authored error prose.

The readiness lane records guest scratch before and after and rejects any stage/spool helper call.
It covers command and script output framing, EOF, over-limit refusal before dispatch, inherited
`BASH_ENV`/`ENV`, and an SSH account hook that runs independently of preparation. Tests do not claim
the hook itself is read-only.

## Hypotheses and live-proof gates

These claims require authorized live evidence before the public surface is wired:

| Hypothesis                                                                            | Required evidence                                                                                                                                                                                                                                                    |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The 32 KiB inline and 24 KiB raw chunk candidates fit every supported carrier request | Whole-request measurements through SSH and QGA on both supported Proxmox majors, including framing overhead and refusal before dispatch above the settled bound.                                                                                                     |
| Linux startup argv has the stated sh/bash behavior                                    | Fixed and user-default shells for all four login/interactive combinations on supported Debian/Ubuntu images, including startup failure, stdin use, output, cwd/env changes, and elevated root lookup.                                                                |
| Direct execution has valid completion evidence on the no-staging substrate            | Prove the selected retrospective normal-completion rule on every no-staging image, including all application exit values and UNKNOWN after unproved entry, pre-exec signals or unavailable wait. Eager STARTED requires its separate exec acknowledgment if offered. |
| Shared identity transition is exact                                                   | SSH direct, passwordless sudo, QGA root demotion, supplementary groups, unavailable elevation, and proof that ordinary QGA execution never remains root.                                                                                                             |
| Decoder filtering is safe under real delivery                                         | Sensitive hook reflection, split/short reads, duplex pressure, 255/drop after proved `STARTED` plus `FINISHED`, drop before `FINISHED`, provider truncation, local interruption, and no raw retained bytes on Linux, macOS, and Windows workstations.                |
| Private staging handles provider limits without replay                                | Large source/stdin and multi-megabyte separate output through SSH and QGA, lost write/read acknowledgments, deadline at every phase, exact range verification, and independently observed guest cleanup.                                                             |
| Proposed Linux helper tools exist at bootstrap                                        | Base image, pre-Phase-B recovery, demoted admin, elevated root, and supported guest release inventory with fixed executable paths and versions.                                                                                                                      |
| Darwin can satisfy the same contract without hidden installation                      | Pre-guest Remote Lima host execution on supported macOS, explicit account-shell lookup, no-staging bounded readiness, binary source/input separation, large staging, disconnect behavior, and cleanup.                                                               |

No successful local fixture substitutes for these cells. A missing prerequisite is a production
blocker or an operator disposition, not permission to report an optional feature or choose a weaker
result.

## API seam decisions before coding beyond value extraction

The lead should settle these points with the named owner before assigning broader code:

1. **Carrier sink extension, with SSH owner:** approve `SinkOutput`, its borrowed endpoint
   lifecycle, `require_live` meaning, and `DELIVERED` report disposition. The `Carrier.execute`
   signature stays unchanged, but current sensitive suppression semantics must change specifically
   for sink mode.
2. **Direct execution evidence, with preparation and platform owners:** prove the selected
   retrospective normal-completion rule on the actual no-staging production path, including
   readiness and the full exit range. A mechanism offering eager application STARTED or precise
   signaled-child attribution needs independent successful-exec acknowledgment. A managed launch
   acknowledgment is a different fact; neither it nor reserved wait-code inference can substitute
   for application completion.
3. **Checked error base, with CLI owner:** approve one `CheckedExecutionError` carrying the
   immutable result, including how the CLI renders known guest failure, uncertainty, and incomplete
   output.
4. **Demotion mechanism, with native carrier owner:** accept fixed `setpriv` plus its bootstrap
   prerequisites or choose a different shared identity launcher before ordinary QGA use.
5. **Cross-carrier inline/chunk constants, with SSH and native owners:** replace candidate values
   with the largest conservative values established by whole-request proof. These remain private
   limits.
6. **File-helper runtime, with file and initialization owners:** implement and prove the approved
   early guest Python prerequisite using Bookworm-compatible helper code. Reusing private transfer
   answers delivery only. Existing-VM native recovery needs its own bootstrap path; neither guest
   nor host readiness may install a missing interpreter implicitly.
7. **Darwin preparation substrate, with platform owner:** implement the approved preinstalled Python
   3.11+ prerequisite, with clean missing/version/Xcode-shim diagnostics and no installation prompt.
   Prove the host helper and no-staging readiness on macOS. Do not make SSH parse application frames
   to compensate.

The models-only first increment does not depend on these decisions and does not authorize public
exports, RunContext accessors, production consumers, grants, file catalog enforcement, or carrier
changes.
