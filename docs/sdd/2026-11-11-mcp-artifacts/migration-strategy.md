# MCP artifacts: migration strategy

## Starting state

Snapshot: `834eb715666d2d8539874a042484bf9da60fdb00`, 2026-11-11 UTC. Artifact bundles and owner
groups have four maps: hints, rules, skills and agents. Capture codecs 2 and 3 are readable, with 3
used for writes. There is no canonical MCP artifact support. Native outer loops do not know a fifth
type and must not silently claim one handled. Existing Codex settings mappings may already contain
native `mcp_servers`; those values belong to the mapping/operator, not implicitly to this new
artifact mechanism.

Closeout PR 887 changes diagnostic detail and predecessor records. MCP can be designed against the
merged implementation while that reviewed closeout awaits its normal merge. Before implementation
handoff, refresh main and account for the actual closeout state. Do not amend its locked SDDs.

## Target and transition

This is additive operator-facing configuration: existing bundles continue to work unchanged, and MCP
is selected explicitly through a new map. Do not auto-import native config, migrate OAuth
credentials, infer a default bundle or activate an integration. Existing native settings retain
their current mapping strategy and removal behavior.

The internal format changes explicitly to capture version 4. Keep v2/v3 readers, original four-type
validation and original digest interpretation. A legacy capture decodes to an empty MCP map; it does
not gain fabricated input or handling evidence. Old envelope versions containing a new map are
malformed, not a format shortcut. No SQL migration is required for a new supported domain payload
version.

Preserve `declaration_digest` for effective bundles with no MCP entries, including inherited empty
new maps. Today that digest includes every type map, even empty ones; simply extending its loop
would incorrectly stale every selected bundle. Preserve existing content/input/native identities and
known-empty ancestor deferral results. New MCP declarations participate in freshness normally.

Every native outer/VM/session adapter and Shell renderer is updated in the same implementation
increment. Until an entry has a successful native application, it is explicitly deferred. Adding the
enum before adapter handling cannot land as a working partial release.

## Worked example

An existing user selects bundle `base` containing one rule. Its stored v3 capture, declaration hash,
user placement and handled routing remain valid after upgrade. The operator adds `mcp_servers.docs`
to `base`. Inspection identifies changed prepared input; it never acquires the source or repairs the
user. The normal user reinit captures a v4 group and adds a qualified native server entry. A later
removal retires only that unchanged owned entry. An edited obsolete entry stays in place with a
warning and retry evidence. Other user settings and OAuth state remain.

An existing workspace with no MCP remains usable without rebuilding. A workspace that needs new
workspace-scoped MCP registration follows the currently supported create/recreate workflow; this
effort does not invent workspace reinit. Session-only opt-in delivery uses genuine invocation
mechanisms and does not silently edit that workspace instead.

## Downgrade and conflict policy

An older binary cannot read v4 captures and may reject new config fields. Document that downgrade
limit; do not promise reverse conversion or delete unknown state. Normal supported cleanup should
run with the version that provisioned the entries before an operator elects to downgrade. The
existing unknown-state behavior remains intact.

Settings mappings and existing hand-written MCP entries remain visible foreign content unless an
identical desired registration is adopted. Derived names reduce collisions without claiming that
native endpoint deduplication or cross-harness discovery cannot occur. A conflicting explicit
mapping at a managed key is diagnosed before canonical mutation, with no new broad settings ban.

## Verification

Prove legacy v2/v3 round trips, malformed/future envelope refusal, old declaration/content hashes,
unchanged four-type placement, known-empty routing and existing-workspace startup. Exercise all
entry creation/adoption/update/removal/conflict/partial-progress paths and settings strategies.
Downgrade limits and native formatting changes belong in permanent operator teaching alongside the
implementation.
