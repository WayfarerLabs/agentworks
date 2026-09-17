# Transport Boundary Proof

Status: Implementation in progress; not acceptance of the joint proof.

## Delivery and ownership

The operator authorized transport-side PoC implementation after #795 merged. This is the
implementation vehicle for that work, not a design prerequisite for SSH to start. The SSH owner is
updating #796 and supplies the independent SSH contribution. Transport owns integration of the
joint proof, shared types, preparation, outcome interpretation and acceptance cases.

The merged [execution contract](execution-contract.md) remains the governing design. The first
implementation is deliberately the buffered subset. `carrier.py` is the executable candidate;
neither its existence nor green local tests closes the [proof matrix](plan.md). Proof findings
amend the candidate before broad parallel implementation.

Production factories, RunContext, consumers, connection/trust state and the old stack are unchanged.
There is no public old/new selector. File operations, jobs, live streaming, terminal attachment and
the complete permission-scoped public API are not part of this slice.

## Candidate carrier boundary

`PreparedInvocation` owns literal bootstrap argv and a safe diagnostic label, never stdin.
`CarrierIO` owns exactly one input, EOF or immutable finite bytes, and capture or discard output.
Its sensitivity is the union of request and input sensitivity; an input marker cannot be cleared.
Finite bytes need no borrowed stream or close operation. The later live-source/sink shape remains
unimplemented until bounded cancellation, short-write and borrowed-stream tests establish it.

`Deadline` carries one monotonic expiry, or explicit unboundedness. Carriers do not renew the budget
while polling. `CarrierReport` separates dispatch evidence, prepared-invocation completion, local
process status, raw output provenance/completeness/retention and a closed failure code. Payloads and
captured bytes have no diagnostic representation; provider exception text is not a diagnostic.
Interruption propagates after local cleanup, never as successful execution or confirmed cancellation.

An observed exit belongs to the prepared invocation. It does not independently establish a nested
application's outcome. In particular, an SSH local status of 255 remains ambiguous. Output parsing
does not introduce an SSH completion guarantee.

## Shared no-staging preparation experiment

The initial test lane is Linux. It investigates an explicit Bash bootstrap with base64 encoding,
using pipes and `/dev/fd` rather than temporary files or an installed guest helper. It does not
assume Python is available during native recovery or readiness. Bash, base64 and descriptor support
are prerequisites to measure, not claims about every supported target.

Application argv, script source, environment, directory and finite stdin travel in an ASCII input
envelope. The bootstrap argv is fixed implementation source, not caller source or secret values.
Application script source and application stdin use distinct descriptors. Fixed `sh`/`bash` and
the actual destination account's supported default shell are separate choices. Login/interactive
startup combinations require their own proof and must not be silently accepted.

Guest stdout and stderr are separately armored into a shared record stream. Raw carrier stderr
remains diagnostic or mixed provenance. Framing validation, output bounds and end markers must
establish complete guest streams before interpreting them as such. Invalid or incomplete framing
is a failed proof, not an empty successful command. The envelope does not carry a new exit-status
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

## Test scope and evidence

Local tests may run bounded subprocess fixtures with synthetic data in session-owned temporary
directories. They neither use operator credentials nor access operator VM/configuration state.
They prove local mechanics only, not SSH delivery or live PVE behavior. Independence must also be
tested in a fresh process with the retirement modules unavailable; ordinary pytest startup imports
legacy fixtures and cannot stand in for that check.

The repository's test-environment skill has no populated inventory in this workspace. No live
Proxmox target or resource budget has been selected or exercised. Before a live native run, obtain
an operator-approved charter naming the exact node/VM, root or demoted execution authority, allowed
workloads, duration/output bounds and independent cleanup checks. No create/delete permission is
inferred from permission to implement the PoC.

Acceptance remains open for SSH contribution/integration, live native evidence, supported tools and
shell startup combinations, identity/elevation, strict deadline behavior, bounded live I/O and the
full joint matrix. Record actual command results here when observed; do not convert pending items
into passing claims because the carrier's local unit tests pass.
