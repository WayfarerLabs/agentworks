# Resource registry: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

The env-and-secrets SDD established the secret declaration / backend / resolver machinery for env
block secrets, but explicitly deferred two adjacent concerns:

1. **`tailscale_auth_key`** and **`git_credentials.*` tokens** still resolve through their pre-SDD
   env-var-or-prompt paths. They sit outside the secrets framework, so operators get one set of
   behaviors for `[admin.env]` secrets and a parallel set of behaviors for these "system" secrets:
   different env var conventions, no backend chain, no `agw secret list` visibility, no per-secret
   `backend_mappings`. This leads to duplicated code paths and a bifurcated operator experience.

2. A general pattern recurs across the config: resources reference other resources by name
   (templates reference parent templates, configs reference catalog entries, env entries reference
   secrets, ...) but the "what happens on unknown name" policy is bespoke per type. Some surface
   graceful errors with context; some don't. Cycle detection in inheritance chains is ad-hoc. There
   is no uniform "who needed this?" trail for diagnostics. As the declared resource graph grows, it
   will become increasingly more difficult for operators to understand and debug their config
   without additional structure.

This SDD addresses both, in one framework:

- A **resource-requirement contract** where each resource type declares what other resources it
  needs (by name, with a system-defined usage). One resource may be required by many; all usages are
  tracked and surfaced via the CLI.
- A **resource-registry** validation pass that walks all requirements, looks each up in the
  registry, and dispatches missing-name policies per kind (auto-declare for secrets; auto-declare
  restricted to `default` for templates; error for catalogs; etc.).
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
- **Resource registry**: the framework's single container consolidating resource definitions. For
  this effort, it is constrained to just config-declared resources but it could be expanded to other
  resource sources in the future. Kind is the primary dimension; within a kind, resources are looked
  up by name, so cross-kind identity is the `(kind, name)` pair. Each kind contributes its own
  **miss policy** (R2) which the registry applies when a requirement points at a `(kind, name)` not
  yet in the registry. Resources arrive in the registry through two origin paths (R4): operator
  declarations in config, and auto-declared synthesis from requirements. Queried by the validation
  pass and surfaced via `agw doctor`, `agw secret list`, and (Phase 2) `agw resource list`.
- **Resource requirement**: a **reference declaration** -- one resource saying "I need this other
  resource by name". Distinct from the resource itself: requirements point at resources but are not
  resources. Carries the target's `name` and `kind`, a system-defined `usage`, the declaring
  resource's `source` as a `(kind, name)` pair, and (optionally) per-kind defaults the registry's
  auto-declare policy may use. A resource may have many requirements pointing at it; the framework
  collects them all.
- **Usage**: the system-defined "role this resource plays for the requirement's source", set by the
  requirement. Each requirement contributes one usage; a resource that is required by multiple
  sources accumulates a list of usages. Surfaced in `agw doctor` and `agw secret describe`. Distinct
  from the operator-set description. Phrased as a short noun phrase that completes the sentence
  template `<target> is used by <source> as <usage>.` -- no capitalization except for proper nouns
  or acronyms, no trailing period, under ~50 chars. Examples: `"the VM-provisioning auth key"`,
  `"the GitHub auth token"`, `"the ANTHROPIC_API_KEY env var"`, `"a parent template"`.
- **Description**: the operator-defined free-form note about a resource (already exists today on
  secrets and `git_credentials`; this SDD formalizes the convention for new resource types).
- **Miss policy**: what the registry does when a requirement's `(kind, name)` has no match in the
  registry. Declared per kind. Two options:
  - **Auto-declare**: synthesize the resource from the kind's defaults and add it to the registry.
    The resulting resource has `origin = auto-declared`. A kind may restrict which names it accepts
    (e.g., template kinds accept only the reserved name `default`); requests for other names error.
  - **Error**: raise a config-load error citing the requirement source.
- **Origin**: the per-resource record of where the resource came from. Two values:
  `operator-declared` (from operator config; carries file path and line number for traceability) and
  `auto-declared` (synthesized to satisfy a requirement; carries the first matching requirement's
  source `(kind, name)`). Set once when the resource is added to the registry; never mutated
  afterwards. Surfaced in `agw doctor`, `agw secret list`, and `agw secret describe`.

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
- **Kind-specific extras** (optional): kind-defined defaults the auto-declare policy uses. For
  `SecretRequirement`, none are defined; the framework's default backend conventions cover the
  common case.

A resource's `required_resources()` method returns the full list (one per reference, no
deduplication at the producer side). The validation pass collects requirements across the entire
config, keyed by `(kind, name)`, and feeds them into the registry.

#### Multi-requirement resources

A single resource may be the target of multiple requirements. The framework explicitly supports
this:

- **Auto-declared resources** synthesize once per `(kind, name)`. When multiple requirements would
  trigger auto-declaration of the same name, the **first-encountered requirement** supplies any
  kind-specific extras and is recorded as the origin source (R4). Walk order is config-load order:
  top-to-bottom in the TOML file, top-level sections in their declaration order. The remaining
  requirements contribute additional usages but do not re-synthesize the resource.
- **Operator-declared resources** are unchanged by additional requirements pointing at them. The
  resource keeps the operator's fields; the requirements just add to its usage list.
- **All usages are retained**. The resource's accumulated usage list is what `agw secret describe`
  (R9) renders. Duplicate usage strings from different requirements are deduplicated for display.

### R2: Per-kind miss policies

Each kind in the registry declares its miss policy:

| Kind                                                              | Miss policy                             | Behavior                                                                                                                                                                  |
| ----------------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Secret                                                            | Auto-declare (any name)                 | A `SecretRequirement` whose name is missing synthesizes a `SecretDecl` carrying the requirement's usage. Origin: `auto-declared`. Additional requirements add more usages |
| VM template, workspace template, agent template, session template | Auto-declare (reserved name: `default`) | A requirement for `default` synthesizes the kind's code-defined default template. Origin: `auto-declared`. Any other missing name is a config-load error                  |
| Catalog command, git credential provider, secret backend kind     | Error                                   | Unknown names are config-load errors                                                                                                                                      |

Error messages include the requirement's `source` so the operator sees, e.g.:

```text
vm_template "azure-prod" references unknown agent_template "claude-experimental"
```

The framework controls error shape; each kind only declares its policy. Migrating an existing
bespoke-validation kind into the framework changes the error wording but not the validation
semantics.

**Policy is per-kind, not per-source.** The same miss policy applies regardless of which requirement
triggered the lookup. A secret requirement from an env-block reference is handled identically to one
from `tailscale_auth_key` or a git credential `token`: if the name isn't operator-declared, the
secret kind's auto-declare policy synthesizes it. Per-source policy divergence is intentionally not
supported -- it would multiply the complexity and undermine the unified model.

**Template default auto-declare is unconditional.** Template kinds always synthesize the
code-defined `default` when a requirement references it. Operators customize by declaring
`[vm_templates.default]` (or the corresponding kind) explicitly -- per-field merge (R3) applies, so
unspecified fields fall back to the framework's defaults. There is no "no default" mode.

### R3: Per-field merge between operator declarations and auto-declared defaults

When a resource is **both** operator-declared and required, the merge is per-field for
operator-settable fields:

- Operator-settable fields (`description`, `hint`, `backend_mappings`): operator wins per field (or
  per key, in the case of `backend_mappings`). Fields the operator omitted fall back to the
  requirement's defaults (if any). Per-key provenance within `backend_mappings` (operator-set vs.
  framework-default vs. backend-default-convention) is tracked and surfaced via
  `agw secret describe` (R9).
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
usage list `["the VM-provisioning auth key"]` from the VM-template requirement. The operator does
not have to retype the usage; the system fills it in automatically and updates it when new
requirements arrive.

### R4: Origin tracking on every resource

Every resource in a registry carries an `origin` field that records how it came to be in the
registry. Two origin types:

- **`operator-declared`**: the resource is declared in operator config. The origin carries the
  **file path and line number** of the declaration's opening line, scoped to the loaded TOML config
  file (e.g., `~/.config/agentworks/config.toml:42`). If multi-file config (manifests, layering) is
  added later, that SDD revisits this field. When the resource is also referenced by requirements,
  the matching requirements are retained separately (R3) but the origin stays `operator-declared`.
- **`auto-declared`**: the resource was synthesized at config-load time to satisfy a missing
  requirement. The origin carries the **first matching requirement in config-load order (R1)** --
  its `source` `(kind, name)` is recorded. Subsequent requirements that match the same name
  contribute additional usages (R1) but do not alter the origin field. Applies uniformly to
  framework-synthesized resources, including default templates (the `source` is the first template
  that referenced `default`).

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

Phase 1 ships the check but exercises nothing (secrets don't reference secrets); Phase 2 (R11)
brings template inheritance into the framework, which is where the check earns its keep.

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
   `usage="the VM-provisioning auth key"`, and `source=("vm_template", <name>)`.
3. If the named secret isn't operator-declared, the auto-declare policy synthesizes it.
4. The orchestrator resolves the secret through the configured backend chain (first wins).
5. The resolved value is threaded as a function argument to the Tailscale install runner.
   Provisioning remains hermetic: no SSH SetEnv, no profile-fragment write.

There is no opt-out at the VM template level. Tailscale is foundational to the system. Operators who
don't want Tailscale auth at all don't configure agentworks at all.

**Sharing semantics**: multiple VM templates with the default value
`tailscale_auth_key = "tailscale-auth-key"` all emit `SecretRequirement` records targeting the same
secret name. Per R1's multi-requirement handling, they share one auto-declared secret with multiple
usages. This is intentional -- operators typically use one Tailscale tailnet across all their VMs.
Operators wanting per-template isolation set a distinct `tailscale_auth_key` on each template (e.g.,
`"tailscale-auth-key-prod"`, `"tailscale-auth-key-dev"`).

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
`~/.git-credentials` on the VM. The default secret name relies on git credential entry names being
unique within `[git_credentials.*]`, which they already are by config-schema.

### R8: Operator description as a distinct field

Every resource type that supports operator declaration carries an optional `description` field that
is separate from the system-collected `usage` list:

- **`usage`** (list, system-collected) comes from the matching requirements; one entry per
  requirement. Operators do not set it. Example entry: `"the VM-provisioning auth key"`. A resource
  required by several sources has several usages.
- **`description`** (string, operator-set) is the operator's free-form note. Example:
  `"Prod tailnet auth key, 90-day expiry, owner: SRE team"`.

Both surface in `agw doctor`, `agw secret list`, and `agw secret describe` (R9). The convention is
the same for any resource type Phase 2 brings into the framework.

`description` is encouraged but not required. The validation pass emits a config-load warning when
an **operator-declared** resource has no `description`, surfacing the gap so the operator can
document their own resources. Auto-declared resources do not trigger the warning (the operator
didn't author them; demanding a description would be noise). Operators who deliberately leave the
field blank pay one warning per CLI invocation.

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
header summary becomes, e.g., `12 secrets (5 auto-declared, 7 operator-declared)`. List shows
summary; for detail, the operator runs describe.

`agw secret describe <name>` (new in Phase 1) is the per-secret detail view:

- **Name, kind, origin, description** (operator-set, when present). Origin is rendered with full
  detail: file path and line for operator-declared; the triggering requirement's `(kind, name)` for
  auto-declared.
- **All registered usages**: one row per requirement, showing the source `(kind, name)` and the
  usage text. A resource referenced by three sources shows three rows. Duplicate usage text is
  collapsed.
- **Backend mappings**: the merged table, with the source per backend (operator-set vs.
  framework-default vs. backend-default-convention).
- **Current resolution preview**: which active backend would resolve this secret right now
  (`would resolve via env-var`, `would prompt`, or `not available in any backend`). Mirrors the
  doctor preview but scoped to one secret.

Describe does not prompt and does not resolve secret values; it reports state.

### R10: Registry construction and eager-resolve scope

#### Registry construction: universal

At every CLI invocation, regardless of command, the validation pass walks the entire requirement
graph and builds the registry. This is config-load-time work: cheap, deterministic, no backend
calls. After the walk, the registry knows every declared resource and every requirement edge.

The walk is not scoped by command. `agw vm list` builds the same registry as `agw vm create`.

#### Eager-resolve scope: per-command

Eager-resolve (asking backends for actual secret values, prompting if needed) is the existing
mechanism from the env-and-secrets SDD. This SDD does not change _how_ eager-resolve works; it just
adds new candidates the resolver may consider.

Two kinds of secrets get eager-resolved per command:

- **Env-block secrets** (existing): resolved at shell-opening commands per the env-and-secrets
  contract. Unchanged.
- **Provisioning secrets** (new): resolved at provisioning commands. Scope is driven by the
  requirement subgraph of the resource(s) being provisioned in this invocation. The framework walks
  that subgraph in the (already-built) registry, collects the secret `SecretDecl`s found, and passes
  them as `extra_decls` to the existing orchestrator.

The subgraph scoping is the natural answer to "why doesn't `agent create` prompt for the Tailscale
auth key?" -- the agent template's requirement subgraph doesn't reach `tailscale_auth_key` (that's
on the VM template, which is not being provisioned at `agent create` time). Resource requirements
are walked transitively (e.g., `admin.config.git_credentials = ["github-prod"]` -->
`git_credentials:github-prod` --> `secret:git-token-github-prod`) so the manager picks up the right
depth.

Current per-command map (Phase 1 state):

| Command                                          | Resource provisioned | Subgraph root for eager-resolve             |
| ------------------------------------------------ | -------------------- | ------------------------------------------- |
| `vm create` / `vm reinit`                        | VM, admin user       | VM template + admin config                  |
| `workspace create` / `reinit`                    | workspace            | workspace template (empty in P1)            |
| `agent create` / `agent reinit`                  | agent                | agent template                              |
| Shell-opening (sessions, consoles, shells, exec) | (no provisioning)    | -- (env-block eager-resolve only, existing) |

The map is current-state, not closed: any kind that later acquires system-secret references is
covered automatically because the framework walks subgraphs by structure, not by hardcoded command
lookup tables.

### R11: Phase 2 scope (resource type migrations)

Phase 2 brings the remaining resource references under the framework. Each of the kinds below
becomes a first-class kind in the registry with its own miss policy:

- **Template inheritance**: `inherits = ["..."]` resolution for VM, workspace, agent, and session
  templates moves into the framework's validation pass. Each template kind uses the auto-declare
  (reserved name: `default`) miss policy so the implicit `default` template is formalized.
  Operator-facing behavior is unchanged; error messages get the framework's consistent shape.
- **Catalog commands**: references to catalog command names (`apt_packages = ["gh"]`,
  `system_install_commands = ["az-cli"]`, `user_install_commands = ["bun"]`) become a kind with the
  error miss policy. Error messages name the referencing scope.
- **Git credential providers**: `[git_credentials.<name>].type` references become a kind with the
  error miss policy.
- **Secret backend kinds**: `[secret_backends.<kind>]` references become a kind with the error miss
  policy.

Phase 2 is primarily a refactor: validation logic consolidates into the framework, error messages
get a consistent shape, and the codebase has one place to register new kinds. There are no
operator-facing config changes beyond improved error messages.

#### `agw resource` cross-kind inspection

Phase 2 also adds an `agw resource` command tree for cross-kind inspection of the registry. It is
deliberately scoped to fields the declaration framework defines; kind-specific details stay in the
kind-specific commands (`agw secret describe`, future `agw template describe`, ...).

```text
agw resource list [--kind <kind1,kind2,...>] [--origin operator|auto]
agw resource describe <kind> <name>
```

- `agw resource list` shows one row per declared resource across all kinds in the registry. Columns:
  kind, name, origin (with detail per R4: file:line for operator-declared, requirement source for
  auto-declared), usage count (or first usage when short), description (truncated). Filters:
  `--kind` (CSV per the cli-conventions filter pattern), `--origin` (one of `operator`, `auto`).
- `agw resource describe <kind> <name>` shows the framework-level detail view: kind, name, origin
  with full detail, all registered usages, and description. Kind-specific detail (backend mappings,
  inheritance chain, resolved fields, ...) belongs in the kind's own `describe` command. The
  two-positional shape is required because resource names are unique only _within_ a kind, not
  across kinds (a `default` secret and a `default` vm_template are different resources).

`agw resource` is gated to Phase 2 because the cross-kind view only earns its keep once multiple
kinds are in the registry; with only secrets in Phase 1 it would be redundant with `agw secret list`
/ `agw secret describe`.

## Non-goals

- **Manifest-style multi-file config**. The framework's `(kind, name)` identity and
  parser-independent validation pass leave the door open; the loader migration is its own SDD.
- **Bringing lifecycle resources into the Resource Registry**. VMs, agents, sessions, and consoles
  are resources but live in the DB and are managed via CLI commands today. Migrating them into the
  registry (with reconciliation against DB state) is reserved for a future manifest-style-config SDD
  that extends the framework's storage backend.
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

## Migration notes

Operators upgrading across this SDD see three observable changes:

- **Undeclared env-block secret references no longer error.** Under env-and-secrets, an env-block
  `{ secret = "foo" }` reference required an explicit `[secrets.foo]` block; an undeclared name was
  a config-load error. Under this SDD, that reference becomes a requirement and the secret kind's
  auto-declare policy synthesizes the missing declaration. The strict-declaration intent of
  env-and-secrets is preserved through visibility instead: `agw doctor` and `agw secret list` show
  every auto-declared secret with its origin source, so operators retain a complete view of what the
  framework inferred on their behalf. Operators who relied on the strict error behavior should add
  `[secrets.<name>]` declarations explicitly; the warning on missing `description` (R8) will prompt
  them to.
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
