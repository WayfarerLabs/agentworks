# 16. YAML Resource Manifests and the Config/Resource/Capability Split

Date: 2026-07-05 (amended 2026-07-07, twice: the capability collapse -- resources reference
capabilities directly, the declarable secret-backend layer removed and the capability taking the
name -- and the resource-definition expansion -- capability rows ARE resources, of capability kinds)

## Status

Accepted

Supersedes the resolver/source _mechanism_ described in ADR 0013 and ADR 0014 (their decisions
stand; see "Relationship to ADRs 0013 and 0014" below).

## Context

The resource registry gave agentworks a publisher-agnostic home for named, referenceable entities
(secrets, templates, git credentials, catalog entries): publishers add Resources, the registry
finalizes (auto-declares, attaches references, detects cycles) and freezes, and the runtime reads
only from the registry. Until this decision, the only operator publisher was `config.toml` -- one
sprawling TOML file holding both machine settings and resource declarations, with single conceptual
resources split across sections (`[session_templates.x]` plus `[session_templates.x.env]`).

Three pressures converged:

1. Operators want resources as reviewable, shareable files rather than sections of one config.
2. The app itself (and future plugins) needs to ship built-in resources through the same mechanism
   operators use, not through bespoke code paths.
3. The secrets subsystem had conflated three different things -- settings, resources, and code
   capabilities -- and the resulting interim designs (resolver objects, caches, pseudo-resource rows
   for configuration) kept telling us the model was wrong.

## Decision

### Two layers, one vocabulary law

Everything the CLI works with belongs to exactly one of two layers:

| Layer     | What it is                                                                      | Where it lives                            | Identity vocabulary |
| --------- | ------------------------------------------------------------------------------- | ----------------------------------------- | ------------------- |
| Config    | Settings: SSH keys, paths, defaults, the active backend chain, (future) plugins | TOML (`config.toml`), the `Config` object | section/field names |
| Resources | Named, referenceable things: secrets, templates, credentials, capabilities      | the resource Registry, fed by publishers  | `kind` + `name`     |

**A resource is a named, referenceable registry entry -- `kind` + `name` -- regardless of where it
comes from.** (Definition expanded 2026-07-07. An earlier revision held capabilities apart as "not
resources, merely mirrored in as descriptor rows" -- a distinction carried entirely in prose,
invisible on every surface, that confused every reader of `agw resource list`, its authors included.
The registry's operational definition of a resource is shape -- a kind, a name, references, an
origin -- and capability rows satisfy all of it.) Kinds split by whether operators can declare them:

- **Declarable kinds** hold data: operator-declared (TOML/YAML), auto-declared, or built-in.
- **Capability kinds** (today `secret-backend` and `git-credential-provider`) hold read-only
  **capability resources**, registered by the app (or, later, plugins) rather than declared. Their
  implementation is code in a per-domain registry (`SECRET_BACKEND_REGISTRY`, keyed by the resource
  name); the manifest loader rejects documents of these kinds with a "provided by the app" error.

The classifier is a per-kind field (`ResourceKind.category`) -- two resources of one kind can never
differ here -- so its display home is `agw resource kinds` (the read-only, code-defined kind
inventory: category, row count, description per kind), not a per-row column. Kinds are baked into
the app: plugins publish resources of existing kinds, declarable and capability alike (a harness
plugin ships its session templates; a VM-provider plugin ships a default platform), never new kinds.

**The vocabulary law: `kind` is a resource-registry concept, full stop.** Nothing outside the
resource registry may use the word "kind" for its identity -- lifecycle entities (VMs, workspaces,
agents, sessions, consoles) are NOT resources, and code calls their type `instance_kind`.

Config is just config: settings that name resources (like `[secret_config].backends`) are never
published as pseudo-resources; the owning subsystem validates them against the finalized registry at
the composition boundary (`build_registry`).

### Resources reference capabilities

A capability is a resource whose implementation is registered code: it enters the registry as a
read-only capability-kind row, so references to it validate through the ordinary framework machinery
and it lists and describes like everything else. Other resources reference capabilities **directly,
many-to-one**: a `git-credential` names its provider, a secret's mappings name backends, a (future)
template names its harness. There is no dedicated "exposure" layer between a resource and a
capability -- an earlier revision of this decision had one (`secret-backend` as a declarable
instantiation of `secret-provider`), and it was removed (2026-07-07) once it became clear the
instance identity carried no content: whether a kind exists is ordinary domain modeling (is there a
real noun operators reason about, referenced from more than one place?), not a pattern requirement.
A credential is such a noun; "a configured place to create VMs" (the plugin SDD's vm-platform) is
such a noun; "a backend instance" was not. If a capability someday genuinely needs multiple
configured instances (two 1Password accounts with different credentials), a declarable instance kind
for THAT capability is an additive graduation, not a redesign.

**Naming**: each domain calls its capability by its natural noun -- `secret-backend`,
`git-credential-provider`, (future) `vm-provider` / `harness` -- adding a disambiguating suffix only
when the bare noun would collide with a resource kind or lifecycle entity. Symbols spell the domain
out (`SECRET_BACKEND_REGISTRY`). The bare word "provider" remains the generic cross-domain term for
the pattern, the conventional name for capability-reference fields (`spec.provider`, where the
owning resource makes the domain clear), and the (future, not certain) possibility of a generic
provider registry.

Capabilities can optionally carry configuration, and its nature and shape is entirely
capability-specific. For example, an AZDO git credential provider requires an organization name; a
(future) Azure VM provider may need a subscription ID; a (future) 1Password secret backend may need
an account URL. Where the reference site is a resource spec, that configuration is limited to the
`spec.provider_config` key: an opaque blob the named capability owns and validates, so the rest of
the spec stays provider-agnostic. Fields specific to the resource's kind are generic by definition
and live at the top level of the resource spec (a `git-credential`'s `token` belongs to every
credential, while `azdo`'s `org` nests). Where the reference site is per-secret
(`backend_mappings`), the structured mapping value carries the per-secret addressing (a vault, item,
and field) -- same principle, capability-owned content at the reference site.

The INTERNAL resource representation follows the nested shape too
(`GitCredentialConfig.provider_config`) as this represents the best representation available. For
backwards compatibility, we continue to support the legacy TOML shapes which aren't as clean.
However, the flat TOML section is the ONLY domain where provider-owned fields are allowed to sit
outside the `provider_config` blob. The TOML loaders translate into the nested shape at their
boundary. Decoders reshape before calling the shared loaders, so validation stays TOML-shared while
the YAML surface and the internal model stay uniform.

For secrets concretely: `SecretBackend` is an ordinary well-defined API (`would_attempt` /
`describe_lookup` / `batch_get`) abstracting where secrets actually come from, and resolution is a
plain loop over the active chain (`[secret_config].backends`, capability names in precedence order)
-- no resolver object, no cache, no memos. Prompt-once is structural (one resolve per command,
values threaded to env composition), not cached. Per-secret behavior (identifier overrides,
opt-outs, store addressing) lives in `backend_mappings`, keyed by backend name.

### YAML manifests, auto-loaded, Kubernetes envelope

Operator resources are declared as YAML documents under `<config-dir>/resources/` (any file layout;
the loader walks everything), using the familiar envelope -- `apiVersion: agentworks/v1`, `kind`
(lower-kebab), `metadata` (framework-uniform: `name`, `description`), `spec` (kind-specific).
Manifests are auto-loaded whenever a command builds the registry: there is no `apply` step and no
persisted registry state to reconcile. App-bundled built-in resources ship through the same loader
with a `built-in` origin (the bundle is wired and currently empty; future built-ins and plugins
reuse the mechanism).

Resource names may not contain `/` (reserved for `KIND/NAME` selectors and per-resource manifest
filenames), enforced source-independently at `Registry.add`. That ban makes `kind/name` the one
parse-safe display syntax, and it is uniform everywhere a typed name appears: CLI tokens
(`resource describe secret/npm-token`, `resource migrate vm-template/dev`), rendered output
(headers, references, auto-declared descriptions, `--names-only`), and the live-instance lines in
`Used by:` sections (`session/foo`). Lifecycle entities (VMs, workspaces, agents, sessions,
consoles) are NOT registry kinds -- the vocabulary law is unchanged, and code keeps calling their
type `instance_kind` -- but they share the display syntax: the section context, not the punctuation,
tells the reader whether a pair is a config resource or a live instance.

### Dual-path: deprecate, don't break

TOML resource sections remain fully supported publishers into the same registry -- deprecation
warnings at load, removal deferred to a future major release. Mixing sources is supported; the same
resource declared in both is a hard error citing both locations. This is not a transitional window:
keeping both paths live forces the "different publishers, single registry" architecture to be real.
`agw resource migrate` moves resources from TOML to YAML incrementally, on the operator's schedule,
with a per-run registry-equivalence verification; `agw resource sample` is the YAML teaching
surface, while `agw config init/edit/sample` continue to own the permanent settings file.

## Consequences

- Operators declare resources as small reviewable files, grouping resources into any number of YAML
  manifests, with the same validation as TOML (the manifest decoders call the TOML loaders, so the
  two sources cannot drift).
- The registry is the single source of truth for the runtime; `Config` carries settings only.
  Consumer code reads resources through registry accessors, never `Config` attributes.
- Plugins get a paved road: resources arrive as bundled manifests with their own origin variant;
  capabilities register in per-domain capability registries. Neither requires new framework
  mechanisms.
- The secrets runtime is small enough to state in one sentence: map the configured chain onto the
  registered backends, loop over them in order, batch per backend. Inspection surfaces reuse the
  same loop with an errors out-param instead of growing parallel code paths.
- Breaking change accepted knowingly: configs carrying slash-bearing quoted resource names
  (`[vm_templates."a/b"]`) stop loading, with a rename hint.

## Relationship to ADRs 0013 and 0014

ADR 0013's decision (CLI-side secret injection at command time -- no VM-side secret storage) and ADR
0014's decision (`AcceptEnv AW_*` wildcard transport via SSH `SetEnv`) both stand unchanged. What
this ADR supersedes is the resolution _mechanism_ those documents describe in passing: the "env var,
then prompt" sourcing is no longer a hardcoded resolver but the default backend chain
(`[secret_config].backends = ["env-var", "prompt"]`) over registered backend capabilities, resolved
by the loop described above. Where 0013/0014 say "the CLI resolves secret values", read "the active
backend chain resolves them through the `SecretBackend` API".
