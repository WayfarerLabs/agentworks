# Execution Lifecycle and Protection Profiles

- Status: Proposed design, 2026-09-18; private mechanism and protocol checkpoints do not claim a
  production lifecycle or containment implementation.
- Governing direction: FRD operator rulings for [profiles](frd.md#operator-rulings-2026-09-18) and
  [staged permission activation](frd.md#operator-rulings-2026-09-19).
- Public API: [execution contract](execution-contract.md); delivery: [plan](plan.md).

## One API, separate concerns and explicit constraints

An authorized target exposes `execution()` and `files()`. `ExecutionAccess` owns both synchronous
execution and job operations. `run` accepts `Command` or `Script` and returns an `ExecutionResult`;
`start` accepts the same invocation forms and returns a `JobRef`. Job means independently
addressable execution, not a duration threshold. Both entry points use the same preparation,
authorization and lifecycle implementation. There is no parallel direct/cgroup/sandbox API family.

These concerns belong in one contract; they are not all new features or freely combinable knobs. In
particular, composition binds identity, while a caller chooses invocation and permitted options.

| Concern     | Where selected                                                      | Constraint                                                                                                                          |
| ----------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Invocation  | Caller supplies literal `Command` or shell `Script`                 | Script selects an explicit interpreter; neither form selects a route or profile.                                                    |
| Observation | Caller uses `run`, `start` or later `wait`                          | Waiting does not choose lifetime or protection; initial `start` has the I/O restrictions below.                                     |
| I/O         | Caller selects granted input/output handling                        | Channel support, observation mode and lifetime constrain live pipes and PTYs; a PTY does not choose shell startup.                  |
| Lifetime    | Caller selects `OPERATION` or `INDEPENDENT`                         | Independent lifetime requires MANAGED or stronger and target-owned I/O/evidence; both `run` and `start` may select either lifetime. |
| Protection  | Caller explicitly selects an allowed core profile                   | Target prerequisites and identity must satisfy every promised guarantee; no automatic downgrade.                                    |
| Identity    | Composition binds target user; caller may request granted elevation | No arbitrary per-call user selector; internal supervisor privilege does not elevate the workload.                                   |

Every script selects `Shell.SH`, `Shell.BASH` or `Shell.USER_DEFAULT`. Separate `Script` keyword
options `login` and `interactive` express startup behavior; a PTY does not imply either.
User-default lookup uses the actual destination execution identity after authorized elevation. Fixed
constants mean fixed interpreter semantics, not an arbitrary executable path or the workstation
shell. An arbitrary interpreter facility, if needed, requires its own reviewed shape; strings do not
bypass the fixed set. Historical buffered PoC methods remain as measured until the implementation
migrates.

`profile` is required on public `run` and `start`: selection cannot be hidden in context defaults.
Lifetime defaults explicitly to `OPERATION`, input to EOF, output to bounded capture, and startup to
non-login/non-interactive. A stronger profile never silently changes those choices. Channel, target
and grant checks validate their combination before launch. Not every Cartesian combination is
supported: independent lifetime rejects caller-owned live input or output pipes; a PTY requires a
real terminal channel. Initial `start` accepts only EOF/finite target-delivered input and
target-owned capture/discard output. It rejects caller-owned live pipes and terminal endpoints
before dispatch, rather than returning while borrowed streams have an undefined owner. `run` owns
synchronous streaming until it returns; `attach` borrows its terminal endpoint for the attachment
call only. Required native jobs use durable output and polling, not a pretend PTY.

## Protection hierarchy and grants

Profile guarantees apply whenever the new API executes work. Recipient grant enforcement below is
the post-removal design: during coexistence consumers explicitly choose profiles, but no new
permission policy forces that choice or withholds DIRECT. Do not claim that a recipient must use
cgroups while it can still reach legacy execution. Isolated proof tests may exercise future denials;
production activation follows the [removal gate](migration-strategy.md#sequence-and-cutover-gates).
Guest-side protections implementing a requested profile are not deferred recipient permissions.

Proposed core profile names describe guarantees, not a selectable backend:

| Profile              | Added promise                                                                                                                                      | What it does not promise                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `Protection.DIRECT`  | Shared identity, literal arguments, sensitivity and truthful outcome rules; ordinary operational cancellation                                      | Escape-resistant process ownership or isolation from the execution account's ambient authority |
| `Protection.MANAGED` | DIRECT guarantees plus an independently identifiable workload boundary, descendant tracking, supervisor-owned stop and terminal-empty verification | Resistance to malicious same-user indirect execution outside the boundary                      |

MANAGED's Linux candidate is a system-owned transient systemd service/cgroup, not a public choice of
unit properties. The [operator ruling](frd.md#file-safety-and-guest-runtime-rulings) excludes
malicious target-user process containment. The earlier CONTAINED proposal is therefore outside this
delivery, not an unimplemented advertised profile or a production gate. Neither current profile name
is evidence of implementation. Future jail or sandbox implementations may satisfy a profile only
after demonstrating every inherited guarantee. Two unrelated sandboxes are not ordered merely
because an enum has larger numeric values. The initial core-owned chain needs explicit definitions
and tests, not a generic plugin profile DSL.

Core binds allowed operations, exact profiles, identities/elevation, lifetime and I/O restrictions
into the target before giving it to a recipient. A context can grant MANAGED but not DIRECT, forcing
managed execution without rewriting the request. A grant for one profile does not implicitly
authorize every stronger profile; stronger mechanisms can need additional host authority or resource
access. Derived contexts intersect grants and cannot broaden them. A file-only context has no
execution interface. An observe-only execution interface cannot start or stop work.

Known grant denials raise `AuthorizationError` before preparation or I/O. An authorized profile
unavailable on the target produces a distinct actionable prerequisite/support refusal, never a
weaker fallback. Availability is separate from authorization and from route connectivity. An
unavailable required core workflow remains a delivery blocker, not permission to classify recovery
as optional. Core must bind the appropriate explicitly approved profile for early bootstrap and
recovery; it cannot silently grant a restricted plugin DIRECT because systemd is unavailable.

The same checks cover commands, scripts, jobs and additional launches into a session. Existing-job
operations revalidate the reference's target/run/profile against current grants; the reference is
not authority. A caller cannot adopt an arbitrary unit, widen a profile, pass arbitrary systemd
properties, or obtain supervisor credentials through the API. File helpers may use internal delivery
under narrow core authority without exposing execution; they accept data, never caller callbacks.
Grants apply to the public action, not private reuse: implementing `run` with launch/wait machinery
does not require the recipient to hold public `start` authority. Readiness's operational no-effects
contract rejects profiles requiring stateful supervisor launch before dispatch even during
coexistence and even through `run`; withholding `start` alone is insufficient. Its DIRECT path still
forbids staging and requested shell startup. This invariant is independent of later recipient grant
enforcement.

## Lifecycle, waiting and attachment

`run(..., profile=MANAGED)` is a foreground experience backed by the same managed launch as
`start(...)` followed by `wait(...)`. The difference is the return shape, not weaker ownership.
Waiting deadlines remain observation bounds. A wait timeout returns safe partial evidence and the
managed reference; it does not claim termination or permit replay. `stop` is a separate operation,
with a bounded graceful interval, escalation and a truthful terminal/uncertain result.

OPERATION gives cleanup responsibility to the owning operation's composition lifecycle. Returning
from `start` does not end that operation; ending the owning operation requests cleanup of its
remaining work. Derived, borrowed and observe-only views retain the original owner: closing those
views never implicitly stops its work. Explicit `stop` still needs the caller's stop grant. For
managed work, the target-side owner must also handle disappearance of the controlling operation;
local `finally` alone is insufficient. The implementation proof must settle the
operation-lease/liveness protocol, partition behavior and bounded cleanup before advertising this
combination. DIRECT has only the ordinary operational stop guarantee: lost connectivity can leave
termination uncertain. It cannot be substituted where target-enforced cleanup on observer loss is
required.

INDEPENDENT gives lifetime ownership to the resource/run rather than the initiating connection. It
requires at least MANAGED and target-owned stdin delivery, output retention and completion records.
Both `run` and `start` can select it: a caller may wait now and resume observation later. It
survives observer loss while the execution host remains running, not reboot or explicit VM stop. It
also requires a platform-owned availability hold for the whole active job wherever the platform
could otherwise idle-stop the VM. An operation-scoped hold is insufficient after the initiating call
returns or the controller disappears. The hold must be recoverable, bound to the exact VM and job,
and released only after terminal job/cleanup evidence; if the platform cannot prove this,
`INDEPENDENT` is unavailable there. This is a platform-neutral condition. On platforms whose VM does
not idle-stop, the availability hold can be a no-op. Neither contract overrides an explicit VM stop,
reboot or host loss.

Every authorized VM operation that can perform guest work, including execution, file work, recovery
and later job observation, enters the same platform availability boundary before activation and
retains it through routing, body and teardown. Passive readiness/preflight only observes
already-existing availability; it cannot start or extend a hold or activate the VM. Admission and
release remain in core's database-owned operation lifecycle; platform hooks supply their actual hold
evidence. Entering the boundary means core has claimed and will account for the entire lifecycle; it
does not mean starting a platform keep-awake process before checking stopped intent or performing
authorized activation. The platform starts its active hold at the appropriate point after those
checks and before guest work, and its exit contributes typed teardown evidence. Explicit VM stop and
reboot are separate authorized lifecycle operations: they share conflict admission but do not
acquire a hold that defeats the requested power transition. A stopped WSL2 distribution during a
supposedly active independent job is a discontinuity to report and recover, not proof that the old
job stayed alive. Whether a later observation may wake an already stopped VM solely to inspect
terminal records is separate from the active-job availability guarantee and must be decided before
public exposure. The operator-stopped flag forbids automatic startup of a definitively stopped VM;
it does not by itself make an already-running VM unavailable. Preserve that existing intent
distinction when converging platform power, and do not treat transitional or unknown provider status
as definitive stopped evidence.

The shared native-operation gate admits only the typed stable states `RUNNING`, `STOPPED` and
`DEALLOCATED`. Operator-stopped intent refuses automatic activation for both inactive states, while
`RUNNING` can proceed regardless of that intent. Core retains passive platform access before effects
and passes the original observed state unchanged; the adapter owns its concrete activation. This
gate does not supply a missing platform adapter or prove native availability by itself.

PTY is an I/O choice, not a background state. `attach(ref, terminal=...)` requires an attach grant
and supported transport, and does not create a new run. A reusable detached terminal needs an owned
terminal endpoint such as the session's tmux server; ordinary inherited pipes are not durable.
Ctrl-C, terminal hangup, detach, local wait interruption and explicit stop need distinct tested
policies. None is silently promoted into whole-workload cancellation. Terminal output is combined;
non-terminal capture preserves separate guest streams and sensitive-output rules.

### Bound job controls and independent evidence

`ExecutionAccess` owns `observe`, `read_output`, `wait`, `stop` and `dispose` as well as `run` and
`start`; no third job-access object sits between the caller and the same owning composition. The
first OPERATION implementation resolves a `JobRef` only through the acknowledged runs retained by
its originating `ExecutionOperation`. It revalidates the exact owner, VM, complete guest identity
and persisted launch before dispatch. A reference alone cannot adopt another operation's run or
reconcile an uncertain start. Independent-job admission retains its separate resource-owned
preflight; this OPERATION lookup does not establish independent-job access after reconnection.

Unified RESOURCE controls use the same `ExecutionAccess` and `ExecutionOperation`. Select a planned
originating OPERATION run explicitly, with no fallback after its admission fails. Otherwise require
the operation's core-bound RESOURCE namespace and exact repository, VM, complete guest identity,
root plan and runtime before inspecting the independent reservation. Mutations require a confirmed
launch receipt. The observer does not adopt the independent run, create or drain its keeper, or put
terminal evidence into an OPERATION cache. RESOURCE stop uses the existing tracked ordinary helper
and operation-lifetime dispatch row. Its empty payload remains appropriate; the exact run-ID payload
belongs to the dedicated disposal attempt, not every ordinary control helper. Accepted stop intent
still requires a fresh settled observation to establish termination.

RESOURCE disposal retains positive terminal evidence only with its exact pending disposal attempt.
Extend the existing active-helper tracking with a typed per-attempt binding for the exact attempt
ID, receipt and terminal evidence. Keep its actual helper, borrow and candidate in the existing
entry; derive the dedicated `managed-dispose` kind, payload version and canonical run-ID payload
from the existing constants and receipt. Retain this entry through unsettled bookkeeping or an
unresolved explicit receipt-bound retry. No separate executor, registry or synthetic OPERATION run
is needed. Resolving this action requires NOT_SENT or clean SENT with zero helper completion and a
validated NOT_READY or DISPOSED response; helper termination alone is insufficient. Registration and
settlement recovery must reconcile that exact row. A retained actual helper blocks ordinary
admission and aggregate finish until custody permits settlement. Bookkeeping retry and finish
perform no carrier calls. A separately requested exact receipt-bound disposal retry may use retained
terminal evidence after proved helper termination and a lost response, because launch artifacts may
already be gone. It reconciles the prior attempt first, never rearms a RESOLVED row and never
interprets row resolution or artifact absence as confirmed disposal. Concrete control orchestration
belongs in a focused private module; admission, helper tracking and finish remain with the existing
operation owner.

Unified RESOURCE launch uses that same access and tracker, not a second job executor or an OPERATION
run with a disabled keeper. Before shell lookup or reservation, require the core-bound RESOURCE
namespace, current native binding and platform-owned independent availability. A proved no-idle-stop
platform may select the existing no-op guarantee; callers cannot request a bypass or infer it from
carrier features. An idle-stoppable platform still needs its recoverable job-length hold. An initial
no-op-backed implementation is only an increment, not acceptance of all platforms.

Freeze the complete finite invocation before tracked shell lookup. Retain its generated run
identity, planned receipt and output policy with one concrete START binding before reservation,
alongside the actual helper, borrow and local delivery in existing tracking. Use the dedicated
`managed-start` version-one run-ID obligation and existing one-shot preparation, reservation,
possible-dispatch and receipt-validation transitions. Reuse the existing one-shot exchange with
supplied tracked custody; retain the allocating wrapper for existing callers, rather than nesting
another borrow or settlement wrapper. Each ownership caller retains the actual returned attempt
before fallible settlement or outcome construction, preserving already observed launch precision
when those steps are interrupted. Guard the allocating wrapper's unused setup and original control
flow as well. Recheck selected binding and operation admission after marking possible dispatch and
immediately before entering delivery; the actual helper attempt still applies its owner fence.

Publish the actual start candidate into the retained entry before repository receipt reconciliation
can commit or raise. A later confirmed database row does not prove that the original helper or local
delivery settled. START bookkeeping therefore requires both exact receipt confirmation and actual
helper/local-custody settlement for a dispatched launch, not code-zero completion alone. Interrupted
reservation is inspected only by its already retained exact identity: proved absence or a matching
RESERVED record can settle reservation uncertainty without launching, while conflict or unreadable
state stays retained. Supported NOT_SENT evidence with actually settled helper, borrow and local
delivery can resolve only that exact temporary START action under its current owner and matching
reservation. Actual proof that the wrapper never entered delivery can do the same without a
candidate. Neither changes a POSSIBLE_DISPATCH run into NOT_LAUNCHED, invents receipt absence or
acknowledges launch. Unknown delivery, interrupted return without supported evidence and unsettled
local or coordination custody remain retained. Preserve unknown registration, possible dispatch and
helper debt. Bookkeeping and finish never launch or reserve again; no replay uses the retained
frozen body.

Discard preparation/source references when their one-shot attempt ends. Retain only the exact launch
evidence and custody needed for zero-dispatch recovery; safe facts and durable payloads carry no
source, environment or input. Acknowledgement resolves temporary launch custody, not the continuing
RESOURCE job. It never enters the initiating operation's managed-run cleanup list.
`run(..., lifetime=INDEPENDENT)` can compose this start with existing RESOURCE wait under the same
finite deadline; timeout and observer cleanup do not become implicit stop. Physical job holds, other
native factories and full RunContext remain separate required gates.

The private foreground composition now supports `run(..., profile=MANAGED)` with OPERATION lifetime
by invoking that same one-shot managed launch and existing operation-owned wait. It does not require
the caller to invoke `start` separately, add a supervisor, or enable INDEPENDENT lifetime. All run
flags, including `check`, are validated before launch. One selected finite observation deadline
covers both launch and wait; it is never renewed by selecting a second deadline policy. If clean
acknowledgement exhausts that budget before the first wait attempt, only locally retained exact
acknowledged-run and output-policy evidence supplies an UNKNOWN application/dispatch result with
DEADLINE and the safe reference. It performs no extra I/O, keeper drain, stop or owner resolution.
Initially expired explicit deadlines still refuse admission. Checked MANAGED run and wait preserve
the exact safe result/reference with the bound logical entity through the existing contextual
checker; known application failures retain application phase, and deadline or observation collection
failures may refine otherwise unknown phase to observation. DIRECT behavior remains unchanged. This
private composition does not establish public RunContext availability or native backend acceptance.

Read-only observation, output and wait use the existing operation-lifetime dispatch obligation and
fresh serial borrows. Repeated settled polls reuse that exact row rather than consuming the bounded
ledger one row at a time. The same interrupted registration, arming, helper settlement and retained
handoff rules apply to inline work and job reads. An uncertain helper blocks new work; a wait
deadline neither drains the keeper nor stops the job or releases its operation.

Caller-facing values project the existing evidence rather than add another lifecycle state machine.
Status distinguishes application state and optional exit status from resource cleanup. Output
returns a selected stream's verified retained bytes and cursor, distinguishing EOF of that retained
prefix from complete source capture. `capture_complete` describes the whole verified source capture,
not consumption of the returned slice; only complete capture makes it true. Intentional discard or
sensitivity suppression can have verified empty-prefix EOF without complete capture or an
output-limit failure. Stop distinguishes accepted intent, proved termination and uncertain delivery.
Disposal distinguishes confirmed release, proved not-ready and uncertainty; `False` alone cannot
stand for both a refusal and an unknown effect. Waiting returns the existing `ExecutionResult` with
the safe managed reference, including on partial or checked-error paths.

For an acknowledged OPERATION run, resource closure requires its authentic exact launch, both stream
ends, positive boundary emptiness and positive exact controller termination. A canonical `wait` fact
is validated when present but is not a cleanup prerequisite. The existing controller can close a
missing-executable or signaled application without publishing that fact. Missing exit evidence
leaves application state UNKNOWN and status absent; it does not invalidate independently proved
resource closure. Observation reads facts before querying the controller, so an absent `wait` is a
snapshot, never a permanent negative fact. A later fresh observation may recover it. Controller
absence remains usable only after acknowledged one-shot admission rules out a pending launch. Local
helper drain, permanent mutation closure and original uncertain dispatch obligations remain separate
requirements.

Disposal observes terminal resource evidence before draining the selected keeper. Active or unproved
work returns not ready without stopping the workload or changing renewal. Once terminal evidence
exists, it retains that evidence on the existing run, drains only its keeper and invokes the
existing exact fenced disposal protocol. Confirmed disposal supplies permanent publication closure;
aggregate cleanup must then use retained closure evidence rather than stop or reread deleted launch
facts. A lost disposal response retains the same terminal evidence and temporary helper debt. Only
separately proved helper termination permits an exact receipt-based retry; no new launch observation
is required after artifacts may have been deleted. Before retry, reconcile the exact current
disposal row under current ownership. A matching RESOLVED row uses a fresh attempt ID, even if
interruption prevented local completion bookkeeping. Retained terminal proof permits receipt-based
retry without rereading deleted artifacts. Missing or unresolved rows are not settlement evidence
and retain the existing ID and helper gates. Neither successful disposal nor retained evidence
clears original start or unrelated helper uncertainty.

Explicit job control is ordinary work, not authority to enter aggregate teardown. Refuse stale
persisted ownership and acquire the whole owner's ordinary serial borrow before draining the
selected keeper. Register and arm the existing lifetime dispatch row, then fence explicit stop at
actual delivery through its borrowed helper attempt. Settle and retain that helper before a fresh
ordinary observation. A borrow held by file work or another component refuses stop before either
drain or dispatch. An uncertain stop helper retains the same ordinary custody and blocks new work;
keeper support custody is not a substitute. The read-only admission check alone is not the delivery
fence. These checks narrow the takeover window but cannot make a database transition and remote
dispatch atomic. Aggregate CLOSING cleanup retains its already admitted support authority after
ordinary admission closes; explicit control cannot obtain that authority merely by reusing the same
fixed helper.

## Supervisor and evidence

The transport effort owns shared lifecycle, native/SSH application of it, and session adoption. SSH
still owns only delivery/connection/trust. The supervisor is above `Carrier.execute`: identical
prepared control operations can arrive by SSH or native QGA, and observation can use a later route.
No carrier gets a second detached-execution protocol or stronger raw exit guarantee.

For managed Linux execution, launch into the final system-owned service boundary before executing
any workload-controlled shell startup or payload. Moving a parent after launch does not contain
already-detached children. Trusted control may require privilege; the workload still starts under
its explicitly authorized UID/groups. Keep source, stdin and secrets out of visible unit arguments,
environment metadata, logs and world-readable staging. The secret-delivery/FD protocol needs proof
with actual privilege changes; systemd invocation alone does not establish it.

Carrier account hooks can execute before the supervisor bootstrap. The proof must specify the
control identity/bootstrap and test it through both carriers; payload shell initialization remains
inside the boundary. A payload wrapper cannot undo prior hooks, and managed execution does not claim
ownership of arbitrary work an account hook starts before the supervisor. Managed session entry
therefore needs a trusted control bootstrap whose pre-boundary startup cannot launch
workload-controlled hooks. Prove this with an ordinary startup hook that launches background work,
not only with an adversarial escape fixture. The delivery account and its shell configuration are
part of this proof; placing only the final tmux command in a unit is insufficient.

Allocate a non-reused launch identity before dispatch. Trusted target state binds it to host/VM
instance, boot incarnation, workload identity, resolved shell, profile revision and unit ownership.
Use existing session UUID/run IDs when the owner is a session; other jobs do not invent sessions.
Record pending launch before starting so lost acknowledgment can be reconciled without another
launch. Prefer protected unit data where sufficient; justify additional storage rather than adding a
generic registry. Preserve durable terminal evidence before unit collection removes it.

Separate workload exit, output completeness and boundary emptiness. A main process exiting does not
prove its descendants are gone. For a generic managed job, the main command is the lifetime anchor:
its exit starts descendant cleanup rather than allowing surviving children to keep the job alive.
The command's exit fact remains observable while cleanup is pending or incomplete. For sessions the
tmux/runtime anchor controls lifetime; arbitrary surviving children cannot prolong a dead session.
Preserve intentional pane retention only within an explicit bounded diagnostic policy. Stop closes
admission, targets the validated owned boundary, escalates and confirms emptiness before successful
disposal or replacement. Stale boot/run/unit/PID observations cannot target replacement work;
partitions and non-terminating kernel tasks remain incomplete. Resource owners set output/record
retention and abandoned-job cleanup.

### Private durable launch checkpoint

The first persistence slice records one `execution_runs` row before launch and remains private and
non-production. Its canonical 32-character run ID derives one stable `agw-managed-<run-id>.service`
name. The row stores only bounded non-secret target, incarnation, boot, workload identity, shell
identity, MANAGED profile revision, owner/lifetime, receipt protocol, launch state and timestamps.
The unit name is derived from the run ID rather than persisted. Application, cleanup and disposal
evidence are later proof gates, to be added with real producers and consumers rather than dormant
state fields.

### Private target fact protocol checkpoint

The next private slice defines canonical version-one JSON facts, each at most 4 KiB. A `launch` fact
encodes the exact realized `ManagedRunReceipt`: run ID and derived unit, target kind/name/
incarnation/boot, workload UID/GID/groups, requested and resolved shell identity, owner/lifetime,
profile revision, receipt namespace and receipt protocol. The target-side service must write this
fact only after the workload boundary is realized. A `wait` fact records exactly one main-process
exit code or terminating signal. Each `stream-end` fact identifies stdout or stderr, the length and
SHA-256 of retained bytes, and one closed disposition: `complete-capture`, `truncated-capture`,
`discarded` or `sensitivity-suppressed`. Only capture has a spool, and its end fact means that spool
is closed and will not grow. Retained capture bytes are an initial prefix; truncated capture means
at least one produced byte was omitted, including when the retained prefix is empty. Discard and
sensitivity suppression retain zero bytes and the SHA-256 of empty bytes. A `boundary-empty` fact is
positive evidence for the exact owned workload boundary. These four kinds are independent:
main-process wait does not imply either stream ended or boundary emptiness, and a closed stream does
not imply wait or emptiness. A missing fact is unknown, never negative evidence.

Every post-launch fact carries the exact run ID, derived unit and SHA-256 of the canonical launch
fact. Consumers must compare that binding to the validated launch fact and to the expected
reservation before granting authority. The digest prevents facts from another launch receipt with
the same run/unit from being mixed into this record. The wire rejects duplicate, extra and missing
fields, alternate JSON spellings, contradictory status, unsupported versions and unrecognized
values. It carries no application input, output bytes, credentials, arbitrary paths, timestamps or
arbitrary service properties as authority. The resolved executable in shell identity is the one
bounded path inherited from `ManagedRunSpec`: normalized absolute POSIX syntax, ASCII code points
0x20 through 0x7e and at most 255 UTF-8 bytes.

The private target-side store uses a protected boot-local directory for each run. Its launch, wait,
stdout-end, stderr-end and boundary-empty facts are each created once and immutable. Bounded stdout
and stderr capture spools are written by the target-side owner, then closed before their
corresponding end facts are published. Discard and sensitivity suppression publish no spool. Fact
creation uses atomic create-once publication; an existing fact is read and compared rather than
overwritten. No shared mutable state file or file-level lock is part of this protocol. The portable
store, its permissions, closed-output validation and exact-source Python 3.11 execution are now
implemented and proved in isolation. The target controller's request, launch gate, normal-wait,
stream-end and cleanup ordering are also implemented hermetically. Private carrier-neutral `observe`
and closed `read-output` exchanges are implemented. A private carrier-neutral `start` exchange now
stages the five fixed request assets and attempts one transient-service activation, with durable
possible-dispatch before delivery. Private managed `stop` now durably publishes one fixed request,
lets the owned controller terminate its main child and boundary, and distinguishes accepted intent
from positive boundary emptiness. Private managed `dispose` now requires the complete exact-run
terminal predicate, commits a launch-linked receipt and removes only validated fixed artifacts;
exact retry resumes partial cleanup. Crash recovery and live destination behavior remain open. The
private start request now carries the observed VM guest marker, kernel boot and PID 1 start time.
Before opening the store or invoking systemd, the bundled guest helper rereads those fixed paths and
refuses any mismatch; host preparation also compares the derived boot fence with the planned run.
This is a hermetic guest-side identity fence, not a production provider-route or locator check. Live
systemd/cgroup behavior and SSH/QGA production delivery remain unproved.

The stream fact is self-describing. Managed-run reservation now persists the requested output policy
atomically alongside run identity. A future consuming service must compare that policy to each
stream end before treating its disposition as fulfillment. `_managed_job_wire.py` now owns the
complete canonical byte schema as a Python 3.11-compatible stdlib module that reuses the portable
`_helper_identity.py` validator. The host typed adapter delegates encoding, decoding and launch
digest to it. Exact-source bundle tests prove byte round trips under Python 3.11 when installed. The
target controller and private observation helper derive bundled source from those first-party
modules. The helper builder removes docstrings from trusted modules; service admission now opts into
this existing compaction and avoids redundant outer compression. The protected store uses the same
portable codec. Host reservation/output-policy reduction, target controller production,
cgroup/systemd launch, carrier proof and live validation remain open.

### Operation lifetime implementation path

The private guest protocol and first controller consumer are implemented; the host composition below
remains the next implementation path, not delivered behavior. Host admission continues to refuse
MANAGED plus OPERATION until the target protocol, keeper, aggregate cleanup and native proof below
are complete. Reuse the existing per-run controller and stop path; do not add another supervisor or
make the observation deadline a job lifetime.

Use one bounded lease for each operation-owned run, within its existing protected run directory. The
lease binds the exact immutable launch digest and guest boot identity. Its expiry is an integer
nanosecond value on the guest's `CLOCK_BOOTTIME`, not a host timestamp or wall clock. Python 3.11
provides this suspend-aware monotonic clock on Linux; absence refuses this combination. This clock
choice comes from the
[Python time documentation](https://docs.python.org/3.11/library/time.html#time.CLOCK_BOOTTIME), not
a proof of VM pause/resume behavior on any platform.

Core first makes a fixed, read-only guest clock observation under the exact prepared guest identity.
It derives an expiry 60 seconds after that observed value and supplies it with the operation-owned
start request. After the clean clock reply, recheck database generation and closing admission before
possible start dispatch, just as for renewal. Guest admission checks expiry before staging or
starting systemd; the controller checks it again before releasing the child. A delayed request
cannot acquire a fresh lifetime merely because its helper finally runs. Independent starts have no
lease and retain their current behavior.

Renewal uses the same two steps: observe the guest clock, then publish that observation plus the
fixed 60-second window for the exact run. The guest publisher never substitutes its execution time
for the supplied observation. It rejects wrong launch/boot identity, overflow, a future clock
observation or an already expired proposed value. A late queued publication therefore cannot grant
more time than the earlier observation allowed. Core normally renews every 10 seconds; those are one
internal policy, not new plugin or configuration knobs.

The lease is mutable control, not immutable completion evidence. Publish a complete bounded record
using a conflict-free stage and same-directory replacement in the protected core store. This does
not change caller-file write semantics or introduce a machine-wide filesystem lock. There is one
publisher and at most one in-flight exchange per keeper; a missing, malformed or unavailable record
does not extend the last accepted expiry. The controller remembers only a later accepted expiry,
never moves it backward, and checks its remembered expiry before accepting another record. Expiry
and explicit stop permanently close renewal admission for that controller; no late record revives a
stopped run. Disposal accounts for only this fixed control leaf and recognized private stages.

Lease publication, exact stop publication and disposal share one cooperative mutation barrier on the
retained per-run directory inode. Each section independently opens that directory and attempts one
exclusive non-blocking Linux `flock`; contention or error retains uncertainty rather than waiting
indefinitely or retrying implicitly. After acquisition, publication checks exact binding, sample
freshness and both permanent closure records before creating a stage or accepting a duplicate.
Initial publication checks the staged request binding before launch; renewal requires the immutable
launch binding. Exact stop holds the barrier through durable `request-stop` publication, but not
through controller observation. Disposal holds it through inventory, receipt commitment and cleanup;
the permanent `disposal` receipt replaces stop intent before its removal. The boot-local run
directory must remain the same inode, never be removed and recreated while delayed helpers exist.

This barrier fences lease mutations, not every store writer or guest process. A delayed publisher
can remain alive, but after confirmed closure it cannot create a stage or replace a lease. The
keeper may settle that publication effect with positive exact closure evidence without claiming
process exit. Lost closure acknowledgments require the existing exact stop or disposal
reconciliation; expiry, boundary emptiness or local client cleanup alone do not prove the fence.
Original start, controller, immutable-publication and capture custody must settle independently
before disposal or owner release. This is Linux guest cooperation, not a machine-wide host lock,
hostile-user containment or a general filesystem locking framework. The
[Linux lock semantics](https://man7.org/linux/man-pages/man2/flock.2.html) distinguish independent
opens from duplicated descriptors; native filesystem and interruption proofs remain required.

Whole-owner settlement also requires an independently validated controller-termination observation
through the existing managed observation helper. Derive the unit from the exact launch and retain
the prepared guest/boot binding. Native metadata must distinguish a running controller, positive
termination and unknown; a failed query or `MainPID=0` alone is not termination. Because `--collect`
can remove execution metadata, a positively absent unit may establish termination only alongside the
authentic exact launch and reconciliation ruling out a later admitted activation. Do not require
retained exit status or add another completion receipt. The
[systemd unit collection rules](https://raw.githubusercontent.com/systemd/systemd/v252/man/systemd.unit.xml)
and
[process properties](https://raw.githubusercontent.com/systemd/systemd/v252/man/org.freedesktop.systemd1.xml)
describe the native evidence; retained and collected units both need platform proof. Controller
termination settles neither original dispatch debt nor workload emptiness, and does not promise that
removing its empty cgroup directory succeeded. No broader store-locking policy is introduced.

The controller checks the lease at its existing bounded polling interval. Expiry follows the same
stop path as explicit intent: close remaining input, give the initial child the existing two-second
grace, then kill the owned cgroup and use the existing five-second cleanup observation bound. Only
the existing positive boundary-empty observation proves cleanup; expiry, a publication reply or a
systemd state does not. Uninterruptible work, controller failure or missing evidence remains
uncertain. A stalled controller cannot promise a wall-clock cleanup bound.

Core owns the keeper as an already admitted lifecycle effect, like a retained platform hold. It
registers and arms exact run custody before first possible publication, owns its background worker
and separate fixed-helper delivery. Independently fence the clock observation and publication; after
a clean clock reply, recheck database generation and closing admission immediately before possible
publication. An expired or failed clock reply, uncertain database read/update, or uncertain
publication stops renewal until that custody settles. This is not an atomic database-plus-remote
dispatch transaction: a takeover after the last check can still race an already admitted exchange.
The supplied sampled expiry bounds that exchange's possible extension; it is not instant target
fencing.

The main path first resolves the workload shell and validates and freezes the body, environment and
output policy, including control-space headroom for the later lease field. Reserve the planned
OPERATION run before admitting its keeper or sampling the guest clock, so recovery can always read
its exact specification. Retain the run ID before reservation and inspect it after an uncertain
commit; never replay start. Attach only the real sampled initial lease, then perform complete
request and carrier preparation before one start. A clock or carrier refusal after reservation may
leave a `RESERVED` one-shot tombstone. This differs from independent start's complete
preflight-before-reserve ordering because the initial operation lease requires a guest observation.
No body launch precedes the durable possible-dispatch transition.

Register the run-ID-only support obligation on the main path before borrowing ordinary work. Bind
OPERATION lifetime and owner kind to the actual operation ID, exact VM and full guest identity once,
then pass immutable facts and a separately prepared carrier to the worker. Reuse the retained
lifecycle handle's admission check after each clock sample; compare its refreshed persisted identity
with the fixed kind, version, payload and revision before publishing. An already possible effect
needs no renewal payload update or new owner API. Start the renewal worker only after a clean
acknowledged initial launch and settled local start delivery. An admitted but uncertain start gets
no worker, retains its original debt and receives no renewal or replay.

Renewal is not a second public command borrowing the owner's ordinary serial-use boundary. The
support effect has two closed phases: LIVE admits only the bound clock/renewal exchanges; CLOSING
admits only exact-run stop and observation after draining the local renewal worker and delivery. An
unresolved guest publication does not prevent the exact stop that establishes its permanent mutation
fence. Neither phase permits arbitrary calls or reopens ordinary admission. Prove this composition
against an ordinary long-running command rather than relaxing serialization globally. A wait timeout
does not end the keeper or stop the job.

Publish ordinary close intent through the owner's monotonic event before signaling and joining the
keeper. The event does not wait for a keeper-held owner guard or a database transition; an already
admitted transition may finish afterward. Bound the join and drain the keeper's exact local custody
before any guarded execution finish, owner cleanup, ledger read or resolution. An alive worker
retains the enclosing operation and prevents those later teardown steps. This does not interrupt
blocked lock waits or turn the signal into remote cancellation.

The proposed internal policy gives each clock-plus-publication cycle one total five-second delivery
budget and nominal ten-second start-to-start cadence, without catch-up bursts. This budget is
independent of a caller's wait deadline. The 60-second guest lease leaves arithmetic margin, not a
native latency guarantee: measure SSH and QGA exchanges, including their polling, before advertising
this combination. Late replies cannot reset the cycle budget or replace the sampled clock value.

Recovery does not persist a high-water expiry on every renewal. After confirmed takeover, sample the
exact guest's same-boot clock and add the fixed 60-second window to obtain a conservative ceiling
for all old possibly admitted lease authority. Every admitted old sample preceded its successful
generation check, which preceded takeover; the recovery sample follows takeover. An old helper can
still execute later, but cannot move its supplied expiry beyond this ceiling. Reading only the
current lease leaf is insufficient because an admitted publication may still replace it.

Deliver this recovery consumer with the keeper. It may allow up to one extra 60-second observation
window, not delay an explicit stop or disregard stronger evidence. An exhausted caller budget, boot
change or unavailable clock retains uncertainty. The ceiling proves neither workload emptiness nor
publisher drain. Recovery never resumes the old keeper or renews its lease, and no per-renewal
database payload update or new table is needed.

Ending the owning operation closes new keeper work, drains its local worker/exchange, then requests
the existing exact-run stop and observes cleanup under an explicit cleanup budget. Ordinary
admission has already closed: use the support effect's already-admitted exact-run cleanup custody,
not a new public borrow. Closing a borrowed view or returning from `start` does none of those
things. Keeper loss, database takeover or a partition stops renewal, and the guest independently
reaches expiry. Boundary emptiness alone does not settle an admitted publisher: uncertain lease
mutation must have positive settlement or permanent-fence evidence, and local support-worker custody
must drain before whole-owner release or disposal. A mutation fence does not prove guest process
exit or settle other writers. Outstanding ordinary dispatch debt remains separate and cannot be
cleared by keeper settlement. Platform availability remains a separate held effect.

The first proof must cover initial expiry before launch and between placement/release, renewal
during ordinary serialized work, normal scope close, wait timeout, host death, partition, controller
death, suspend/boot changes, delayed/duplicate publications, wrong run/boot/launch, interrupted
publication and disposal. Tests use controlled clocks; native Linux SSH/QGA proof must independently
observe body admission, descendant cleanup and retained uncertainty. Keep public exposure and
broader completion checkboxes open until these obligations are proved.

Local packed tests exercise the canonical clock/lease codec and actual start/controller with real
children under Python 3.11 and 3.12. Their boundary is a synthetic process group, not native cgroups
or systemd. Controlled clocks cover pre-staging and pre-release expiry, later renewal, irreversible
expiry, explicit stop, interrupted publication and disposal binding. Host admission stays refused;
these tests do not establish keeper concurrency, recovery, aggregate lifecycle or platform
acceptance.

Give each keeper exchange separate delivery, preparation, nonce, collector and I/O custody. No
generic concurrent-carrier capability is needed or inferred from immutable connection data.
Operation-repository fencing is thread-safe; that does not make ordinary database repositories or
RunContext dependencies safe to use from a background worker. Exact route and guest fencing remain
required. The former raw Proxmox worker constructed before its cleanup guard and used unbounded
post-kill `communicate()`. The private shared-owner implementation replaces that path with pre-held
custody and bounded cleanup observation. Native drain and the complete launch/interruption and
keeper-drain gates still need proof; an ordinary delivery deadline alone does not supply it.

Use the carrier contract's explicit caller-held `LocalDeliveryCustody`, rather than adding cleanup
handles to reports or escaping exception causes. Ordinary dispatch attaches it to the existing
operation attempt before native admission; keeper and pre-target provider reads attach it to their
existing enclosing lifetime. It holds at most one unsettled native owner, with no new owner thread,
process registry or replay mechanism. Every temporary Proxmox wire consumer must pass that retained
storage, including power, current configuration, activation and guest-info queries. Do not continue
polling while its local worker remains unsettled. The aggregate must drain retained custody after
borrow handoff without reopening dispatch or clearing remote debt. Keep guest-helper process
settlement unchanged until an actual guest-side consumer can retain borrowed source descriptors;
host custody cannot survive serialization into the guest. Implement and prove this complete path
before claiming bounded keeper shutdown or owner release.

### First private managed service

The first host-admitted service slice is Linux-only, `MANAGED` and `INDEPENDENT`. Its guest
controller also accepts the private operation-lease request described above. Host admission does not
offer `OPERATION`, terminal attachment, live input/output, provisional output reads or a public job
API. Those combinations refuse before dispatch. This is a sequencing limit, not a weaker public
contract or an implicit downgrade. The existing no-staging DIRECT readiness path remains separate;
an independent managed job necessarily hands finite request material to a target-owned service.

The protected boot-local store is `/run/agentworks/managed-runs-v1/<run-id>/`. The namespace and
each per-run directory are `root:root` mode `0700`. Fixed immutable fact names are `launch`, `wait`,
`stdout-end`, `stderr-end` and `boundary-empty`, each mode `0400`. Closed capture spools are
`stdout` and `stderr`, each mode `0600`; discard and sensitivity suppression create no spool.
Request control, script source, environment and finite stdin remain separate fixed root-only assets
so neither source nor secrets appear in unit arguments, environment metadata, logs or a
world-readable location. A versioned finite ceiling applies before staging. The service consumes
each request exactly once and never treats an absent request asset as proof that launch did or did
not occur. After exact launch validation, `stop` may create one separate root-owned mode-`0400`
empty `request-stop` leaf. It is durable stop intent, not a sixth initial request asset, a fact or
termination evidence. Its protected non-reused run directory supplies identity; duplicating the run,
unit or launch digest in the empty leaf would add no authority. Exact retries accept the same empty
leaf, while strange or nonempty objects refuse.

Facts publish create-once from a conflict-free stage in the same run directory. The producer writes
and syncs the complete stage, atomically links it to the fixed final name only if that name is
absent, syncs the directory and removes the stage. If the final name already exists, it is decoded
and compared byte-for-byte with the intended canonical fact; equality is an idempotent observation
and disagreement is a conflict. No overwrite, shared mutable status document or file-level lock is
part of the protocol. A capture spool is closed and synced before its stream-end fact publishes.
Readers do not return capture bytes until the matching validated end fact exists, so the first slice
has closed output rather than a provisional cursor protocol.

The database persists the requested output disposition and, for capture, the per-stream byte ceiling
in two nullable columns on the run reservation. Reservation writes one complete row; inspection
refuses a missing or malformed policy instead of inventing one for an older private row. These are
host request facts, not launch-receipt identity, so they do not revise the version-one fact schema.
Later observation accepts a stream-end fact only when its disposition and retained length fulfill
that persisted request. The initial default is a one-MiB prefix per stream and the version-one core
ceiling is 16 MiB per stream. Truncation remains success of collection with incomplete output, not
complete capture.

The service is one transient system service per run. The fixed launch uses `systemd-run --system`
with the exact derived unit, notify service type, collection enabled and a closed property set:
`Delegate=yes`, `NotifyAccess=main`, `ExitType=main`, `KillMode=control-group`, `Restart=no` and
finite start/stop timeouts. It does not use a scope, `--pipe`, `--wait` or caller-selected unit
properties. The root service main is the trusted controller, not the workload identity. It resolves
its delegated cgroup, verifies that its membership ends in the exact derived transient service unit,
creates a dedicated workload child cgroup, forks a gated child, moves that child into the workload
cgroup, applies the exact groups/GID/UID and verifies them before any caller-controlled shell
startup or payload can run. Immediately before exec, the child restores the standard payload signal
dispositions and clears the inherited signal mask.

The fixed service process receives only the derived run ID and canonical bounded admission data: the
prepared controller UID/GID/groups and full guest checkpoint. This is separate from workload request
material, which remains in protected assets and descriptors. Before controller modules or
store/cgroup access, the fixed entry validates that data and reuses numeric root admission plus the
INLINE two-phase loader. The canonical observer loads once under the held init reader; a full guest
mismatch or non-root entry refuses before controller effects. Root controller identity does not
authorize a fallback body identity. The notify environment survives this bootstrap. Its tests
simulate admission and observations, not native root/systemd success. Later workload-child guest
admission and complete outer managed-helper adoption remain open.

After placement and identity are proved, the controller publishes `launch`, sends `READY=1`, then
releases the child gate. This ordering makes normal `systemd-run` return a launch acknowledgment
without tying job lifetime or byte streams to the delivery connection. The controller concurrently
drains both workload pipes to the selected bounded prefix or to discard and waits for the exact main
child. Main-child termination starts bounded descendant cleanup immediately. A close-on-exec status
pipe separately proves successful application entry: the first service publishes `wait` only for a
proved normal application exit, preserving every exit code including 126. Setup/exec failure and a
signaled death leave `wait` unknown because close-on-exec EOF cannot distinguish a signal delivered
immediately before exec from one delivered to the application. Draining continues concurrently so a
descendant that inherited a stream cannot delay the cleanup decision. Each stream-end fact publishes
only after that pipe reaches EOF and its spool is closed. Independently, cleanup uses `cgroup.kill`
and waits for `cgroup.events` to report `populated 0` before publishing `boundary-empty`. Before
that publication, the controller must have reaped the main child, settled the close-on-exec status
pipe and published `wait` when the proved outcome permits it. Stream ends remain independent and may
publish later. Thus boundary emptiness alone does not claim complete output, while boundary
emptiness plus both stream ends leaves no controller fact publication that can begin afterward. An
error while requesting `cgroup.kill` does not invalidate a later positive empty-boundary
observation. A task stuck in uninterruptible sleep can prevent that proof indefinitely; the
controller stops waiting at its bound and leaves boundary state unknown rather than fabricating
emptiness. Cleanup or controller failure may likewise leave a stream-end fact unknown even when
other terminal facts are present.

`KillMode=control-group` is a manager-owned fallback if the controller dies, but it cannot publish
positive facts after that death. Therefore an abrupt controller exit leaves any unrecorded wait,
stream-end or boundary fact unknown even when systemd later removes every process. The initial
service reports that uncertainty. A later recovery owner or independently proved post-stop hook may
strengthen it, but ordinary observation cannot infer completion or emptiness from unit absence.

The first stop mechanism keeps the controller alive long enough to produce that positive evidence. A
fixed root helper revalidates the exact immutable launch, publishes `request-stop` create-once, then
observes only within a finite guest-local bound. Helper acceptance proves the request leaf was
durably published, not that the controller consumed it or that the workload ended. A shorter carrier
deadline can lose the response without canceling the durable request, and an unbounded carrier
deadline does not make the target helper wait forever. A complete helper failure after dispatch
remains unknown because publication can become visible before a later sync or cleanup error. Once
publication returns successfully, a later invalid or unreadable boundary fact preserves accepted
intent and only withholds termination proof. Exact retry is safe and never replays start.

The controller polls the request before releasing its one already-admitted child and while observing
that child. First observation closes finite stdin and sends `SIGTERM` only to the exact main child
that it has not yet reaped. The fixed grace interval lasts only while that anchor remains alive,
giving it an opportunity to coordinate its descendants. Anchor exit, whether natural or during
grace, immediately enters the existing whole-boundary cleanup. Grace expiry does the same: invoke
`cgroup.kill`, continue draining and reaping, and wait within the separate cleanup bound for
`populated 0`. Repeated request reads do not extend grace. There is no PID enumeration,
caller-selected grace or second post-anchor lifetime. Only the existing exact `boundary-empty` fact
proves termination; missing wait or stream-end facts remain independent incompleteness.

Publishing the launch fact commits admission of the initial child even though its final gate release
follows, so a concurrent stop may prevent or briefly precede payload entry without reopening launch.
This private generic job admits no later work. Session entry and every future additional-launch path
must consult the same stop request before admission; that requirement does not justify a generic
admission-lock protocol before such a producer exists. Stop requires an exact launch fact. A
possible-dispatch run with no launch fact must reconcile first and remains uncertain. No separate
`execution_runs` stop state is added: target intent and target boundary evidence are the durable
truth, while production host coordination later uses the existing operation claim and lifecycle
obligation.

OPERATION and RESOURCE stop share the existing tracked ordinary helper and operation-lifetime
dispatch row. Retire the unused standalone host-bound stop checkpoint and its separate
`managed-stop` dispatch API; retain the fixed guest stop protocol. A complete validated `ACCEPTED`
response establishes durable target intent only after the actual helper settles. It neither proves
termination nor resolves the operation-lifetime row: aggregate finish still accounts for every
helper. Only the exact `boundary-empty` fact proves termination. An invalid or incomplete stop
response does not establish acceptance, but does not retain helper debt when exact helper
termination and local custody are proved settled. Unknown helper termination, lost delivery or
uncertain admission retains the claim until recovery proves settlement; ambiguous arming is not
positive `NOT_SENT` evidence.

Before stop dispatch, the host requires the exact run's reconciled launch receipt and current target
facts. A merely possible-dispatch run is not stop authority. The observing operation does not adopt
a RESOURCE run, drain its keeper or take ownership of its continuing workload and durable stop
request. No database stop-state field is introduced. The shared row preflight can compare the
guest's derived boot with the persisted target, but cannot derive the opaque incarnation fingerprint
without the provider locator and persisted marker. Its caller must compare the observed guest marker
with the persisted VM marker, then supply a target composed from the selected locator and that same
marker before invoking any later-action adapter. A boot-only match is not sufficient production
authority.

The first `dispose` mechanism is an explicit authorized release of terminal retained artifacts, not
a retention timer or a synonym for stop. Before committing release, the fixed helper requires the
byte-exact canonical launch, its launch-bound `boundary-empty` and both launch-bound stream-end
facts. A `wait` fact is validated when present but is not required, because setup failure and
signaled application death intentionally leave it unknown. Capture contents, lengths and digests
matter when serving output, not when authorizing release: disposal validates only that any fixed
spool leaf is a regular root-owned mode-`0600` single-link object before unlinking it. Missing
terminal evidence reports not ready without deleting anything. Unknown names, malformed fixed finals
and strangely linked objects refuse before release commitment. Recognized private stages may contain
partial bytes after a crash, so disposal validates their name, owner, type, mode and link shape
rather than decoding their contents.

Crash-safe retry keeps one minimal boot-local tombstone rather than trying to prove deletion from
absence. The helper creates the root-owned mode-`0400` immutable `disposal` leaf by hard-linking the
already validated immutable `launch` leaf, then syncs the run directory before any deletion. It does
not create a new receipt stage. Concurrent exact retries either link that same launch inode or
validate the existing receipt, so a delayed retry cannot create residue after receipt-only success.
The cleanup path explicitly accounts for the temporary two-link receipt/launch topology and a
possible third internal fact-stage link, then requires a one-link receipt for success. It validates
and unlinks only the structurally safe fixed request assets, facts, stop intent, capture spools and
recognized private publication stages through the held directory descriptor, syncs again and
verifies that only the matching disposal leaf remains. It never accepts a caller path,
recursive-delete choice or file list. Success means all retained application artifacts are gone and
only that exact-launch receipt remains until reboot. A receipt plus remaining known leaves means
committed cleanup is incomplete, so an exact retry resumes it; a matching receipt alone proves
already disposed. Missing launch and receipt proves nothing, and a mismatched receipt never
authorizes cleanup.

The target store refuses new request, fact, stop or capture publication after it observes the
disposal receipt. This check is defense in depth, not a target-side concurrency lock: the existing
core operation claim must serialize distinct start, stop and dispose operations before production
dispatch, while simultaneous exact dispose retries converge on the same receipt and fixed cleanup.
No file lock, mutable disposal status, expiry clock, automatic abandoned-run cleanup or database
retention state is added. A complete helper failure or lost carrier response after dispatch remains
unknown because the receipt or some deletions may already be durable. A complete `disposed` response
is accepted only with the matching receipt and final receipt-only inventory.

The host-side disposal obligation owns only one explicitly requested release attempt under the exact
VM operation claim. It requires a reconciled launch receipt but derives no terminal state or
disposal state from the managed-run row; the fixed target helper checks the terminal predicate and
receipt-only inventory. Positively proved `NOT_SENT` with settled coordination, or a complete,
validated `NOT_READY` response with settled delivery, resolves the temporary obligation without
claiming disposal. Complete, validated `DISPOSED` with settled delivery also resolves it and proves
the exact retained artifacts were released. An incomplete response, helper failure, lost delivery or
interrupted possible effect retains the claim and run-ID recovery identity. Concurrent exact guest
retries are protocol-safe; they do not authorize host replay of uncertain delivery until recovery
separately proves the earlier dispatch drained. No mutable database disposal state or retention
timer is introduced.

The private host-bound disposal checkpoint applies this one-attempt rule under a caller-held exact
VM owner. It shares the persisted-run and guest preflight with observation, output reading and stop,
and additionally requires a reconciled launch receipt. Its distinct `managed-dispose` obligation
contains only the canonical run ID. Settled no-delivery, validated `NOT_READY` and validated
`DISPOSED` resolve temporary custody; every uncertain or interrupted possible effect retains it. The
adapter leaves the managed-run row unchanged and makes no production route, authorization, recovery
takeover or native carrier claim.

Carrier-neutral control remains a fixed closed protocol with `start`, `observe`, `read-output`,
`stop` and `dispose`; it accepts no arbitrary path, unit, command or systemd property. `start` is
the only operation that can consume staged request assets and is never replayed after possible
dispatch. Every later operation revalidates target incarnation, boot, run, unit, launch fact and
launch digest before it obtains authority. `stop` closes further admission by publishing the fixed
request for the exact owned controller and reports accepted intent, boundary proof or uncertainty.
It does not equate a helper response, controller death, unit disappearance or systemd cleanup with
positive emptiness. `dispose` removes only a terminal exact-run store after retention policy permits
it. SSH and QGA must deliver these same operations; neither owns a second lifecycle implementation.

The first private end-to-end managed-job slice may enable only `INDEPENDENT`, whose target-owned
evidence survives observer loss. `OPERATION` must refuse before dispatch until target-side owner
liveness or lease, partition behavior and bounded cleanup are proved. This sequences implementation
without changing the public lifetime contract above. The private helper now exposes fixed `observe`
and closed `read-output` over the existing carrier interface. A private `start` preparation now
validates the planned independent run, output policy, request and carrier before reservation. The
single-use preparation must match the exact reserved row before the existing durable
possible-dispatch wrapper may consume it. Private `stop` revalidates the exact launch, publishes the
empty request create-once and waits within a fixed guest-local bound for the existing boundary fact.
Its controller path closes finite input, gives only the main child that has not yet been reaped one
fixed `SIGTERM` grace, then uses the existing whole-cgroup cleanup without extending grace on retry.
Private `dispose` validates the complete terminal predicate, commits the exact launch-linked
receipt, removes only validated fixed artifacts and accepts success only after receipt-only
inventory. The five carrier-neutral operations are therefore implemented hermetically. Production
host reservation/policy reduction and provider-route revalidation remain open. The private start
helper rereads fixed guest marker and boot paths, but SSH and QGA must each prove the protocol in
production before public exposure.

The private ownership-backed start kernel consumes one validated, already reserved independent run
under an already acquired exact-VM `OperationOwner`. Its core `managed-start` obligation holds
temporary dispatch custody and persists only the run ID. The managed-run row and its resource owner
hold the continuing independent job. Pure start preflight precedes reservation; a canceled or
mismatched preparation refuses before borrowing. The run row records possible dispatch before the
borrowed carrier attempts the one launch. Only a settled carrier attempt with a durable confirmed
receipt permits the start obligation to resolve. After admission, any other launch state retains
core custody, including a code-zero carrier completion without the receipt. This does not resolve
the whole operation or establish job termination. Production binding and recovery remain open.
Private observe, read-output, stop and dispose now reread guest identity before store access. Their
current-guest fences are hermetic; host-side action composition and production route proof remain
open.

The private host-bound observe adapter reads the exact independent resource-owned VM run row before
borrowing a caller-held VM operation owner. It constructs the expected launch from the persisted
specification and makes one fenced read-only observation attempt. It returns raw facts and custody
state; on interrupted control flow the original exception remains primary with custody facts as its
cause. This read-only action does not update the run row, choose an output view, reconcile
uncertainty, release the owner, or establish a production provider route. Its private read-output
sibling uses the same exact-run admission and custody path. Only a validated selected stream end
matching the persisted capture policy and prefix bound admits bytes; discard and sensitivity
suppression require their matching no-output dispositions. The raw candidate is retained only as
internal evidence and cannot be forwarded as a caller-facing output view. This does not reconcile
job state or establish production route freshness. Stop and disposal have their own exact-VM
host-bound adapters; those private guest fences likewise do not establish a production provider
route.

A separate private observe-and-reconcile action may update a `POSSIBLE_DISPATCH` run only after one
complete, validated observation contains its exact launch receipt. It uses the persisted row and the
repository's idempotent reconciliation transition, treating the historical launch dispatch as
unknown rather than borrowing the later observation's carrier status. A missing, refused, invalid or
incomplete observation never proves `NOT_LAUNCHED`. Receipt reconciliation does not settle an
earlier start attempt or obligation, release a retained operation owner, prove route freshness, or
imply workload completion; those remain independent evidence and recovery gates.

The private one-shot result collector requires a reconciled launch receipt before attempting another
carrier call. It observes once, then reads only streams whose exact end facts were present in that
observation, under the original finite deadline and caller-held VM claim. The later observation's
carrier dispatch is not reused as the historical launch dispatch: the result retains `UNKNOWN`. A
launch receipt alone leaves application state unknown; only a validated `wait` fact gives exit or
signal status. Each stream's admitted output, complete or truncated disposition, and positive
`boundary-empty` remain separate. A zero exit cannot make the result successful without both
requested streams, boundary emptiness and settled current collection custody. That boundary and
custody govern the result's `owned_cleanup_confirmed` flag; neither settles an earlier
`managed-start` obligation. The collector does not poll, stop, dispose, replay start, release its
owner or establish production route freshness. A distinct pre-borrow deadline refusal before a later
stream read returns a partial deadline result with the already observed facts; unrelated validation
refusal is never relabeled as a deadline. An interrupted attempt that acquired custody instead
preserves the original exception with aggregate attempt facts.

The private bounded wait repeatedly invokes that collector only while its last observation is
complete, valid and settled, the carrier has no failure, and missing run/stream/boundary facts may
still progress. Every poll uses the original finite deadline and caller-held exact VM owner. It does
not retry a launch, an ambiguous observation, refused output, truncation, or a fully observed
terminal outcome. A nonzero main-process exit can still be polled for missing descendant-boundary
and stream-end facts; it is never re-executed or reported as success. Expiry between polls or at the
next poll's admission returns the latest partial evidence with a deadline flag; it never claims
termination or clears an earlier start obligation. This is a private lifecycle step, not public
`JobAccess`, production routing, recovery takeover or native carrier acceptance.

At the first production later-action caller, bind route freshness at the borrowed fixed helper's
actual carrier boundary. An optional internal check supplied by that caller runs after the exact
operation attempt has durably admitted its possible effect and immediately before the underlying
carrier call. It compares the selected provider locator, connection and runtime facts; the shared
carrier knows none of those platform details. Invalid local inputs refuse before the check, and a
changed or unconfirmed route refuses without invoking the managed helper. The callback's own failure
must retain its original exception and truthful custody facts; an admitted obligation is not
silently resolved merely because the local check refused. This narrow seam is added with its first
real caller, not as an unused callback on the private adapters. Like managed start's existing
post-arm check, success narrows the route-change window but cannot make comparison and dispatch
atomic.

Before arming, a refused registration, expired deadline or owner close releases the unused borrow;
an installed but unarmed obligation resolves during that release. A failed reservation leaves the
caller responsible for discarding its prepared input. Once arming may have begun, the borrow retains
the possible effect even when the database transition reply is lost. The generic borrow records this
conservative state separately from whether a carrier attempt is outstanding.

The private bound-start caller supplies and retains the exact run ID. If any control flow escapes,
including uncertainty about the reservation commit, it inspects that ID and never retries start with
it. A refusal after reservation may leave a durable `RESERVED` row as a one-shot tombstone;
`RESERVED` alone is not permission to delete it, because a managed-start obligation may already be
armed while that row still has this state. An inconclusive inspection or obligation state retains
owner custody until reconciled. Do not fabricate possible-dispatch evidence merely to fit the
current database terminal-state constraint. Production recovery and disposition of these retained
rows remain open.

Target identity is structured as a core resource kind/name, a versioned incarnation fingerprint and
a separate boot UUID. The core name supports binding and diagnostics but is not authority. The
incarnation fingerprint must bind the provider-owned locator plus a core-provisioned or explicitly
adopted random instance marker. Existing VMs require an explicit adoption workflow; an ordinary
operation does not silently write that marker. `/etc/machine-id` does not replace the marker because
it does not reliably distinguish clones. Production target composition remains blocked until it can
construct and verify these facts.

The private implementation checkpoint now supplies the version-one VM fingerprint codec, a bounded
fixed-path Linux guest probe and a pure composer. The probe makes one no-replay carrier attempt,
admits the pinned runtime, validates the protected root-owned marker path, reads the canonical
kernel boot UUID and PID 1 start ticks, and keeps carrier facts separate from the nonce-bound
observation. The codec length-prefixes the exact provider locator bytes; the composer verifies the
persisted marker against the guest observation and keeps a derived boot UUID as a separate target
fence. The derived UUID binds the kernel boot UUID and the guest init process start ticks under a
fixed domain. This is necessary on WSL2, where a distribution can restart without rebooting the
shared utility-VM kernel. It identifies an ordinary guest boot, not a provider incarnation that
cannot be copied; native restart and recovery proof remain open. Provider-locator unavailability,
legacy NULL markers, unsafe guest paths and mismatches refuse without mutation. This does not claim
production/platform wiring, explicit adoption, a locator-unavailable alternative or a public
RunContext target; the combined target-identity gate remains open.

Private target preparation now consumes those pieces under one caller-owned exact-VM operation and
finite deadline. It validates that the current coarse claim covers the VM, refuses static missing or
malformed evidence before borrowing, makes one serialized helper attempt, records the guest result
before settlement and retains the operation after uncertain dispatch or control flow. Normal helper
completion may settle that attempt while preparation still rejects an invalid observation, runtime
refusal, carrier failure or identity mismatch. The helper neither acquires nor closes the outer
owner and performs no activation, route selection, adoption or persistence. A second private seam
accepts the selected VM, its bound platform, an expected locator observed before binding resolution,
and one caller-owned native binding. It validates owner, marker and deadline before borrowing. Under
one serial borrow it validates a fresh plugin locator and compares it to that expected value, probes
the guest through the supplied binding and confirms the locator afterward. It never resolves another
binding. The resolving caller must use the same deadline, validate and retain the selected route,
and refuse a late resolver result. WSL2's private factory copies the selected distribution and
executable into separate ordinary-delivery and initial fixed guest-facts carriers. The probe uses
root entry and named admission to the configured account under the same operation borrow; file
dispatch retains ordinary admin delivery. A registration replacement during resolution is detected
before the probe; one between the pre-probe observation and dispatch may still receive the read-only
probe, but a changed post-probe locator suppresses file dispatch. Unavailable or invalid
confirmation is unconfirmed; two valid unequal locators establish change. Release failure suppresses
the target and attaches an uncertain custody fact with guest evidence. The private WSL2
owned-operation extraction now shares this selected route, VM owner, durable hold, prepared target
and exact release with download and independent managed start. The managed caller checks a fresh
locator, connection and runtime, then confirms the locator after resolving the binding immediately
before entering the bound-start composition. It does not automatically release the hold after start.
Exact hold settlement and whole-owner closure are separate: unresolved managed-start custody can
retain VM ownership after the guest anchor is gone. A second route check now follows durable
managed-start obligation arming and precedes the run's possible-dispatch record and carrier attempt.
A valid changed route and an unconfirmed observation are distinguished; a post-arm refusal retains
the exact `RESERVED` run as a one-shot tombstone and keeps owner custody. Exceptional observation
propagates its original control exception. This narrows the dispatch gap but cannot make locator
observation atomic with carrier delivery; this sibling has no production caller or native WSL2
proof. Production still needs an owner before activation, a locator-bound route lifetime, typed
whole-span cleanup evidence and native proof. WSL2 supplies the first positive locator and binding
pair. Proxmox now supplies a private current-generation locator hook under the exact configured
verified authority; native PVE 8/9 generation/adoption policy and whole-span composition remain
unproved. SSH-backed cloud and Lima bindings remain later work. Malicious or engineered A-B-A host
behavior is outside this checkpoint's threat scope. Future hierarchical admission replaces exact-VM
equality with a core-owned coverage decision rather than a target-local ancestry guess.

New VM creation generates and persists one non-secret marker before provider dispatch. The marker is
exactly 32 lowercase hexadecimal characters and the shared create bootstrap writes that same value
to `/var/lib/agentworks/instance-id` as a root-owned `0444` regular file. Bootstrap replaces an
ordinary template-cloned predecessor but refuses a symlink, non-regular leaf or a leaf with more
than one hard link. Existing rows remain NULL and existing-VM operations do not write a missing
marker. This is only creation evidence: explicit legacy adoption and production provider/guest
composition remain delivery gates despite the private probe and codec checkpoint above.

Lima is an exception to retained-bootstrap delivery: its `mode: system` provisioner reruns on guest
restart. Lima therefore excludes marker publication from its retained YAML and streams the fixed
installer once through `limactl shell ... sudo -n /bin/bash -s` after create/start, inside create
rollback. Later Lima start/restart operations do not install or repair a marker. Isolated
user-namespace tests prove the installer guards and resulting modes, but live platform proof of the
target root owner and delivery remains a later integration gate.

`ProvisionRequest.instance_marker` was receive-side additive to the v1 platform contract, so an old
third-party implementation could receive and ignore it without establishing managed target identity.
Vm-platform v2 is a hard cutover that adds only read-only provider-locator observation: one bounded,
opaque token or explicit unavailable. It does not read guest markers, compose a target fingerprint,
persist locator state or make managed target identity available. The private guest probe and
composer above do not change that platform-hook claim. Production marker delivery and composition,
explicit legacy adoption and live carrier proof remain conformance and delivery gates.

Reservation commits before dispatch, and possible dispatch commits before calling the one-shot
launch boundary. An exception or ambiguous result retains possible dispatch and cannot call the
boundary again. Reconciliation accepts an exact receipt only when run, unit, target incarnation,
boot, workload, resolved shell, profile, owner/lifetime, namespace and protocol all match. A
carrier-proved `NOT_SENT` observation establishes non-launch only with a typed exact absence
observation from that same protected target receipt namespace. Mismatch, contradiction and malformed
persisted state fail closed without launching or stopping work.

The coarse operation claim has a wider lifetime than any launch attempt. Core first arms that claim
before activation or another lifecycle effect, then lends it serially to child dispatches. Each
child may settle only its own attempt. The claim becomes resolved only when core has aggregated
typed evidence that activation, holds, routes, workflow and teardown can no longer cause effects.
Closing an owner with a possible-dispatch claim but no explicit whole-operation resolution refuses;
it never promotes "no child attempt is currently open" into quiescence. Interrupted admission,
resolution and release reconcile the same fenced record before any later dispatch or release.

### Owned native platform access

The planned remaining-platform composition keeps one core operation owner and one aggregate
teardown, but moves concrete activation and keep-awake evidence out of that orchestrator. A passive
platform factory builds a private owned access object under the already-acquired VM owner. Core
retains that object before preparation can perform effects. The passive native binding remains a
route description, not an activation method or a lifecycle owner.

The small access contract exposes its selected binding, existing target-preparation result, and
optional bound route-check callable. Preparation receives the observed power and original deadline;
settlement receives a finite cleanup deadline and reports whether its own effects are settled. The
object retains actual partial progress and interrupted-reply evidence, never resolves or closes the
whole operation, and does not copy a second guest-identity field. Platform preparation calls the
shared locator/guest helper rather than reimplementing it. WSL2 additionally compares the prepared
guest with its durable held READY identity before admission. Core performs the common numeric
account preparation and supplies file/execution views only afterward.

Keep the existing WSL2 route checks at managed read, shell lookup, stop, disposal and launch
admission by binding its route-check callable. An absent callable does not assert fresh route
evidence. No dummy hold is constructed for a platform without idle-stop, and there is no shared task
model for Windows anchors, Proxmox UPIDs or cloud-provider payloads.

Aggregate close first stops body admission and settles execution, files, keepers and local delivery.
Only then may platform settlement reconcile its existing activation or release its real hold. WSL2
must refuse hold release while unrelated obligations remain and preserve exact native-client and
guest-anchor absence evidence. Proxmox observes or reconciles its single retained activation, never
retries the POST, and never substitutes running power or HA handoff for supported task settlement.
The core subsequently checks the final ledger and local custody independently before sealing,
resolving and releasing its owner. RESOURCE jobs remain outside operation-owned cleanup. Ordinary
teardown does not stop a VM merely because this operation activated it.

Cloud passive observations use deadline-derived SDK budgets and reject successful late results, not
hard preemption. AWS configures socket timeouts at client construction, so setup can consume part of
that budget before the request. Azure and GCP disable service/network retry middleware but SDK
authentication can still resend a read. Core does not manually replay the provider query or accept
late evidence as activation authority. These limitations do not weaken the separate one-shot
execution/activation admission rules. Cloud activation and route factories still require their own
reviewed evidence; passive power observation alone is not full native access.

The 2026-10-09 locked-SDK audit found automatic resource-provider registration in Azure's default
compute and network pipelines. Disabling retry middleware did not remove that policy: a failed GET
could cause registration POST, polling and another GET. The audited new reads did not satisfy the
passive contract. They now own separate clients with explicit disabled-retry and ARM-authentication
policies, without registration or redirect following; legacy clients are unchanged. The generated
GET methods decode their own responses, so a separate content-decoding policy is unnecessary. Actual
SDK transport tests establish refusal of this implicit mutation for both clients, not live native
acceptance. A finite authentication resend remains read-only and deadline checks remain best effort.
Future mutation producers must separately prove absence of authentication, redirect and
provider-registration replay; one SDK method invocation is not that proof.

### Cloud SSH route composition

The first cloud binding increment selects current public IPv4 endpoints from provider-owned
exact-instance reads, not from the retired native transport. AWS retains its persisted account,
region and instance checks; GCP also retains the recorded network, subnet and access-configuration
checks. Azure follows NIC/public-IP references from its verified VM and validates the linked
identities and subscription. Its additional reads share the original deadline and reject late
successful responses. These SDK budgets retain the best-effort limitations above.

Composition requires the explicit new `operator.ssh` settings, using their resolved identity,
managed trust directory, agent and client policy. It does not read ambient SSH configuration or fall
back to legacy fields. The first increment intentionally uses endpoint-keyed host trust
(`host_key_alias=None`); it does not invent a VM alias mapping, enroll a target, reset trust or
inspect key files during binding. Preserving/importing existing trust and creation-time enrollment
remain separate work. A changed endpoint without admitted trust is refused rather than learned by
ordinary execution.

The Linux binding retains the recorded VM admin account for delivery and a fixed root-entry,
named-account guest probe. Delivery as root uses direct entry; other admin accounts use explicit
passwordless sudo. The existing bootstrap resolves the account's actual UID/GID/groups before its
named body; no UID is guessed. Managed delivery uses fresh carriers with the same immutable
connection. Binding alone advertises no independent-job availability: owned activation, firewall
routes, keep-awake guarantees, complete RunContext composition and native proof remain open.

### AWS activation admission and acknowledgment

Implement AWS startup in its plugin, not in a generic cloud task framework. A private
`plugins/aws/_activation.py` adapter owns one EC2 start submission under the existing exact VM
operation owner. Its constructor is passive. The platform factory retains the adapter before its
`start(deadline)` can perform work; it supplies the selected provider session and the already
validated account, region and instance ID. Compare that triple with the selected canonical
`aws-ec2:<account>:<region>:<instance>` locator. Do not copy a locator hash, credentials or the
legacy EC2 client cache into the adapter.

The first implementation unit is the admission and acknowledgment producer, not complete cloud
access. It deliberately provides no power-based settlement method. This is a recoverable private
commit within #833, not a separately mergeable feature or permission to expose stopped AWS access.
The subsequent access integration must establish and review the limited successful-start settlement
rule, then prove it natively before advertising that access. Missing acknowledgment cannot be
reconstructed from a later running-power observation.

Use one fresh EC2 client with public deadline-derived connection/read timeouts and standard retries
configured for one total attempt. The locked SDK dispatch proof covers the ordinary service
pipeline, not arbitrary custom event handlers, credential-provider requests, exactly-once server
acceptance or hard elapsed deadlines. Client setup and credential work remain synchronous and
best-effort bounded; reject expiry before admission and reject successful late returns. Do not use a
waiter, automatic retry, background polling, private SDK policy mutation or the legacy start method.
Close a successfully returned client during setup and dispatch cleanup. Ordinary close errors must
not replace the earlier outcome. A new close-time `KeyboardInterrupt` or `SystemExit` escapes;
preserve an already escaping original control. This does not promise atomic resource handoff inside
the SDK client constructor.

Before invoking `StartInstances` with exactly the selected instance ID, register a fresh
`aws-ec2-start` lifecycle obligation and durably mark its possible effect. The adapter admits at
most one call, including after interruption. Its canonical payload contains only account, region,
instance ID and an optional provider request ID, with version 1 carried by the ledger row. Bound and
validate that persisted recovery input under the existing lifecycle payload limit. The request ID is
a bounded non-secret acknowledgment identifier, not an idempotency token or an operation lookup
endpoint.

Validate returned external data before retaining acknowledgment: one state-change entry for the
selected instance, ordinary successful HTTP metadata, zero SDK retries and a nonempty bounded
request ID (at most 256 UTF-8 bytes, an internal storage bound). State-change fields do not
establish acknowledgment identity or startup completion, so this producer does not interpret them.
Retain the matching acknowledgment before payload publication and before testing whether the
original deadline expired. A lost database reply can then reconcile the exact initial row or the
exact acknowledged revision without another provider call. Lost, malformed, foreign or exceptional
provider responses retain possible effect; generic SDK exceptions do not prove rejection.

Reconciliation performs only fenced ledger bookkeeping. An exact registered row may be resolved only
if this retained adapter never began possible-effect admission. Once that admission began, even a
local interruption before the SDK call conservatively retains uncertainty. A matching acknowledgment
may advance the payload once; it cannot resolve the possible effect. Reject foreign kind, version,
payload, revision, fence or premature resolution. The adapter never closes the whole operation,
stops the VM or replays the start.

The next integration still owns fresh power observation, guest preparation, changed-endpoint trust,
firewall-route lifetime and aggregate release. AWS has no public start operation token in this
response, so those pieces must not inherit Proxmox's UPID settlement algorithm. See the
[research](prior-art-research.md#aws-start-submission-proof) for the SDK proof and its limits.

### Proxmox activation evidence boundary

Stopped Proxmox startup needs a new bounded producer, not the retired start waiter. Its first
delivery slice adds fixed body-free start POST and task-status GET primitives to the existing
verified, deadline-owned HTTP worker. The start response's scalar `data` is raw acknowledgment; task
status retains an object envelope. Neither primitive constitutes operation admission, a validated
task receipt, completed startup or provider freshness. The wire capacity for task IDs is 255 UTF-8
bytes, a conservative task-log filename capacity rather than an API schema limit. Higher composition
validates the receipt before using it; the wire quotes it as one path component and never selects
arbitrary provider routes or start options.

The planned owner integration retains one fresh activation obligation before the single start POST,
under the exact VM claim acquired before observation. Its versioned non-secret payload identifies
the selected authority/VM route and expected locator, then preserves the returned UPID and task node
before later observation. Do not persist credentials, provider diagnostics or task output. The same
owner covers preparation, guest body and aggregate teardown. Stopped intent refuses automatic
activation; passive readiness never starts the VM. Ordinary teardown does not stop a successfully
activated VM merely because its operation ended.

A matching receipt identifies asynchronous work. It must match the selected node, VM ID, start task
kind and original API identity; task status must match that retained receipt. Token status splits
the original identity into user and token ID, so compare the complete original identity, not the
displayed base user alone. Preserved historical task identity is separate from authorization of a
later observation route. Terminal status, task outcome and fresh VM power are distinct facts.
Preserve warnings separately from failure; neither arbitrary error text nor a missing task log
establishes completion or rejection. A lost/malformed acknowledgment, generic HTTP failure,
interruption or late response retains uncertain admission without another start POST. Known receipts
may be observed again under a fresh finite budget; task-list search and a running-power snapshot
cannot reconstruct or settle a missing receipt.

The activation row owns the one admitted start request, not every future effect of the VM. A fully
matching ordinary start worker observed stopped with exact `OK` or supported `WARNINGS: N` can
settle that request. Stopped status alone is insufficient: killing the original worker can leave its
forked launch child running. Failed, unknown, missing or mismatched task evidence therefore retains
custody. Retain successful terminal observation before resolution so a lost database reply retries
only bookkeeping. This is neither whole-operation resolution nor permission for a guest body: fresh
running power and exact target preparation remain separate admission conditions. The VM may keep
running after aggregate operation release, and an HA handoff cannot use ordinary-worker settlement
to discharge manager work. Native proof of the limited successful-worker interpretation remains
required before production startup.

Successful task settlement and fresh running power precede a separate passive guest-agent wait. Use
only the fixed body-free `GET /agent/info` route, under the original finite deadline and selected
verified connection. Its response must contain the required version string and supported-command
array. A valid response proves channel responsiveness, not Python availability, target identity,
account admission or permission for a body. Repeat only this read-only observation while boot is
pending; never poll by launching guest helpers or replaying start. Exact guest/numeric preparation
still follows once, under the same operation owner. Malformed, unavailable or late readiness cannot
authorize guest work; settled activation does not make the whole workflow resolved.

HA-managed VMs return a different handoff task. Completion of that handoff is not proof that the HA
manager's activation work has ended. Native ownership/availability proof must cover that path rather
than treating it as an ordinary completed start worker. Proxmox's public start API has no expected
generation/configuration precondition: its worker loads configuration under a later VM lock.
Client-side pre/post observations are therefore not atomic stale-request prevention. Exact receipt
observation does not establish queued-request drain, incarnation safety or a blanket provider
quiescence guarantee. Owner integration, PVE 8/9 token/HA/generation proof and production startup
remain open; the wire slice alone cannot expose complete RunContext availability.

### Durable lifecycle-obligation ledger

Production ownership needs a durable handoff between the coarse resource claim and the independent
effect owners inside one workflow. The `lifecycle_obligations` ledger supplies that handoff. It is
generic coordination state, not a serialized workflow. Each row belongs to one exact fenced
operation identity and has a fresh obligation identifier, a registered lower-kebab kind, a positive
payload version, a bounded opaque non-secret payload, timestamps and one closed state:

- `registered`: the obligation exists and no effect is yet admitted;
- `possible-effect`: admission committed before the remote or local effect;
- `resolved`: typed adapter evidence proves this obligation can cause no further effects.

Core validates the exact operation fence for every transition but never decodes adapter payloads.
The registered adapter owns payload validation, target/incarnation comparison, observation and the
typed evidence accepted for resolution. Bound each encoded payload to 8,192 bytes and unfinished
`registered`/`possible-effect` obligations to 128 per operation. Retain immutable `resolved`
receipts until operation release for exact-ID registration and resolution retries; completion frees
admission capacity but does not permit rearming or rewriting the receipt. Payloads may contain
target identity, protected namespaces and cleanup receipts, but never credentials, application
input/output or arbitrary workflow state. Keep managed run records specialized until a real consumer
proves that folding their typed schema into opaque obligations would simplify rather than weaken it.

Pending-obligation enumeration returns only the bounded unfinished rows. Exact receipt lookup
separately checks current operation ownership, returning absence only under that valid fence;
ownership and queried rows must come from one coherent database snapshot. A takeover between
separate autocommit reads must not turn stale ownership into an empty ledger or missing receipt. The
snapshot's owner read is its linearization point, not a promise that ownership cannot change after
the read returns. Resolved receipt consumers must not treat exclusion from pending enumeration as
missing history. Use a matching partial index for the fixed `state IS NOT 'resolved'` predicate so
admission counts and pending queries do not scan completed history. Unexpected states remain
unfinished for query purposes and must fail decoding, never count as completed. Bound enumeration
before decoding. Whole-operation resolution and release use unfinished-row existence checks;
reserved abandonment still refuses if any obligation exists, including a resolved receipt. A known
never-dispatched registration refusal may relinquish its local admission only with current-owner
absence evidence; interrupted replies, present/conflicting receipts and failed/stale observations
retain original custody. When failure classification completes, preserve the original registration
exception object and traceback, including ordinary errors as well as control flow. A new control
interruption during the absence read escapes without classifying the registration as clean; the
original registration error remains its exception context and custody remains uncertain. An internal
typed immediate cause may carry proved-absent registration evidence for local cleanup, preserving
the original explicit cause beneath it; consumers use one shared recognition rule rather than infer
absence from error text. Cancellation does not become an ordinary refusal or receive invented
no-registration evidence. Absence evidence belongs to one registration attempt. Reusing an exception
object must not carry its previous absence marker into a later attempt that persisted or became
uncertain.

The bound is on unfinished debt and decoded recovery work, not total within-operation disk use or
constant memory/close time for the complete operation. Completed receipt rows and retained managed
run instances grow with work, and release deletes that history. Do not add receipt retirement,
tombstones, reusable slots or another adapter ledger solely to make that history constant-sized.
Migration 42 adds the state index through the ordinary upgrade pipeline; migrations 39-41 remain
unchanged and existing data is retained. This capacity correction remains an implementation gate.

An adapter may replace its opaque payload with exact recovery identity while the obligation remains
`possible-effect`. That update is not a generic lifecycle state or a sealing prerequisite. A typed
no-effect result can resolve an obligation that never created a runtime identity.

Ordinary registration refuses after the ledger is sealed. The first `possible-effect` transition
also arms the coarse claim in the same short transaction; each later dispatch revalidates the
current owner generation against its already-possible obligation before permission returns. Later
obligations advance independently. Once the workflow cannot create more effects, core seals the
ledger. Whole-operation resolution requires a sealed ledger, every registered obligation in
`resolved`, and no active borrow, outstanding attempt or retained in-memory cleanup custody. Final
release deletes the resolved obligations and releases the exact claim atomically. No automatic
expiry, generic retry runner, dependency graph or force-release belongs in this layer.

The private inline-capacity correction uses one empty `carrier-dispatch` obligation for the lifetime
of the core-owned `ExecutionOperation`, shared by ordinary and elevated views. Each call still
acquires a fresh serial borrow. It installs the same exact retained identifier, explicitly arms that
borrow before candidate validation, settles the actual helper attempt only with the existing
termination evidence, and hands off the retained effect instead of resolving the row. This permits
intervening file calls without holding a workflow-long borrow or consuming a new row for every clean
command. Exact registration already accepts the unchanged possible-effect row; resolved rows are
never reopened, pruned or repurposed. The row bounds and payload schema remain unchanged.

An explicit execution `finish` permanently closes this state's admission, coordinates with call
admission, and resolves only its own row after its active and uncertain custody is settled. It
creates no row for unused state and retries only exact bookkeeping after a lost reply, never replays
a request. Registration and arming retain the actual call, borrow and identifier before submission.
In particular, installation alone does not arm a new borrow: a validation failure must not
default-close the lifetime row or attempt an unarmed retained handoff. Unknown attempts block new
work and resolution. The outer workflow finishes this execution state before whole-ledger
finalization while preserving its aggregate file, availability and owner checks. The private
implementation now supplies this lifetime row, exact bookkeeping retry and native teardown seam.
LocalCarrier high-volume and mixed-file tests exercise actual helper composition; these do not
establish native/platform or complete RunContext acceptance. Integrated review of interruptions,
reply loss and concurrent finalization remains required before publication. DIRECT helper completion
does not thereby acquire a stronger user-descendant cleanup or native-drain guarantee.

### Later observation of an acknowledged QGA helper

A QGA observation deadline can end while its acknowledged invocation still runs. For this case, the
selected native binding supplies a passive per-call delivery/observer factory. Core retains the
resulting object before dispatch, alongside the actual helper call, original attempt and
still-settleable borrow. This is a private optional native composition seam, not another parameter
on `Carrier.execute`, a stronger `CarrierReport`, or an SSH requirement. Plain delivery remains
unchanged. The first implementation slice covers inline DIRECT helpers; managed read preparation
then uses the same seam without creating a second recovery mechanism.

Preparation supplies immutable non-payload closure expectations explicitly: original nonce, runtime
candidates and shim, execution identity and the existing selected guest checkpoint where applicable.
Do not derive these from generated argv or downcast a caller sink. After the original call returns
or raises, retained cleanup custody detaches finite input, application collectors and sinks. Later
observation uses a new bounded runtime-prefix reader with a discard destination, not the completed
reader or caller I/O. It never reconstructs the returned application result.

The concrete observer retains the validated acknowledged PID before another status request. It
publishes validated terminal closure evidence before optional output callbacks, because a terminal
QGA status read consumes its record. A fresh finite cleanup budget first settles the original local
delivery, revalidates exact database ownership and rejects a known changed route/guest binding, then
permits only a status GET for that held PID. No new POST, fallback, supervisor or guest file is
introduced. A failed status exchange may already have consumed a terminal response; missing status,
lost acknowledgment and lost terminal evidence remain unknown, never absence or completion. This
slice does not retry a failed status exchange or promise signal-atomic acknowledgment publication.

Closure requires an authentic original runtime READY prefix and independent normal-zero completion
of the exact prepared helper invocation. READY precedes the helper's guest check: it is neither a
fresh current-boot receipt nor proof that a managed observation succeeded. Closure settles only that
helper's future effects and original attempt. RESOURCE disposal still requires its separate positive
terminal observation and action proof. A failed or identity-refused managed read that returns zero
must not acquire those proofs from helper closure.

Core exposes explicit finite observation cleanup before aggregate execution `finish`; existing
bookkeeping retry stays no-I/O. It retains the actual call rather than handing off its borrow and
reducing it to booleans while continuation is possible. Local cleanup, observation and lost database
replies reconcile the same retained objects. Lost ACK, consumed status, unknown nonzero completion,
controller restart and other carriers still need their separately tracked recovery paths. Prove
deadline-then-closure, interruption after ACK and terminal publication, wrong nonce, changed
binding, unsettled local custody and unchanged application UNKNOWN before native acceptance.

Recovery must be fenced from a delayed original controller before it mutates an old obligation. The
logical operation keeps its stable random operation identifier, while each database owner carries a
separate random generation identifier. Initial acquisition creates both. A recovery controller
chooses and retains a fresh generation identifier before its takeover transaction. The transaction
compares the complete predecessor ownership, rotates only the generation, and seals the obligation
ledger atomically. An exact retry with the same predecessor and requested generation is idempotent,
including after commit without reply; a different generation or delayed predecessor update is stale.
Obligations remain attached to the stable operation identifier, so takeover neither copies nor moves
adapter state to a different owner.

Within one local database/controller, exact takeover retries also resolve to one live recovery-owner
object and therefore one serial-use guard. Each retry validates the durable claim before consulting
that weak in-process canonical owner, so a referenced object cannot revive released or stale
ownership. Repository wrappers backed by that controller share the identity; separate `Database`
instances and independent controllers do not. Do not add a durable "dispatch active" bit to bridge
that excluded case: a crashed controller would leave another recovery protocol whose safe clearing
still requires adapter drain evidence.

Takeover also creates a restricted recovery owner, not ordinary dispatch authority. It may rebind an
exact persisted row, publish recovery identity for an effect that was already possible, or resolve a
row with typed evidence. It cannot borrow the ordinary dispatch path, treat registration retry as
rebind, or transition a previously `registered` row to `possible-effect`. Any future recovery probe
or cleanup dispatch requires its own adapter-owned admission contract and remote-drain proof.

Recovery also needs owned availability around its preparation and reconciliation. A predecessor's
hold observation is not a live lease, and its native pipe or process handles cannot be reconstructed
from a durable identity. Core may therefore admit a new, bounded recovery-support effect under the
same logical operation and exact recovery generation. This is a separate admission API, not ordinary
registration, an unsealed ledger or application replay. The transaction requires a sealed,
unresolved recovery claim, inserts a fresh row directly as `possible-effect` and arms the coarse
claim atomically. It retains every predecessor row and enforces the existing row/payload bounds.
There is no intermediate `registered` row that recovery could promote into an old application.

The caller chooses and retains the support obligation ID before admission. An exact persistence
retry may confirm the same kind, payload version and bytes in a still-possible row; it never
authorizes repeating an uncertain launch. Conflicting or terminal rows refuse. Each renewed
availability span owns its own controller/nonce/anchor row, rather than adding several anchors to a
predecessor payload. Another takeover inherits all those rows and their unresolved observation
debts. Capacity exhaustion refuses further support admission without overwriting history.

Core composition selects the concrete availability and fixed read-only preparation adapters needed
for recovery. It uses the same platform availability contract as ordinary workflows, including
stopped intent; a platform needing no hold may supply a no-op span. Support handles provide no
ordinary application dispatch. Each adapter keeps admission, launch-attempt custody and cleanup
together, proves the applicable endpoint/drain boundary and retains uncertainty after interrupted
launch or observation. Fixed guest/account observations use the existing recovery-dispatch serial
boundary, not ordinary borrowing or a direct carrier escape. A live support span must cover fresh
preparation through the final retained-file action. Final resolution still checks every original and
support obligation and excludes active or uncertain custody. The hold's durable row and retained
native object represent its lifetime; holding the serial recovery-dispatch guard for that entire
lifetime would incorrectly exclude the preparation and file attempts it supports.

The private owner/repository now implement atomic support admission while ordinary registration
remains sealed. The private WSL2 hold's explicit recovery startup consumes that admission and reuses
its existing single-use anchor, transition lock, READY publication and exact release. Intentional
live custody belongs to the row and retained native object, not a finite recovery attempt or another
coordinator state. If takeover follows admission but precedes launch, the already admitted effect
remains inherited debt and stale publication/resolution refuse; an extra in-process check would not
establish a remote fence. Core awaits durable READY before fixed preparation and keeps the hold
through its final recovery action. This is not a complete recovery path: outer renewed-availability
composition, retained-file adoption, predecessor drain and native acceptance remain open.

One serial fixed guest/account preparation batch owns a separate bounded `carrier-dispatch`
obligation, preserving the existing version-one empty-payload meaning used by ordinary fixed helper
preparation. Core retains its fresh ID and adapter before atomic support admission. Each concrete
query uses exact recovery-dispatch custody and the existing termination classification. Completed
protocol refusal can settle a helper without granting preparation facts. The batch closes its
dispatcher and resolves only its own row after it stops and every admitted query is accounted for;
unknown dispatch, interruption or uncertain bookkeeping stops further queries and retains debt.
Another takeover inherits that row independently of the availability hold. Anchor absence, a newer
successful preparation or hold release cannot discharge an earlier uncertain batch. This requires no
new schema, codec, query counter or row per individual query. The empty row retains uncertainty; it
cannot reconstruct native endpoint identity or prove complete predecessor drain after process loss.
Until concrete adapter evidence supplies that proof, retain the claim without replay.

The private `RecoveryGuestPreparationBatch` implements that serial boundary on one pinned native
binding. It composes fresh fixed guest/selected-locator observations before numeric delivery,
workload and optional elevated account observations. It shares ordinary observation bodies without
opening ordinary borrowing to recovery. Original escaping control exceptions carry safe retained
preparation facts; unknown or uncertain attempts stop the batch. A retry can reconcile only a
stopped, settled batch's exact resolution, never repeat a probe. The caller must retain the batch
before admission and separately own the durable-ready availability span through its last recovery
action. The binding retains its exact local dispatcher before activation. Separate stopped-query
accounting permits a retry to close only an unused unreturned opening or the batch's interrupted,
fully settled local close before resolving its row. It never clears another dispatcher or an
outstanding attempt. Binding reuse cannot let an early refused opening target a prior caller's
handle. This narrow cleanup does not promise signal-atomic Python bookkeeping or native drain. This
private implementation does not establish that outer span, adopt retained-file records or supply
native endpoint/drain proof.

The private core `RecoveryVMSpan` now composes that outer lifetime under the caller's existing
sealed recovered owner. Its first concrete factory requires explicit administrative WSL2 work; other
platforms still refuse until their bounded power, route and availability adapters are implemented.
The caller retains the span and fresh independent hold/preparation IDs before opening. Fresh
persisted VM/site/marker/account and operator-stopped intent checks precede activation. The selected
copied route and actual native object remain attached before recovery support admission; only exact
durable READY permits the separate preparation batch. Prepared target, full guest, numeric plans and
runtime cannot be substituted independently of that lifetime.

A finite local action scope serializes against close and exposes a carrier usable only in that
action and thread, under no later deadline. Admission and concrete carrier dispatch revalidate
owner, VM, route and exact durable hold custody. An escaped action carrier refuses; host-client
ACTIVE remains only a negative failure screen. That screen reads the retained native owner's current
local snapshot separately from historical READY/guest facts and refuses late observations. This does
not turn a READY history or power read into positive continuing guest identity or native queue-drain
evidence. Individual retained-file adapters still own their recovery attempts and must supply
applicable predecessor proof.

Close stops only span/view admission. It first permits exact settled preparation cleanup, including
an interrupted local opening or close, then requires idle ownership before hold teardown. Unknown
attempts and conflicting dispatch remain retained; all other possible-effect rows must resolve
before releasing the span's hold. A hold known never to have launched can reconcile its own cleanup
without resolving predecessor debt. Cleanup neither repeats preparation, stops aggregate owner
admission nor resolves another row. Original escaping exceptions carry the actual retained span.
SQLite and fake-native tests cover this private composition, not native availability/drain or
retained-file adoption; the complete production recovery and platform-neutral cutover remain open.

That contract uses a distinct internal recovery-dispatch object, not a recovery mode on the ordinary
borrow. Admission requires the sealed recovery owner, its exact current generation, a
`possible-dispatch` coarse claim and the exact `possible-effect` obligation identity, state, payload
revision and bytes. Every attempt revalidates those facts in the database, including after an
adapter publishes more exact recovery identity. The object shares the owner's serial-use guard, so
an active or uncertain recovery attempt excludes another recovery dispatch, obligation resolution
and owner finalization. Settling and closing it release only in-memory dispatch custody. They never
register an obligation, mark an effect possible, publish payload, resolve the obligation or infer
whole-operation quiescence.

The adapter keeps recovery dispatch admission, concrete action and terminal classification in one
handled control-flow region. If attempt admission mutates custody but does not return an attempt,
the adapter aborts that unreturned in-memory attempt. Once it has received the attempt, an exception
before terminal classification conservatively hands the attempt off unresolved. This closes handled
exception gaps without claiming signal-atomic Python bookkeeping or implementing general
cancellation.

Concrete drain evidence and its producer remain adapter-owned. The generic operation coordinator
validates exact ownership and row custody; it does not learn carrier process identities, helper
tokens or proof mechanisms, and it exposes no generic replay surface or proof-provider registry. A
proof is bound to the requested generation transition and obligation, but its meaning is broader:
every outstanding dispatch for that obligation across all earlier generations has drained or been
remotely fenced. Recovery of recovery cannot forget work admitted by an earlier predecessor. A
controller exit, an arbitrary list of generation identifiers or a proof covering only the immediate
predecessor is insufficient. If the adapter cannot reconstruct complete coverage after restart, it
refuses recovery and retains the claim.

That database fence prevents further cooperating submissions but does not establish remote
quiescence. For every obligation already in `possible-effect`, the adapter must then prove that
every admitted dispatch has drained or is remotely fenced against further effects, including work
already active. A fenced late arrival may still occur but must refuse before effects. Acceptable
evidence includes carrier-proved non-dispatch paired with exact absence, an operation-specific
remote generation fence, or equally strong proof from a synchronous local substrate whose controller
and dispatch endpoint are both gone. Controller absence by itself is insufficient because provider,
carrier or guest queues may outlive it. This recovery fence is separate from #377's future resource
hierarchy. The ledger remains attached to the logical operation across that transition so later
hierarchy can bind one operation to several resource memberships without moving adapter state onto
one VM row.

Local process-loss evidence must observe the actual dispatch endpoint or helper, not merely join the
controller process. A controller may die after launching a subprocess that continues independently.
The local proof therefore observes every helper associated with the exact obligation before
admitting recovery and includes a negative surviving-helper case that retains the claim. A
test-owned helper journal may demonstrate this local property; it is not production state or a
generic registry. This evidence says nothing about SSH, QGA or another native carrier until that
adapter proves an equivalent boundary.

The unavoidable crash window is conservative. Core registers and marks `possible-effect` before
dispatch, then the adapter publishes exact identity as soon as it observes it. If the controller
dies after the effect but before identity publication, recovery never replays the mutation. It uses
the registered preparation payload to discover exact identity or prove exact absence when the
adapter supports that operation. Missing identity publication is not absence. If delayed delivery,
discovery, quiescence or absence cannot be established, the obligation stays `possible-effect` and
the resource claim remains held.

For a WSL2 platform hold, the adapter payload binds a versioned, domain-separated SHA-256 digest of
the bounded opaque provider locator, VM instance marker, exact distribution, execution user and
exact Windows controller process identity. After `READY`, it adds the guest boot UUID, distribution
PID 1 start ticks, anchor PID and Linux process start ticks; PID/start evidence is meaningful only
within that distribution epoch. Host Job settlement and `wsl.exe` exit remain host-client evidence
only. Recovery must prove the recorded controller process is absent before using the creation-time
Job and synchronous Windows process-launch facts to establish that no delayed **Windows client**
launch remains. Those facts do not drain a `CreateLxProcess` request already admitted by WSLService.
Before a later guest-absence observation can resolve the hold, the adapter must independently prove
that the earlier service-side guest launch cannot still arrive. A durable `READY` identifies an
already launched helper; without it, dispatch remains ambiguous. A changed guest boot or
distribution init proves the old guest process cannot survive but does not excuse locator or marker
mismatch. Each `vm_active()` lifetime registers its own obligation and anchor under the enclosing
operation. Do not add hidden reference counting or collapse nested holds into one platform process.
A recovery adapter retains the obligation when dispatch drain, identity, quiescence or absence
cannot be established safely.

The private hold now constructs the guest anchor from its exact WSL2 connection and the same native
owner that captures the already-running controller PID and creation time in native Windows ticks
before registration. The controller is not the new `wsl.exe` child. It validates exact VM scope,
finite deadline and payload fields before native observation, then registers one obligation, marks
possible effect, dispatches once and immediately CAS-publishes READY identity, even when startup
then raises. The hold serializes start and release so a pre-dispatch snapshot cannot resolve an
in-progress startup. A caller's finite deadline also bounds entry to either transition; a timeout
leaves the obligation unresolved. It accepts only bare `wsl` or `wsl.exe` case-insensitively,
resolved by the native owner through the trusted Windows system directory, rather than persisting an
executable path. Registration, mark and publication uncertainty retain the caller-owned hold without
replay. New version-4 payloads explicitly distinguish root launch from the configured body account.
Existing version-3 records retain their former same-user launch and query meaning. Recovery rejects
an envelope/body version mismatch and preserves the decoded version on rebind and publication; it
does not rewrite records or treat a failed root-entry query as permission for another route. New
hold and query bodies use fresh reads from the bootstrap-held PID 1 descriptor after named
admission. Runtime readiness is consumed separately and establishes neither guest identity nor
credential admission. Before an ordinary guest query, the hold durably CAS-publishes the one-way
`query_may_have_been_admitted` marker with the exact guest identity. A failed READY publication
whose row did not advance can be followed by this combined publication; a committed publication with
a lost reply leaves a stale revision, so admission refuses without querying or resolving. Ordinary
release resolves only after local settlement and independent exact absence; it never resolves on
`EXITING`, client exit, Job settlement or missing guest identity alone. An interrupted query may
retry local settlement but cannot dispatch another query in that controller or claim absence. A
complete, validated `PRESENT` result permits a later query. The durable marker is not a service-side
dispatch-drain proof. Recovery discovery and production controller-absence composition remain
separate unfinished obligations. A private Windows observer now reports exact controller presence,
confirmed absence or unknown under a finite deadline; it does not establish dispatch drain or guest
absence.

This checkpoint does not wire `systemd.py`, carriers, platform factories, public `ExecutionAccess`,
`JobRef` or RunContext. It does not implement OPERATION liveness, leases, application/output
evidence, stop/cleanup, retention, disposal, adapter-owned recovery drain and quiescence, session
adoption or production recovery. Those remain the proof gates below.

## Session containment and #770 reconciliation

This effort owns the unified implementation rather than splitting cgroup launch/stop between two
stacks. Sessions retain their domain lifecycle, tmux/harness readiness and restart consent, using
the shared supervisor. The operator explicitly assigned this implementation to transport on
2026-09-19 and closed [#770](https://github.com/WayfarerLabs/agentworks/pull/770), whose
[closing note](https://github.com/WayfarerLabs/agentworks/pull/770#issuecomment-5744754690) points
here. It is requirements input, not a second implementation assignment. This resolves implementation
ownership; it does not complete requirements reconciliation, weaken containment guarantees or
authorize editing saga-owned records.

The [verbatim source snapshot](inputs/session-cgroups-frd-2c406948.md) preserves #770's full FRD at
`2c406948`, including threat model, acceptance cases, exclusions and rulings, independently of draft
branch retention. The closed PR's final head matches that snapshot. It is review input, not a second
evolving FRD or a new authority source. Carry the accepted text into its designated requirements
home as part of reconciliation. The table below is a routing index and disposition under the current
operator rulings; the historical snapshot itself is not rewritten.

| Source requirement                          | Proposed implementation destination                                     |
| ------------------------------------------- | ----------------------------------------------------------------------- |
| R1, execution identity                      | Shared supervisor/run identity binding                                  |
| R2, complete termination                    | Shared stop, rollback, restart and migration                            |
| R3, independent lifetime authority          | Target-owned session lifetime and runtime-anchor cleanup                |
| R4, resistance to escape and relaunch       | Malicious target-user containment excluded by the later operator ruling |
| R5, usable sessions and explicit boundaries | Session, harness and named-console adoption                             |
| R6, supported environments and migration    | Compatibility pricing and legacy-run transition                         |
| R7, foundation for permission checks        | Trusted VM-side membership lookup and socket test consumer              |

R7's process exit, PID reuse, namespace-relative identifiers, stale runs and unknown membership
cases all remain required. Its acceptance table also includes transferred descriptors: ambiguous
attribution must refuse. This does not promote a future service's per-request authorization,
connection/descriptor-transfer protocol or revocation model into this effort. Account-shell lookup
in the buffered PoC proves none of this run-membership authentication.

Membership lookup is descriptive, not an authorization grant. A stopping run can still contain
processes while cleanup proceeds; lookup may report that verified membership together with the
stopping state, but must not represent the run as accepting new work. A stale reference never
inherits membership from a reused PID or replacement unit. Work induced through another session's
accessible tmux server can legitimately belong to that server's run, so lookup does not establish
isolation between hostile sessions sharing a UID. Future permission consumers must preserve that
distinction; this effort does not add a general permission service or activate recipient
enforcement.

Do not add restricted same-UID or per-run-user isolation to satisfy the historical R4 proposal.
Existing home/workspace semantics remain required. General quotas, a jail product, broad egress
policy and in-process Python-plugin isolation are not implied. The API must accommodate future
profiles without implementing or advertising those mechanisms now. Cgroup membership is a lifecycle
and resource-management boundary, not a process-inspection permission boundary; Linux ptrace
restrictions are separate mechanisms such as
[Yama](https://docs.kernel.org/admin-guide/LSM/Yama.html).

Managing an elevated job is distinct from containing a hostile administrator. MANAGED can supply
ordinary descendant ownership and cleanup for an elevated workload without promising resistance to
that workload's administrative authority. Selecting the profile does not revoke ambient root
authority. Session adoption therefore does not silently turn admin-mode sessions into a containment
guarantee.

VM platform hosts are used by the platform implementation, not restricted guest resources. Their
existing administrative authority is not contained by a cooperative supervisor or file lock. Do not
introduce a host setup service or a new weaker profile merely to simulate Linux guest MANAGED
execution. A target advertising MANAGED must still prove its complete lifecycle guarantees; host
platform workflows need their actual provisioning, readiness, rollback and resource-lifetime
behavior proved instead of inheriting a blanket requirement to support that profile.

### Placement-host resource lifetime

A provisioning command can intentionally create a resource that outlives the command. This does not
give its ordinary descendants an exemption from managed-job cleanup. The
[Lima source audit](prior-art-research.md#lima-provisioning-and-vm-runtime-ownership) identifies the
current remote-create wrapper as such a case: ordinary `limactl start` returns after a background
host agent reports running, whereas `limactl start --foreground` remains the VM-runtime anchor.

The VM platform owns persistent resource lifecycle. For Lima, use its supported create/start/status/
stop/delete behavior and prove readiness, disconnect recovery and rollback for the selected driver.
Evaluate a foreground anchor only where that concrete workflow needs it; it is not a mandatory
transport-managed replacement for Lima's own runtime. Provisioning completion and VM-runtime
completion remain distinct. Neither the SSH carrier nor generic job cleanup understands Lima.

Platform operations participate in core database-level coordination for conflicting VM or shared
host resources. Loss of observation does not release unresolved ownership or prove cleanup, and a
numeric PID file does not authorize killing arbitrary host processes. These are correctness and
recovery requirements, not a hostile-platform boundary. Linux guest sessions retain the full MANAGED
guarantees above. The host workflow must report any cleanup it cannot establish rather than claiming
cgroup-equivalent descendant ownership.

### WSL2 guest-anchor ownership candidate

The controller constructs an inert native WSL client owner and wraps it in a caller-owned guest
anchor lifecycle before dispatch. Construction has no native effects. The native owner owns the
`wsl.exe` process, its process and pipe handles, and any Windows Job Object handle as one local
capability. The caller never receives those handles. Job assignment remains a separate observation
from Job-handle settlement. The selected Windows adapter must create the process with both
`PROC_THREAD_ATTRIBUTE_JOB_LIST` and an explicit `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`. Assignment
therefore occurs before the initial thread runs; post-spawn assignment, suspended-create fallback
and unmanaged downgrade are not supported. A default-deny owner thread retains the Job, process and
pipe capabilities through caller interruption. Python delivers the supported caller control
interruption on the main thread, not by unwinding this raw owner. Each native API primitive either
returns a complete handle value or fails, returned values are retained immediately, and preallocated
process information is harvested in `finally` if process creation returns or raises. Arbitrary
external exception injection into the raw owner thread is not a supported cancellation mechanism.
The portable interface does not make those native facts true merely by naming the obligation.

`start` dispatches one literal, no-shell Python helper and accepts only its nonce-bound `READY`
record with the guest boot UUID, distribution PID 1 start ticks, anchor PID and Linux `/proc` start
ticks. The caller retains the lifecycle object if dispatch, readiness, handoff or local cleanup is
interrupted, so cleanup can be retried without replay. The operation deadline bounds dispatch and
receipt observation. Local settlement instead gets one fresh 0.5-second allowance per explicit
attempt and returns immutable facts for client exit, client-handle closure, Job assignment and
Job-handle closure. Settlement is complete only when the client is known never-created or exited and
both handle sets are known never-created or closed. Missing observations remain unknown and cannot
reuse a pre-dispatch settled snapshot.

`release` first requests cooperative EOF and observes the helper and client under the caller's
deadline, then invokes local settlement. A later call may retry unresolved local cleanup or repeat
the independent exact-identity guest observation after local resources have settled. Neither an
`EXITING` helper record, client exit, closed client handles, successful Job assignment nor closed
Job handle proves the guest anchor is absent. Only the exact guest boot/init/PID/start-time observer
may make that claim.

The portable implementation and tests establish this orchestration shape and execute the helper
protocol on local Linux procfs. The private hold payload now uses version 3 for the distribution
epoch and one-way query-admission marker; version 1 and 2 records are not accepted because no
production hold was released with either shape. Hosted synthetic Windows tests separately exercise
creation-time Job membership, restricted handle inheritance, bounded pipe observation, retryable
exact settlement and controller hard-death cleanup. The earlier live Tier 2 Windows/WSL2 proof drove
the same owner against real `wsl.exe` and established acknowledged guest-anchor absence after
ordinary release and controller hard death while unrelated work survived. It also confirmed literal
argument, binary stream, finite input, EOF, bounded observation and conservative nonzero-status
behavior. The epoch-bound READY/query protocol requires a new live proof; neither round wires the
production platform hold, recovery factory, target identity or RunContext.

## Delivery sequence and proof criteria

The reviewed design is published and the operator has settled implementation ownership. Complete
these bounded proofs before enabling the corresponding behavior:

| Proof                                       | Observable acceptance                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Linux managed launch over SSH and QGA       | Work starts inside the owned boundary under the requested identity/shell; sensitive input stays suppressed, ordinary binary streams remain exact, and foreground wait and independent launch both work on the recorded kernel/systemd versions.                                                                                                                                                                                                                                                                                                     |
| Lifecycle and failure evidence              | Lost acknowledgment reconciles without replay; wait timeout does not stop work; explicit stop, anchor death and OPERATION observer loss clean the owned descendants or report incomplete. Concurrent forks, stale identity, reboot and retained output cannot produce false completion or affect unrelated work.                                                                                                                                                                                                                                    |
| Membership identity                         | Exercise source R7's exit, PID reuse, stale-run and descriptor-attribution cases; ambiguous identity refuses. Do not claim hostile same-user containment or add a general permission service.                                                                                                                                                                                                                                                                                                                                                       |
| macOS placement-host workflows              | Run actual platform provisioning before guest creation; lose observation, reconcile through platform state without replay, and prove supported stop/rollback while unrelated VMs survive. Coordinate conflicting operations through the state database. Report unproved cleanup without claiming generic descendant containment.                                                                                                                                                                                                                    |
| WSL2 lifetime and native readiness/recovery | Measure operation work with the platform hold retained and released. A public independent job additionally requires its own recoverable, job-length availability hold; refuse that lifetime until proved. Required readiness/recovery work runs without staging or requested startup; unsupported terminal/live I/O does not block it. Treat Job Object closure and `wsl.exe` exit as host-client evidence only. Prove an acknowledged guest anchor is absent after ordinary release and controller hard death while unrelated guest work survives. |

Session adoption exercises these shared lifecycle proofs through the actual domain entry points:

- Follow run identity through environment removal, fork/exec, double fork, reparenting and
  additional tmux panes/sessions. Include named-console agent shells and record their lifetime
  owner.
- Exercise stop, restart, delete, failed-launch rollback and cascading agent/workspace cleanup;
  verify that each targets the selected run and preserves unrelated work.
- Interrupt persistence of the launch record and lose the launch acknowledgment. Neither case may
  permit replay or certify an unobserved boundary as empty.
- Distinguish independently verified reboot or VM destruction from suspension or transport loss.
  Only evidence establishing that the old workload cannot remain may discharge its cleanup.
- Keep legacy runs controllable without certifying detached descendants from parent/tmux exit.
  Replacement cannot inherit a completion claim unsupported by the old run's evidence.

These are proposed proof cases for the existing lifecycle and migration design, not adoption of the
entire #770 FRD. Requirements-home reconciliation, supported-environment pricing and the unresolved
containment mechanisms remain open gates in the plan.

After proof, migrate sessions and other jobs, preserving legacy-run uncertainty, consent and
ownership. The plan's complete-workflow and physical-deletion checks are the migration acceptance
criteria, not a checkbox asserting migration happened. A failed proof returns measured costs and
alternatives for disposition before production enablement; it does not weaken a profile.

The [test-bed gaps](prior-art-research.md#lifecycle-test-bed-gaps) are explicit inputs to each live
charter. In particular, macOS workstation SSH results are not placement-host evidence, and buffered
Bookworm/Trixie results are not kernel/systemd compatibility evidence.

This document supplies a reviewable contract and proof plan, not resolved system-call/FD, lease,
sandbox or compatibility protocols. Those bounded details must be completed with evidence before
enabling the relevant profile. The accepted buffered PoC proves none of these new lifecycle
protections.
