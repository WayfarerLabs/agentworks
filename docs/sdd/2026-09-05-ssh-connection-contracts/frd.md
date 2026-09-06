# SSH Connection Contracts: Functional Requirements

- Status: Draft for design review
- Date: 2026-09-05
- Operator: authenticated direction in the `agw-ssh-improv` session
- Architecture: [hla.md](hla.md)

## Purpose

Agentworks must connect predictably regardless of the operator's SSH configuration, report what it
knows about an operation's completion, and recover persistent interactive attachments without
repeating remote work. SSH access to a virtualization host is a reusable connection concern; Remote
Lima is its first existing consumer, not the name of that concern.

This effort covers issues [#740](https://github.com/WayfarerLabs/agentworks/issues/740),
[#745](https://github.com/WayfarerLabs/agentworks/issues/745),
[#492](https://github.com/WayfarerLabs/agentworks/issues/492), and
[#409](https://github.com/WayfarerLabs/agentworks/issues/409).

## R1: Isolated SSH connections

Every Agentworks-controlled SSH connection must obtain its endpoint, login identity, routing,
execution options, and host-key storage from explicit Agentworks configuration or platform-owned
connection data. User and system SSH configuration must not affect an Agentworks invocation. The
requirement applies to every hop, including a virtualization host and any supported jump host.

Coverage includes captured commands, sensitive and ordinary stdin delivery, interactive sessions and
shells, streamed execution, file transfers, workspace archive streams, backup transfers, forwarding,
bootstrap, recovery, and remote virtualization-platform access. Provider-owned inner connections
must be audited and validated against the same behavioral boundary; a library or subprocess boundary
is not an exemption.

Host-key verification remains enabled. Accepted host keys must be stored in an explicit
Agentworks-owned location, with no implicit writes to the operator's SSH files. Existing trust must
not be silently replaced or mismatches treated as first-use acceptance during migration.

Only the configured authentication identity may be offered. An SSH agent may sign for that explicit
identity; unrelated agent keys or implicit default keys must not become fallback credentials.
Generated manual SSH aliases remain an operator convenience, not an input to managed operations.

## R2: Reusable virtualization-host access

Connection configuration must describe an SSH host independently of the virtualization platform. The
existing Remote Lima placement must consume this connection model for its outer host access.
Lima-specific command rendering and guest selection remain platform concerns.

Existing installations using an operator SSH alias must have documented migration to explicit
connection settings. Missing settings must fail before remote work with actionable guidance.
Migration must not execute or import arbitrary operator SSH configuration implicitly.

No additional virtualization provider, generic remote platform framework, or new fallback route is
required. The shared model must serve the concrete existing consumers without Lima-specific fields.

## R3: Preserve execution behavior

Consolidation must preserve explicit PTY selection, finite stdin when no payload is supplied,
byte-exact stdin delivery, stream separation where supported, environment delivery, sensitive-data
suppression, and terminal restoration. Raw archive streams must remain byte-transparent.

All supported controller platforms must receive equivalent guarantees. Interactive and streaming
operations must detect an unresponsive connection within a documented keepalive budget; buffered
operations and transfers must have explicit connection and operation timeout semantics.

Intentional forwarding must retain its requested listeners while rejecting inherited forwarding. A
failure to establish a requested listener must fail the operation rather than leave an apparently
successful tunnel process. Operator cancellation must stop local connection processes and release
their operation-owned resources.

## R4: Evidence-based outcomes

Every operation must preserve the distinction between a received remote exit status, a local
cancellation or launch failure, a deadline, and a connection or channel failure. A remote exit of
255 must not be classified as a connection drop solely from its numeric value. Failure to establish
a connection must not be described as loss of an established session.

Connection outcome and remote completion are separate facts. If completion evidence is lost, remote
completion is unknown: the command may have run and changed state. No diagnostic may claim
otherwise. Nested operations must identify the failing hop when evidence supports that distinction,
and preserve uncertainty when it does not.

A clean detach or remote exit must be distinguished from a detected connection failure when the
corresponding completion evidence arrives. Perfect knowledge after loss is not promised. An
unacknowledged clean detach is an uncertain outcome, not proof that the operator intended recovery.

Remote command failures must not automatically become connectivity failures. Logs and transfers must
not present failed or incomplete output as a successful complete result. Existing CLI exit status
behavior must be preserved wherever unambiguous; any unavoidable compatibility change needs an
explicit migration decision before implementation.

## R5: Retry and reconnect

A deadline is not proof of failure before execution and must not automatically replay a command.
Repetition is allowed only when the owning operation establishes that it is safe, such as a
deliberately idempotent readiness probe. Arbitrary commands and mutations receive no automatic
repetition after ambiguous execution.

Provide an explicit opt-in reconnect option for persistent session and console attachment. Recovery
must target the same persisted runtime identity, verify that it is still attachable, and preserve
the required access and route lifetime throughout the attempt. It must not create, restart, or
replace the workload. A received clean detach or exit, operator cancellation, missing runtime, or
non-retryable authentication or trust failure ends recovery.

Recovery has finite attempts, bounded backoff, visible status, and immediate local cancellation. An
indeterminate termination requires an explicit operator decision before reattachment; it must not
silently undo a possible intentional detach. A successful reattachment resets no lifetime budget in
a way that permits an unbounded reconnect loop.

Plain shells, arbitrary execution, file transfers, and tunnels receive reliable outcome reporting in
this effort but no automatic continuation. Reopening a plain shell is a new shell. Transfer resume
and command replay require separate operation-specific contracts and are out of scope.

## R6: Shared transport coordination

The native execution effort in [PR #746](https://github.com/WayfarerLabs/agentworks/pull/746) owns
the `ExecTransport` extraction, native platform contracts, and Proxmox guest-agent implementation.
Native means independent of Tailscale, not necessarily independent of SSH.

This effort owns SSH connections and the subsequent shared outcome migration. Isolation and outcome
research may proceed in parallel; shared result and error changes must integrate with the native
effort's resulting contract. No silent canonical-to-native fallback is introduced.

The implementation must preserve non-SSH evidence, including an acknowledged guest-agent process
identity and unknown completion after polling failure. It must not impose SSH-specific failure codes
or reconnect semantics on all transports.

## Acceptance

Behavioral validation must exercise all in-scope invocation families with disruptive user and system
SSH configuration, explicit identities, isolated host-key storage, and expected host-key mismatch
refusal. Verify supported controller platforms and the virtualization-host path.

Outcome validation must cover remote exits 0, 1, and 255; clean detach; local interruption;
authentication and trust failures; connection loss before and during execution; and loss after
execution before completion acknowledgement. Verify byte-transparent streams, secret suppression,
terminal restoration, finite recovery, and no automatic replay of an uncertain mutation.

An operation coverage matrix must name observed passes, failures, and unavailable environments.
Mocks alone cannot establish network-loss, PTY, or supported-platform acceptance.

## Delivery and authority

The operator requested FRD and HLA review in one draft PR using `review-requested`, with up to three
authorized design feedback/fix cycles. After design convergence, planning and implementation may be
pushed together in that same PR, followed by up to three implementation feedback/fix cycles. The PR
remains draft unless the operator separately directs otherwise; no merge is authorized.

The initial checkpoint contains these design documents, not a claim of completed implementation.
Material requirement changes, an unworkable outcome mechanism, or unresolved cross-effort ownership
must be escalated rather than absorbed through speculative complexity. Implementation starts only
after the design's decision gates are resolved.
