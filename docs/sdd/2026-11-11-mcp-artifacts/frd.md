# MCP artifacts: functional requirements

## Status and authority

Draft requirements for the operator-directed MCP successor effort. The operator requested a single
review round containing the FRD, HLA, plan and supporting designs before implementation, followed by
delivery through the normal development process within three rounds. This document does not claim
design approval or shipped behavior. The effort lead owns the response and drafts these requirements
under that charter.

This builds on the merged artifact pipeline and harness facets. Their separate
[closeout PR 887](https://github.com/WayfarerLabs/agentworks/pull/887) has a clean integration
report; its locks become binding at merge. This effort does not reopen either predecessor
specification. The next separate effort is single-session workspace modes.

## Problem and outcome

Operators can distribute hints, rules, skills and agent personas through Agentworks, but MCP server
connections still require native configuration per harness. An MCP server definition describes how a
harness connects to a server. It is an artifact, not a new declared-resource kind, a server
installation, a running service, a secret store or a permission grant.

Operators should define reusable connections once, select them through ordinary artifact bundles at
the intended resource scope and let each activated harness integration apply or defer them. The
integration translates one canonical definition into its native registration without silently
weakening its meaning. Existing workload provisioning supplies executables and existing env/secret
configuration supplies credentials. The harness owns connections, stdio child processes and native
approval/authentication policy.

## Delivery phases

Phase 1 delivers registration with public literals and env-based credentials. Phase 2 delivers OAuth
support under this same SDD. The first push rejects OAuth configuration fields rather than silently
accepting unfinished behavior. Existing native OAuth behavior remains the harness's own. The
detailed OAuth design follows phase 1; its remaining work is tracked explicitly and prevents locking
the overall SDD after registration alone.

## Requirements

### R1. One additional artifact type

Add MCP server definitions alongside the four existing artifact types, using a top-level
`mcp_servers` name-keyed map in artifact bundles and normalized owner groups. Existing explicit
bundle selection and integration activation remain required. There is no default bundle or automatic
registration merely because a harness supports MCP.

### R2. Canonical authoring and acquisition

Support structured inline definitions and workstation/Git JSON or JSONC sources through existing
artifact acquisition. Use Rulesync's named `mcpServers` source document and shared field vocabulary
as prior art, with a strict documented Agentworks profile. Support stdio and Streamable HTTP. Reject
unknown or contradictory structure, duplicate keys, unsupported transports and unsupported policy
fields with an error identifying the declaration and field without exposing its value. Normalize
designated text to Unix line endings. Acquisition never executes a server, installs its
dependencies, logs in or connects to its endpoint.

### R3. Ordinary composition and provenance

Type maps merge by key with whole-entry replacement inside one owner, including inherited bundles
and selected bundle ordering. Preserve replacement diagnostics and original scope/resource
provenance. Different resource scopes remain separate groups even when they use the same name; there
is no per-producing-resource namespace inside a scope and no receiver-map flattening.

### R4. Credential references

Credential-bearing phase-1 values use references to variables supplied through the existing
consuming environment and secret mechanism. Preserve references through acquisition, propagation,
capture and applied-state records. Never resolve them into those records, inspection output, process
arguments or diagnostics. Native runtime interpolation/forwarding is the ordinary delivery path.
Outer setup preserves references without requiring the environment of an unknown future consumer; a
workspace definition can use a variable each actual user supplies later. An unrepresentable binding
is explicitly deferred. For session-local inputs, a missing variable in the actual managed-launch
environment warns and leaves that entry unhandled. Previously registered outer connections retain
the native client's runtime binding/error behavior, without a new Agentworks launch gate or probe.
Public literal values remain supported; operators must not author credentials as literal content.

### R5. Defining-scope placement and routing

Apply at the defining scope whenever the harness has a genuine native mechanism. Preserve the
existing independent-facet graph, original-owner deferrals and inactive-facet routing: inactive VM
to user; inactive user/workspace to session. No facet consults downstream activation. Unsupported
scope or native semantics are explicitly deferred. Terminal unhandled session entries warn and do
not block launch; stale upstream placement is advisory and never repaired by session start.

### R6. Honest native support

Deliver user and workspace registration for Claude Code, Codex and Grok where their documented
native mechanisms are verified. Native VM facets defer when no genuine VM registration exists. Shell
publishes canonical files and its ordinary index at every scope, without launching servers or
introducing deferrals. Session-only native registration is opt-in through `enabled_workarounds`,
preserves other sources, and must demonstrate isolation on start and resume before being advertised.
Publishing into a shared home/project catalog is not session isolation.

### R7. Independent managed entries

Managed native identities distinguish original owners and same-named definitions across scopes.
Prevent silent shadowing among managed entries. Preserve unrelated entries and native policy in
shared config documents. Adopt an identical unowned entry; skip a different unowned entry with a
specific warning. Native trust, endpoint deduplication, authentication and connectivity remain
separate from registration and must be disclosed rather than claimed as guarantees.

### R8. Idempotency and cleanup

Use ordinary instance state to track the smallest owned native unit, an MCP server entry or a
private file, including partial progress. Retry converges owned registrations. Removing a
declaration, activation or session retires unchanged owned effects wherever safely possible;
operator-modified obsolete entries are retained with actionable diagnostics and cleanup evidence. Do
not delete native OAuth credentials, foreign entries or shared documents wholesale during ordinary
artifact cleanup. Explicit OAuth logout belongs to its own authentication workflow. Artifact cleanup
adds no prerequisite to parent deletion or workspace rehome. Existing persistence, cancellation,
internal-failure and recoverable-facet-failure semantics remain intact.

### R9. Diagnostics and offline inspection

Extend existing per-type applying/deferring output and `agw artifact show` to explain MCP entries,
original owners, native registration identities/destinations and unhandled reasons. Keep the current
small-list convention. Do not display connection URLs, command arguments, env/header values or
artifact bodies. Inspection acquires no source, resolves no secret, runs no native MCP inventory
that might connect to a server, and repairs nothing. Report registration as registration, not proof
of tool discovery, successful authentication, connectivity or model use.

### R10. Existing installations and permanent teaching

Retain reading of supported four-type captures and preserve their identities, declaration freshness
and native effects when no MCP is selected. An empty new map must not force existing workspaces or
users to rebuild. Document new-state downgrade limits. Ship permanent artifact/integration guides,
module contracts, sample config, examples and schema/inspection collateral with implementation.
Demonstrate the actual supported native paths and independently verified test cleanup before closing
this effort.

### R11. OAuth in phase 2

Support MCP OAuth through native harness mechanisms in the subsequent phase of this SDD. Provide
explicit configuration for supported native OAuth options and an actionable authentication workflow
for the actual user, including connections declared at workspace scope. Native harnesses own token
storage, refresh and credential isolation; acquired artifact bodies and applied-state records never
become a token store. Agentworks authentication orchestration is an explicit operator activity, not
an automatic side effect of acquisition, reinit or session startup. Native client authentication
retains its own ordinary policy and interaction.

Settle the canonical auth fields and amount of Agentworks login orchestration in the phase-2 design
before implementing them. Preserve native auth intent and disclose unsupported combinations; do not
silently substitute bearer credentials, anonymous access or broader permissions. Phase 1 retains a
viable extension path without implementing a second OAuth client or credential service.

## Explicit exclusions

No MCP daemon hosting, dependency installation, endpoint probing during setup, tool-filter/approval
translation, trust-policy bypass, native config import, automatic feature emission, hook support,
new bundle distribution formats or compressed capture storage. Transport/SSH redesign and
single-session workspace modes remain separate efforts.

## Operator ruling, 2026-11-11

Asked whether to include MCP-specific tool filters and OAuth automation, the operator selected:

> Defer tool filters and OAuth automation; reject those fields explicitly

The phase-1 profile therefore rejects these fields instead of silently ignoring them. Native
approval and authentication continue independently under the harness's own configuration.

The operator then clarified the overall scope:

> Note that oauth should absolutely be part of the overall mcp sdd, just not in the initial push

OAuth is phase 2 of this effort, not an excluded follow-up SDD. The workflow question is whether
Agentworks should orchestrate native login or configure native OAuth with operator-run login;
neither choice transfers token ownership to Agentworks.

## Response

See the [HLA](hla.md), [format and delivery design](mcp-lld.md), [research](prior-art-research.md),
[migration strategy](migration-strategy.md), [acceptance matrix](acceptance.md) and [plan](plan.md).
