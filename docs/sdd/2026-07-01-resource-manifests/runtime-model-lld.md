# Runtime model LLD: backends are the door

Status: DRAFT for maintainer review. Nothing below is implemented; this replaces the secrets runtime
layer (which predates this SDD) and supersedes the interim resolver plumbing built during Phases
3-3.5 (memos, registry-purity threading, the secret-config row experiment).

## The three layers, and the vocabulary law

| Layer        | What it is                                                                        | Where it lives                                                   | Identity vocabulary  |
| ------------ | --------------------------------------------------------------------------------- | ---------------------------------------------------------------- | -------------------- |
| Config       | Settings: ssh keys, prefs, the active backend chain, (future) active plugins      | TOML, `Config`                                                   | section/field names  |
| Resources    | Declared things: secrets, backends, templates, credentials                        | the resource Registry, fed by publishers                         | `kind` + `name`      |
| Capabilities | Code implementations: secret providers, VM provisioners, git credential providers | capability registries (static today, plugin-registered tomorrow) | bare capability name |

**The vocabulary law: `kind` is a resource-registry concept, full stop.** Providers are not
resources and have no kind. Backends are resources whose kind is `secret-backend`; `provider` is a
field on that resource naming a capability. Nothing outside the resource registry may use the word
"kind" for its identity.

Capability registries have their own API surfaces: a registration side (today a static dict,
tomorrow `register_provider(...)` for plugins) and an invocation side. **The invocation side is
exposed only to the resource objects that instantiate the capability** -- backends call their
provider; nothing else touches the provider API.

## Backends are the door

All secret operations are methods on the backend resource (`SecretBackendDecl`). A backend owns its
mapping resolution and invokes its provider through the provider API:

```python
@dataclass(frozen=True)
class SecretBackendDecl:
    name: str                      # the ONLY identity runtime surfaces use
    provider: str                  # capability name; a field, not an identity
    description: str = ""
    config: dict[str, object] = ...
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

This also names the reusable pattern for the schema wrinkle: **a capability-instantiating resource
kind delegates its provider-specific spec tail to the named capability's `validate_config`.**
`secret-backend` does this at manifest decode today; VM provisioner-flavored resources adopt the
same shape when their time comes.

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
  resolution, per FRD R10.

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
  (`agw resource describe secret-backend op-work` shows `provider: onepassword`).
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
