# Runtime model LLD: backends are the door

Status: IMPLEMENTED (Phase 3.6, 2026-07-03); REVISED by the Phase 5.5 capability collapse
(2026-07-07). The loop, prompt-once, batching, mapping, and inspection semantics pinned here are
unchanged; what the collapse changed is WHO the door is -- the provider/backend split dissolved, so
the door methods live on the capability itself (protocol `SecretBackend`, registry
`SECRET_BACKEND_REGISTRY`, one descriptor row per capability). Read the two-row model
(`secret-provider` descriptors + `secret-backend` resources) below as the pre-collapse shape.
Current model: FRD R8 (revised), the HLA secret-backend section, and ADR 0016. (The capability API
carries `would_attempt(secret, mapping)` alongside `describe_lookup`, per FRD R4's soft-skip
semantics for backends without default conventions.)

## Part 1: the general pattern (all capability-backed domains)

| Layer            | What it is                                                                        | Where it lives                                                            | Identity vocabulary  |
| ---------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------- | -------------------- |
| Config           | Settings: ssh keys, prefs, the active backend chain, (future) active plugins      | TOML, `Config`                                                            | section/field names  |
| Resources        | Declared things: secrets, backends, templates, credentials                        | the resource Registry, fed by publishers                                  | `kind` + `name`      |
| Raw capabilities | Code implementations: secret providers, VM provisioners, git credential providers | per-domain provider registries (static today, plugin-registered tomorrow) | bare capability name |

**The vocabulary law: `kind` is a resource-registry concept, full stop.** Providers are not
resources and have no kind. The resource that exposes a capability has its own kind (for secrets:
`secret-backend`), and `provider` is a field on that resource naming the capability. Nothing outside
the resource registry may use the word "kind" for its identity.

The general pattern, stated once:

- **Raw capability**: code, registered in a per-domain provider registry. A plugin brings the code
  and registers it.
- **Exposed resource**: a declared resource that exposes the capability as a usable thing,
  optionally with config. "Exposed" means it exposes the capability -- the resource is the
  capability's public face -- not merely "visible in the registry" (every resource is visible). A
  plugin (or operator) declares these like any other resource; the resource's kind delegates its
  `spec.provider_config` blob to the named capability's `validate_config` at decode. One raw
  capability may back many exposed resources.
- **Exposed resources are the door**: ALL runtime access to a capability goes through one of its
  exposed resources. The capability's invocation API is domain-owned and visible only to its exposed
  resources; nothing else calls a provider. (Part 2's title is this principle's secrets
  instantiation.)
- **Domain terminology varies**: secrets say provider -> backend; VMs will likely say provider ->
  platform; git credentials will get their own words when their turn comes. "Provider" is likely the
  stable half; the exposed-resource noun belongs to the domain.
- **Registries are per-domain**: secret providers, VM provisioners, and git credential providers
  each have their own registry today. The registration _shape_ should stay common so plugin wiring
  is uniform, but what the capabilities DO is wildly different, so each domain owns its invocation
  API. Whether a universal provider registry ever earns its keep is deliberately undecided and out
  of scope here.

Everything past this line is **secrets only**. The pattern above is the template; the rest of this
document is its secrets instantiation, and nothing below should be read as prescribing VM or git
credential design.

## Part 2: secrets -- backends are the door

All secret operations are methods on the backend resource (`SecretBackendDecl`). A backend owns its
mapping resolution and invokes its provider through the provider API:

```python
@dataclass(frozen=True)
class SecretBackendDecl:
    name: str                      # the ONLY identity runtime surfaces use
    provider: str                  # capability name; a field, not an identity
    description: str = ""
    provider_config: dict[str, object] = ...  # spec.provider_config blob
    # declared_at / origin / references as today

    def mapping_for(self, secret: SecretDecl) -> Mapping:
        # keyed by BACKEND NAME (self.name), not by provider
        return secret.backend_mappings.get(self.name)

    def would_attempt(self, secret) -> bool      # False mapping -> opt out (generic, provider never sees it)
    def describe_lookup(self, secret) -> str | None
    def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]   # batch; invokes provider
```

The provider API is what backends need and nothing more (consumed only by `SecretBackendDecl`
methods; `validate_config` also runs at manifest decode):

```python
class SecretProvider(Protocol):
    name: str
    def validate_config(self, backend_name, config) -> config        # decode-time schema
    def describe_lookup(self, config, secret, mapping) -> str | None # convention or mapping
    def batch_get(self, config, wants: list[tuple[SecretDecl, Mapping]]) -> dict[str, str]
```

Providers are stateless: every call receives the backend's `config`. Default conventions
(`AW_SECRET_<NAME>`) are provider logic, applied per call. The opt-out (`mapping is False`) is
handled generically by the backend and never reaches the provider. Hard-miss semantics are preserved
at the provider layer: a persistent-store provider raises `SecretMappingError` when an explicit
mapping definitively has no value (halting the chain so a store misconfiguration doesn't fall
through to a masking prompt); env-style providers soft-miss by omitting the secret from
`batch_get`'s result.

`backend_mappings` on secrets are keyed by **backend name**. The built-in backends are unaffected in
practice because each one's name coincides with its provider's name (`env-var`, `prompt`) --
documented as a naming choice, never relied on in code. Two onepassword backends (`op-work`,
`op-personal`) get independent mappings, opt-outs, and describe rows.

Schema delegation follows Part 1's pattern: `secret-backend` decode pops `provider` and hands the
`spec.provider_config` blob to the named capability's `validate_config` (pattern established
2026-07-05: provider-owned config nests; the rest of the spec is provider-agnostic).

## Resolution is a loop, not an object

```python
def resolve_secrets(secrets: list[SecretDecl], backends: list[SecretBackendDecl]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for backend in backends:                       # chain order = precedence
        wanted = [s for s in secrets if s.name not in resolved and backend.would_attempt(s)]
        resolved.update(backend.resolve(wanted))   # batch per backend
    missing = [s for s in secrets if s.name not in resolved]
    if missing:
        raise SecretUnavailableError(...)          # per-secret list of backends tried
    return resolved
```

`active_backends(config, registry) -> list[SecretBackendDecl]` maps the chain (config) onto backend
rows (resources), in order. That, plus the loop above, is the entire runtime.

The loop carries both failure policies via an optional `errors` out-param (same idiom as the config
loaders' `issues`): `None` (commands) is the all-or-nothing raise shown above; a dict (inspection
surfaces, e.g. `env show --reveal-secrets`) collects per-secret failures and returns partial values
from the SAME single pass -- one loop, one code path, and already-answered prompts are never
discarded and re-asked.

**No caching.** There is no resolver object, no per-command value cache, no memo. A command resolves
once at its entry point and passes the **values** down; "prompt-once" is true by construction
because there is exactly one resolve call per command. Caching across CLI invocations (a
keyring-style daemon) would be a different feature with different security properties -- explicitly
out of scope.

## Composition root

A command's entry point composes the layers once, top to bottom:

```text
config = load_config(...)                       # settings
registry = build_registry(config)               # publish -> finalize -> validate_chain(config, registry)
backends = active_backends(config, registry)    # chain names -> backend rows
values = resolve_for_command(targets, config, registry)   # ONE resolve; returns {name: value}
... values travel down the call chain to compose_env sites ...
```

- `validate_chain(config, registry)` stays in `build_registry` (Phase 3.5's timing survives): chain
  names must be backend rows, backend configs must satisfy their providers, every operator-declared
  secret must be reachable. Config vocabulary in every error.
- `compute_needed_secrets` / `resolve_for_command` keep their orchestration roles;
  `resolve_for_command` returns the values dict that the command threads to its `compose_env` sites
  (the same scope-dict discipline that already keeps the eager-resolve set and the render set from
  drifting).
- `compose_env(values=..., ...)` renders `EnvEntry(secret=name)` from the values dict and raises
  loudly on a secret absent from it -- drift between "what was resolved" and "what is rendered" is a
  bug, not a fall-through.
- Inspection surfaces (`agw secret list/describe`, doctor, `env show`) call `would_attempt` /
  `describe_lookup` directly on the active backends. They display **backend names**. No resolver, no
  resolution, per FRD R8.

## What this deletes

- `SecretSource` protocol, `SecretSourceBase`, `EnvVarSource`, `PromptSource` (their logic becomes
  the `env-var` / `prompt` providers), and the word "source" from the secrets vocabulary.
- `SecretResolver`, `resolver_for`, the `_RESOLVERS` WeakKeyDictionary.
- `_STANDARD_REGISTRIES` + the weakref eviction callback: with the registry built once at the
  composition root and threaded (already true after Phase 3.5), nothing builds it twice.
  `build_registry` becomes a pure function.
- `SecretSource.kind` and every surface keyed or rendered by it.
- `SecretBackendConfig` (the legacy kind-keyed TOML row type; the sections are deprecated no-ops).
- The secret-config registry row/kind and the `validate`/`miss_hint` framework hooks (already
  unwound in the working tree; land with this pass).

## What this fixes

- Multiple backends per provider actually work end to end: independent mappings, opt-outs, describe
  rows, chain positions.
- Operator surfaces speak backend names everywhere; providers appear only as a field
  (`agw resource describe secret-backend/op-work` shows `provider: onepassword`).
- The weakref/memo machinery and its failure modes are gone; prompt-once is structural.
- The provider API is private to backends, so a future plugin provider has exactly one contract to
  satisfy and no way to be misused from the outside.

## Test surface (sketch)

Backend-door unit tests (mapping keyed by name, opt-out generic, provider invoked with backend
config); two-backends-one-provider end to end (the current conflation test inverts: `sibling-env`
must present as `sibling-env`); resolve-loop semantics (precedence, hard-miss halt, unavailable
error content); one-resolve-per-command pinned at the orchestration layer (spy on `batch_get`, not
on a cache); compose drift error; inspection surfaces render backend names. Existing
declaration-layer tests (decode, publish, reserved names, provider descriptors) carry over.
