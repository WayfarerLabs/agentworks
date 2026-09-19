# Execution Lifecycle and Protection Profiles

- Status: Proposed design, 2026-09-18; no lifecycle or containment implementation claimed.
- Governing direction: [FRD operator rulings](frd.md#operator-rulings-2026-09-18).
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

Proposed core profile names describe guarantees, not a selectable backend:

| Profile                | Added promise                                                                                                                                      | What it does not promise                                                                                    |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `Protection.DIRECT`    | Shared identity, literal arguments, sensitivity and truthful outcome rules; ordinary operational cancellation                                      | Escape-resistant process ownership or isolation from the execution account's ambient authority              |
| `Protection.MANAGED`   | DIRECT guarantees plus an independently identifiable workload boundary, descendant tracking, supervisor-owned stop and terminal-empty verification | Resistance to malicious same-user indirect execution outside the boundary                                   |
| `Protection.CONTAINED` | MANAGED guarantees plus the reviewed restrictions preventing workload escape, outside relaunch and control-plane impersonation                     | General cross-session data secrecy, revoked external credentials, or a sandbox for the Python plugin itself |

MANAGED's Linux candidate is a system-owned transient systemd service/cgroup, not a public choice of
unit properties. CONTAINED is a proposed profile whose complete enforcement must pass the tests
below before it is advertised. Neither profile name is evidence of implementation. Future jail or
sandbox implementations may satisfy a profile only after demonstrating every inherited guarantee.
Two unrelated sandboxes are not ordered merely because an enum has larger numeric values. The
initial core-owned chain needs explicit definitions and tests, not a generic plugin profile DSL.

Core binds allowed operations, exact profiles, identities/elevation, lifetime and I/O restrictions
into the target before giving it to a recipient. A context can grant MANAGED/CONTAINED but not
DIRECT, forcing managed execution without rewriting the request. A grant for one profile does not
implicitly authorize every stronger profile; stronger mechanisms can need additional host authority
or resource access. Derived contexts intersect grants and cannot broaden them. A file-only context
has no execution interface. An observe-only execution interface cannot start or stop work.

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
does not require the recipient to hold public `start` authority. Readiness composition separately
withholds profiles requiring stateful supervisor launch. Such a request is denied before dispatch
even through `run`; withholding `start` alone is insufficient. The approved DIRECT readiness path
still forbids staging and requested shell startup.

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

Carrier account hooks can execute before the trusted supervisor bootstrap. Managed launch, including
CONTAINED, cannot rely on a workload-controlled SSH account shell or startup path to establish its
boundary. The proof must specify a trusted control identity/bootstrap and test that prerequisite
through both carriers; payload shell initialization remains inside the boundary. A payload wrapper
cannot undo prior hooks.

Allocate a non-reused launch identity before dispatch. Trusted target state binds it to host/VM
instance, boot incarnation, workload identity, resolved shell, profile revision and unit ownership.
Use existing session UUID/run IDs when the owner is a session; other jobs do not invent sessions.
Record pending launch before starting so lost acknowledgment can be reconciled without another
launch. Prefer protected unit data where sufficient; justify additional storage rather than adding a
generic registry. Preserve durable terminal evidence before unit collection removes it.

Separate workload exit, output completeness and boundary emptiness. A main process exiting does not
prove its descendants are gone. For sessions the tmux/runtime anchor controls lifetime; arbitrary
surviving children cannot prolong a dead session. Preserve intentional pane retention only within an
explicit bounded diagnostic policy. Stop closes admission, targets the validated owned boundary,
escalates and confirms emptiness before successful disposal or replacement. Stale boot/run/unit/PID
observations cannot target replacement work; partitions and non-terminating kernel tasks remain
incomplete. Resource owners set output/record retention and abandoned-job cleanup.

## Session containment and #770 reconciliation

This effort owns the unified implementation rather than splitting cgroup launch/stop between two
stacks. Sessions retain their domain lifecycle, tmux/harness readiness and restart consent, using
the shared supervisor. The earlier [#770 draft](https://github.com/WayfarerLabs/agentworks/pull/770)
is design input, not a second implementation assignment. Its disposition requires an explicit
owner/operator handoff; this revision neither edits its artifacts nor closes its PR. Do not retire
that record until its complete requirements have a designated home or explicit operator disposition.
The sibling effort's charter remains in force; ownership disposition gates overlapping
implementation, not just artifact retirement. Neither lane can settle the saga assignment by
changing its own plan.

The [verbatim source snapshot](inputs/session-cgroups-frd-2c406948.md) preserves #770's full FRD at
`2c406948`, including threat model, acceptance cases, exclusions and rulings, independently of draft
branch retention. It is review input, not a second evolving FRD or a new authority source. At an
authorized transfer, reconcile the then-current source and carry the accepted text into its
designated requirements home before retiring the old record. The table below is only a routing
index; its labels neither replace nor narrow the source requirements.

| Source requirement                          | Proposed implementation destination                        |
| ------------------------------------------- | ---------------------------------------------------------- |
| R1, execution identity                      | Shared supervisor/run identity binding                     |
| R2, complete termination                    | Shared stop, rollback, restart and migration               |
| R3, independent lifetime authority          | Target-owned session lifetime and runtime-anchor cleanup   |
| R4, resistance to escape and relaunch       | CONTAINED enforcement and adversarial proof                |
| R5, usable sessions and explicit boundaries | Session, harness and named-console adoption                |
| R6, supported environments and migration    | Compatibility pricing and legacy-run transition            |
| R7, foundation for permission checks        | Trusted VM-side membership lookup and socket test consumer |

R7's process exit, PID reuse, namespace-relative identifiers, stale runs and unknown membership
cases all remain required. Its acceptance table also includes transferred descriptors: ambiguous
attribution must refuse. This does not promote a future service's per-request authorization,
connection/descriptor-transfer protocol or revocation model into this effort. Account-shell lookup
in the buffered PoC proves none of this run-membership authentication.

CONTAINED needs a concrete access map and comparison of restricted same-UID execution with per-run
users before selecting either, against the profile guarantees above and the complete R4 source.
Preserve existing home/workspace semantics or obtain operator disposition of demonstrated costs.
General quotas, a jail product, broad egress policy and in-process Python-plugin isolation are not
implied. The API must accommodate future profiles without claiming those mechanisms ship now.

Non-systemd placement hosts, including macOS before VM creation, still need MANAGED independent jobs
for provisioning and rollback. They must supply all four added promises: identifiable workload
ownership, ordinary descendant tracking, supervisor-owned stop and verified terminal emptiness, plus
disconnect survival and retained job evidence. The mechanism is not selected or proven here; a
process-group wrapper is acceptable only if it demonstrably satisfies those same promises. No
Linux-specific containment claim is implied. DIRECT-only host support would drop required work and
needs operator disposition, not a silent implementation shortcut.

## Delivery sequence and proof criteria

First publish the reviewed design and settle the cross-effort ownership disposition before
overlapping lifecycle implementation. Then complete these bounded proofs before enabling the
corresponding behavior:

| Proof                                       | Observable acceptance                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Linux managed launch over SSH and QGA       | Work starts inside the owned boundary under the requested identity/shell; sensitive input stays suppressed, ordinary binary streams remain exact, and foreground wait and independent launch both work on the recorded kernel/systemd versions.                                                                                        |
| Lifecycle and failure evidence              | Lost acknowledgment reconciles without replay; wait timeout does not stop work; explicit stop, anchor death and OPERATION observer loss clean the owned descendants or report incomplete. Concurrent forks, stale identity, reboot and retained output cannot produce false completion or affect unrelated work.                       |
| CONTAINED and membership identity           | Execute the complete source R4/R7 and acceptance cases under an authorized adversarial charter; escape/relaunch is denied or remains in the run, and ambiguous identity refuses. No general permission service is added.                                                                                                               |
| macOS placement-host jobs                   | Launch actual host provisioning work before guest creation; disconnect the observer, re-observe the same job, stop ordinary detached descendants during rollback and independently verify the owned workload empty while unrelated work survives. Retain launch/output/completion evidence and exercise lost contact/stale references. |
| WSL2 lifetime and native readiness/recovery | Measure work with the platform hold retained and released; do not imply a job reference owns power. Required readiness/recovery work runs without staging or requested startup; unsupported terminal/live I/O does not block it.                                                                                                       |

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
