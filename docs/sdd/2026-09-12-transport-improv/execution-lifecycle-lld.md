# Execution Lifecycle and Protection Profiles

- Status: Proposed design, 2026-09-18; no lifecycle or containment implementation claimed.
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

Non-systemd placement hosts, including macOS before VM creation, still need MANAGED independent jobs
for provisioning and rollback. They must supply all four added promises: identifiable workload
ownership, ordinary descendant tracking, supervisor-owned stop and verified terminal emptiness, plus
disconnect survival and retained job evidence. The mechanism is not selected or proven here; a
process-group wrapper is acceptable only if it demonstrably satisfies those same promises. No
Linux-specific containment claim is implied. DIRECT-only host support would drop required work and
needs operator disposition, not a silent implementation shortcut.

## Delivery sequence and proof criteria

The reviewed design is published and the operator has settled implementation ownership. Complete
these bounded proofs before enabling the corresponding behavior:

| Proof                                       | Observable acceptance                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Linux managed launch over SSH and QGA       | Work starts inside the owned boundary under the requested identity/shell; sensitive input stays suppressed, ordinary binary streams remain exact, and foreground wait and independent launch both work on the recorded kernel/systemd versions.                                                                                        |
| Lifecycle and failure evidence              | Lost acknowledgment reconciles without replay; wait timeout does not stop work; explicit stop, anchor death and OPERATION observer loss clean the owned descendants or report incomplete. Concurrent forks, stale identity, reboot and retained output cannot produce false completion or affect unrelated work.                       |
| Membership identity                         | Exercise source R7's exit, PID reuse, stale-run and descriptor-attribution cases; ambiguous identity refuses. Do not claim hostile same-user containment or add a general permission service.                                                                                                                                          |
| macOS placement-host jobs                   | Launch actual host provisioning work before guest creation; disconnect the observer, re-observe the same job, stop ordinary detached descendants during rollback and independently verify the owned workload empty while unrelated work survives. Retain launch/output/completion evidence and exercise lost contact/stale references. |
| WSL2 lifetime and native readiness/recovery | Measure work with the platform hold retained and released; do not imply a job reference owns power. Required readiness/recovery work runs without staging or requested startup; unsupported terminal/live I/O does not block it.                                                                                                       |

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
