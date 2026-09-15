# Independent SSH Carrier: Functional Requirements

- Status: Revised design; joint proof and new implementation remain uncompleted
- Updated: 2026-09-12
- Architecture: [hla.md](hla.md)
- Coordination:
  [transport proposal at `698ddb23`](https://github.com/WayfarerLabs/agentworks/tree/698ddb23b278f364e460f0ae15bac5fab8c74b12/docs/sdd/2026-09-12-transport-improv)

## Purpose and direction

Make SSH delivery predictable despite operator configuration, as an independent carrier for the new
execution stack. The operator requested this rewrite to match PR #795 after accepting its revised
ownership and proof-first sequence. This replaces the interface-preserving consolidation plan;
earlier local implementation is reusable source material, not the new carrier or its acceptance.

SSH owns reusable connection, authentication, trust, delivery and forwarding mechanics. Transport
owns common execution semantics, applying SSH policy in platform/provisioning paths, and complete
production/plugin cutover followed by physical deletion of the old stack. This serves
[isolation #745](https://github.com/WayfarerLabs/agentworks/issues/745) and
[consolidation #740](https://github.com/WayfarerLabs/agentworks/issues/740) through replacement, not
a preliminary migration of legacy callers. This revision changes artifacts only; it does not run the
proof, authorize broad implementation, or claim the shared contract is proven.

## R1. Independent, reusable SSH delivery

Build without importing, wrapping, subclassing or calling legacy execution code, directly or
indirectly. Useful code and behavioral tests may be copied and adapted. Ordinary retained project
utilities may be reused after checking their dependency closure.

The carrier receives an explicit connection and prepared work, not a VM, global configuration
loader, application shell policy or resource context. The same carrier supports guest execution and
SSH-backed virtualization-host access. Remote Lima is the first platform-host consumer, not a
concept inside SSH. Platform adapters own management commands, guest selection, inner hops and
provisioning/rollback lifetimes. Do not add a host registry or virtualization framework.

## R2. Isolated connection and authentication policy

All SSH/scp processes launched by the new SSH implementation ignore user and system SSH
configuration. Explicit inputs select host, port, account, identity, agent and trust sources. Apply
one policy to buffered delivery, live streams, terminals, forwarding and any SCP acceleration.
Transport applies and validates policy at platform boundaries, including provider-inner SSH;
outer-hop isolation alone is not proof of inner-hop isolation.

Only the configured authentication identity may be offered. An explicitly selected endpoint or
deliberately supported platform/default agent may sign for it; unrelated agent keys, default key
files, automatically discovered sibling certificates and agent forwarding are not fallbacks. Missing
or invalid explicit settings fail before dispatch. Never resolve alias-only configuration through
operator SSH config, Match execution or automatic discovery. Manual SSH aliases remain an operator
convenience, not an execution input.

Use installed OpenSSH with an operator-approved minimum of 8.5. The [HLA](hla.md) distinguishes
workstation clients, platform-host clients and provider-owned inner clients from server versions.
Use the installed client's default algorithms. No arbitrary option passthrough, ProxyJump,
ProxyCommand, custom algorithm policy, silent routing fallback or downgrade is added. Unsupported
existing policies require explicit migration refusal and operator disposition.

## R3. Preserve trust independently of code replacement

Direct connections use explicit Agentworks-owned trust sources and strict verification for existing
targets. Preserve complete applicable known-host records, aliases, ports, CA policy and revocations,
including separate KRL files. OpenSSH interprets trust; no custom trust parser or trust engine is
introduced. Never reset trust, silently widen authentication or implicitly modify operator files.

An existing owned trust store may be reused under the same policy. Importing operator trust requires
explicit owned copies and matching lookup identity. Missing files or unknown keys do not make an
existing target new. Only explicit creation provenance permits new-target enrollment; it never
overwrites existing trust or accepts a known mismatch. Creating a guest does not enroll its existing
platform host. Provider-owned guest identity/trust remains separately scoped and documented.

Specify trust-writer ownership, migration refusal and rollback evidence before production cutover.
Endpoint changes retain strict verification; recovery uses independently confirmed ownership and
previously trusted identity or applicable CA policy, not automatic acceptance at the new address.

## R4. One attempt, byte-safe I/O and truthful evidence

Implement the shared carrier contract owned by transport. Input has one home in CarrierIO: EOF,
finite source, live source or terminal. Borrow caller streams without closing them; close owned
pipes, stop pumps and restore terminal state before returning or propagating interruption. Meet
bounded flow control, cancellation and source/sink failure behavior without hidden input replay.

Preserve raw bytes and explicit output completeness; decoding belongs above the carrier. Honor the
effective sensitivity and authorized live presentation supplied by shared preparation. Payloads must
not leak through process arguments, diagnostics, exceptions or retained artifacts. Application
script source and stdin remain separate; transport owns preparing their delivery and public output
suppression, while SSH enforces the resulting carrier I/O policy.

Each execute call makes at most one dispatch attempt and uses the remaining shared deadline without
resetting it. Report dispatch/completion evidence, local status, observed guest status and output
provenance separately. Status 255 or stderr wording alone does not prove a connection drop. Local
cleanup does not prove remote cancellation. Nested host completion does not establish inner guest
completion. Source/sink failure retains safe partial evidence; interruption propagates after
cleanup.

The proof must establish the public distinct-guest-stream contract through shared preparation and
SSH delivery, including no-staging readiness. Do not relabel mixed client/guest stderr as pure guest
output. Unknown outcomes remain unknown; no SSH completion-envelope protocol, reconnect manager,
pool, mux implementation or new SSH library is implied. AsyncSSH remains excluded. Shared managed
jobs and later observation belong to transport, not a second SSH-specific job implementation.

Explicit terminal and live-streaming behavior remain available for their supported workflows.
Requested forwarding has an explicit lifetime and setup failure; inherited forwarding is disabled.
Preserve the stdin/terminal guarantees of
[ADR 0020](../../adrs/0020-close-ssh-stdin-instead-of-forcing-a-tty.md) and fresh connections from
[ADR 0015](../../adrs/0015-abandon-ssh-controlmaster.md).

## R5. Proof, integration and acceptance

Agree on the small contract and authorized proof resources, demonstrate the joint buffered SSH slice
and bounded real QGA case, then incorporate findings into both SDDs before broad parallel work.
Transport owns preparation, outcomes and the proof harness; SSH owns its connection/delivery
portion. The [plan](plan.md) tracks our obligations without claiming the other effort's work is
done.

New-stack tests must work with legacy execution modules unavailable. Final acceptance additionally
requires complete production workflows after physical retirement under the transport-owned cutover.
Verify hostile config isolation, authentication offers, trust preservation/refusal and writes,
binary I/O, sensitivity, interruption, terminal restoration, forwarding and cleanup. Cover supported
Linux, macOS and Windows workstations separately from execution hosts, providers and inner clients.
Missing live evidence requires operator disposition, not a passing claim.
