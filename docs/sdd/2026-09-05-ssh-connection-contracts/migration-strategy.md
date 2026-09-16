# Independent SSH Carrier: Migration Strategy

- Status: Proposed transition, not a completed conversion or acceptance record
- Updated: 2026-09-16
- Requirements: [frd.md](frd.md)
- Shared cutover:
  [transport migration at `6809827f`](https://github.com/WayfarerLabs/agentworks/blob/6809827f64fb288880167fe2a4d9d7b42e29a21e/docs/sdd/2026-09-12-transport-improv/migration-strategy.md)

## Baselines and destination

The published SSH design at `2694d31a` proposed interface-preserving isolation/consolidation. Local
work through `2f11662d` includes explicit connection settings and a shared legacy builder; it was
not pushed as an implementation handoff. That local code and its earlier fixtures may inform the new
implementation but do not establish its independence or semantics. Refresh the production inventory
against main before preparing cutover rather than assuming this branch is deployed. The
[2026-09-16 main comparison](main-comparison.md) records the current baseline and new consumers.
Core delivery code is unchanged from the original baseline; artifact publication, discovery and
session cleanup expand the transport-owned migration inventory.

| Existing surface                                                  | Destination and owner                                                                                                                 |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Legacy SSH runner, transport wrapper and scattered options        | Independent SSH carrier and policy; SSH owns the replacement, transport owns caller cutover.                                          |
| Operator config and alias-only host placement                     | Explicit connection inputs; SSH specifies reusable schema/trust migration, transport applies it at factories and platform boundaries. |
| Operator/owned trust records and credentials                      | Preserved data under explicit policy, not retired implementation.                                                                     |
| Application wrappers, results/logging, files and detached helpers | Shared execution semantics owned by transport, not copied into a new SSH-specific API.                                                |
| Lima host commands and provider-inner delivery                    | Platform-owned consumers of reusable SSH policy, not SSH configuration or special cases.                                              |

There is no preliminary legacy consolidation release. Build the replacement independently after the
joint proof and design reconciliation, validate internal workflows, then switch production and
physically remove the old execution stack in the transport-owned cutover. Do not introduce a
permanent bridge, runtime old/new selector, or a second mutation for comparison.

## Configuration shape and compatibility

The reusable SSHConnection contains host, user, port, configured identity, independently selected
agent, trust-file references and optional lookup alias/revocation source. It contains no VM state or
command. Runtime value ownership belongs to SSH; transport owns composition and integration.

The existing local proposal of operator defaults under `[operator.ssh]` and explicit placement
fields is a candidate storage mapping, not a newly shipped schema or finalized conversion format.
Current main still uses `operator.ssh_private_key` and alias-based platform-host placement; it has
no `[operator.ssh]` connection/trust schema. Freeze config spellings, paths and refusal guidance in
the SSH LLD before build/cutover. A representative desired connection is a literal
host/user/identity/port plus explicit trust sources, not an alias interpreted through the operator's
SSH configuration.

Operators supply values hidden in existing aliases; migration does not run ssh -G, evaluate Match
clauses or discover arbitrary routing policy. Identity selection is independent of agent selection:
a deliberately supported default/platform agent, explicit endpoint or disabled agent must never
offer alternate identities. Disable automatic sibling certificate discovery and forwarding.
ProxyJump, ProxyCommand and custom algorithm restrictions are unsupported by the agreed surface;
refuse unsupported migration rather than silently choosing a direct route or weaker policy.

Use the HLA's OpenSSH 8.5 client boundary and separate server/provider compatibility inventory. No
server upgrade or provider-inner version is implied merely because the workstation passes.

## Preserve trust

Before existing-target use, the operator identifies all applicable trust sources and lookup policy.
An already-owned store can be reused without resetting it when the new connection preserves the same
scope. For operator/system trust, use explicit owned copies of complete applicable files. Preserve
bytes, including hashed records, CA patterns, aliases, nondefault ports and revocations. Copy
separate revoked-key files, including binary KRLs, intact. OpenSSH interprets these records; the new
implementation never calls a legacy runner to interpret them.

Keep connection-specific trust scoped to that connection, rather than unioning unrelated host
policies into global VM defaults. Do not apply one host's lookup alias to other targets. Strict
verification refuses missing files, unknown identities, mismatches and revocations; an empty store
does not constitute migration. Unsupported source policy requires operator disposition, not weaker
verification. No implicit writes to operator files, CA-to-pin conversion or automatic trust reset.

For an alias-based virtualization host, the operator supplies its actual endpoint/account/identity,
copies its applicable complete trust sources to dedicated owned paths, preserves any host-key alias
and revocation source, and verifies the explicit connection strictly. Creating a guest on that host
does not make the host new or authorize its enrollment. Provider-owned guest trust remains separate.

New-target enrollment requires creation provenance from transport's composition, including the
explicitly new VM's first canonical connection where applicable. The primary owned trust store may
be created, never replaced; additional trust and revocation sources must already exist. Subsequent
operations are strict. Missing trust, incomplete initialization and a changed endpoint are not
creation evidence.

After an existing VM's IP changes, retain strict checks. Independently confirm the endpoint belongs
to the intended VM, then associate previously trusted identity or applicable CA policy with the
correct alias/host/port, retaining revocations and old evidence. Verification must not repeat a
mutating rekey or accept whichever host answers.

## Writer ownership, cutover and rollback

Proof and conversion tests use isolated copies, not operator trust stores or concurrent production
writers. Before cutover, identify old/new writers, serialize or quiesce the transition, and preserve
original configuration and trust evidence. Resolve exact locking/publication mechanics in the SSH
LLD with the transport-owned cutover; no unproven concurrency claim belongs in this draft.

For each copied trust or revocation source, the LLD also names its post-cutover authority and
maintenance path: who supplies CA rotations and revocation updates, how owned copies are refreshed,
and how failed updates retain evidence and prevent use of policy known to be superseded. An import
is a snapshot, not an automatic subscription to its source. This is part of the existing trust
preservation obligation; no background synchronization service is proposed.

Source rollback and state rollback are distinct. Record which paths and formats old code can read
and how trust learned after cutover is retained; never restore an older empty or incomplete trust
snapshot merely to restore old code. If safe rollback cannot preserve evidence and policy, stop and
choose an operator-approved forward repair. Credential material is not copied into job references.

Transport inventories all production/plugin consumers and surviving detached work, settles their
migration/compatibility policy, and deletes legacy modules only after complete new-stack workflow
validation. SSH supplies its migration and isolation evidence; it does not claim to dispose jobs or
complete that cross-stack cutover on its own. Include current native artifact publication,
inspection, retirement and session restore/cleanup in that inventory. Preserve ownership records and
partial-failure checkpoints while replacing their command/copy calls with shared execution and file
operations. The later file-only slice precedes broader file-consumer migration, while the small
carrier proof still precedes independent SSH implementation.

## Required evidence

Acceptance includes populated-trust migration/reuse, byte-preserved CA/revocation sources, correct
alias/port matching, strict unknown/mismatch/revocation refusal, genuine new-target enrollment and
no enrollment of an existing platform host. Observe authentication offers and actual trust writes.

Combine that with config-isolation and I/O/lifetime evidence across supported workstations and
platform/inner-client boundaries. Old-stack tests, passing unit tests or a single Linux fixture
cannot prove the new carrier, joint contract, or complete cutover. Permanent operator guidance ships
with implemented behavior; proof results and unavailable cases are recorded in the plan.
