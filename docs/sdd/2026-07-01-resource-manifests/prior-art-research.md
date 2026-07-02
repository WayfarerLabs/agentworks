# Resource manifests: prior art research

Executive summary: the design borrows the Kubernetes object envelope for operator familiarity, the
`conf.d` auto-load convention for loading semantics (rather than Kubernetes' `apply`), and
multi-document YAML streams for by-kind file grouping. Nothing here required novel invention; the
research questions were which conventions to adopt, which to deliberately deviate from, and what the
Python tooling supports. Claims below were checked against the cited primary docs during drafting;
library/version specifics get re-verified at implementation time per the latest-stable-versions
rule.

## Findings by dimension

### 1. Object envelope (Kubernetes)

Kubernetes objects use `apiVersion` / `kind` / `metadata` / `spec`, with `metadata.name` as the
identity field (Kubernetes docs, "Objects In Kubernetes",
<https://kubernetes.io/docs/concepts/overview/working-with-objects/>). Names are unique per kind
(and namespace), matching the registry's existing `(kind, name)` identity model exactly.

- **Adopted**: the four-field envelope verbatim, including `apiVersion`'s camelCase, and `metadata`
  as the home of framework-uniform fields. Decision: FRD R3.
- **Deviated**: `kind` values are the registry identifiers in lower-kebab (`vm-template`, not the
  PascalCase `VmTemplate`), per the project's own snake-keys / kebab-values convention; the
  vocabulary stays one canonical set so no mapping layer is needed. Decision: FRD R9, HLA
  "Kubernetes envelope, agentworks vocabulary".
- **Deferred, room left**: `metadata.labels` / `metadata.annotations`, `status` subresource
  (relevant only if drift tracking ever wants it).

### 2. Loading model: apply vs auto-load

`kubectl apply -f <dir>` submits manifests to a server that persists desired state and reconciles
continuously (Kubernetes docs, "Declarative Management of Kubernetes Objects"). That model earns its
complexity from held state; agentworks holds no registry state between invocations. The closer prior
art is the Unix `conf.d` convention: drop files in a directory, loaded on every start in sorted
order (systemd.unit drop-in directories, man systemd.unit; apt's `/etc/apt/sources.list.d/`; nginx
`include conf.d/*.conf`). Sorted-filename ordering as the determinism guarantee is the established
convention there.

- **Adopted**: auto-load everything under `resources/` recursively, lexicographic path order,
  dotfile skip. Decision: FRD R2, HLA "Auto-load, not apply".
- **Rejected**: an `agw apply` verb. Revisit only if a future SDD introduces persisted desired
  state.

### 3. Multi-document files and file organization

YAML streams (`---` separators) are core spec (yaml.org spec, "Documents") and the standard way
Kubernetes ecosystems group related objects in one file; Helm renders many objects into per-source
multi-doc files, and kustomize consumes both layouts interchangeably. Community practice treats file
layout as user preference, not semantics.

- **Adopted**: multi-doc support in the loader; layout carries no meaning; by-kind grouping as the
  migration tool's default output. Decisions: FRD R2/R10, HLA "By-kind migration output".

### 4. Python tooling

- **PyYAML**: `SafeLoader` parses streams and exposes per-node `start_mark` (line/column) via the
  node-level API; safe loading is the documented default recommendation (pyyaml docs,
  <https://pyyaml.org/wiki/PyYAMLDocumentation>).
- **ruamel.yaml**: round-trip mode preserves comments and exposes `lc` position info; heavier API.
  Fallback if PyYAML mark plumbing proves awkward. We do not need comment preservation on the load
  path (manifests are read-only to the app), which removes ruamel's main advantage.
- **tomlkit**: round-trip TOML with comment and whitespace preservation
  (<https://github.com/python-poetry/tomlkit>), which stdlib `tomllib` cannot do; needed only by the
  migration tool's `config.toml` rewrite. The prior SDD already established that no TOML library
  surfaces section line numbers usefully; that concern disappears with YAML on the load path.

Decisions: HLA "YAML library" bullet and migration tool section. Exact versions pinned at
implementation.

### 5. Capability vs instance split

The provider/backend split follows a widely-used two-layer pattern: a named code capability plus
named configured instances referencing it. Nearest analogues: Kubernetes `StorageClass.provisioner`
(many classes per provisioner, each with parameters), Terraform provider blocks with aliased
configurations. The design predates this SDD in-repo: the resource-registry plan's plugin-SDD prep
notes (its follow-ups section) already sketched providers-as-code plus
backends-as-registry-instances.

- **Adopted**: `secret-backend.spec.provider` naming a code capability; multiple backends per
  provider; provider validates instance config. Decision: FRD R8.
- **Deviated from the in-repo note**: providers are mirrored into the registry as read-only
  descriptor rows (the note left this open, flagging the inconsistency with
  `git-credential-provider`); this SDD resolves it toward registry citizenship for uniform reference
  validation and inspectability.

## Refuted / do-not-rely-on

- "Kubernetes familiarity requires PascalCase kinds": rejected; the envelope shape carries the
  familiarity, and a second kind casing would need a permanent mapping layer.
- "tomlkit can capture source line numbers for the loader": already investigated and refuted in the
  resource-registry SDD (its HLA, "Regex section-line scanner" decision); irrelevant now for
  loading, still fine for rewriting.
- "Auto-load needs file-watching to feel declarative": no; per-invocation rebuild is already the
  system's model and matches conf.d precedent.

## Open questions not resolved by research

- Whether PyYAML's stream API surfaces document start marks cleanly enough, or whether the loader
  parses per-document via `compose_all` to get node marks (LLD question; both are documented APIs).
- Long-term envelope versioning policy (when, if ever, `agentworks/v2` becomes necessary and what
  compatibility it promises). Out of scope until a breaking schema need exists.

## Sources

| Source                                                                | Quality                     | Angle                     |
| --------------------------------------------------------------------- | --------------------------- | ------------------------- |
| kubernetes.io object/declarative-management docs                      | high (primary)              | envelope, apply model     |
| YAML 1.2 spec (yaml.org)                                              | high (primary)              | multi-document streams    |
| man systemd.unit; Debian apt sources.list(5); nginx include docs      | high (primary)              | conf.d auto-load order    |
| pyyaml.org documentation                                              | high (primary)              | safe loading, marks       |
| ruamel.yaml docs (yaml.readthedocs.io)                                | medium (primary, sprawling) | round-trip alternative    |
| python-poetry/tomlkit README                                          | high (primary)              | comment-preserving TOML   |
| Kubernetes StorageClass docs; Terraform provider-alias docs           | high (primary)              | capability/instance split |
| docs/sdd/2026-06-17-resource-registry (plan.md plugin-SDD prep notes) | high (in-repo)              | provider/backend design   |
