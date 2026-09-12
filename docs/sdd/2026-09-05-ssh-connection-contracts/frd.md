# SSH Isolation and Consolidation: Functional Requirements

- Status: Draft for reduced-scope design review
- Updated: 2026-09-09
- Architecture: [hla.md](hla.md)

## Purpose and scope

Make existing SSH operations predictable despite operator SSH configuration, using installed
OpenSSH. This PR covers consolidation [#740](https://github.com/WayfarerLabs/agentworks/issues/740)
and isolation [#745](https://github.com/WayfarerLabs/agentworks/issues/745).

On September 9 the operator approved reducing this effort to consolidation, isolation, explicit
Remote Lima connection settings, preserved trust, and honest status-255 diagnostics. Exact exit/drop
classification, reconnect, and shared outcome redesign are deferred, not completed. AsyncSSH and
multiplexing/control-protocol implementations are excluded.

## R1: One isolated SSH policy

Every Agentworks-launched SSH or scp invocation must ignore user and system SSH configuration.
Endpoint, user, port, identity, trust files, and intentional execution options come from explicit
Agentworks settings or platform data. This deliberately supersedes #745's suggestion to retain
ambient routing and identity configuration.

Cover buffered execution, interactive attachment and shells, streaming, transfers, workspace
archives, backups, forwarding, bootstrap, and virtualization-host access. Consolidate duplicate
policy construction and buffered execution rather than adding another parallel helper.

Audit provider-launched inner SSH, including local and remote Lima, for operator-config influence.
Validate that boundary using provider-owned connection data; do not infer isolation from the outer
hop alone. If the provider cannot be isolated with a bounded configuration change, escalate before
implementation handoff rather than introduce a new guest transport or claim complete isolation.

## R2: Explicit connection and compatibility boundary

Use a small reusable SSH connection value, independent of the virtualization platform. Remote Lima
is its first virtualization-host consumer; Lima command rendering and guest selection stay local to
the Lima adapter. No host registry or generic virtualization framework is required.

Alias-only placements must migrate to explicit host, user, identity and port settings. Missing
settings fail locally before remote work. Do not evaluate operator SSH configuration to discover
values. Manual SSH aliases remain an operator convenience, not a managed-connection input.

Only the configured authentication identity may be offered. An agent may sign for that identity;
agent selection uses an explicit endpoint or the deliberately supported SSH_AUTH_SOCK/platform-agent
selection, not IdentityAgent from SSH config. Migration guidance must explain that distinction.
Unrelated agent keys, default identity files and agent forwarding are not fallback mechanisms.

Connections use the installed OpenSSH version's default algorithm policy. Targets or organizational
policies requiring custom cipher, key-exchange, MAC or signature restrictions are outside this PR's
supported configuration. No automatic negotiation downgrade, arbitrary SSH option passthrough,
ProxyJump or ProxyCommand configuration is added. Existing alias-based routing or algorithm
restrictions do not carry over. This is a declared compatibility limit, not a claim that an operator
configuration survey found no such users.

## R3: Preserve trust and existing I/O

Direct SSH/scp connections retain OpenSSH host verification, using explicit Agentworks-owned trust
files without implicit reads or writes to operator SSH files. Migration must preserve existing
host-key mismatch protection, including applicable aliases, ports, CA and revocation records. Never
silently replace existing trust with an empty store or treat an existing target as new. Use
OpenSSH's trust implementation, not a custom parser or a new trust engine.

Provider-owned guest identity and trust remain provider concerns in this PR; their existing
limitations must be documented separately from the direct-connection guarantee. Auditing their
configuration isolation does not authorize weakening verification or redesigning their trust model.

Preserve explicit PTYs, byte-exact stdin, stream separation where supported, sensitive-data
suppression, environment delivery, terminal restoration, and existing timeout/keepalive behavior.
Buffered calls supply EOF without a payload; interactive and streaming paths retain their explicit
stdin behavior. Preserve the contracts in
[ADR 0020](../../adrs/0020-close-ssh-stdin-instead-of-forcing-a-tty.md) and
[#737](https://github.com/WayfarerLabs/agentworks/pull/737).

Keep requested forwards and reject inherited forwards. Listener setup failure must fail the tunnel
operation. Preserve cancellation cleanup and resource holds. Consolidation must not broaden retries
or add automatic replay; redesigning existing retry semantics is a separate effort.

## R4: Honest diagnostics, unchanged result interface

Keep existing return codes and result/error interfaces. Status 255 alone must not be described as a
proven connection drop: it can also be the remote command's exit status. Correct the affected
diagnostics without parsing OpenSSH prose or introducing a new outcome model. Do not infer a failing
inner hop from a Remote Lima outer-process status.

No reconnect options, exact completion protocol, transfer resume, connection pooling, or changes to
non-SSH outcome semantics belong in this PR. Preserve the execution interface delivered by
[#746](https://github.com/WayfarerLabs/agentworks/pull/746), now merged.

## Acceptance and delivery

Demonstrate the shared policy across every invocation family with disruptive SSH configuration,
including forced PTYs, remote/local commands, environment injection, identity additions, routing,
multiplexing and forwarding. Observe actual behavior and trust-file writes, not only argv strings.
Verify configured-key exclusivity, trust mismatch/revocation refusal, migration without trust reset,
binary streams, secret suppression, terminal restoration, intentional forwarding and cleanup.

Record workstation OS and VM platform separately, covering supported Linux, macOS and Windows
workstations and the virtualization-host/provider-inner boundary. Name unavailable cases; mocks do
not establish live SSH or terminal acceptance. Test exits 0/1/255 and interruption only for
preserved behavior and honest diagnostics, not as proof of exact classification.

Republish FRD/HLA in the same draft PR with review-requested after private project and complexity
reviews and gates. The operator authorizes up to two further feedback/fix rounds on this reduced
checkpoint. These documents are design, not shipped behavior. After convergence, the previously
authorized plan and implementation share one push in this PR; no implementation starts as part of
this republication. Keep the PR draft; no merge is authorized.
