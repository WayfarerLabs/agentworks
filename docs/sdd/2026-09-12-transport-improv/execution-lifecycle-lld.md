# Execution Lifecycle and Protection Profiles

- Status: Proposed design, 2026-09-18; no lifecycle or containment implementation claimed.
- Governing direction: [FRD operator rulings](frd.md#operator-rulings-2026-09-18).
- Public API: [execution contract](execution-contract.md); delivery: [plan](plan.md).

## One API, independent decisions

An authorized target exposes `execution()` and `files()`. `ExecutionAccess` owns both synchronous
execution and job operations. `run` accepts `Command` or `Script` and returns an `ExecutionResult`;
`start` accepts the same invocation forms and returns a `JobRef`. Job means independently
addressable execution, not a duration threshold. Both entry points use the same preparation,
authorization and lifecycle implementation. There is no parallel direct/cgroup/sandbox API family.

| Dimension   | Explicit choice                                                                     | Independent of                           |
| ----------- | ----------------------------------------------------------------------------------- | ---------------------------------------- |
| Invocation  | Literal `Command` or `Script` with typed shell selection                            | Transport and protection profile         |
| Observation | `run` waits; `start` returns after acknowledged launch; `wait` observes a reference | Whether execution has a managed boundary |
| I/O         | EOF/finite/live input; bounded capture, discard, streaming or PTY                   | Waiting and shell startup                |
| Lifetime    | `Lifetime.OPERATION` or `Lifetime.INDEPENDENT`                                      | Whether this particular call waits       |
| Protection  | Named core-owned profile with additive guarantees                                   | Workload identity, shell and route       |
| Identity    | Bound target user and separately granted elevation                                  | Privilege of the internal supervisor     |

Every script selects `Shell.SH`, `Shell.BASH` or `Shell.USER_DEFAULT`. Separate `ShellStartup`
options express login and interactive initialization; a PTY does not imply either. User-default
lookup uses the actual destination execution identity after authorized elevation. Fixed constants
mean fixed interpreter semantics, not an arbitrary executable path or the workstation shell. An
arbitrary interpreter facility, if needed, requires its own reviewed shape; strings do not bypass
the fixed set. Historical buffered PoC methods remain as measured until the implementation migrates.

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
that record until every requirement below has a retained home or an explicit operator disposition.

The following mapping preserves the concerns in #770 at `2c406948`; it does not silently adopt every
proposed mechanism or claim its open compatibility/security decisions are settled:

| #770 requirement               | Home in this effort and acceptance obligation                                                                                                                                               |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1, execution identity         | Shared supervisor/run binding; fork/exec, reparenting, detached children and added panes preserve association; no workload-controlled identity proof.                                       |
| R2, complete termination       | Shared stop/rollback/restart and migration; concurrent fork/TERM resistance cannot yield a false empty claim; unrelated runs survive.                                                       |
| R3, independent lifetime       | INDEPENDENT managed sessions; CLI/SSH loss preserves work, runtime-anchor death triggers target-side descendant cleanup.                                                                    |
| R4, escape/relaunch resistance | CONTAINED proof: sibling tmux, user service managers, cron/at, SSH login, process injection, container daemons, writable startup state and inherited handles cannot launch outside the run. |
| R5, usable sessions            | Session adoption preserves harnesses, grants, inspection and contained named-console agent shells. No new admin-session security claim or standalone companion-shell command.               |
| R6, compatibility/migration    | Price Bookworm versus Trixie and actual kernel/systemd/WSL2 support; no silent downgrade or certification of untracked legacy descendants.                                                  |
| R7, identity lookup            | Trusted VM-side Unix-socket test consumer authenticates live run membership with PID reuse, namespace, transferred-FD and revocation cases; no general permission service.                  |

CONTAINED needs a concrete access map and comparison of restricted same-UID execution with per-run
users before selecting either. Root-owned cgroups alone do not close indirect execution channels.
Preserve existing home/workspace semantics or obtain operator disposition of demonstrated costs.
General quotas, a jail product, broad egress policy and in-process Python-plugin isolation are not
implied. The API must accommodate future profiles without claiming those mechanisms ship now.

Non-systemd platform hosts, especially pre-VM macOS work, require a separately proved implementation
of the applicable job guarantees. Do not label a process-group wrapper equivalent to a managed
cgroup or promise Linux containment there. The implementation gate must resolve required host jobs
and safe early bootstrap without weakening the approved profile definitions.

## Proof and delivery gates

1. Review the profile guarantees, operation grants and #770 mapping; settle ownership disposition
   before retiring any artifacts. Publish this design before broad lifecycle implementation.
2. Prove system-owned launch through SSH and native QGA: finite sensitive input, binary output,
   explicit identities/shells, foreground wait and independent background launch. Measure supported
   Debian/systemd/kernel versions; capability detection is not a distro-name assumption.
3. Prove local wait timeout versus explicit stop, lost acknowledgment reconciliation, runtime-anchor
   death, concurrent forks, stale/reused identity, reboot, output retention and independently
   checked cleanup. Settle OPERATION observer-loss detection before offering its stronger cleanup
   promise.
4. Prove CONTAINED escape refusal and R7 socket identity using an authorized adversarial test
   charter, without exposing admin execution. Missing protection fails closed and blocks that
   profile's use.
5. Prove macOS host jobs and WSL2 power-lifetime behavior, plus no-staging readiness and recovery.
   Required workflows cannot depend on optional terminal/live streaming support.
6. Migrate sessions and other jobs to the same implementation, preserving legacy-run uncertainty,
   consent and exact ownership. Then complete the existing full-stack cutover/deletion gates.

This document supplies a reviewable contract and proof plan, not resolved system-call/FD, lease,
sandbox or compatibility protocols. Those bounded details must be completed with evidence before
enabling the relevant profile. The accepted buffered PoC proves none of these new lifecycle
protections.
