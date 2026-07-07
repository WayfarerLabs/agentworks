# Resource manifests: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

The resource-registry SDD (`docs/sdd/2026-06-17-resource-registry`, locked 2026-06-30) established
the two-layer model: a parsing layer composes Resources and publishes them into a publisher-agnostic
`Registry`, which finalizes (auto-declares, attaches references, detects cycles) and freezes. Its
HLA explicitly reserved the next step: swap the TOML parsing layer for per-resource manifests
without touching the framework.

This SDD takes that step. Operator-declared resources move out of `config.toml` into any number of
auto-loaded YAML manifest files using a Kubernetes-style envelope. The motivations:

1. **UX.** Many small files are easier to find, manage, share, and review than one sprawling TOML
   file. YAML's native nesting removes the TOML workarounds where one conceptual resource is split
   across sections (`[secrets.foo]` plus `backend_mappings` dot-keys, `[session_templates.baz]` plus
   `[session_templates.baz.env]`, `[admin.config]` plus `[admin.env]`). A resource becomes one
   document.
2. **Config is not resources.** Machine and identity settings (SSH keys, paths, platform connection
   settings, CLI defaults) are not named, referenceable entities. They stay in TOML. The split makes
   each file's job obvious: `config.toml` configures the operator's install; manifests declare
   resources.
3. **Bring-your-own resources.** With resources as data, the app itself can ship built-in resources
   through the same mechanism (built-in secret backends, and eventually catalog entries), and future
   plugins can do the same. This SDD lays the origin taxonomy and the built-in-manifest mechanism;
   the plugin system itself is a future SDD.
4. **Capabilities are code, not resources.** Secret backends, VM provisioners, and git credential
   providers have associated code. They are not manifest-declarable; they are mirrored into the
   registry as read-only descriptor rows, and resources reference them directly (R8; revised
   2026-07-07 -- the original provider/backend split for secrets was collapsed, see the plan's
   sequencing note).

The move is dual-path (revised 2026-07-03 from the original hard cutover): YAML manifests and TOML
resource sections are both fully supported publishers into the one registry, mixing included; TOML
resource sections are deprecated with load-time warnings, and a migration tool
(`agw resource migrate`, R10) moves resources from TOML to YAML on the operator's schedule --
wholesale or incrementally, a recurring mover rather than a one-time conversion (resource sections
become manifests, config sections stay).

### Scope

In scope: the manifest loader and envelope schema, the config/resource split of the current TOML
surface, built-in resource manifests, the origin taxonomy cleanup, the secret-backend capability
descriptor rows (R8, revised), the git-credential provider field alignment, the migration tool, and
the deprecation of the TOML resource surface (its removal is deferred to a future major release).

Out of scope: the plugin system (system and external plugins), apply-style reconciliation against
provisioned state, lifecycle resources (VMs, agents, sessions, consoles stay in the DB), and moving
config itself to YAML.

## Terminology

- **Manifest**: a YAML file containing one or more resource documents. Manifests are auto-loaded
  from the resources directory; there is no `apply` step.
- **Document**: one YAML document (`---`-separated) inside a manifest. Each document declares
  exactly one resource.
- **Envelope**: the Kubernetes-style outer structure of a document: `apiVersion`, `kind`, `metadata`
  (framework-uniform fields), `spec` (kind-specific fields).
- **Config**: operator machine and identity settings that are not named, referenceable entities.
  Stays in `config.toml`.
- **Resource, reference, registry, origin, miss policy**: as defined by the resource-registry SDD.
  This SDD changes where operator-declared resources come from, not what they are.
- **Capability**: a resource whose implementation is registered code (a git credential provider, a
  VM provisioner, a secret backend), provided by the app or, later, a plugin. Capability kinds are
  not manifest-declarable; the rows are read-only, and references to them validate uniformly through
  the registry. (Definition expanded 2026-07-07: an earlier revision held capabilities apart as "not
  resources, mirrored in as descriptor rows" -- prose-only ontology that confused every reader of
  `agw resource list`.)
- **Backend**: the secret-domain capability -- the code that produces secret values (`env-var`,
  `prompt`; later `onepassword`, ...). A capability resource of kind `secret-backend`, named by
  `[secret_config].backends` (the chain) and by `backend_mappings` keys on secrets. (Revised
  2026-07-07: originally "backend" named a declarable instantiation of a "secret-provider"
  capability; the collapse made the capability itself the backend.)
- **Provider**: the conventional name for a capability-reference field on a resource
  (`git-credential.spec.provider` naming `github`/`azdo`), and the generic cross-domain word for
  capabilities in pattern prose (ADR 0016's naming rule).
- **Built-in resource**: a resource shipped with the app itself, published from app-bundled
  manifests or code publishers. Origin variant `built-in`.

## Requirements

### R1: Config/resource split of the current TOML surface

Every section of today's `config.toml` gets exactly one destination:

| Current TOML section                      | Destination                                         | Notes                                                                                                          |
| ----------------------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `[operator]`                              | config (TOML)                                       | SSH keys, host prefixes                                                                                        |
| `[paths]`                                 | config (TOML)                                       |                                                                                                                |
| `[defaults]`                              | config (TOML)                                       | CLI flag defaults                                                                                              |
| `[azure]`, `[proxmox]`                    | config (TOML)                                       | provisioner capability settings; plugin SDD may revisit                                                        |
| `[session.config]`                        | config (TOML)                                       | non-template session settings                                                                                  |
| `[secret_config]`                         | config (TOML)                                       | active backend chain; a setting, never published -- validated against the registry at the composition boundary |
| `[secrets.<name>]`                        | manifest (`secret`)                                 |                                                                                                                |
| `[secret_backends.<kind>]`                | dropped by the migrator (with a note)               | semantically empty; the kind is a capability descriptor, not declarable                                        |
| `[git_credentials.<name>]`                | manifest (`git-credential`)                         | `type` renamed to `provider` per R9                                                                            |
| `[vm_templates.<name>]` (+ `.env`)        | manifest (`vm-template`)                            |                                                                                                                |
| `[agent_templates.<name>]` (+ `.env`)     | manifest (`agent-template`)                         |                                                                                                                |
| `[workspace_templates.<name>]` (+ `.env`) | manifest (`workspace-template`)                     |                                                                                                                |
| `[session_templates.<name>]` (+ `.env`)   | manifest (`session-template`)                       |                                                                                                                |
| `[admin.config]`, `[admin.env]`, ...      | manifest (`admin-template`, name `default`)         | flattened into one document                                                                                    |
| `[named_console]`                         | manifest (`named-console-template`, name `default`) |                                                                                                                |
| `[apt_sources.<name>]`                    | manifest (`apt-source`)                             | operator catalog extension                                                                                     |
| `[apt_packages.<name>]`                   | manifest (`apt-package`)                            | operator catalog extension                                                                                     |
| `[system_install_commands.<name>]`        | manifest (`system-install-command`)                 | operator catalog extension                                                                                     |
| `[user_install_commands.<name>]`          | manifest (`user-install-command`)                   | operator catalog extension                                                                                     |

TOML resource sections keep loading, with a per-section deprecation warning pointing at
`agw resource migrate` (R11). They become load errors only in a future major release.

Config sections that reference resources by name (`[secret_config].backends` referencing backend
names) keep doing so; the owning subsystem validates the names against the finalized registry at the
composition boundary (`build_registry`), with config vocabulary in the errors. Settings do not
become pseudo-resources.

### R2: Manifest directory and auto-loading

- Manifests live in the **resources directory**: `<config-dir>/resources/` (default
  `~/.config/agentworks/resources/`, sibling to `config.toml`).
- Every CLI invocation loads all files matching `**/*.yaml` and `**/*.yml` under the resources
  directory, recursively. Subdirectory structure is operator-organizational only; it carries no
  semantics.
- Dotfiles and dot-directories (`.git/`, `.backup.yaml`) are skipped.
- Load order is deterministic: per directory, files sort by name and precede subdirectories (which
  sort by name and recurse the same way); documents within a file load in file order. This order
  defines "config-load order" wherever the framework depends on it (first-matching-reference origin
  attribution, auto-declare extras).
- A missing resources directory is valid (equivalent to empty). Zero manifests is valid; built-in
  resources and auto-declaration cover the zero-config experience exactly as today.
- There is no `apply` step and no watch mode. The registry is rebuilt from the full manifest set on
  every invocation, exactly as it is rebuilt from config today.

### R3: Document envelope

Each document uses the Kubernetes envelope shape, with agentworks vocabulary inside it:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: claude
  description: Claude Code interactive session
spec:
  inherits: [default]
  command: claude --name {{session_name}}
  restart_command: claude --resume {{session_name}}
  required_commands: [claude]
  env:
    CLAUDE_LOG_LEVEL: info
```

- **`apiVersion`** (required): `agentworks/v1`. Any other value is a load error. The field exists so
  future schema evolution has a lever; no other version is defined by this SDD. The camelCase
  spelling is kept verbatim for Kubernetes familiarity.
- **`kind`** (required): the registry kind identifier, verbatim (`secret`, `vm-template`,
  `session-template`, ...), lower-kebab per R9. One canonical kind vocabulary across manifests, CLI
  (`--kind`), origins, and error messages. Unknown kinds are load errors listing the valid kinds.
- **`metadata`** (required): the framework-uniform fields. `name` (required; name validation matches
  the TOML loader exactly: the resource-name rule applies where TOML applied it, i.e. to `secret`
  names, with other kinds pass-through; uniform tightening is deferred past the
  migration-equivalence window) and `description` (optional; the operator-set description per the
  resource-registry SDD's R9, including the missing-description warning). No labels or annotations;
  they can be added under `metadata` later without breaking anything.
- **`spec`** (required, may be empty): the kind-specific fields, exactly the fields the kind's TOML
  section accepted, with nesting expressed natively (`env`, `backend_mappings` as nested maps).
  Kind-specific validation semantics are unchanged from today.

Not manifest-declarable, rejected with a specific error: kinds reserved to capabilities
(`secret-backend`, `git-credential-provider`) and any future code-only kind. The error names the
kind and explains that it is provided by the app (or a plugin). (A `kind: secret-backend` document
gets exactly this error post-collapse -- the kind exists, as descriptor rows, and cannot be
declared.)

Singleton-shaped kinds (`admin-template`, `named-console-template`) accept only
`metadata.name: default`; other names are load errors. For `named-console-template` the Config-side
plurification is still deferred to its own SDD. For `admin-template` the framework side is already
plurified (resource-registry Phase 2a.3); restricting the envelope to `default` is a deliberate
operator-surface parity choice, since multi-admin operational semantics (provisioning several admin
users) are out of scope here.

### R4: One resource, one document

TOML's section-splitting workarounds are gone. Everything that belongs to a resource lives in its
one document: env tables, backend mappings, dotfiles settings, mise settings. The admin resource,
today spread across `[admin.config]`, `[admin.env]`, and friends, becomes a single `admin-template`
document with a flat spec:

```yaml
apiVersion: agentworks/v1
kind: admin-template
metadata:
  name: default
spec:
  username: agentworks
  git_credentials: [github]
  dotfiles_source: git::https://github.com/user/dotfiles
  env:
    EDITOR: nvim
```

Env entry polymorphism is unchanged: a value is a plaintext string or a `{ secret: <name> }`
mapping.

The Config layer's resource-composition step (recomposing TOML sections into Resources) is removed;
the manifest loader produces fully-formed Resources directly.

### R5: Duplicates and load-order guarantees

- The same `(kind, name)` declared in two operator documents (same file or different files) is a
  load error citing both locations (`file:line` each). TOML's parser previously caught same-file
  duplicates for free; the loader now owns this check across the whole manifest set.
- Operator redeclaration of a built-in resource follows the kind's override policy (R6).
- Every document's `file:line` (the document's first line) is captured as its source location and
  carried into the resource's `operator-declared` origin, replacing the TOML section-line scanner.

### R6: Built-in resources

The app ships resources of its own through the same framework:

- **Mechanism**: app-bundled manifests (same envelope, packaged with the app) and/or code
  publishers, both landing in the registry with origin `built-in`. Which mechanism each built-in
  uses is an implementation choice; the operator-visible contract is the `built-in` origin.
- **Initial built-ins**: the `env-var` and `prompt` secret-backend descriptor rows (R8), published
  by the secrets code publisher. The catalog (apt sources, apt packages, install commands) keeps its
  existing code publisher and simply reports the `built-in` origin (R7); moving it to bundled
  manifests or a system plugin is future work. The app-bundled-manifest mechanism itself stays wired
  but currently empty (its original content, the bundled backend manifests, died in the 2026-07-07
  collapse); the loader path remains exercised by tests, and plugins/future built-ins are its
  consumers.
- **Override policy is per kind**:
  - Catalog kinds keep today's documented behavior: an operator manifest with the same name as a
    built-in entry overrides it.
  - `secret-backend` is not manifest-declarable at all (revised 2026-07-07): any
    `kind: secret-backend` document is the R3 capability-kind envelope error. The reserved-names
    override tier keeps its type but has no reachable member (retained for the plugin SDD's default
    exposed resources); per-secret customization is `backend_mappings`, and chain composition is
    `[secret_config].backends`.
  - Template kinds are unaffected (their defaults remain framework-synthesized via
    always-materialize, not built-in rows; an operator declaring `default` replaces the synthesis
    exactly as today).

### R7: Origin taxonomy cleanup

The origin model is cleaned up in anticipation of the plugin distribution tiers:

- **`operator-declared`**: from an operator manifest. Carries `file:line` of the document.
- **`built-in`**: shipped with the app, inseparable from it. Replaces today's `code-declared`
  variant; carries the same source identifier (e.g. `agentworks.catalog`, `agentworks.secrets`). All
  current code-declared rows (catalog entries, git credential provider descriptors, and the built-in
  secret backend rows) become `built-in`.
- **`auto-declared`**: unchanged (synthesized by miss policy or always-materialize).
- **Reserved for the plugin SDD**: `system-plugin` (distributed with the app but separable, possibly
  requiring explicit enable) and `external-plugin` (installed from outside sources). This SDD
  defines the taxonomy and display vocabulary so nothing needs renaming later; the two plugin
  variants are not constructible until the plugin SDD.

Surfaced everywhere origins appear today (`agw doctor`, `agw secret list/describe`,
`agw resource list/describe`). The `agw resource list --origin` filter vocabulary becomes
`operator | builtin | auto` (extended, not redefined, when plugin variants arrive).

### R8: Secret backends (revised 2026-07-07: the capability collapse)

This SDD originally split secrets into a capability layer (`secret-provider`) and a declarable
exposure layer (`secret-backend` resources instantiating providers). A maintainer-directed revision
(2026-07-07, out of the plugin-system SDD design review; full ruling chain in the plan's sequencing
notes) collapsed the split: the exposure layer was ceremony -- no config-bearing capability exists,
the built-in "backends" were forced to share their providers' names, and the declarable kind's
sample was prose-only because nothing declarable could exist. The requirements as they now stand:

- **`secret-backend`** (capability, registry descriptor): the code that produces secret values --
  what the ecosystem calls a secrets backend. Built-ins: `env-var` (convention `AW_SECRET_<NAME>`),
  `prompt`. Backends are registered code-side (`SECRET_BACKEND_REGISTRY`) and mirrored into the
  registry as read-only `built-in` descriptor rows (error miss policy, not manifest-declarable) so
  chain references validate uniformly and the capabilities are visible in `agw resource list`. There
  is no declarable instantiation kind.
- **Chain**: `[secret_config].backends` lists backend (capability) names in precedence order;
  unknown names are load errors validated against the descriptor rows. The zero-config default chain
  (`env-var`, then `prompt`) is unchanged. The v0.10.0 TOML vocabulary is unchanged and now
  literally correct.
- **Per-secret addressing lives in the mapping**: `backend_mappings.<backend>` values remain string
  / structured dict / `false` (the env-and-secrets SDD's value forms). The structured form is where
  instance-flavored addressing belongs -- a future 1Password backend reads
  `backend_mappings.onepassword = { vault = "Work", item = "npm", field = "token" }` per secret.
- **Backend-level configuration** (connection material: a service-account token, an account URL):
  none exists today. When the first config-bearing backend ships, its configuration is
  backend-scoped -- one configured form per capability. If a genuine multi-instance need
  materializes (two 1Password ACCOUNTS with different credentials), that backend graduates to a
  declarable instance kind then; graduation is additive, and the path is smooth because mappings may
  then key either the capability name (resolving to its sole instance) or an instance name.
- **Unchanged semantics**: per-secret resolution order, prompting, batching, per-backend opt-outs,
  and the never-persist-values guarantee are untouched.
- Inspection surfaces (`agw secret describe` mappings and resolution preview, doctor rows) enumerate
  the active chain and compute each backend's default convention by asking the capability, so a
  future config-bearing backend renders correctly with no display-layer changes.

### R9: Kind vocabulary and git credential alignment

Two vocabulary alignments ride this SDD's release train:

- **Kind identifiers move to lower-kebab** (`vm-template`, `session-template`, `secret-backend`,
  ...), everywhere the vocabulary appears: manifest `kind:` values, `--kind` filter values,
  `agw resource describe` positionals, origin and reference source tuples, and error messages.
  Rationale: the project convention is snake_case for framework keys and kebab-case for
  operator-typed values, and after the cutover kind identifiers appear exclusively in value position
  (`kind:`, `--kind`); their snake_case spelling was inherited from the TOML section keys this SDD
  retires (TOML section names themselves are keys and stay snake_case until the cutover removes
  them). The rename lands in Phase 0 so every new surface (manifests, migration output) is born
  kebab.

Git credentials already follow the resources-reference-capabilities pattern (they were never a
split-model example -- see the 2026-07-07 sequencing note); this SDD aligns their vocabulary:

- The `type` field on git credential entries is renamed to **`provider`** (`github`, `azdo`),
  matching the capability-reference field convention (ADR 0016's naming rule). The migration tool
  rewrites it.
- The registry kind for entries is renamed from `git_credentials` to **`git-credential`** (singular,
  consistent with every other kind identifier), riding the same Phase 0 sweep as the casing change.
- The `git-credential-provider` descriptor kind is unchanged in behavior (read-only rows, error miss
  policy); its rows report origin `built-in` per R7. The provider classes keep owning behavior
  (credential line formatting, auth hints, provider-specific fields like `org`).

### R10: Migration tool (revised 2026-07-05 for the dual-path era)

`agw resource migrate` moves resources from TOML to YAML -- a recurring, incremental mover, not a
one-time converter (under R11 dual-path there is no cutover to gate). Renamed from `config migrate`:
its object is resources; the TOML edit is a side effect on the source. Full design in
`migration-tool-lld.md`; requirement-level behavior:

- **Selectors** scope the move: `KIND` (one kind), `KIND/NAME` (one resource; the token splits at
  the first `/`), or `--all` (everything TOML-declared -- an explicit opt-in; a bare invocation
  errors rather than migrating the whole config by accident, and `--all` plus selectors is an error
  too). Overlapping selectors union. Only operator-declared TOML rows are migratable. An EXPLICIT
  selector matching nothing errors before anything is written; `--all` with nothing left reports
  "nothing to migrate" and exits 0, keeping scripted re-runs idempotent.
- **Writes manifests** per `--layout`: `per-kind` (default; `vm-templates.yaml` per the bundled
  plural convention), `single` (one file), or `per-resource` (`<kind>/<name>.yaml`). Layout is
  operator ergonomics only -- the loader does not care -- and declaration order is preserved.
- **Append-only YAML**: existing files are never parsed or rewritten; new documents are appended
  with `---`. This is what makes repeat and incremental runs safe, and removes any overwrite
  `--force`.
- **Edits the TOML -- mandatorily**: dual-path makes a both-sources declaration a hard load error,
  so migrated sections cannot stay live. `--toml comment` (default) comments them out in place with
  a `# migrated to resources/<file>` marker (reversible); `--toml delete` removes them. Either way:
  tomlkit round-trip preserves surviving sections' comments/formatting, the original is backed up to
  `paths.backups` before ANYTHING is written (manifests included), and the rewrite is atomic.
- **Applies the renames**: `git_credentials.<name>.type` becomes `provider`.
  `[secret_backends.<kind>]` sections are dropped with a note whenever the tool rewrites a file
  containing them: they were semantically empty, and the kind is a capability descriptor, not
  manifest-declarable -- there is nothing to convert them into.
- **Verifies every real run**: rebuilds the registry from the result and confirms row-for-row
  equivalence with the pre-migration registry (modulo declaration location/origin), printing
  `verified: registry unchanged (N resources)`; on mismatch it rolls back (backup + created-file
  removal + append truncation) and errors. The one-time golden test is thereby promoted to an
  operator-facing guarantee on every run.
- **Safety**: preview + confirmation (`--yes` to skip); `--dry-run` prints the summary (which
  resources go where) and writes nothing -- `--full` opts into the complete YAML documents and the
  TOML diff. An interrupted run fails loudly at the next load (cross-source duplicates citing both
  locations); recovery is from the pre-write backup or by hand-finishing the TOML edit -- the tool
  itself refuses to run on a broken config.

### R11: Dual-path with deprecation (revised from "hard cutover", 2026-07-03)

- YAML manifests and TOML resource sections both publish into the single registry indefinitely --
  different publishers, one registry. Mixing is supported; the same resource declared in both is a
  duplicate error citing both locations.
- TOML resource sections emit a per-section deprecation warning at load, naming the section and
  pointing at `agw resource migrate`. Operators migrate on their own schedule; the TOML resource
  path's removal waits for a future major release.
- Release notes carry the change and the one-command migration path.
- The YAML teaching surface is `agw resource sample (KIND | --all) [--write FILENAME]`: commented
  sample manifests per manifest-declarable kind (`--all` for every kind; a bare invocation errors,
  mirroring `resource migrate`), shipped bundled and guaranteed to load through the real loader --
  and to build a full registry as a set. `--write` saves into the resources directory instead of
  stdout, appending if the file exists -- the same append-only rule as the migrator, minus the `---`
  separator (the samples are fully commented, so appended text is inert and a separator would create
  a null document the loader rejects). `agw config sample` is unchanged: it documents the settings
  file, which is permanent under config-is-config.

### R12: Framework invariance

The registry framework's operator-visible behavior is unchanged: publish/finalize lifecycle, miss
policies, always-materialize, auto-declared descriptions, reference collection, cycle detection,
`Referenced by:` / `Used by (per current config):` surfaces, and eager-resolve scoping all work
identically. This SDD swaps the operator publisher's source format and completes the consumer-side
migration (all resource reads go through the registry, since resources no longer live on `Config`);
it does not change the framework contract. Where display strings mention TOML file locations, they
now mention manifest locations; the shapes stay the same.

## Non-goals

- **Plugin system**: system and external plugins (and their origin variants becoming constructible)
  are a future SDD. This SDD only shapes the taxonomy and mechanisms they will reuse.
- **Apply-style reconciliation / provisioned-state tracking**: auto-load is deliberate;
  reconciliation only matters with persisted state, which stays reserved for the drift-tracking
  future noted in the resource-registry lockfile.
- **Lifecycle resources in manifests**: VMs, agents, sessions, consoles stay DB-managed.
- **YAML for config**: `config.toml` stays TOML. The split is the feature, not a step toward
  all-YAML.
- **Schema versioning beyond `agentworks/v1`**: `apiVersion` is a reserved lever, nothing more.
- **`metadata.labels` / `metadata.annotations`**: not defined; the envelope leaves room.
- **Moving the built-in catalog to bundled manifests or a system plugin**: the code publisher stays;
  only its origin display changes. Candidate for the plugin SDD.
- **Plurifying `named-console-template` / relaxing `admin-template` to multiple names**: still
  deferred to their own SDDs; the envelope accepts only `default` for them meanwhile.
- **New secret backends** (`onepassword`, vaults): implementations are future work; R8's
  backend-scoped-config-then-graduate story is where their configuration lands.
- **Configuration on the built-in backends** (e.g. an env var prefix option): no known operator ask;
  per-secret `backend_mappings` covers identifier customization.

### R13: '/' is disallowed in resource names (added 2026-07-05)

Resource names may not contain `/` -- it is reserved for `KIND/NAME` selectors
(`agw resource migrate`) and per-resource manifest filenames. Enforced source-independently at
`Registry.add`, so TOML, YAML, and future plugin publishers share one rule. This is a deliberate
BREAKING tightening of the name rule for the (unlikely) configs carrying slash-bearing quoted
section names (`[vm_templates."a/b"]` loaded before this SDD); it amends the earlier
name-validation-parity position (which kept non-secret names pass-through) for this one character,
and rides the Phase 5 release notes.

## Migration notes

Operators upgrading across this SDD migrate with `agw resource migrate` on their own schedule, in
one run or many (selectors scope each run). Observable changes beyond the file moves:

- `git_credentials.*.type` becomes `provider` (rewritten by the tool).
- Explicit empty `[secret_backends.env-var]` / `[secret_backends.prompt]` sections disappear; the
  built-in backends cover them. Per-secret `backend_mappings` overrides keep working unchanged.
- `agw resource list` origin values change: `code-declared` reads `built-in`; the `--origin` filter
  accepts `builtin`.
- Kind identifiers are lower-kebab: `--kind vm_template` becomes `--kind vm-template`,
  `--kind git_credentials` becomes `--kind git-credential` (also singularized), and error messages
  use the new spellings.
- Everything else (secret resolution, template inheritance, auto-declaration, eager-resolve
  prompting) behaves identically.
