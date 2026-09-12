# Harness Facet Migration

- Status: Implemented locally; live migration acceptance pending
- Baseline: `3641ea8c`, 2026-09-07
- Governing contracts: [FRD](frd.md), [HLA](hla.md), [plan](plan.md)

## Baseline surfaces

Four first-party integrations currently expose session config through one harness capability. Two
core setup callers provision Claude marketplaces/plugins: VM admin Phase B and agent initialization.
AdminConfig and AgentTemplate carry `claude_marketplaces` and `claude_plugins`; resolved agent
config repeats these fields. The existing helper warns on failure and records no native ownership.

Session resolution silently defaults missing effective selections to shell. Existing session rows
store their selected template name, not a frozen integration selection. Their instance overlay is
stored separately; conversation-state namespaces are not authoritative selection.

Desired instance overlays are versioned JSON in the existing instance-state store. VM payloads have
separate VM/admin components; agent payloads carry their own declaration. Runtime field removal must
not accidentally classify known legacy fields as unknown future data or discard them.

## Declarative cutover

Replace old fields with one explicitly selected user activation:

```yaml
# Previous admin-template or agent-template spec fields
claude_marketplaces: [example-org/team]
claude_plugins: [reviewer@team]
```

```yaml
# New fields on the same declaring resource
harness_integrations:
  - name: claude-code
    marketplaces: [example-org/team]
    plugins: [reviewer@team]
```

The list belongs to the resource. Omission inherits where that template kind already supports
inheritance; an authored list replaces the whole inherited list, and `[]` clears it. Converting a
layered old setup therefore requires constructing the intended complete activation list, including
other integrations and the effective Claude values. Do not translate a single old field into a
partial replacement list that silently loses the other values.

Update shipped manifests and examples with the implementation. Old authored fields receive normal
unknown-field diagnostics plus specific migration guidance. Do not keep two live runtime dispatch
paths. Admin activations reside on the selected admin-template, whose user schema is the same as
agent-template; no new admin selection on VM templates is introduced.

## Persisted desired overlays

Retain decoding for supported old payload versions at the persisted-data boundary, separate from new
operator-authored input validation. A legacy adapter extracts the old Claude fields before the new
declaration model rejects them, validates their original value types, and resolves them with the
owning template context before building the new full activation list. Invalid or ambiguous old/new
declarations refuse with field-only diagnostics and preserve the stored payload.

A migrated effective list captures the intended current configuration under the new whole-list
semantics; it cannot pretend to preserve old per-field list inheritance indefinitely. Document this
change and the resulting complete list. The owning reinit operation persists the new canonical
overlay only through its normal desired-state checkpoint; inspection can explain a legacy overlay
without mutating it. Unrelated fields and VM/admin components are retained. A migration failure must
not overwrite the original record, and unknown future payload versions remain unsupported rather
than being guessed into this migration.

The finite adapter covers marketplace-only and plugin-only overlays, empty legacy lists, templates
with other integrations, and old/new collisions. Empty legacy lists append nothing; they do not
clear inherited entries. Conversion captures the complete effective list, including unrelated
integrations, under the new replacement semantics. Reinit serializes desired-state changes before
reading the context and commits automatic conversion only after successful setup. Record-only
inspection reports migration pending when template context is unavailable.

## Native ownership

The old installer provides no ownership records. A native installation matching old config is
therefore not automatically owned by Agentworks. Before managing it, report the native identifier
and destination conflict. The supported initial remediation is explicit operator removal using the
native CLI, followed by owning reinit to provision and record it. No automatic adoption or general
force flag is introduced by this migration.

Once new applied-state records exist, removal of an entry or whole activation can safely reconcile
recorded claims. Settings mapping policy controls explicit replacement/merge of its declared file;
it does not claim other plugin installations. Removing a settings mapping retains the current
document, as specified by R15. Settings mappings do not create ownership claims.

## Explicit session selection

Give synthesized `session-template/default` an explicit `harness_integration: {name: shell}` block.
Keep ordinary default shell behavior through that declaration. Custom template lineages must have an
effective selection, authored locally or inherited; absence becomes an actionable configuration
error in finalize and dictionary resolution.

Existing sessions using the synthesized default remain shell sessions. Existing sessions using a
custom silent lineage must add a selection to that template or a parent before start/restart. An
instance-specific correction uses explicit recreation with `session create --spec`; no new
start/restart overlay replacement flag is implied. Preserve current fresh/resume launch semantics
and refuse invalid selection before tearing down an existing runtime.

## Order and evidence

Complete the internal facet selector/schema plumbing first. Expose integration activation fields on
setup resources only when their owning lifecycle invokes them. Cut over the two Claude callers and
legacy fields alongside native reconciliation, persisted-overlay handling and updated collateral. Do
not claim migration complete until the real CLI has exercised existing stored overlays and native
conflicts, as well as newly created resources.

Database automatic backups retain their existing safeguards. Native applied-state record
export/round-trip and database restore tests are required; this effort introduces no VM restore
workflow. Preserve resource-bound invocation independence so the artifact successor can extend it
without depending on downstream instance discovery.
