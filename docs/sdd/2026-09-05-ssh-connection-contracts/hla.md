# SSH Connection Contracts: High-Level Architecture

- Status: Draft for design review
- Date: 2026-09-05
- Requirements: [frd.md](frd.md)
- Source baseline: `a64b1b9c`

## Current architecture and defects

`cli/agentworks/ssh.py` and `cli/agentworks/transports/ssh.py` duplicate command construction and
whole-process timeout retries. Interactive execution, streaming, Remote Lima, workspace copying,
backup progress, and port forwarding construct additional SSH or scp commands independently.
Consequently, policy changes in a shared builder do not cover the whole surface.

Canonical VM connections already carry host, user, and configured identity
(`transports/__init__.py:92`). Remote Lima instead delegates its outer host resolution and identity
to operator SSH configuration (`capabilities/vm_platform/lima.py:144`,
`transports/remote_lima.py:59`). The latter is the migration boundary, not a reason to retain
ambient configuration for all operations.

`SSHResult` carries only an integer and streams (`ssh.py:58`), while `SSHError` conflates checked
command failures with connectivity (`ssh.py:71`). Interactive status 255 is described as a drop
(`transports/ssh.py:75`). Log capture ignores failed execution (`sessions/tmux.py:917`). These
represent information loss at different layers, so repairing the warning alone is insufficient.

## Components and responsibilities

### Explicit SSH connection

Introduce one immutable connection value describing an endpoint, user, port, configured identity,
host-key policy reference, and any supported jump connection. It contains no Lima VM name, guest
command, PTY mode, or workload lifecycle state. Connection data comes from validated Agentworks
configuration and platform results, never from evaluating the operator's SSH configuration.

For managed VMs, factories construct this value from existing VM and operator data. For remote
virtualization hosts, placement configuration supplies the equivalent fields through a reusable SSH
connection schema. The initial consumer is Lima's SSH placement; no separately persisted host
resource or connection registry is needed merely to make the schema reusable.

Each jump uses the same explicit identity and trust rules. Retained jump support must describe
actual hops instead of an opaque alias string interpreted through ambient configuration. No
arbitrary proxy command escape hatch is added as a substitute for a defined routing contract.

### SSH policy and execution

One component owns connection policy for every Agentworks-launched SSH/scp process. It disables
ambient configuration using `-F none` or a private generated file with no external includes. It
selects the configured identity, restricts offered identities, and sets explicit known-hosts paths.
It preserves host-key verification and does not disable trust checks to simplify isolation.

An SSH agent may provide signing for the configured identity. This is distinct from agent
forwarding, which remains disabled. The implementation must test actual offered identities and
encrypted-key behavior instead of equating `-i` alone with exclusive identity selection.

Operation-specific execution choices are supplied separately: PTY or no PTY, stdin payload or EOF,
captured or inherited streams, explicit environment, connection timeout, operation deadline, and
intentional forwarding. Keepalive detection applies to long-lived connections, including nested
outer hops. Forwarding uses explicit listeners and fails when their establishment fails.

Buffered primitive execution and `SSHTransport.run` share one execution path, including logging,
sensitive-input handling, error translation, and any caller-authorized retries. Interactive,
streaming, transfer-progress, archive, and tunnel operations consume the same connection policy
without forcing their different I/O lifetimes through a buffered subprocess API.

### Virtualization-platform composition

Remote platform access composes two existing concerns: an explicit SSH connection reaches the
virtualization host, and the platform adapter renders commands for its control plane. Lima owns
`limactl` invocation, guest selection, staging, and any inner connection behavior.

The outer SSH implementation is platform-neutral. Refactor the existing Remote Lima path to consume
it; do not introduce a generic virtualization adapter hierarchy without another concrete
requirement. Inner guest failures retain their origin instead of being relabeled as failure of the
outer connection. Where the provider does not expose that evidence, the result remains indeterminate
at that boundary.

### Execution outcomes

Represent observed execution facts independently:

- remote completion, with exit status or signal when received;
- local cancellation or process-launch failure;
- deadline expiry;
- connection establishment failure or established connection/channel loss when known; and
- indeterminate failure when the carrier cannot distinguish those facts.

Connection termination can coexist with unknown remote completion. Preserve the endpoint/hop and
operation phase that produced the evidence. Ordinary output remains separate from these facts;
diagnostics must not leak sensitive payloads or translate uncertainty into claims of no mutation.

The shared contract belongs at the execution boundary introduced by #746. SSH-specific evidence is
produced by the SSH adapter; guest-agent and local transports report their own evidence. Checked
remote failure and transport failure have distinct internal handling. CLI adapters retain remote
exit codes where appropriate while rendering actionable failure and uncertainty information.
Cancellation is intentional termination and never reconnects.

### Completion mechanism decision gate

OpenSSH's process status alone cannot provide the requested distinction: it returns a remote command
status or 255 on error. A remote status of 255 is therefore ambiguous. Parsing diagnostic wording or
reserving that remote status would not meet the requirements.

Before design convergence, a focused executable prototype must select one feasible mechanism:

1. Retain OpenSSH with a separate, reliable completion/control mechanism that does not alter
   arbitrary command output, stdin, PTYs, or the command's exit semantics.
2. Use a protocol-aware SSH implementation that exposes channel exit-status and connection events
   independently, while proving supported identity, trust, terminal, transfer, and platform
   behavior.

The preferred starting point is retaining OpenSSH because it is the established client, but that
preference does not waive the evidence requirement. A marker appended to arbitrary stdout/stderr is
not a separate control channel, and a remote file descriptor does not automatically become an SSH
channel. No library migration is selected by this document.

The prototype must demonstrate remote exits 0/1/255, PTY detach, raw binary streams, sensitive
stdin, nested execution, and loss before acknowledgement. Record the selected mechanism, its
tradeoffs, and executable evidence in this HLA before claiming design convergence. If neither option
meets the requirements with proportionate complexity, stop and escalate; do not implement a
heuristic and describe it as exact classification.

Even a protocol-aware implementation cannot infer remote non-execution from missing completion
evidence. That uncertainty survives the mechanism choice and is part of the contract.

### Recovery belongs to the operation

Session and console attachment own the opt-in reconnect loop above the transport. They retain the
same runtime identity and operation resource hold, classify the outcome, check that the runtime
still exists, and reconnect only when the outcome and policy permit it. Access is revalidated when
needed, and failures of authorization or host trust terminate recovery.

Received clean completion and local cancellation end attachment. Known connection loss permits
bounded reattachment; an indeterminate termination asks the operator before reattachment. Recovery
never invokes session creation, restart, or console realization as a side effect. Terminal cleanup
runs after each lost attachment and before recovery diagnostics.

The future implementation plan will specify the exact CLI option, finite attempt budget, backoff,
and non-interactive refusal behavior together with completions and help. Those choices cannot be
left implicit at implementation handoff. Automatic replay of arbitrary execution and automatic
transfer or tunnel continuation remain outside this effort.

## Migration and integration

Implement one coherent migration of all SSH invocation families. Remove duplicate builders once
their consumers move; do not leave an ambient-config compatibility path behind the isolation claim.
Manual alias export remains available for operator use.

For example, a Lima placement currently containing only an SSH alias must be replaced by explicit
host, user, identity, and optional port/jump settings. The operator supplies those values rather
than Agentworks executing `ssh -G` against arbitrary configuration to discover them. Missing fields
produce local migration guidance before credentials are used or remote commands run.

Before implementation, record how existing trusted host keys move to the owned store, including
host/port matching, conflict refusal, and whether an explicit import action is needed. An empty new
store must not silently erase prior mismatch protection. This is a design decision gate alongside
the completion mechanism, not permission to ship a trust reset.

Coordinate shared changes with the native execution effort: #746 lands its extraction first, then
this effort migrates outcomes on the resulting execution interface and all implementations. SSH
isolation and prototypes do not depend on that extraction. If the dependency remains unavailable,
continue independent work and report it; do not take ownership of another effort's artifacts.

The approved delivery vehicle is a single draft PR. After design convergence, add the detailed plan,
migration strategy, selected-mechanism evidence, and complete implementation in one push. Permanent
docs describe shipped behavior only and accompany implementation. The final plan must include
updating transport documentation, configuration examples, CLI help/completions, and guide topics
affected by the actual changes.

## Verification strategy

Use behavioral tests at the execution boundary, plus isolated real SSH fixtures and live platform
validation. Disruptive configuration should include PTY forcing, multiplexing, remote/local
commands, environment injection, forwarding, identity additions, and routing overrides. Observe
actual connection behavior and filesystem writes, not only helper-generated argument lists.

Fault injection must cover connection establishment, active execution, and completion delivery.
Verify exact data delivery, partial-output refusal, cleanup, preserved exit status, unknown remote
completion, and no replay of ambiguous mutations. Exercise plain SSH, virtualization-host access,
the relevant inner transport, and supported controller platforms. Maintain an operation coverage
matrix with explicit gaps; local mocks cannot close unavailable live acceptance cases.

Independent project and complexity reviews precede each handoff. The draft `review-requested`
checkpoint solicits PR-level design feedback; it is not a ready-for-merge or live-validation signal.

## Related work and primary sources

- [Native execution design #746](https://github.com/WayfarerLabs/agentworks/pull/746): shared
  execution boundary and Proxmox evidence semantics.
- [Secret delivery #516](https://github.com/WayfarerLabs/agentworks/issues/516): coordinate any
  diagnostic changes; do not broaden exposure while adding outcome evidence.
- [AWS route lifetime #408](https://github.com/WayfarerLabs/agentworks/issues/408): independent
  source of connection interruption; reconnect does not repair shared route ownership.
- [OpenSSH client manual](https://man.openbsd.org/ssh.1): configuration isolation, jump-host option
  scope, terminal behavior, and ambiguous process exit status.
- [OpenSSH configuration manual](https://man.openbsd.org/ssh_config.5): identity selection,
  known-hosts files, keepalives, forwarding, and configuration evaluation.

The source audit and protocol limitations above motivate the component boundaries. The pending
prototype and trust migration decision are explicitly unresolved; the rest is the proposed design
submitted for review, not a statement that these behaviors already exist.
