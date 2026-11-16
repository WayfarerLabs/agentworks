# Artifact bundle sources: functional requirements

## Status and purpose

Draft for operator review. The operator requested directory discovery, local/Git source parity,
remote bundle definitions and references resolved relative to their bundle. This proposal also
contains the previously requested Agent Plugins import. It does not claim approval of the additional
design choices below or shipped behavior. Implementation follows the direct-declaration effort in
[PR 908](https://github.com/WayfarerLabs/agentworks/pull/908), then approval of this design.

Operators should maintain an artifact collection in one place, reference it once and pick up newly
added artifacts during ordinary supported setup without editing an inventory of artifact names. An
imported bundle remains an ordinary named `artifact-bundle`, selected explicitly at an owning scope.
The existing normalized artifact pipeline remains the delivery boundary.

## Requirements

### R1. Discover directories

A bundle can select a directory of one artifact type instead of enumerating its entries. It can also
select a directory with a documented layout covering multiple supported types. Both forms work with
workstation paths and Git repository/subdirectory references. Explicit entries remain useful for
inline content, selective inclusion and overrides.

Discovery has documented depth, accepted files, name derivation and invalid-entry behavior. A newly
added valid artifact is included at the next owning capture without changing the consuming
declaration. Removing it participates in ordinary state-tracked cleanup. There is no filesystem
watcher, continuous Git polling or implied workspace reinitialization.

Select candidates before content capture. Ordinary non-artifact neighbors are ignored; malformed
selected artifacts fail validation. Mixed-directory selection reuses the per-type discovery path.
The HLA names the supported owning refresh operations and their effects.

### R2. Import bundle definitions

A local named bundle can reference a bundle definition on the workstation or in Git. The remote
definition can itself select directories, so moving an enumerated manifest into Git is not the only
supported solution. Loading a definition does not register other resource kinds, run setup or enable
integrations. Source selection remains explicit; there is no automatically inherited default bundle.

### R3. Resolve from the declaring bundle

Relative artifact and directory references resolve against the bundle definition that declared them.
They do not change meaning with the command's working directory, importing wrapper or child bundle.
A Git definition's relative references stay in its repository at the same resolved commit. They
never accidentally select a workstation path. Workstation-authored declarations retain explicit
independent Git sources. Whether Git-imported definitions may introduce additional repositories
using workstation Git authentication remains part of the open import/trust contract.

Artifact references in owner templates must use the same relative-path meaning as bundle entries.
Collection ingestion remains a bundle facility; direct owner declarations remain explicit maps. The
design must define containment, definition-import depth and treatment of existing local/stored
relative paths before implementation. Diagnostics identify the declaration without exposing
credentials or artifact bodies.

### R4. Preserve composition and delivery

Imported content becomes the existing maps for hints, rules, standard Agent Skills, agents (agent
personas) and MCP servers. Existing canonical validation and Unix line-ending normalization apply;
opaque supporting files retain their existing treatment. Sources do not introduce a new artifact
type, resource namespace, native-name prefix or scope.

Operators can intentionally override imported entries with explicit entries and later selected
bundles, preserving whole-entry replacement and original-owner provenance. The same name at
different scopes remains distinct. Imported names and collisions must be deterministic and visible.
No runtime integration should need to understand Git, discovery or bundle documents.

### R5. Bound acquisition and preserve provenance

Capture records the selected source, imported-definition provenance, resolved Git commit and
discovered member provenance. One operation reads each repository/ref consistently and shares
acquisition budgets across definition reads, discovery and explicit entries. Reject unsupported
definition nesting; if nested definitions are accepted, reject their cycles. Invalid selected names,
path escapes, ambiguous portable paths and exceeded limits fail clearly. A failed capture does not
publish a partial new owner capture or silently substitute old content.

Read-only artifact inspection explains captured source and selection without fetching Git, walking
the source filesystem, exposing connection values or changing state. Floating refs update only at
supported owning capture; pinned commits remain reproducible. Imported bodies and inventory are
captured evidence, not inputs to offline declaration hashing. Unknown inventory must not be
presented as empty or as removed explicit entries.

### R6. Support an honest Agent Plugins import

Accept a package source using a documented Agent Plugins import profile, reusing acquisition and
discovery. Translate supported components to the ordinary maps. Do not claim full client conformance
or silently discard semantics the normalized pipeline cannot represent. The initial supported
profile, including package-relative MCP execution, must be settled before implementation.

## Acceptance examples

- Point a bundle at local `skills/`, add another skill directory and capture again: both skills
  appear without editing the declaration. The same layout at a Git source behaves identically.
- Point a bundle at a mixed directory with rules, hints, skills, agents and MCP definitions:
  supported content is discovered with stable names and existing validation.
- Import `team/bundle.yaml` from a repository. Its `./skills/review` reference resolves next to that
  document at the same commit, even when the CLI runs from a different directory.
- Override one imported rule locally. Only that type/key is replaced; inspection shows its
  provenance. Removing a discovered skill cleans up the previously managed placement through the
  existing lifecycle.
- Change a Git branch after its commit is resolved during capture: that operation never combines the
  old definition with new member content. Inspection of the result performs no acquisition.

## Boundaries and decisions still needed

This effort adds ingestion, not native plugin installation, a package registry, hooks, OAuth
automation, feature execution or session/workspace isolation. Existing native approval policy,
artifact routing, optional-artifact warnings and cleanup rules remain authoritative.

Two decisions are open in the [HLA](hla.md#decisions-before-implementation): the containment and
inheritance contract for imported definitions, and the first Agent Plugins MCP profile. These are
requirements questions for the operator; this draft does not silently settle them.
