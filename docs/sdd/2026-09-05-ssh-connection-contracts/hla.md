# Independent SSH Carrier: High-Level Architecture

- Status: Reconciled with current main and transport proposal; joint proof remains open
- Updated: 2026-09-16
- Requirements: [frd.md](frd.md)
- Shared contract:
  [transport proposal `6809827f`](https://github.com/WayfarerLabs/agentworks/blob/6809827f64fb288880167fe2a4d9d7b42e29a21e/docs/sdd/2026-09-12-transport-improv/execution-contract.md)

## Boundary and ownership

Build `agentworks.execution.carriers.ssh` independently alongside the operational legacy stack.
`SSHCarrier(SSHConnection)` implements transport's leaf `Carrier.execute` contract. Shared target
and preparation code depend on the carrier protocol; SSH does not depend on targets, RunContext, VM
models, platform selection or legacy runners.

| Owner                          | Responsibility                                                                                                                                    |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| SSH effort                     | Explicit connection/trust values and migration, isolated SSH/scp options, process delivery, keepalives and explicitly owned forwarding resources. |
| Transport effort               | Shared contract/types, preparation, public outcomes/sensitivity policy, commands/files/jobs, other carriers and scoped RunContext delivery.       |
| Transport/platform integration | Apply reusable SSH policy in host access, Lima/provisioning and provider-inner paths; own management commands and route/VM holds.                 |
| Transport cutover              | All production/plugin caller migration, state-transition coordination, workflow acceptance and physical legacy deletion.                          |

Proposed SSH files are `connection.py`, `client.py`, `trust.py` and `forwarding.py` beneath that
package, with exports in `__init__.py` and independent tests under
`cli/tests/execution/carriers/ssh/`. These are responsibilities, not a class hierarchy. Optional SCP
acceleration stays below shared file publication semantics; it is not required to prove the
mandatory execute primitive. The later file-only SSH/QGA slice belongs to transport: core path and
action grants, JSON updates, privileged publication and FIFO lifecycle stay above delivery. An
optimization cannot bypass shared destination checks or expose command access to a file-only
consumer.

## Connection and policy

SSHConnection is immutable resolved data: host/port, account, configured identity, independently
selected agent, host-key lookup alias, trust files and bounded intentional connection options.
Construction and feature inspection perform no I/O. Composition owns configuration/credential
resolution and route activation; operation-time checks enforce file availability and trust policy.

One SSH/scp option policy ignores ambient config with `-F none`, selects only the configured
identity, disables automatic sibling certificates, inherited forwarding and connection sharing, and
supplies explicit trust sources. Existing targets use strict verification. Enrollment is separately
selected from creation provenance, never inferred from failure or a missing store. Apply the
[migration strategy](migration-strategy.md) without importing old execution code.

Use installed OpenSSH and its default algorithms, with no pool, alternative library, proxy route or
generic option dictionary. Operation-specific PTYs, keepalives and forwarding do not override
isolation. Forwarding resources fail setup if requested listeners cannot be established and close
within the composition root's lifetime; they are not hidden inside execute or a passive accessor.

## Client version and execution locations

The minimum is OpenSSH 8.5 for client binaries whose options the new SSH package constructs:

- Workstation `ssh`, and `scp` if used, on supported Linux, macOS and Windows systems.
- A platform-host client if the new SSH policy directly constructs an invocation there.
- Provider-launched inner clients are inventoried separately by transport. Provider-owned options do
  not inherit an assumed version or verification guarantee from the outer hop. If our builder's
  options are applied there, the same minimum applies; otherwise prove the provider's isolation and
  document its own compatibility boundary before proof acceptance.

Record actual executable selection and versions in the proof inventory. The pre-dispatch version
check and native executable/path behavior belong in the SSH LLD; unsupported clients fail locally
without weakened fallback. Construction remains passive. No new sshd minimum follows from this
client floor; server/authentication compatibility is separately recorded and tested.

## Prepared delivery and I/O

Use the shared PreparedInvocation, CarrierIO, Deadline and CarrierReport rather than cloning them
into SSH. PreparedInvocation supplies literal bootstrap argv and a safe label, with no stdin field.
CarrierIO is the sole input selector and carries output mode, sensitivity and authorized
presentation. SSH serializes prepared argv through the supported account-shell bootstrap; it does
not add application-shell, login, sudo, environment or directory policy a second time.

The shared contract owns borrowed-stream and failure rules. SSH consumes once, drains concurrently
with bounded buffering, handles short writes and EOF, and leaves no pump using borrowed streams
after return. Owned processes/pipes and terminal state receive bounded cleanup. An I/O failure
retains partial facts, not a successful overall result or a claim of guest termination.
KeyboardInterrupt propagates so operation rollback still runs. Raw bytes are not newline-normalized.

Carrier reports distinguish dispatch evidence, completion evidence, client status and observed guest
status. Do not parse error prose into connection classification or retry after uncertain delivery.
No SSH-specific completion envelope is planned. Shared job evidence may establish a later result;
the carrier does not own job records or reconnect policy.

The proof must settle guest stdout/stderr separation from client diagnostics, application source
versus stdin delivery, account-shell startup effects and readiness without staging. Test installed
OpenSSH facilities before proposing framing. An inner payload cannot undo hooks already run by
sshd/account-shell startup. Supported bootstrap combinations and refusal behavior must be explicit;
no broad implementation begins while these boundaries remain unresolved.

The [main comparison](main-comparison.md) identifies a bootstrap constraint: Python is installed
during initialization, after SSH is already in use. Initial delivery, package installation and
no-staging readiness cannot assume that prerequisite is present. Transport owns helper preparation
and must identify which lifecycle stages can use which tools; SSH does not install them or infer
platform-host prerequisites from an initialized guest.

## SSH-backed platform access

A platform operation composes the same SSH carrier with a host target and invokes its management
tools. Remote Lima is the first consumer. SSH neither accepts Lima configuration nor invents a VM
identity before creation. Transport owns host shell/PATH preparation, limactl commands, guest hops,
provider-inner isolation integration and provisioning/rollback. Other platforms can use the same
connection and host-target composition without importing Lima or introducing another SSH runner.

Shared host file/job helpers must support the real host userspace, including macOS; transport owns
that portability and its proof. Host-command completion is evidence only for that invocation, not
for a nested guest command. Provider identity/trust remains separate from the outer host trust
store.

## Independence and coordination gates

The initial retirement set is `agentworks.transports`, `agentworks.ssh`, `agentworks.remote_exec`,
`agentworks.harness_setup.runner` and `agentworks.plugins.proxmox.transport`. New code and tests
cannot reach them through result/error/logger aliases, lazy imports or shared utilities. Audit
retained utilities such as terminal restoration and identity parsing before reuse. Copy/adapt useful
implementation with provenance; do not copy automatic replay or text-normalizing result semantics.

Run isolated new-stack tests with retirement modules unavailable, including normal package imports.
Transport later proves installed production entry points after physical deletion. Trust/config data
has its own transition and rollback policy; deleting code never licenses deleting operator evidence.

The [plan](plan.md) follows #795: agree on the small contract and charter, prove it jointly,
reconcile both SDDs with observations, then build independently, validate workflows and cut over.
The current comparison updates the candidate assignment; post-proof reconciliation remains required.
Only the SSH owner edits these artifacts; common-contract amendments return to the transport owner.

## Evidence and references

The
[transport prior-art record](https://github.com/WayfarerLabs/agentworks/blob/6809827f64fb288880167fe2a4d9d7b42e29a21e/docs/sdd/2026-09-12-transport-improv/prior-art-research.md)
supplies shared execution research. No new library-selection study is required: installed OpenSSH is
settled. Primary SSH references are [ssh](https://man.openbsd.org/ssh.1),
[ssh_config](https://man.openbsd.org/ssh_config.5), [sshd](https://man.openbsd.org/sshd.8) and
[8.5 release notes](https://www.openssh.org/releasenotes.html#8.5p1). They inform the design, not
proof of our implementation, native path handling, provider-inner isolation or platform coverage.
