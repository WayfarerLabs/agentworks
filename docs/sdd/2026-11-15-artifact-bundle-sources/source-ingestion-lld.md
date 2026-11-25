# Artifact source ingestion

## Status and boundaries

Technical response to [R1-R6](frd.md). The import, migration and Agent Plugins profile clauses below
remain proposals until the operator answers the existing held choices. This document does not claim
implemented behavior or start an additional approval workflow.

Keep one acquisition operation and the existing normalized maps. Collection ingestion belongs to
bundles. Direct owner declarations remain explicit maps. Integration facets receive ordinary
artifact inputs; they do not parse source selectors, Git definitions or external manifests. Later
feature emitters can enter that normalized pipeline independently of collection ingestion.

## Declaration shape

Add one nullable `source` field to `ArtifactBundle`. Its entire value uses replacement merge
semantics: omission inherits, a value replaces the inherited selector, and null clears it. Existing
explicit maps retain their existing complete-entry replacement behavior. A bundle with a source must
participate in capture even when its explicit maps are empty.

Use a discriminated selector with these proposed fields:

| Field           | Values and meaning                                              |
| --------------- | --------------------------------------------------------------- |
| `format`        | `directory`, `definition`, or `agent-plugins`                   |
| `location`      | One address in the existing workstation/Git source grammar      |
| `artifact_type` | An optional existing artifact type, valid only with `directory` |

A directory without `artifact_type` selects the mixed layout from the HLA. One with an artifact type
selects only that type's discovery profile. Other formats do not accept that field. Reject unknown
fields and invalid combinations. Do not infer a format from arbitrary neighboring files.

```yaml
spec:
  source:
    format: directory
    location: git::https://github.com/example/agent-content.git//content?ref=main
```

```yaml
spec:
  source:
    format: directory
    location: file::~/agent-content/skills
    artifact_type: skill
```

Definition and Agent Plugins selectors use the same shape with their respective format and location.
An imported definition is one ordinary artifact-bundle manifest. Parse its envelope without
publishing it to the registry. Reject additional documents or another resource kind. Its local
wrapper supplies the bundle identity; imported names never become artifact prefixes.

Guard external definition parsing before constructing YAML values: reject aliases, anchors,
duplicate keys and excessive structural depth or node count within the operation's existing bounds.
Reuse the ordinary envelope/schema validation after that guard. Translate parser, envelope and model
errors at this boundary into value-free diagnostics identifying the wrapper and safe field or line;
do not forward source excerpts, rejected values or underlying exception chains. This is an imported
content boundary, not a redesign of the workstation manifest loader.

Under the proposed initial import contract, imported content may contain the five maps and at most
one directory profile, including the accepted Agent Plugins directory profile. Empty inherited
defaults have no effect; nonempty registry inheritance and a further bundle-definition import are
refused. Composition of independent collections uses the existing ordered bundle selection rather
than another collection list inside a selector.

## Reference projection

Preserve authored registry rows and their source strings. Apply one artifact-specific projection to
copies of incoming declarations before the existing inheritance reducer merges them. Reuse that
projection for bundles and the existing template folds covering VM, admin, agent, workspace and
session declarations. This is a transform within one fold, not another inheritance pass.

The projection touches artifact `source` fields and selector `location` only. It does not rewrite
instruction text, MCP commands, arguments, environment values, headers or native options.

- A durable operator document supplies its own containing directory, including for inherited values.
- Relative workstation addresses are normalized lexically against that directory. Explicit absolute,
  home-based and independent Git addresses retain their explicit meaning. Relative locators must
  never acquire a hidden command-cwd dependency.
- A Git definition supplies repository, requested ref and its document directory. Relative members
  are joined lexically, checked against the repository root and rendered through the ordinary Git
  address form. Reuse the requested-ref cache key whose commit the operation already resolved.
- The proposed remote contract prohibits workstation absolute/home reads and additional
  repositories. Explicit same-origin references must remain within the same pinned source identity;
  unsupported independent references fail before any additional acquisition.
- A source location used for diagnostics is not proof of a durable operator base. Synthesized and
  temporarily extracted package declarations must not persist an extraction path as a source.
- Stored source overrides without a declaring file follow the accepted migration policy. No
  write-time cwd is stored today, so a historical base cannot be reconstructed from applied file
  placements.

The proposed migration is uniform file-relative interpretation for file-authored declarations, and
explicit absolute/home/Git references where a durable base is absent. Diagnose ambiguous stored
relative sources with their owner and field, without clearing applied state. Keep authored spelling
available through the original declaration and preserve ordinary contributor provenance. Do not
create a legacy mode or silently fall back to cwd.

Offline resolution and declaration hashing may perform this lexical work. They must not stat source
paths, fetch Git, load an imported body or enumerate a collection.

## Acquisition and absence

Pass the existing `PackageCapture` operation through definition reads, directory expansion and
explicit entries. Reuse its repository/ref cache and member, byte, depth, storage and time budgets.
Imported bodies and selected neighbors count toward those budgets. Do not add a second acquisition
cache or a checkout-based Git reader.

The new collection path needs bounded root-kind metadata and a precise absence result at the reader
boundary. An explicitly selected missing or wrong-kind root is an error. Only proven absence of an
optional fixed child yields an empty contribution. Permission, transport, unsafe-path,
changed-source and parse failures must retain their failures. A generic acquisition exception cannot
mean missing.

Use metadata only to validate the explicit root and select known locations. Capture each selected
type directory once and discover from its immutable captured members. Do not enumerate candidates
and then capture them one by one or add a candidate-list race protocol. Existing local two-pass
checks remain authoritative per selected directory; no atomic snapshot across separate directories
is promised.

For a mixed root, select `hints/`, `rules/`, `skills/`, `agents/` and the supported MCP file
locations. Never capture the whole repository before filtering. For the single MCP type profile, use
the same `mcp.json`/`mcp.jsonc` locations inside the selected directory. Both files present is an
ambiguity error. A selected valid empty directory is a known empty collection.

Fixed profile child locations use exact documented case on workstation and Git sources. Reject a
case-only alias instead of allowing a case-insensitive filesystem to select different content or
report it absent. Bound any child-name metadata needed for this check; it does not become a second
candidate enumeration or content-capture pass.

## Discovery and normalization

Apply the HLA's documented candidate patterns to captured members. Ignore ordinary non-artifact
neighbors as artifacts while retaining their acquisition cost. Links and submodules in a selected
type subtree remain capture failures, even when the affected member would not become an artifact. Do
not loosen member-path checks to accommodate a repository root.

Skill roots are immediate child directories with a `SKILL.md`. Partition each skill's complete
captured member set, retain executable bits and byte-preservation policy, then use existing
canonical validation. Other skill directories are not recursively discovered from supporting assets.
Text and persona candidates use the documented immediate-file patterns. Validate selected names,
required metadata and key/name agreement with the same validators as explicit declarations.

MCP discovery reads every selected entry from the ordinary duplicate-safe MCP envelope and reuses
the canonical definition validator. Validate each discovered definition, not only one requested key.
Use deterministic member/key order. Duplicate generated keys are ambiguity errors; traversal order
is not an override policy.

Bound expanded entries as they are emitted, using the existing operation member limit, including
entries later replaced. A single MCP document can contain many entries; its file count and the final
winning group's codec limit alone do not bound discovery or imported-map expansion.

Perform composition in this order:

1. Directory discoveries.
2. An imported definition's explicit maps, when present.
3. The importing bundle's effective explicit maps.
4. Later selected bundles, in their existing order.
5. Owner direct declarations.

Each replacement affects one artifact type/key at its owner. Reuse current changed-content warnings
and replacement provenance. A parent's effective explicit entry still overrides discovery when a
child replaces the selector; clearing the selector does not clear those explicit entries. No new
resource namespace, native prefix or same-name cross-scope policy is added.

## Capture evidence and compatibility

Winning artifact records cannot describe a known-empty or wholly shadowed collection. Record a
bounded core capture-evidence list alongside the normalized group, with one record per effective
selected bundle source. Include:

- The local wrapper bundle and effective selector identity.
- Separate existing `ArtifactProvenance` values for the imported definition and the selected
  collection when present, each retaining its address and resolved Git commit where applicable.
- Definition/manifest digests: at most one imported bundle document and one external profile
  manifest.
- Discovered and imported-explicit artifact type/key membership before wrapper and later-bundle
  overrides. Keep their declaring-document association even for inline entries.
- The recognized external profile version where applicable.

Do not print imported bodies or connection values during inspection. An imported document's digest
and source provenance provide its identity; do not add its body or floating revision to offline
hashes. A capture must retain evidence even when it contributes no winning artifacts. In particular,
a workstation definition selecting an empty Git collection must retain that collection's commit
independently of the definition's own provenance. Keep these facts in one record per wrapper rather
than introducing a source graph.

Keep the normalized artifact-group representation independent of this evidence. Extend the core
capture envelope with a new supported version when the evidence is present; keep existing v2-v4
reads and unchanged explicit-only captures. The implementation must identify envelope version and
embedded group-codec version separately rather than accidentally changing every existing receipt. No
database table or native runtime protocol needs another representation of source selectors.

Known selectors and lexical bases participate in declaration hashing. Omit an unset selector so
unchanged explicit-only declarations keep their existing fingerprints. Collection evidence is not an
input to native preparation or a new ancestor-freshness gate. Native delivery identities continue to
derive from the same artifact type, name, owner and content.

Validate the complete normalized group and its capture evidence before replacing the owner's saved
capture. Any failure retains the prior capture as historical evidence and publishes no partial new
capture. Existing file/registration ownership continues to govern cleanup.

## Inspection and refresh

Inspection reads current effective selectors and saved evidence only. With no matching capture,
report unknown membership. Do not report an empty collection or infer that a captured member was
removed merely because it is absent from today's explicit maps. A changed selector can be explained
without claiming the current source contents are known. Show known-empty membership explicitly.

Current VM/admin, agent, workspace and session operations retain their existing capture boundaries.
Descendant setup does not refresh ancestor sources, and this effort adds no workspace reinit.
Changes and removals converge through existing owner capture, routing and applied-state cleanup.

## Agent Plugins profile

The proposed first profile imports portable skills and compatible Streamable HTTP registrations from
[published Agent Plugins 1.0](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.0.0.md).
Read and validate the required root manifest before selecting fixed supported component locations.
Recognize supported schema identifiers locally; loading must not retrieve schemas from their URLs.

Map supported portable data to the same skill and MCP models. Preserve literal HTTP values and
reject unsupported transport or runtime semantics with safe diagnostics. Do not silently turn a
package-relative stdio server into a PATH-only command. Plugin cwd, installed package files and
persistent plugin data require a separately accepted execution lifecycle.

Validate the external profile before canonical mapping, including its schema identifier, JSON
structure and stricter URL rules: reject fragments and non-loopback cleartext HTTP. Reject header
spellings that the canonical/native pipeline would interpret as environment references, including
`${NAME}` and `Authorization: Bearer ${NAME}`. They are literals in the imported format and cannot
become authority to read a target environment variable. Keep ordinary explicit MCP reference
semantics unchanged; do not add a renderer mode to support these imported literals.

This is an ingestion profile, not native plugin installation or a full client-conformance claim.
Unknown extension content is not inferred to be rules, hints, agents or executable hooks. There is
no version polling, OAuth automation or imported package execution during capture or inspection.

## Verification

Use local fixture repositories and temporary workstation sources for deterministic discovery and
failure tests. Real owning setup and native placement acceptance remains in the integration lane.

- Local/Git parity, add/change/remove, known-empty and fully shadowed membership.
- A local definition selecting an empty Git collection retains its commit during offline inspection;
  an imported inline entry overridden inline retains its declaring-document membership.
- Parent and child declarations in different files, changed cwd, direct maps, selector inheritance,
  whole replacement/null clearing and stored references without a declaring file under the accepted
  migration.
- A moving branch after the first read, proving definitions and members use one resolved commit.
- Missing optional children versus missing/wrong-kind roots, permission/acquisition failures,
  symlinks/submodules, ambiguous MCP locations, malformed names/content and shared budget
  exhaustion.
- Canonical output equivalence to explicit declarations, including line endings, executable files,
  replacement provenance, ordinary stdio declarations and unchanged native names.
- Old capture reads, explicit-only fingerprint preservation, new evidence round trips, failed
  capture retaining state, and inspection that cannot access a source reader or reveal
  content/credentials.
- External format fixtures for supported and rejected semantics. Preserve normal native approval,
  optional-artifact handling and cleanup behavior.
- Imported YAML expansion/nesting and duplicate-key rejection with value-free failures; case-only
  fixed-location aliases; expanded MCP entries exceeding limits even when later overridden.
- Accepted literal HTTP headers survive native rendering without environment lookup;
  reference-shaped literals and URLs outside the external profile fail before normalization.
