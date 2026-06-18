# Resource registry: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

The env-and-secrets SDD established the secret declaration / backend / resolver machinery for env
block secrets, but explicitly deferred two adjacent concerns:

1. **`tailscale_auth_key`** and **`git_credentials.*` tokens** still resolve through their pre-SDD
   env-var-or-prompt paths. They sit outside the secrets framework, so operators get one set of
   behaviors for `[admin.env]` secrets and a parallel set of behaviors for these "system" secrets:
   different env var conventions, no backend chain, no `agw secret list` visibility, no per-secret
   `backend_mappings`. That divergence is a wart that grows with every new system-managed secret.

2. A general pattern recurs across the config: resources reference other resources by name
   (templates reference parent templates, configs reference catalog entries, env entries reference
   secrets, ...) but the "what happens on unknown name" policy is bespoke per type. Some surface
   graceful errors with context; some don't. Cycle detection in inheritance chains is ad-hoc. There
   is no uniform "who needed this?" trail for diagnostics.

This SDD addresses both, in one framework:

- A **resource-requirement contract** where each resource type declares what other resources it
  needs (by name, with a system-defined usage). One resource may be required by many; all usages are
  tracked and surfaced via the CLI.
- A **resource-registry** validation pass that walks all requirements, looks each up in the
  appropriate registry, and dispatches missing-name policies per registry kind (auto-declare for
  secrets; built-in default for templates; error for catalogs; etc.).
- A **migration of `tailscale_auth_key` and `git_credentials.*.token`** to first-class secret
  references, using the auto-declare policy so the zero-config UX is preserved.

The framework ships in two phases (see plan): Phase 1 introduces the framework and migrates the two
system secrets; Phase 2 migrates the remaining resource references (template inheritance, catalog
commands, git credential providers, secret backend kinds) to use the same framework.

### Scope

All named agentworks entities are _resources_: templates, secrets, backends, catalog entries, VMs,
agents, sessions, consoles, and so on. This SDD's framework manages the **config-declared** subset.
Resources with lifecycle and observable state (VMs, agents, sessions, consoles) live in the DB today
and are out of scope here. The framework's storage abstraction is shaped to admit a future
manifest-style-config SDD that would bring lifecycle resources into the registry alongside DB-side
reconciliation, but that move is deliberately not part of this design.

## Terminology

- **Resource**: any named entity in agentworks. The umbrella term covers both config-declared
  resources (templates, secrets, backends, catalog entries) and lifecycle resources (VMs, agents,
  sessions, consoles). The framework in this SDD manages only the config-declared subset; lifecycle
  resources live in the DB and stay there. Resources carry their own fields (name, kind, origin,
  description, usage list, kind-specific data); they are distinct from the **requirements** that
  reference them by name.
- **Resource registry**: a per-kind container of resources of that kind. One registry per kind
  (`config.secrets`, `config.vm_templates`, ...). Holds resources arriving from any of the three
  origin paths (built-in, operator-declared, auto-declared) and carries the kind's **miss policy**
  (R2) which dictates what happens when a requirement points at a name the registry does not yet
  contain. Lookup within a registry is by `name`; cross-kind identity is the `(kind, name)` pair.
  Queried by the validation pass and surfaced via `agw doctor`, `agw secret list`, and (Phase 2)
  `agw resource list`.
- **Resource requirement**: a **reference declaration** -- one resource saying "I need this other
  resource by name". Distinct from the resource itself: requirements point at resources but are not
  resources. Carries the target's `name` and `kind`, a system-defined `usage`, the declaring
  resource's `source` as a `(kind, name)` pair, and (optionally) per-kind defaults the registry's
  auto-declare policy may use. A resource may have many requirements pointing at it; the framework
  collects them all.
- **Usage**: the system-defined "what this resource is being used for", set by the requirement. Each
  requirement contributes one usage; a resource that is required by multiple sources accumulates a
  list of usages. Surfaced in `agw doctor` and `agw secret describe`. Distinct from the operator-set
  description.
- **Description**: the operator-defined free-form note about a resource (already exists today on
  secrets and `git_credentials`; this SDD formalizes the convention for new resource types).
- **Miss policy**: what a registry does when a requirement's name has no match in the registry.
  Per-registry. Options:
  - **Auto-declare**: synthesize the resource from the requirement's defaults and add it to the
    registry. The resulting resource has `origin = auto-declared`.
  - **Built-in fallback**: instantiate a code-defined resource for a specific reserved name (e.g.,
    `default`). The resulting resource has `origin = built-in`. Any other name is a config-load
    error.
  - **Error**: raise a config-load error citing the requirement source.
- **Origin**: the per-resource record of where the resource came from. Three values: `built-in`
  (from code; e.g., the implicit `default` template), `operator-declared` (from operator config;
  carries file path and line number for traceability), `auto-declared` (synthesized to satisfy a
  requirement; carries the first matching requirement's source `(kind, name)`). Set once when the
  resource is added to the registry; never mutated afterwards. Surfaced in `agw doctor`,
  `agw secret list`, and `agw secret describe`.

## Requirements

### R1: Resource requirements as a generic contract

Every resource type that references other resources by name declares those references as
`ResourceRequirement` records. Each requirement carries:

- **`name`** (required): the referenced resource's name. Operator-overridable when the declaring
  resource exposes the name as a config field (e.g. `vm_templates.x.tailscale_auth_key`); otherwise
  fixed.
- **`usage`** (required): one-line description of what the declaring resource needs the referenced
  resource for. Each requirement contributes one usage.
- **`source`** (required): the declaring resource's identity as a `(kind, name)` pair (e.g.
  `("vm_template", "default")`, `("git_credentials", "github-prod")`). Same `(kind, name)` shape
  used throughout the framework for resource identity. Surfaced in diagnostics and provenance.
- **`kind`** (required): the kind of resource being referenced (`secret`, `vm_template`, etc.).
- **Kind-specific extras** (optional): defaults the registry's auto-declare policy uses to
  synthesize a missing resource. For secrets in Phase 1, no extras are needed (the framework default
  conventions cover the common case); the slot exists for future and Phase-2 use.

A resource's `required_resources()` method returns the full list (one per reference, no
deduplication at the producer side). The validation pass collects requirements across the entire
config, keyed by `(kind, name)`, and feeds them into the registries.

#### Multi-requirement resources

A single resource may be the target of multiple requirements. The framework explicitly supports
this:

- **Auto-declared resources** synthesize once per `(kind, name)`. When multiple requirements would
  trigger auto-declaration of the same name, the first-encountered requirement supplies any
  kind-specific extras (deterministic walk order: alphabetical by section). The remaining
  requirements contribute additional usages but do not re-synthesize the resource.
- **Operator-declared resources** are unchanged by additional requirements pointing at them. The
  resource keeps the operator's fields; the requirements just add to its usage list.
- **All usages are retained**. The resource's accumulated usage list is what `agw secret describe`
  (R9) renders. Duplicate usage strings from different requirements are deduplicated for display.

### R2: Per-registry miss policies

Each resource registry declares its miss policy:

| Registry                                                                  | Miss policy       | Behavior                                                                                                                                                                  |
| ------------------------------------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Secret registry                                                           | Auto-declare      | A `SecretRequirement` whose name is missing synthesizes a `SecretDecl` carrying the requirement's usage. Origin: `auto-declared`. Additional requirements add more usages |
| VM, workspace, agent, session template registries                         | Built-in fallback | The reserved name `default` instantiates the code-defined default template. Origin: `built-in`. Any other name is a config-load error                                     |
| Catalog command registry, git credential provider registry, backend kinds | Error             | Unknown names are config-load errors                                                                                                                                      |

Error messages include the requirement's `source` so the operator sees, e.g.:

```text
vm_template "azure-prod" references unknown agent_template "claude-experimental"
```

The framework controls error shape; resource types only declare the policy. Migrating an existing
bespoke-validation registry into the framework changes the error wording but not the validation
semantics.

### R3: Per-field merge between operator declarations and auto-declared defaults

When a resource is **both** operator-declared and required, the merge is per-field for
operator-settable fields:

- Operator-settable fields (`description`, `hint`, `backend_mappings`): operator wins per field (or
  per key, in the case of `backend_mappings`). Fields the operator omitted fall back to the
  requirement's defaults (if any).
- System-collected fields (`usage`): always accumulated from the matching requirements, regardless
  of operator declaration. The operator does not set usage; the system tracks all of them.
- Origin is `operator-declared` (the operator declared the resource; see R4), and the framework
  retains the matching requirement sources so doctor can report "operator-declared; also required by
  `<source>`".

Example: operator writes

```toml
[secrets.tailscale-auth-key]
backend_mappings.env-var = "TS_KEY"
description = "Prod tailnet key, owner: SRE team"
```

The final `SecretDecl` carries the operator's `backend_mappings.env-var` and `description`, and the
usage list `["Tailscale auth key for VM provisioning"]` from the VM-template requirement. The
operator does not have to retype the usage; the system fills it in automatically and updates it when
new requirements arrive.

### R4: Origin tracking on every resource

Every resource in a registry carries an `origin` field that records how it came to be in the
registry. Three origin types:

- **`built-in`**: the resource is defined in code and loaded into the registry on demand. Example:
  the implicit `default` VM template (instantiated by the built-in-fallback miss policy when no
  operator declared one and a requirement referenced `default`). Built-in resources carry no
  file:line; their source is agentworks itself.
- **`operator-declared`**: the resource is declared in operator config. The origin carries the
  **file path and line number** of the declaration's opening line (e.g.,
  `~/.config/agentworks/config.toml:42`). When the resource is also referenced by requirements, the
  matching requirements are retained separately (R3) but the origin stays `operator-declared`.
- **`auto-declared`**: the resource was synthesized at config-load time to satisfy a missing
  requirement. The origin carries the **first matching requirement's `source`** (a `(kind, name)`
  pair). Subsequent requirements that match the same name contribute additional usages (R1) but do
  not alter the origin field.

The origin field is set once when the resource is added to the registry and is never mutated
afterwards. It is the primary signal for "where did this resource come from?" -- useful for
debugging unexpected declarations, locating an operator declaration in a sprawling config, or
confirming an auto-declared resource came from the source the operator expected.

Origin is surfaced in `agw doctor`, `agw secret list` (origin column), and `agw secret describe`
(full origin detail, including file:line or first-requirement source as appropriate). The Phase-2
`agw resource` commands surface origin generically (R11).

### R5: Cycle detection in the validation pass

The validation pass detects cycles in the resource reference graph. Inheritance chains
(`vm_templates.x inherits = ["y"]` where `y inherits = ["x"]`) and any future cross-resource cycle
are caught uniformly with a clear error naming the cycle and the resources involved. Cycle errors
are config-load errors with no fallback.

Today's resource graph is tree-shaped outside inheritance; the cycle check is defensive but cheap.

### R6: Tailscale auth key as a secret reference

The VM template schema gains:

- **`tailscale_auth_key`** (string, default `"tailscale-auth-key"`): name of the declared (or
  auto-declared) secret that holds the Tailscale auth key.

The value is a bare string naming a secret. Inline `{ secret = "..." }` polymorphism and plaintext
literals are not accepted; this field's only valid value is a secret name. Plaintext-or-secret
polymorphism is reserved for `[admin.env]` / `[*_templates.*.env]` entries where mixing literals and
refs has a legitimate use.

Resolution:

1. `agw vm create vm1` triggers manager-entry eager-resolve.
2. The resolved VM template emits a `SecretRequirement` with `name=<tailscale_auth_key>`,
   `usage="Tailscale auth key for VM provisioning"`, and `source=("vm_template", <name>)`.
3. If the named secret isn't operator-declared, the auto-declare policy synthesizes it.
4. The orchestrator resolves the secret through the configured backend chain (first wins).
5. The resolved value is threaded as a function argument to the Tailscale install runner.
   Provisioning remains hermetic: no SSH SetEnv, no profile-fragment write.

There is no opt-out at the VM template level. Tailscale is foundational to the system. Operators who
don't want Tailscale auth at all don't configure agentworks at all.

### R7: Git credential tokens as secret references

The git credential entry schema gains:

- **`token`** (string, default `"git-token-<credential-name>"`): name of the declared (or
  auto-declared) secret that holds the credential's token.

Like `tailscale_auth_key`, the value is a bare string; no polymorphism, no inline value.

Each git credential entry emits one `SecretRequirement` for its `token`. The default secret name
follows the `git-token-<credential-name>` convention:

```toml
[git_credentials.github-prod]
type = "github"
description = "Prod org credentials"
# token defaults to "git-token-github-prod"

[git_credentials.github-personal]
type = "github"
description = "Personal access"
token = "shared-github-token"   # share a secret across credentials (uncommon but supported)
```

Resolution mirrors R6: requirement collected, auto-declared if missing, eager-resolved via the
backend chain, resolved value threaded to the git credential install runner that writes
`~/.git-credentials` on the VM.

### R8: Operator description as a distinct field

Every resource type that supports operator declaration carries an optional `description` field that
is separate from the system-collected `usage` list:

- **`usage`** (list, system-collected) comes from the matching requirements; one entry per
  requirement. Operators do not set it. Example entry: `"Tailscale auth key for VM provisioning"`. A
  resource required by several sources has several usages.
- **`description`** (string, operator-set) is the operator's free-form note. Example:
  `"Prod tailnet auth key, 90-day expiry, owner: SRE team"`.

Both surface in `agw doctor`, `agw secret list`, and `agw secret describe` (R9). The convention is
the same for any resource type Phase 2 brings into the framework.

`description` is encouraged but not required. The validation pass emits a config-load warning when a
declared resource has no `description`, surfacing the gap. Operators who deliberately leave the
field blank pay one warning per CLI invocation; a future iteration may upgrade this to an error if
the encouragement-toward-discipline pays off.

### R9: Origin and inspection via doctor, secret list, and secret describe

`agw doctor`'s Secrets group surfaces:

- **Per-secret origin** (R4): the origin string with relevant detail. For `operator-declared`, shown
  as `operator-declared (config.toml:42)`. For `auto-declared`, shown as
  `auto-declared by vm_template:default`. For per-field-merge cases (operator declared, also
  required) the origin remains `operator-declared` and the matching requirement sources are listed
  as supplemental.
- **Usages**: count and first entry; the full list is on `agw secret describe`.
- **Description**: operator-set, when present.

`agw secret list` adds an `Origin` column with the same shape (kind plus the relevant detail). The
header summary becomes, e.g., `12 secrets (1 built-in, 4 auto-declared, 7 operator-declared)`. List
shows summary; for detail, the operator runs describe.

`agw secret describe <name>` (new in Phase 1) is the per-secret detail view:

- **Name, kind, origin, description** (operator-set, when present). Origin is rendered with full
  detail: file path and line for operator-declared; the triggering requirement's `(kind, name)` for
  auto-declared; agentworks itself for built-in.
- **All registered usages**: one row per requirement, showing the source `(kind, name)` and the
  usage text. A resource referenced by three sources shows three rows. Duplicate usage text is
  collapsed.
- **Backend mappings**: the merged table, with the source per backend (operator-set vs.
  framework-default vs. backend-default-convention).
- **Current resolution preview**: which active backend would resolve this secret right now
  (`would resolve via env-var`, `would prompt`, or `not available in any backend`). Mirrors the
  doctor preview but scoped to one secret.

Describe does not prompt and does not resolve secret values; it reports state.

Phase 2 generalizes the same origin display to other resource registries when their migration lands;
until then they use the existing per-type reporting. The `describe` command pattern extends
naturally to other resource kinds in Phase 2 (e.g., `agw template describe <name>`), though only
`agw secret describe` is in scope for Phase 1.

### R10: Eager-resolve integration

No changes to the eager-prompting contract from the env-and-secrets SDD. Shell-opening commands
continue to walk their target chains and resolve all needed secrets up front. After this SDD, the
set of "needed secrets" expands:

- Env-block secrets reached via `effective_env` (existing).
- System secrets reached via `required_resources()` on the target's resolved templates (new).

The orchestrator's `extra_decls` parameter (left in place by the env-and-secrets SDD as the
migration hook) is the integration point. Manager-entry code at `vm create`, `vm reinit`,
`agent create`, `agent reinit` walks the resolved templates' `required_resources()` and passes the
resulting `SecretDecl`s into `resolve_for_command(extra_decls=...)`.

### R11: Phase 2 scope (resource type migrations)

Phase 2 migrates the remaining resource references to use the framework:

- **Template inheritance**: `inherits = ["..."]` resolution moves into the `VMTemplateRegistry` /
  `WorkspaceTemplateRegistry` / `AgentTemplateRegistry` / `SessionTemplateRegistry` validation pass.
  The default-only auto-declare policy formalizes the current "built-in default" fallback.
  Operator-facing behavior is unchanged; error messages get the framework's consistent shape.
- **Catalog commands**: references to catalog command names (`apt_packages = ["gh"]`,
  `system_install_commands = ["az-cli"]`, `user_install_commands = ["bun"]`) move into a
  `CatalogRegistry` with the error miss policy. Error messages name the referencing scope.
- **Git credential providers**: `[git_credentials.<name>].type` references move into a
  `GitCredentialProviderRegistry` with the error miss policy.
- **Secret backend kinds**: `[secret_backends.<kind>]` references move into a
  `SecretBackendKindRegistry` with the error miss policy.

Phase 2 is primarily a refactor: validation logic consolidates, error messages get a consistent
shape, and the codebase has one place to add new resource types. There are no operator-facing config
changes beyond improved error messages.

#### `agw resource` cross-kind inspection

Phase 2 also adds an `agw resource` command tree for cross-kind inspection of the registry. It is
deliberately scoped to fields the declaration framework defines; kind-specific details stay in the
kind-specific commands (`agw secret describe`, future `agw template describe`, ...).

```text
agw resource list [--kind <kind1,kind2,...>] [--origin built-in|operator|auto]
agw resource describe <kind> <name>
```

- `agw resource list` shows one row per declared resource across all kinds in the registry. Columns:
  kind, name, origin (with detail per R4: file:line for operator-declared, requirement source for
  auto-declared), usage count (or first usage when short), description (truncated). Filters:
  `--kind` (CSV per the cli-conventions filter pattern), `--origin` (one of `built-in`, `operator`,
  `auto`).
- `agw resource describe <kind> <name>` shows the framework-level detail view: kind, name, origin
  with full detail, all registered usages, and description. Kind-specific detail (backend mappings,
  inheritance chain, resolved fields, ...) belongs in the kind's own `describe` command.

`agw resource` is gated to Phase 2 because the cross-kind view only earns its keep once multiple
kinds are in the registry; with only secrets in Phase 1 it would be redundant with `agw secret list`
/ `agw secret describe`.

## Non-goals

- **Manifest-style multi-file config**. This SDD acknowledges an anticipated direction toward
  Kubernetes-style YAML manifests but does not introduce them. The framework as designed is
  compatible with such a future shift: resource identity is already `(kind, name)`-shaped and the
  validation pass is independent of the parser. The loader changes (single TOML to multiple YAML
  files) are a separate concern handled in its own SDD if and when that direction is committed.
- **Bringing lifecycle resources into the Resource Registry**. VMs, agents, sessions, and consoles
  are resources, but they live in the DB and are managed via CLI commands today. Migrating them into
  the registry (with reconciliation against DB state) is reserved for a future manifest-style-config
  SDD. That future SDD would extend the framework's storage backend rather than replace the
  framework, but the work is deliberately out of scope here.
- **Namespaces / multi-tenant resource scoping**. Resource identity remains globally `(kind, name)`.
- **A new transport for system secrets**. Resolved values reach provisioning runners as function
  arguments, not via SSH SetEnv or profile fragments. The hermetic-provisioning contract from the
  env-and-secrets SDD is preserved end-to-end.
- **Backwards compatibility with legacy env var names**. The pre-SDD env vars for Tailscale and git
  credentials no longer resolve under their old names. No deprecation warnings are emitted; the
  cutover is documented in release notes. Operators wanting to keep their existing env var names do
  so via explicit `backend_mappings.env-var` overrides on each secret.
- **Plaintext or polymorphic forms for `tailscale_auth_key` and `git_credentials.*.token`**. Both
  fields accept secret names only. EnvEntry-style `{ secret = "..." }` polymorphism is intentionally
  not extended to these fields.
- **Soft-disable for built-in-default template names**. A registry that auto-declares `default` does
  so unconditionally. Operators override behavior by declaring `[vm_templates.default]` explicitly;
  there is no "no default" mode.
- **Resource removal semantics for auto-declared resources**. An auto-declared resource that has no
  remaining requirement (e.g., the operator changed `vm_templates.x.tailscale_auth_key` to reference
  a different secret) is silently pruned at the end of the validation pass. The framework does not
  surface "this used to be auto-declared but isn't anymore"; the only signal is the updated
  `agw secret list`.

## Migration notes

Operators upgrading across this SDD see two observable changes:

- **Tailscale auth key resolution moves to the framework default convention.** Set
  `AW_SECRET_TAILSCALE_AUTH_KEY` (or whichever value your active backend chain produces). Operators
  who prefer to retain their previous env var name declare:

  ```toml
  [secrets.tailscale-auth-key]
  backend_mappings.env-var = "<your previous env var name>"
  ```

- **Git credential tokens move to the framework default convention.** Each git credential entry now
  resolves its token via `AW_SECRET_GIT_TOKEN_<NAME>` (framework default). Operators retaining the
  previous env var convention declare the corresponding `backend_mappings.env-var` on each
  `[secrets.git-token-<name>]`.

Both migrations are one-time cutovers per operator; release notes call them out explicitly. The
already-deprecated `GIT_CREDENTIALS_<NAME>` fallback is removed in this SDD.

After this SDD, every secret -- operator env, Tailscale, git credential tokens -- is reachable
through the same backend chain. `agw secret list` becomes the canonical view of "what credentials
does my operator-side environment need to provide".
