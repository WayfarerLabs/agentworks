# Native Execution Transport: Functional Requirements

- Status: Design
- Date: 2026-09-04
- Target release: after 0.18.0
- Tracking issue: [#727](https://github.com/WayfarerLabs/agentworks/issues/727)
- Predecessor: `docs/sdd/2026-06-19-polymorphic-transports/`

## Purpose

Require every VM platform to provide a Tailscale-independent way for Agentworks to run bounded
administrative commands on a managed VM. This is the recovery channel Agentworks needs to inspect
and repair Tailscale without already depending on Tailscale.

Today the vm-platform contract asks for one full `Transport`, including interactive shell, file
copy, and streaming methods. Proxmox cannot truthfully provide that surface through QEMU Guest
Agent, even though QGA can run the noninteractive commands needed by Agentworks. The optional return
temporarily makes Proxmox non-compliance look like a supported platform choice.

This effort separates the required execution surface from the richer interactive transport. It makes
native execution mandatory, implements it for Proxmox through QGA, and leaves native interactive
shell support optional and honestly reported.

## Personas and user stories

- As an operator, I want Agentworks to recover Tailscale on any supported VM platform without
  silently falling back to another channel or asking me to use a provider console.
- As a Proxmox operator, I want rejoin, rekey, release attestation, and logout to use QGA when
  Tailscale is unavailable.
- As an operator using `vm shell --platform`, I want a clear unsupported result when a platform has
  native command execution but no interactive native shell.
- As a platform maintainer, I want the capability type to require exactly the native behavior core
  needs, without pretending that noninteractive execution implies terminal or file-transfer support.
- As a maintainer handling secrets, I want a sensitive stdin value to remain absent from command
  arguments, logs, results, diagnostics, exceptions, and chained causes on every implementation.

## Settled product decisions

1. Agentworks requires one Tailscale-independent native execution transport from every bundled VM
   platform.
2. Native interactive shell, streaming, and file transfer are richer capabilities. A platform may
   omit them.
3. Core recovery and lifecycle paths use only the required execution surface. They do not use
   interactive or file-transfer methods accidentally hidden behind a broad type.
4. The canonical VM transport remains Tailscale SSH. Native execution never becomes an automatic
   fallback for canonical operator work.
5. Proxmox implements native execution through QEMU Guest Agent. `vm shell --platform` remains
   unsupported on Proxmox.
6. The vm-platform capability remains contract version 1. There are no external VM platforms, so all
   bundled implementations change atomically instead of preserving a transitional API.
7. No database or operator configuration is added. Existing Proxmox VM identity and site credentials
   are sufficient.
8. pyinfra is design prior art and a possible future initialization layer. This effort does not add
   it as a dependency, copy its internals, or adopt its inventory, fact, or operation model.
9. Runtime implementation must not ship in 0.18.0. The SDD may land independently because it changes
   no runtime behavior.

## Functional requirements

### Required native execution

- **R1.** The vm-platform contract shall require each platform to construct a native execution
  transport for an existing VM. The transport shall not depend on the managed VM's Tailscale node,
  Tailscale DNS, or Tailscale SSH.
- **R2.** VM creation shall return a native execution transport that core can use for live Debian
  release attestation and initialization work before switching to the canonical transport.
- **R3.** Start-time Tailscale repair, Tailscale rekey, Tailscale logout, release attestation, and
  every other core native-channel consumer shall type against the execution-only surface.
- **R4.** Native transport construction shall retain the existing transient-route lifetime. A
  platform may open temporary provider access before constructing the transport and must unwind it
  through the caller-owned context stack.
- **R5.** Native transport construction shall retain a bounded reachability probe before the first
  operational command. A failed probe shall preserve platform-specific recovery guidance and shall
  not cause a canonical-transport fallback.

### Execution behavior

- **R6.** The execution-only surface shall provide a stable endpoint description, mutable command
  logger, default timeout, timeout override, root selection, environment values, checked and
  unchecked exit handling, captured output, output discard, retry arguments, and a result containing
  exit status, stdout, and stderr.
- **R7.** `sudo=False` shall execute with the configured VM admin user's authority. `sudo=True`
  shall execute as root. A provider transport that starts with root authority shall deliberately
  demote the ordinary case.
- **R8.** With neither stdin payload supplied, execution shall present immediate EOF to the guest
  command. `input_text` and `input_data` remain mutually exclusive. `input_data` is ordinary
  protocol input; `input_text` is sensitive input with suppressed output and diagnostics.
- **R9.** Sensitive stdin shall be byte-exact and absent from argv, logs, returned output, provider
  error text, Agentworks diagnostics, exception text, and chained causes. A transport shall reject a
  requested TTY when that would weaken this contract.
- **R10.** Retry parameters shall remain accepted by every execution transport. An implementation
  shall not redispatch a command after an ambiguous provider response unless it can prove the first
  dispatch did not begin.

### Optional full transport

- **R11.** The existing full `Transport` shall extend the required execution surface and retain
  interactive shell, streaming, copy, and file helper behavior.
- **R12.** `vm shell --platform` shall require a full native `Transport`. When the platform returns
  an execution-only transport, the command shall fail before attempting interaction and provide the
  platform's native-console guidance.
- **R13.** Lima, remote Lima, WSL2, Azure, EC2, and GCE shall continue returning their existing full
  transports. Their operator-visible behavior shall not change except where type names or
  documentation become more accurate.

### Proxmox

- **R14.** Proxmox shall implement the required transport through the existing authenticated QGA
  exec and exec-status endpoints, using the persisted node, VMID, and admin username.
- **R15.** Proxmox shall deliver stdin through QGA's `input-data` facility and reject payloads over
  the provider's 65,536-character API-field limit before dispatch. Existing bootstrap file staging
  remains a separate create-time mechanism.
- **R16.** Proxmox shall map normal exit, nonzero exit, abnormal signal, malformed response, QGA
  unavailability, and truncated captured output into explicit transport results or typed failures.
  It shall never silently accept incomplete output.
- **R17.** A Proxmox timeout shall stop Agentworks polling and explain that the guest process may
  still be running, because the provider exposes no cancellation operation. Agentworks shall not
  claim that the command was stopped.
- **R18.** Proxmox creation shall return the QGA execution transport for core attestation and
  initialization. Normal post-initialization commands continue to use canonical Tailscale SSH.

### Compatibility and collateral

- **R19.** The vm-platform capability shall remain version 1. All bundled platform implementations,
  conformance fixtures, capability documentation, and core callers shall change in the same runtime
  implementation.
- **R20.** The change shall add no database migration, persisted compatibility marker, new platform
  setting, sample configuration, or CLI completion entry.
- **R21.** Permanent root and vm-platform capability documentation shall distinguish required native
  execution, optional full native transport, and canonical Tailscale transport. The Proxmox guide
  shall document QGA recovery support and the remaining interactive-shell limitation.
- **R22.** The implementation shall retain the repository's documented Proxmox VE 8 scope. Support
  for a later Proxmox permission model is a separate compatibility decision.

## Quality requirements

- **Q1.** An execution-only fake shall pass every core native-channel path without implementing
  interactive, streaming, or file-transfer methods.
- **Q2.** Tests shall prove admin-versus-root rendering, finite stdin, provider payload limits,
  timeouts, ambiguous dispatch, signals, truncation, malformed responses, checked exits, and
  sensitive-input non-disclosure.
- **Q3.** Tests shall prove that `vm shell --platform` accepts a full native transport and refuses
  an execution-only transport without invoking an interactive method.
- **Q4.** Existing full transport contract tests shall continue to pass. The refactor shall not
  duplicate their implementation or introduce a second native-route factory.
- **Q5.** Tests assert behavior, structure, values, and side effects. They shall not pin authored
  error or guidance prose.

## Acceptance criteria

1. Every bundled platform satisfies one required Tailscale-independent native execution hook.
2. An execution-only fake completes all core recovery and create-time call paths.
3. Proxmox rejoin and rekey work through QGA while canonical Tailscale SSH is unavailable.
4. Proxmox `vm shell --platform` fails clearly and points to an available provider console.
5. Sensitive stdin is absent from every captured Agentworks and provider-facing diagnostic surface.
6. QGA timeout, signal, truncation, malformed response, and nonzero exit behavior is explicit and
   covered by tests.
7. Full automated gates, installed-wheel smoke tests, private reviews, CI, and
   capability-appropriate live validation pass before implementation merge intent.

## Out of scope

- Interactive QGA shell, terminal emulation, streaming QGA output, or QGA file transfer as a public
  transport feature.
- Automatic fallback from canonical Tailscale transport to native execution.
- A second native execution hook, transport selection registry, or operator-selectable recovery
  strategy.
- pyinfra dependency, vendored pyinfra code, connector adapter, inventory, facts cache, deploy
  engine, or operation DSL.
- Rewriting the existing Proxmox bootstrap staging path without an independent need.
- Proxmox VE 9 support or a version-aware Proxmox permission setup change.
- Renaming the established `SSHResult`, `SSHError`, or `SSHLogger` compatibility shapes across the
  codebase.
- Database, configuration, schema, command, or completion changes.

## Rulings

- 2026-09-04: The operator chose an SDD before implementation and authorized up to three published
  feedback/fix rounds for the SDD PR.
- 2026-09-04: The operator excluded runtime implementation from 0.18.0.
- 2026-09-04: pyinfra may inform this design and a later configuration-management direction, but
  #727 remains the lower transport-layer correction.
