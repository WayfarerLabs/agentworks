# Independent SSH Carrier: Functional Requirements

- Status: Two-phase delivery; PoC and full implementation remain uncompleted
- Updated: 2026-09-17
- Architecture: [hla.md](hla.md)
- Coordination: [transport design](../2026-09-12-transport-improv/hla.md)

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
a preliminary migration of legacy callers.

Delivery under this SDD has two phases: PR #796 carries the entire SSH portion of the joint PoC with
these artifacts; the second SSH PR carries full implementation and its integration/retirement
obligations. There is no separate SSH design PR to merge. The current artifact checkpoint requests
feedback before PoC implementation and does not claim proof completion. Transport is the sole owner
of the carrier contract and acceptance criteria; SSH contributes implementation and feasibility
input. The [plan](plan.md) defines phase gates without maintaining a second shared contract.

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

Implement the
[transport-owned carrier contract](../2026-09-12-transport-improv/execution-contract.md#carrier-contract),
including its input/stream ownership, sensitivity, deadline, one-attempt delivery and evidence
rules. Those rules and the transport FRD remain the source of truth for shared execution semantics.
SSH consumes the shared types directly and never weakens them to accommodate installed-client
limits.

The SSH binding must preserve bytes and expose only evidence the installed client actually supplies.
Mixed client/guest stderr cannot become pure guest output by relabeling it, status 255 alone cannot
establish a connection drop, and local process cleanup cannot establish remote cancellation.
Preparation and public result interpretation remain transport-owned. Unresolved feasibility returns
to transport before proof acceptance; requirement changes return to the operator.

No SSH-specific completion-envelope protocol, reconnect manager, pool, mux implementation or new SSH
library is implied. AsyncSSH remains excluded. Shared managed jobs and later observation belong to
transport, not a second SSH-specific job implementation.

Explicit terminal and live-streaming behavior remain available for their supported workflows.
Requested forwarding has an explicit lifetime and setup failure; inherited forwarding is disabled.
Preserve the stdin/terminal guarantees of
[ADR 0020](../../adrs/0020-close-ssh-stdin-instead-of-forcing-a-tty.md) and fresh connections from
[ADR 0015](../../adrs/0015-abandon-ssh-controlmaster.md).

## R5. Proof, integration and acceptance

Use the transport-owned
[joint PoC definition](../2026-09-12-transport-improv/plan.md#2-prove-the-shared-boundary-before-broad-implementation)
and its prerequisites as the single acceptance matrix. Phase 1 delivers all SSH implementation,
fixtures and observed evidence required by that definition, integrated with transport's preparation,
outcomes and proof harness. Transport owns the non-SSH proof and the combined acceptance record. SSH
records its findings and reconciles this SDD against the transport-owned proven revision before
Phase 2 begins. The [plan](plan.md) tracks our work without declaring the other effort's work done.

New-stack tests must work with legacy execution modules unavailable. Final acceptance additionally
requires complete production workflows after physical retirement under the transport-owned cutover.
Verify hostile config isolation, authentication offers, trust preservation/refusal and writes,
binary I/O, sensitivity, interruption, terminal restoration, forwarding and cleanup. Cover supported
Linux, macOS and Windows workstations separately from execution hosts, providers and inner clients.
Missing live evidence requires operator disposition, not a passing claim.
