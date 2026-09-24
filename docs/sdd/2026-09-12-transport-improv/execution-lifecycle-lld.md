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
does not extend a WSL2 platform hold or keep a macOS placement host awake by itself.

PTY is an I/O choice, not a background state. `attach(ref, terminal=...)` requires an attach grant
and supported transport, and does not create a new run. A reusable detached terminal needs an owned
terminal endpoint such as the session's tmux server; ordinary inherited pipes are not durable.
Ctrl-C, terminal hangup, detach, local wait interruption and explicit stop need distinct tested
policies. None is silently promoted into whole-workload cancellation. Terminal output is combined;
non-terminal capture preserves separate guest streams and sensitive-output rules.

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
from positive boundary emptiness. `dispose`, crash recovery and live destination behavior remain
open. Hermetic start/stop proof does not establish live systemd/cgroup behavior, current target
marker or boot rereads, or SSH/QGA production delivery.

The stream fact is self-describing. Managed-run reservation now persists the requested output policy
atomically alongside run identity. A future consuming service must compare that policy to each
stream end before treating its disposition as fulfillment. `_managed_job_wire.py` now owns the
complete canonical byte schema as a Python 3.11-compatible stdlib module that reuses the portable
`_helper_identity.py` validator. The host typed adapter delegates encoding, decoding and launch
digest to it. Exact-source bundle tests prove byte round trips under Python 3.11 when installed. The
target controller and private observation helper bundle those sources verbatim. The protected store
uses the same portable codec. Host reservation/output-policy reduction, target controller
production, cgroup/systemd launch, carrier proof and live validation remain open.

### First private managed service

The first production-shaped service slice is Linux-only, `MANAGED` and `INDEPENDENT`. It does not
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
and waits for `cgroup.events` to report `populated 0` before publishing `boundary-empty`. An error
while requesting `cgroup.kill` does not invalidate a later positive empty-boundary observation. A
task stuck in uninterruptible sleep can prevent that proof indefinitely; the controller stops
waiting at its bound and leaves boundary state unknown rather than fabricating emptiness. Cleanup or
controller failure may likewise leave a stream-end fact unknown even when other terminal facts are
present.

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
and closed `read-output` over the existing carrier interface. A private `start` exchange also
preflights a reserved independent run and its persisted output policy, then uses the existing
durable possible-dispatch wrapper. Private `stop` revalidates the exact launch, publishes the empty
request create-once and waits within a fixed guest-local bound for the existing boundary fact. Its
controller path closes finite input, gives only the main child that has not yet been reaped one
fixed `SIGTERM` grace, then uses the existing whole-cgroup cleanup without extending grace on retry.
`dispose` remains unimplemented, as do production host reservation/policy reduction and live target
marker/boot rereads. SSH and QGA must each prove the protocol in production before public exposure.

Target identity is structured as a core resource kind/name, a versioned incarnation fingerprint and
a separate boot UUID. The core name supports binding and diagnostics but is not authority. The
incarnation fingerprint must bind the provider-owned locator plus a core-provisioned or explicitly
adopted random instance marker. Existing VMs require an explicit adoption workflow; an ordinary
operation does not silently write that marker. `/etc/machine-id` does not replace the marker because
it does not reliably distinguish clones. Production target composition remains blocked until it can
construct and verify these facts.

The private implementation checkpoint now supplies the version-one VM fingerprint codec, a bounded
fixed-path Linux guest probe and a pure composer. The probe makes one no-replay carrier attempt,
admits the pinned runtime, validates the protected root-owned marker path, reads the canonical boot
UUID and keeps carrier facts separate from the nonce-bound observation. The codec length-prefixes
the exact provider locator bytes; the composer verifies the persisted marker against the guest
observation and keeps the boot UUID as a separate target fence. Provider-locator unavailability,
legacy NULL markers, unsafe guest paths and mismatches refuse without mutation. This does not claim
production/platform wiring, explicit adoption, a locator-unavailable alternative or a public
RunContext target; the combined target-identity gate remains open.

Private target preparation now consumes those pieces under one caller-owned exact-VM operation and
finite deadline. It validates that the current coarse claim covers the VM, refuses static missing or
malformed evidence before borrowing, makes one serialized helper attempt, records the guest result
before settlement and retains the operation after uncertain dispatch or control flow. Normal helper
completion may settle that attempt while preparation still rejects an invalid observation, runtime
refusal, carrier failure or identity mismatch. The helper neither acquires nor closes the outer
owner and performs no activation, route selection, platform lookup, adoption or persistence.
Production composition therefore still needs an owner acquired before activation plus typed
whole-span lifecycle and cleanup evidence; legacy lifecycle returns cannot close that gate. Future
hierarchical admission replaces exact-VM equality with a core-owned coverage decision rather than a
target-local ancestry guess.

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

### Durable lifecycle-obligation ledger

Production ownership needs a durable handoff between the coarse resource claim and the independent
effect owners inside one workflow. A bounded `lifecycle_obligations` ledger supplies that handoff.
It is generic coordination state, not a serialized workflow. Each row belongs to one exact fenced
operation identity and has a fresh obligation identifier, a registered lower-kebab kind, a positive
payload version, a bounded opaque non-secret payload, timestamps and one closed state:

- `registered`: the obligation exists and no effect is yet admitted;
- `possible-effect`: admission committed before the remote or local effect;
- `resolved`: typed adapter evidence proves this obligation can cause no further effects.

Core validates the exact operation fence for every transition but never decodes adapter payloads.
The registered adapter owns payload validation, target/incarnation comparison, observation and the
typed evidence accepted for resolution. Bound both the encoded payload size and number of
obligations per operation. Payloads may contain target identity, protected namespaces and cleanup
receipts, but never credentials, application input/output or arbitrary workflow state. Keep managed
run records specialized until a real consumer proves that folding their typed schema into opaque
obligations would simplify rather than weaken it.

An adapter may replace its opaque payload with exact recovery identity while the obligation remains
`possible-effect`. That update is not a generic lifecycle state or a sealing prerequisite. A typed
no-effect result can resolve an obligation that never created a runtime identity.

Registration refuses after the ledger is sealed. The first `possible-effect` transition also arms
the coarse claim in the same short transaction; each later dispatch revalidates the current owner
generation against its already-possible obligation before permission returns. Later obligations
advance independently. Once the workflow cannot create more effects, core seals the ledger.
Whole-operation resolution requires a sealed ledger, every registered obligation in `resolved`, and
no active borrow, outstanding attempt or retained in-memory cleanup custody. Final release deletes
the resolved obligations and releases the exact claim atomically. No automatic expiry, generic retry
runner, dependency graph or force-release belongs in this layer.

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
quiescence. For every obligation already in `possible-effect`, the adapter must then prove both that
no admitted dispatch can still arrive and that any existing effect can cause no more work.
Acceptable evidence includes carrier-proved non-dispatch paired with exact absence, an
operation-specific remote generation fence, or equally strong proof from a synchronous local
substrate whose controller and dispatch endpoint are both gone. Controller absence by itself is
insufficient because provider, carrier or guest queues may outlive it. This recovery fence is
separate from #377's future resource hierarchy. The ledger remains attached to the logical operation
across that transition so later hierarchy can bind one operation to several resource memberships
without moving adapter state onto one VM row.

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

For a WSL2 platform hold, the adapter payload binds the opaque provider locator, VM instance marker,
exact distribution, execution user and exact Windows controller process identity. After `READY`, it
adds the guest boot UUID, PID and Linux process start time; PID/start-time evidence is meaningful
only within that boot. Host Job settlement and `wsl.exe` exit remain host-client evidence only.
Recovery must prove the recorded controller process is absent before using the creation-time Job and
synchronous Windows process-launch facts to establish that no delayed client launch remains, then
independently observe that the acknowledged guest identity is absent. A changed guest boot proves
the old guest process cannot survive but does not excuse locator or marker mismatch. Each
`vm_active()` lifetime registers its own obligation and anchor under the enclosing operation. Do not
add hidden reference counting or collapse nested holds into one platform process. A recovery adapter
retains the obligation when dispatch drain, identity, quiescence or absence cannot be established
safely.

This checkpoint does not wire `systemd.py`, carriers, platform factories, public `ExecutionAccess`,
`JobRef` or RunContext. It does not implement OPERATION liveness, leases, application/output
evidence, stop/cleanup, retention, disposal, the recovery takeover fence, session adoption or
production recovery. Those remain the proof gates below.

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
record with the guest PID and Linux process start time. The caller retains the lifecycle object if
dispatch, readiness, handoff or local cleanup is interrupted, so cleanup can be retried without
replay. The operation deadline bounds dispatch and receipt observation. Local settlement instead
gets one fresh 0.5-second allowance per explicit attempt and returns immutable facts for client
exit, client-handle closure, Job assignment and Job-handle closure. Settlement is complete only when
the client is known never-created or exited and both handle sets are known never-created or closed.
Missing observations remain unknown and cannot reuse a pre-dispatch settled snapshot.

`release` first requests cooperative EOF and observes the helper and client under the caller's
deadline, then invokes local settlement. A later call may retry unresolved local cleanup or repeat
the independent exact-identity guest observation after local resources have settled. Neither an
`EXITING` helper record, client exit, closed client handles, successful Job assignment nor closed
Job handle proves the guest anchor is absent. Only the exact guest PID/start-time observer may make
that claim.

The portable implementation and tests establish this orchestration shape and execute the helper
protocol on local Linux procfs. Hosted synthetic Windows tests separately exercise creation-time Job
membership, restricted handle inheritance, bounded pipe observation, retryable exact settlement and
controller hard-death cleanup. The subsequent live Tier 2 Windows/WSL2 proof drives the same owner
against real `wsl.exe` and establishes exact acknowledged guest-anchor absence after ordinary
release and controller hard death while unrelated work survives. It also confirms literal argument,
binary stream, finite input, EOF, bounded observation and conservative nonzero-status behavior. That
proof does not wire the production platform hold, recovery factory, target identity or RunContext.

## Delivery sequence and proof criteria

The reviewed design is published and the operator has settled implementation ownership. Complete
these bounded proofs before enabling the corresponding behavior:

| Proof                                       | Observable acceptance                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Linux managed launch over SSH and QGA       | Work starts inside the owned boundary under the requested identity/shell; sensitive input stays suppressed, ordinary binary streams remain exact, and foreground wait and independent launch both work on the recorded kernel/systemd versions.                                                                                                                                                                                               |
| Lifecycle and failure evidence              | Lost acknowledgment reconciles without replay; wait timeout does not stop work; explicit stop, anchor death and OPERATION observer loss clean the owned descendants or report incomplete. Concurrent forks, stale identity, reboot and retained output cannot produce false completion or affect unrelated work.                                                                                                                              |
| Membership identity                         | Exercise source R7's exit, PID reuse, stale-run and descriptor-attribution cases; ambiguous identity refuses. Do not claim hostile same-user containment or add a general permission service.                                                                                                                                                                                                                                                 |
| macOS placement-host workflows              | Run actual platform provisioning before guest creation; lose observation, reconcile through platform state without replay, and prove supported stop/rollback while unrelated VMs survive. Coordinate conflicting operations through the state database. Report unproved cleanup without claiming generic descendant containment.                                                                                                              |
| WSL2 lifetime and native readiness/recovery | Measure work with the platform hold retained and released; do not imply a job reference owns power. Required readiness/recovery work runs without staging or requested startup; unsupported terminal/live I/O does not block it. Treat Job Object closure and `wsl.exe` exit as host-client evidence only. Prove an acknowledged guest anchor is absent after ordinary release and controller hard death while unrelated guest work survives. |

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
