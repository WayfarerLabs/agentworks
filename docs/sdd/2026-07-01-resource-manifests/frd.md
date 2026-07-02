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
4. **Capabilities are code, not resources.** Secret providers, VM provisioners, and git credential
   providers have associated code. They are not manifest-declarable. This SDD formalizes the
   provider/backend split for secrets: providers are code capabilities mirrored into the registry as
   read-only descriptor rows; backends are the named, configured, manifest-declarable instantiations
   of a provider.

The move is a hard cutover with a migration tool (`agw config migrate`) that converts an existing
`config.toml` in place: resource sections become manifests, config sections stay.

### Scope

In scope: the manifest loader and envelope schema, the config/resource split of the current TOML
surface, built-in resource manifests, the origin taxonomy cleanup, the secret provider/backend
split, the git-credential provider field alignment, the migration tool, and the cutover.

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
- **Capability**: a unit of code the app (or, later, a plugin) provides: a secret provider, a git
  credential provider, a VM provisioner. Capabilities are not manifest-declarable. Where a
  capability is referenced by name from resources, it is mirrored into the registry as a read-only
  descriptor row so references validate uniformly.
- **Provider**: a capability that produces secret values (`env-var`, `prompt`; later `onepassword`,
  ...) or git credentials (`github`, `azdo`). Named by the `provider` field on the resources that
  instantiate or use them.
- **Backend**: a named, configured instantiation of a secret provider. A resource. Multiple backends
  may share one provider (e.g. two `onepassword` backends pointed at different vaults).
- **Built-in resource**: a resource shipped with the app itself, published from app-bundled
  manifests or code publishers. Origin variant `built-in`.

## Requirements

### R1: Config/resource split of the current TOML surface

Every section of today's `config.toml` gets exactly one destination:

| Current TOML section                      | Destination                                         | Notes                                                   |
| ----------------------------------------- | --------------------------------------------------- | ------------------------------------------------------- |
| `[operator]`                              | config (TOML)                                       | SSH keys, host prefixes                                 |
| `[paths]`                                 | config (TOML)                                       |                                                         |
| `[defaults]`                              | config (TOML)                                       | CLI flag defaults                                       |
| `[azure]`, `[proxmox]`                    | config (TOML)                                       | provisioner capability settings; plugin SDD may revisit |
| `[session.config]`                        | config (TOML)                                       | non-template session settings                           |
| `[secret_config]`                         | config (TOML)                                       | active backend chain; references backends by name       |
| `[secrets.<name>]`                        | manifest (`secret`)                                 |                                                         |
| `[secret_backends.<kind>]`                | manifest (`secret-backend`)                         | reshaped per R8                                         |
| `[git_credentials.<name>]`                | manifest (`git-credential`)                         | `type` renamed to `provider` per R9                     |
| `[vm_templates.<name>]` (+ `.env`)        | manifest (`vm-template`)                            |                                                         |
| `[agent_templates.<name>]` (+ `.env`)     | manifest (`agent-template`)                         |                                                         |
| `[workspace_templates.<name>]` (+ `.env`) | manifest (`workspace-template`)                     |                                                         |
| `[session_templates.<name>]` (+ `.env`)   | manifest (`session-template`)                       |                                                         |
| `[admin.config]`, `[admin.env]`, ...      | manifest (`admin-template`, name `default`)         | flattened into one document                             |
| `[named_console]`                         | manifest (`named-console-template`, name `default`) |                                                         |
| `[apt_sources.<name>]`                    | manifest (`apt-source`)                             | operator catalog extension                              |
| `[apt_packages.<name>]`                   | manifest (`apt-package`)                            | operator catalog extension                              |
| `[system_install_commands.<name>]`        | manifest (`system-install-command`)                 | operator catalog extension                              |
| `[user_install_commands.<name>]`          | manifest (`user-install-command`)                   | operator catalog extension                              |

After the cutover, a resource section in `config.toml` is a config-load error naming the section and
pointing at `agw config migrate` (R11).

Config sections that reference resources by name (`[secret_config].backends` referencing backend
names) keep doing so; the names are validated against the finalized registry.

### R2: Manifest directory and auto-loading

- Manifests live in the **resources directory**: `<config-dir>/resources/` (default
  `~/.config/agentworks/resources/`, sibling to `config.toml`).
- Every CLI invocation loads all files matching `**/*.yaml` and `**/*.yml` under the resources
  directory, recursively. Subdirectory structure is operator-organizational only; it carries no
  semantics.
- Dotfiles and dot-directories (`.git/`, `.backup.yaml`) are skipped.
- Load order is deterministic: files sorted by path (lexicographic, relative to the resources
  directory), documents within a file in file order. This order defines "config-load order" wherever
  the framework depends on it (first-matching-reference origin attribution, auto-declare extras).
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
- **`metadata`** (required): the framework-uniform fields. `name` (required; validated by the
  existing resource-name rule, which permits underscores; kebab-case remains the encouraged style)
  and `description` (optional; the operator-set description per the resource-registry SDD's R9,
  including the missing-description warning). No labels or annotations; they can be added under
  `metadata` later without breaking anything.
- **`spec`** (required, may be empty): the kind-specific fields, exactly the fields the kind's TOML
  section accepted, with nesting expressed natively (`env`, `backend_mappings` as nested maps).
  Kind-specific validation semantics are unchanged from today.

Not manifest-declarable, rejected with a specific error: kinds reserved to capabilities
(`secret-provider`, `git-credential-provider`) and any future code-only kind. The error names the
kind and explains that it is provided by the app (or a plugin).

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
- **Initial built-ins**: the `env-var` and `prompt` secret backends (R8). The catalog (apt sources,
  apt packages, install commands) keeps its existing code publisher and simply reports the
  `built-in` origin (R7); moving it to bundled manifests or a system plugin is future work.
- **Override policy is per kind**:
  - Catalog kinds keep today's documented behavior: an operator manifest with the same name as a
    built-in entry overrides it.
  - `secret-backend` built-in names (`env-var`, `prompt`) are reserved: an operator manifest
    redeclaring one is a load error. Customization is by declaring a sibling backend with the same
    provider and adjusting `[secret_config].backends`; there is no field-level merge or shadowing,
    consistent with the framework's declare-a-sibling philosophy.
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

### R8: Secret providers and backends

The secret system splits into a capability layer and a resource layer. The split rides this SDD
rather than a future plugin or onepassword SDD for two concrete reasons: the migration rewrite is
the one cheap moment to reshape `[secret_backends.<kind>]` (keyed by provider kind, exactly one
instance each) into named backend resources without a second operator-facing migration later, and
the built-in backends are the first real exerciser of the built-in-manifest mechanism this SDD ships
anyway.

- **`secret-provider`** (capability, registry descriptor): the code that produces secret values.
  Built-ins: `env-var`, `prompt`. Providers are registered code-side and mirrored into the registry
  as read-only `built-in` rows (error miss policy, not manifest-declarable) so references to them
  validate uniformly and they are visible in `agw resource list`.
- **`secret-backend`** (resource, manifest-declarable): a named instantiation of a provider.
  `spec.provider` (required) references a `secret-provider` by name; the rest of `spec` is
  provider-specific configuration validated by the provider capability, not the framework.

```yaml
apiVersion: agentworks/v1
kind: secret-backend
metadata:
  name: work-vault
  description: Work 1Password vault
spec:
  provider: onepassword
  vault: Work
```

(Illustrative: the `onepassword` provider is future work. Today's built-in providers accept no
configuration, so this shape earns its keep when the first config-bearing provider lands.)

- **Built-in backends**: `env-var` (provider `env-var`, convention `AW_SECRET_<NAME>`) and `prompt`
  (provider `prompt`) ship as built-in resources. Their names are reserved (R6). The zero-config
  default chain (`env-var`, then `prompt`) is unchanged.
- **Built-in providers accept no configuration**: a backend spec with anything beyond `provider` is
  a validation error from their schemas. Customizing env var identifiers stays per-secret via
  `backend_mappings`, exactly as today; a provider-level option (an env var prefix, say) would be a
  purely additive schema field if a real ask ever lands. The provider-config plumbing itself (schema
  validation, defaults, error framing, config reaching instantiation) is exercised by a test-only
  provider in the test suite, not by shipping artificial configuration on the built-ins.
- **Multiple backends per provider** is fully supported and is the point of the split (two
  `onepassword` backends with different vaults, later).
- **Unchanged semantics**: `backend_mappings` on secrets are keyed by backend name (now including
  custom backends). `[secret_config].backends` lists backend names in precedence order; unknown
  names are load errors. Per-secret resolution, prompting, batching, and the never-persist-values
  guarantee are untouched.
- Inspection surfaces (`agw secret describe` backend mappings and resolution preview, doctor rows)
  enumerate the active chain's backends and compute each backend's default convention by asking the
  provider-instantiated source, so future config-bearing providers render correctly with no
  display-layer changes.

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

Git credentials already follow the capability/instance pattern; this SDD aligns their vocabulary:

- The `type` field on git credential entries is renamed to **`provider`** (`github`, `azdo`),
  matching `secret-backend.spec.provider`. The migration tool rewrites it.
- The registry kind for entries is renamed from `git_credentials` to **`git-credential`** (singular,
  consistent with every other kind identifier), riding the same Phase 0 sweep as the casing change.
- The `git-credential-provider` descriptor kind is unchanged in behavior (read-only rows, error miss
  policy); its rows report origin `built-in` per R7. The provider classes keep owning behavior
  (credential line formatting, auth hints, provider-specific fields like `org`).

### R10: Migration tool

`agw config migrate` converts a pre-cutover `config.toml` in place:

- **Reads** the TOML, splits sections per the R1 table.
- **Writes manifests** grouped one file per kind into the resources directory (`secrets.yaml`,
  `vm-templates.yaml`, `git-credentials.yaml`, ...), multi-document within each file, preserving
  declaration order. Only kinds present in the config produce files. Grouping is a tool default, not
  a loader requirement; operators are free to reorganize afterwards.
- **Rewrites `config.toml`** with the resource sections removed and everything else (including
  comments and formatting of the surviving sections) preserved.
- **Applies the renames**: `git_credentials.<name>.type` becomes `provider`;
  `[secret_backends.<kind>]` sections become `secret-backend` documents with `spec.provider: <kind>`
  (empty sections for `env-var` / `prompt` are dropped entirely since the built-in backends cover
  them).
- **Safety**: prints a preview and asks for confirmation (`--yes` to skip); `--dry-run` shows the
  preview and writes nothing; refuses to overwrite existing manifest files without `--force`; backs
  up the original `config.toml` to the configured backups directory (`paths.backups`) before
  rewriting; idempotent on an already-migrated config (reports nothing to do).

### R11: Hard cutover

- One release contains both the migration tool and the cutover. There is no dual-source support
  window: after upgrade, resource sections in `config.toml` are load errors listing the offending
  sections and pointing at `agw config migrate`.
- Release notes carry the change and the one-command migration path.
- `agw config sample` output is rewritten: a config-only `config.toml` sample plus sample manifests
  demonstrating the envelope for each commonly-used kind.

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
- **New secret providers** (`onepassword`, vaults): the split makes room; implementations are future
  work.
- **Configuration on the built-in providers** (e.g. an env var prefix option): no known operator
  ask; per-secret `backend_mappings` covers identifier customization. Would be a purely additive
  provider schema field later.

## Migration notes

Operators upgrading across this SDD run `agw config migrate` once. Observable changes beyond the
file moves:

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
