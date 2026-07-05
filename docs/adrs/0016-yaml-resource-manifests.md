# 16. YAML Resource Manifests and the Config/Resource/Capability Split

Date: 2026-07-05

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

### Three layers, one vocabulary law

Everything the CLI works with belongs to exactly one of three layers:

| Layer            | What it is                                                                      | Where it lives                            | Identity vocabulary  |
| ---------------- | ------------------------------------------------------------------------------- | ----------------------------------------- | -------------------- |
| Config           | Settings: SSH keys, paths, defaults, the active backend chain, (future) plugins | TOML (`config.toml`), the `Config` object | section/field names  |
| Resources        | Declared things: secrets, backends, templates, credentials, catalog entries     | the resource Registry, fed by publishers  | `kind` + `name`      |
| Raw capabilities | Code implementations: secret providers, VM providers, git credential providers  | per-domain provider registries            | bare capability name |

**The vocabulary law: `kind` is a resource-registry concept, full stop.** Providers (raw
capabilities) are not resources and have no kind. The resource that exposes a capability has its own
kind (for secrets: `secret-backend`), and `provider` is a field on that resource naming the
capability. Nothing outside the resource registry may use the word "kind" for its identity.

Config is just config: settings that name resources (like `[secret_config].backends`) are never
published as pseudo-resources; the owning subsystem validates them against the finalized registry at
the composition boundary (`build_registry`).

### Exposed resources are the door

A raw capability is registered code; an **exposed resource** is the declared resource that makes it
usable (optionally with configuration). ALL runtime access to a capability goes through one of its
exposed resources; the capability's invocation API is domain-owned and visible only to them. One
capability may back many exposed resources (two future `onepassword` backends pointed at different
vaults). Domain terminology varies on the exposed-resource side -- secrets say provider -> backend;
VMs will likely say provider -> platform -- while "provider" is the stable generic term for the raw
capability across domains. Symbols and kinds that are domain-specific spell the domain out
(`SECRET_PROVIDER_REGISTRY`, kind `secret-provider`); the bare word is reserved for the generic
concept and for the `provider` field on exposed resources, whose owner scopes it.

For secrets concretely: backends (`SecretBackendDecl`) own `would_attempt` / `describe_lookup` /
`resolve`; providers are stateless code invoked only from those door methods; and resolution is a
plain loop over the active backends in chain order -- no resolver object, no cache, no memos.
Prompt-once is structural (one resolve per command, values threaded to env composition), not cached.

### YAML manifests, auto-loaded, Kubernetes envelope

Operator resources are declared as YAML documents under `<config-dir>/resources/` (any file layout;
the loader walks everything), using the familiar envelope -- `apiVersion: agentworks/v1`, `kind`
(lower-kebab), `metadata` (framework-uniform: `name`, `description`), `spec` (kind-specific).
Manifests are auto-loaded whenever a command builds the registry: there is no `apply` step and no
persisted registry state to reconcile. App-bundled built-in resources (the built-in secret backends)
ship through the same loader with a `built-in` origin; future plugins reuse the mechanism.

Resource names may not contain `/` (reserved for `KIND/NAME` selectors and per-resource manifest
filenames), enforced source-independently at `Registry.add`.

### Dual-path: deprecate, don't break

TOML resource sections remain fully supported publishers into the same registry -- deprecation
warnings at load, removal deferred to a future major release. Mixing sources is supported; the same
resource declared in both is a hard error citing both locations. This is not a transitional window:
keeping both paths live forces the "different publishers, single registry" architecture to be real.
`agw resource migrate` moves resources from TOML to YAML incrementally, on the operator's schedule,
with a per-run registry-equivalence verification; `agw resource sample` is the YAML teaching
surface, while `agw config init/edit/sample` continue to own the permanent settings file.

## Consequences

- Operators declare resources as small reviewable files, one document per resource, with the same
  validation as TOML (the manifest decoders call the TOML loaders, so the two sources cannot drift).
- The registry is the single source of truth for the runtime; `Config` carries settings only.
  Consumer code reads resources through registry accessors, never `Config` attributes.
- Plugins get a paved road: resources arrive as bundled manifests with their own origin variant;
  capabilities register in per-domain provider registries. Neither requires new framework
  mechanisms.
- The secrets runtime is small enough to state in one sentence: map the configured chain onto
  backend rows, loop over them in order, batch per backend. Inspection surfaces reuse the same loop
  with an errors out-param instead of growing parallel code paths.
- Breaking change accepted knowingly: configs carrying slash-bearing quoted resource names
  (`[vm_templates."a/b"]`) stop loading, with a rename hint.

## Relationship to ADRs 0013 and 0014

ADR 0013's decision (CLI-side secret injection at command time -- no VM-side secret storage) and ADR
0014's decision (`AcceptEnv AW_*` wildcard transport via SSH `SetEnv`) both stand unchanged. What
this ADR supersedes is the resolution _mechanism_ those documents describe in passing: the "env var,
then prompt" sourcing is no longer a hardcoded resolver but the default backend chain
(`[secret_config].backends = ["env-var", "prompt"]`) over declared backend resources, resolved by
the loop described above. Where 0013/0014 say "the CLI resolves secret values", read "the active
backend chain resolves them through the backends-are-the-door model".
