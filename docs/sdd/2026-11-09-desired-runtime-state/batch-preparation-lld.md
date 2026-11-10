# Batch preparation and reconciliation execution

This implements FRD R3 through R7 within the [architecture](hla.md). The existing lifecycle managers
remain the domain authorities; the coordinator composes their concrete helpers.

## Baseline seams

At main `438c4546c`, batch session launch prepares a shared VM boundary and then calls the entire
singular launch for each session (`sessions/manager/_lifecycle.py:1333`, `:1379`). That singular
launch makes a new resolver and separately resolves its environment (`:613`, `:854`, `:879`). The
common VM boundary resolves before activation and yields no cache (`_scope.py:417`, `:458`).

Session preflight requires the individual session's scope and transport. Harness readiness returns
early for non-SESSION scope (`capabilities/harness_integration/base.py:519`), while shell preflight
also skips command rendering outside SESSION scope (`shell.py:139`). `RunContext` carries one admin
and one agent target, not a VM/user target map (`capabilities/base.py:196`).

Two existing preparation-looking operations are not read-only: `_ensure_pid` persists evidence, and
console start removes residual staging before pane-secret resolution
(`multi_console/attach.py:701`). Neither belongs in dry run or the pre-secret eligibility sweep
unchanged.

## Concrete prepared operations

Extract helpers with the following responsibilities. Exact private file splits may follow the
smallest focused neighboring modules; preserve public manager entry points and existing test seams.

| Helper                          | Retained data and responsibility                                                                                                                                                 |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| VM preparation                  | Selected VM UUID/target, bound live VM/site nodes, declaration names, guarded provider context, current power observation, held-active context                                   |
| Session launch preparation      | Selected session UUID/target, row/workspace/agent/template snapshots, per-session graph, SESSION scope, correct transports, observed status, launch policy, effective env target |
| Session stop preparation        | Selected identity/target, exact owning transport/socket, read-only status and recovery refusal; no env targets                                                                   |
| Console realization preparation | Console/target selected under the family guard, definition/member snapshot, VM target, exact canonical/staging observation, pane targets only when a build is needed             |
| Console stop preparation        | Guarded console/target, exact managed names and transport, observed presence; no pane targets                                                                                    |

Prepared records contain references and observations, not resolved secret values. They are internal
invocation data and are never serialized. They expose no generic `start`/`stop` protocol. Executors
are named domain functions that accept the appropriate prepared record and supplied value mapping.

Every prepared session keeps its own SESSION scope, targets, bound harness, and graph. Run that
session's readiness sweep and native-setup readiness using those targets. Do not preflight a
combined multi-session graph with a SYSTEM context or walk other sessions under one session's
identity. If common ancestor nodes are memoized, reuse one object per graph key; otherwise retain
separate per-operation walks and share only declarations and resolved values across compositions.

Ancestors' cheap checks may repeat across these walks. Actual secret resolution must not. There is
no need to change capability signatures or create a generic context-routing layer for this.

## Selection, intent, and policy

VM scope expands to stored sessions and consoles belonging to that VM. Fleet scope expands selected
VMs once, then their children. Console membership does not expand session selection: sessions are
already selected by VM scope, and focused console reconciliation never selects member runtimes.
Deduplicate by kind/name. Retain VM/session UUIDs across guard acquisition. A preliminary focused
console lookup only locates its family guard; select its row, target, and members after acquisition
and refuse a changed parent. VM/fleet scopes enumerate consoles under their acquired guards.

Public start/restart/stop performs acceptance before entering runtime execution. The
start/restart/stop batch controller retains every accepted match for intent and results, including
healthy running start no-ops and absent stop no-ops. It selects runtime actions separately. Restart
confirmation retains the existing exact selected session UUIDs and authorized-running names; a newly
running unconfirmed target is not replaced.

Batch stop uses non-activating parent observation and prepared stop execution. It records stopped
for accepted matches, preserves existing filters and force boundaries, and retains accepted intent
on later failure. An off parent satisfies guest absence without a hold or activation.

Reconciliation never records a target or requests a fresh conversation. Its session launch policy is
ordinary best-effort continuation with no implicit force. Existing explicit batch options keep their
meanings and refuse known conflicts before collecting that target's runtime-only secrets.

### Desired-running sessions

| Observation                         | Reconciliation action                                                                                                                  |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Healthy running managed runtime     | No-op; preserve runtime identity and start observations                                                                                |
| Stopped                             | Prepare ordinary start                                                                                                                 |
| Reachable residual dedicated server | Prepare existing safe residual cleanup and start                                                                                       |
| Broken runtime requiring force      | Block with existing recovery guidance                                                                                                  |
| Unknown observation                 | Block; never assume absence                                                                                                            |
| Proven interrupted managed startup  | Use existing non-replacing startup recovery after preparation, or block if its ownership cannot be proved                              |
| Legacy shared-server row            | Use existing exact legacy observation; preserve a running runtime, and use domain-safe migration only when ordinary start is permitted |

Existing batch start omits residual state; replace that filtering with domain preparation rather
than passing a desired-running set to the unchanged batch service. Do not call PID repair merely to
decide eligibility. Read-only ownership/fingerprint observations may classify an interrupted managed
start; evidence repair and startup finalization belong after the secret boundary.

### Desired-running consoles

| Canonical                     | Staging | Action and secret requirements                                     |
| ----------------------------- | ------- | ------------------------------------------------------------------ |
| Present                       | Absent  | No-op, no pane secrets                                             |
| Present                       | Present | Clean staging only, preserve canonical view, no build-only secrets |
| Absent                        | Present | Clean staging and build, collect required pane targets             |
| Absent                        | Absent  | Build, collect required pane targets                               |
| Unknown in either observation | Any     | Block without teardown or secret collection                        |

Use `_console_build_secret_targets` for new shell panes only. Attach panes reference an existing
session and add no workload secret requirement. Preserve admin-mode shell promotion and the
workspace/user context of sidecar shells; session-template env does not apply to sidecars.

The baseline admin-only shell collector gathers VM/admin env while the builder launches that window
without delivering it. Align this existing intended scope during extraction: compose and deliver the
collected VM/admin env through the same literal env mechanism used by console shell panes. This
bounded repair prevents requesting values that the new execution path would ignore; it introduces no
new scope or transport contract. Verify delivery and redaction behavior.

Desired-stopped sessions/consoles prepare their existing safe teardown when present. They add no
environment targets. An off VM needs no activation to establish that its guest runtimes cannot be
running; report the provider observation and lack of guest inspection instead of performing repair.

## Infrastructure preparation

Build and preflight VM/site observation contexts for every affected family before requesting
credentials that it cannot use. Use one site instance where several prepared VM graphs share it.
Binding/observation is distinct from activation: every selected running-intent VM is activated as
needed, including those whose children are absent, stopped, or awaiting adoption. Permitted
on-demand parents are activated only for selected running workload candidates needing checks or
execution. Stopped parents block those candidates without contributing their workload targets.

A parent needed only for child-stop work still receives non-activating observation. If already off,
its children need no guest stop. If running, prepare direct canonical guest teardown without an
activation gate. Do not open a keepalive whose acquisition itself starts a VM, as WSL's hold can;
stop-only work can use the already-live guest without that hold. If it goes off before teardown,
re-observe provider power and report the stopped target satisfied only when absence is proved.
Unknown observation still blocks. A hold justified by another selected running target may be reused.

Use one invocation-owned `Resolver` for infrastructure and workload preparation. Existing
`resolve_gate` already projects a complete singleton result and stores success in its seed mapping.
Extend it to memoize value-free failure metadata, never traceback-bearing exceptions or partial
batch values. UserAbort propagates without memoization. Resolve a new gate declaration before
registering it; register/seed only a successful result. Repeated requests reuse success or failure.

Only node-declared gate and repair names may reach this callback; retain the gate reader's repair
allowlist. Front-load known required requests, then resolve conditional repair names lazily when
needed. A shared repair name is attempted once; healthy paths never request it. Do not register all
infrastructure graph declarations on the Resolver before their gate attempts: a failed independent
VM must not leave an unused declaration that poisons the workload boundary.

Singleton gate projections preserve complete-resolution semantics and independent credential failure
isolation. Distinct names may repeat source setup; shared names do not repeat resolution. The later
workload union still gets one complete source-first batch. No backend API changes.

Prepare provider contexts with scoped values, observe power, activate required VMs, and perform
conditional reconnection/rejoin before workload resolution. Reuse the ordinary explicit-start
connection path for a provider-running VM whose guest connection needs repair. Retain active holds
through final observation, including WSL's keepalive requirements. Record completed infrastructure
changes even if later workload preparation fails.

No VM stop executes in this phase. Failed infrastructure targets are excluded from workload
preparation while independent families continue. Their memoized failures remain available to the
same Resolver if an eligible workload later requires the same name.

## The shared workload boundary

After every eligible target's pre-secret checks:

1. Use the same Resolver that performed infrastructure gate preparation; do not resolve its full
   boundary during infrastructure preparation.
2. Register eligible graphs' secret declarations and effective session/pane `SecretTarget`s,
   including transient declarations where current env consumers permit them.
3. Inside `Resolver.resolve()`, check required declarations against its gate-failure memo before any
   source call. A required cached failure fails the complete boundary without retrying it or
   prompting for other names in a known-doomed batch. Unrelated failed names remain excluded.
4. Reuse existing seeded successes and resolve all remaining declarations once. Validate every
   target's use of the values, retaining winning-scope merging and `${agw:...}` context rules.
5. Supply graph values through `ScopedSecrets` and only the relevant composed env to each session or
   pane. Never deliver the whole credential set as process environment.

The overlap check belongs inside Resolver; no coordinator cache, `registered_names` query, or seed
transfer is needed. A failed complete boundary terminates the invocation and disposes the Resolver;
no executor/final observer receives a resolution callback, and no catch-and-continue path reopens
it. Successful resolution preserves its existing refusal of later gate calls or unresolved
additions.

A required workload secret failure prevents every deferred action, including stops and staging
cleanup on other selected targets. It does not roll back accepted intent or completed VM activation.
Render those completed effects, mark deferred work blocked or failed honestly, and return nonzero.
Do not preserve values from an incomplete complete-resolution batch or fall back to resolving each
failed workload separately.

Existing batch start/restart use this arrangement and session executors rather than calling the
whole singular launcher in a loop. Batch stop uses the same stop preparation without activation or
workload env targets. Retire `_batch_vm_boundary_impl`, `_batch_vm_boundary`, the unused
`_best_effort_batch_vm_boundary`, their exports, and the sole `Resolver.resolve_late_repair` path
after these callers move. Singular services use the same concrete helpers. Extract focused modules
rather than extending the already-large lifecycle file.

A stopped parent or failed infrastructure credential blocks its own batch family while independent
families continue. This changes the old batch boundary's whole-batch refusal and must be documented;
shared required workload-secret failure still prevents every deferred runtime action.

## Execution and final observations

Execution receives cached values; it has no resolution callback. Before each consequential action,
reload VM/session UUIDs or the guarded console definition, verify parent/target, and re-observe the
relevant runtime under the held family guard. If the world has changed, skip an already-satisfied
action or report failure/blockage; do not change intent, adopt a replacement, or open another secret
prompt.

Session execution retains existing ordering: resolve env from supplied values, prepare artifacts and
harness launch selection, validate application, stage prospective files, tear down only the
authorized old runtime, commit artifacts, create tmux, persist the complete namespaced harness state
and runtime fingerprint, and finalize startup. Current per-target checks before teardown continue to
protect healthy runtimes even if a post-secret compilation step fails.

All artifact staging and evidence repair occur after shared secret success. Preserve old artifact
files until authorized teardown. Broken recovery never signals a numeric PID or bypasses the
existing fingerprint proof. Continuation remains owned by the integration; core promises no
conversation restoration beyond that contract.

Console execution first performs its prepared exact staging cleanup or teardown, then uses the
existing staged `_build_console_tmux` with supplied values. A canonical view remains unchanged for a
cleanup-only action. Build failure cannot publish a partial canonical view. Console membership and
layout come from the retained, guarded definition. A failed member workload does not itself
invalidate a console's waiting view.

After session actions, console actions, and deferred provider stops, use the prepared provider
contexts and existing direct session/console observers for bounded final checks. These checks cannot
resolve secrets or activate VMs. Use the existing finite probe limits; an indeterminate result is
not successful convergence. A target observed absent again after its start is failed. Do not
introduce a sustained monitoring period or retry loop.

## Results and cancellation

Use a small immutable per-resource result record: kind/name, VM/session UUID internally where
applicable, desired target, available domain observation, proposed/performed action, outcome, and a
value-free reason. The common outcome vocabulary is unchanged/changed/failed/blocked, not a common
runtime status enum. Preserve domain status meanings and avoid placing command strings or values in
records.

Include successful prerequisite VM activation when a focused workload command caused it. Selected
pending-adoption resources and stopped-parent running children are blocked. Parent power-off can
satisfy stopped children without per-child guest teardown; running children stay blocked with their
targets retained. A failed prerequisite blocks only operations that need it, except for the explicit
complete workload-secret barrier.

Raise or render aggregate failure only after reporting independent results. Cancellation propagates
as UserAbort after reporting already completed effects, releases every hold/guard, clears
invocation-owned mappings, and prevents later actions. Never downgrade cancellation to a warning
inside infrastructure, per-item execution, best-effort cleanup, or result rendering.

## Dry run

Use the same local selection, target interpretation, and domain action classification, but no
activation/execution helpers or native mutation guards. Dry run must not create guard directories or
files. Direct status observers and prepared provider status contexts are allowed; workload source
preview/resolution, PID repair, artifact staging, and tmux cleanup are not. An off prerequisite
makes future guest eligibility conditional. A failed attempted observation is unknown and blocks
action; an intentionally unattempted off-guest probe is not proof that a replacement is safe.
Execution rebuilds and revalidates its plan rather than consuming preview text.

## Required behavioral cases

- A running-intent VM with zero children and one whose children are all stopped both activate in
  infrastructure preparation, before an unrelated workload's missing secret aborts deferred work.
- One site's unavailable credential leaves another site's infrastructure preparation usable; shared
  successful and failed credential names are each attempted once per invocation.
- A failed infrastructure name also required by an eligible workload fails the shared workload
  barrier without a second source call. The same failure does not block an unrelated workload union.
  A stop-only parent receives observation without activation or an implicitly booting hold.
- Two repair paths share one conditional key; a healthy VM never requests it.
- Mixed session launches and console builds share one workload union; an invalid value for any
  consumer prevents all tmux changes and every deferred provider stop.
- No-op, refused, stopped-parent, pending-adoption, and cleanup-only targets contribute no unused
  environment secrets. No executor or final observer calls a secret source.
- Residual and interrupted startup recovery retain domain ownership checks; unknown and broken
  observations never authorize implicit force or replacement.
- Mid-operation cancellation leaves accepted targets and completed infrastructure facts truthful,
  releases guards, and allows a fresh invocation to retry without duplicating healthy runtimes.
