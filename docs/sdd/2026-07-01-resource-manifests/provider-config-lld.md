# Phase 3 LLD: secret providers, backends, and the resolver swap

> Status: SUPERSEDED -- by [runtime-model-lld.md](runtime-model-lld.md) (2026-07-03, runtime design)
> and the Phase 5.5 capability collapse (2026-07-07, which dissolved the provider/backend split this
> document pins). Still current: the git-credential `provider` alias. Current design lives in FRD R8
> (revised), the HLA secret-backend section, and the plan's 2026-07-07 sequencing note; the body
> below is historical record.

The capability/resource split for secrets: providers are code, backends are resources. This LLD pins
the provider protocol, the registry surfaces, the manifest shape, the resolver construction swap
(including the prompt-once identity semantics that constrain it), and the consumer repoint.

## Provider protocol and registry

`agentworks/secrets/providers.py`:

```python
class SecretProvider(Protocol):
    name: str
    def validate_config(self, backend_name: str, config: Mapping[str, object]) -> Mapping[str, object]:
        """Validate and normalize a backend's provider-specific config.
        Raises ConfigError naming the offending field. Built-ins accept
        only an empty config."""
    def instantiate(self, backend_name: str, config: Mapping[str, object]) -> SecretSource:
        """Build the SecretSource for one configured backend instance."""

SECRET_PROVIDER_REGISTRY: dict[str, SecretProvider]  # built-ins: env-var, prompt
```

- Built-in providers accept no configuration (`validate_config` rejects any key). The
  `config_schema`-style plumbing is exercised end to end by a TEST-ONLY provider registered via a
  fixture (never shipped): it accepts a small schema (one required str field, one optional int) so
  validation, defaults, error framing, and config-reaches-instantiate are all covered.
- Descriptor rows: new kind module `resources/kinds/secret_provider.py` (`secret-provider`, error
  miss policy, `manifest_declarable = False`, reserved). `providers.publish_to(registry)` adds one
  row per registered provider with `Origin.built_in(source="agentworks.secrets")`. The envelope's
  existing "provided by the app" rejection covers manifest attempts.

## SecretBackendDecl (the resource)

New dataclass in `secrets/base.py`: `name`, `description` (metadata.description; the kind joins
`_DESCRIPTION_KINDS`), `provider` (str), `config` (mapping of provider-specific fields),
`declared_at` / `origin` / `references`. `referenced_resources()` emits one `ResourceReference`
(kind `secret-provider`, name = the provider field, usage "the secret provider", source
`("secret-backend", <name>)`), so a typo'd provider fails through the framework's uniform miss
policy at finalize.

Manifest shape (`secret-backend` becomes `manifest_declarable = True`):

```yaml
apiVersion: agentworks/v1
kind: secret-backend
metadata:
  name: work-vault
  description: Work 1Password vault
spec:
  provider: onepassword
  provider_config:
    vault: Work # provider-owned blob; validated by the provider
```

Decode (revised by the 2026-07-05 provider_config ruling): `spec.provider` required; provider-owned
configuration nests under `spec.provider_config` (any other top-level spec key is an error naming
the nesting rule). When the provider is registered, `validate_config` runs on the blob at decode
(errors carry the document `file:line`); when it isn't, decode defers so the framework's reference
miss policy reports the unknown provider uniformly at finalize.

**Reserved names**: `ManifestSet.publish_to` rejects operator manifests redeclaring a built-in
backend name (`env-var`, `prompt`) with a declare-a-sibling hint. The check lives at the OPERATOR
publisher (not decode, which the bundle also flows through; not `Registry.add`, whose
`builtin_override` stays `"allow"` for the TOML dual-source window and flips at Phase 5).

## Built-in backends move to the bundle

`agentworks/manifests/builtin/secret-backends.yaml` declares the `env-var` and `prompt` backends
(`spec.provider` matching, no config). `secrets.publish_to` stops publishing backend rows and
becomes the provider-descriptor publisher; the legacy `SecretBackendConfig` rows survive only via
operator TOML `[secret_backends.*]` sections (which still override the bundled rows,
operator-over-builtin "allow") until the cutover deletes that path.

## Resolver construction swap

> Superseded 2026-07-03 by [runtime-model-lld.md](runtime-model-lld.md) (Phase 3.6): there is no
> resolver at all -- backends are the door, resolution is a loop, values thread from one resolve per
> command, and the memos described below were deleted. This section stands as the historical record
> of the interim design.

Today `load_config` builds `Config.secret_resolver` from `[secret_config].backends` chain names via
zero-arg source factories, and hard-errors at PARSE time on unknown chain names. That cannot survive
manifest-declared backends (unknowable at `load_config`). The swap:

- `providers.resolver_for(registry) -> SecretResolver` (as built, after the maintainer's
  registry-purity revisit): a plain projection. The chain comes from the published
  `secret-config:default` row -- Config is never consulted after the registry exists.
  `SecretBackendDecl` rows instantiate via `SECRET_PROVIDER_REGISTRY[row.provider]`; legacy
  `SecretBackendConfig` rows instantiate via the provider matching `row.kind` (their kind IS the
  provider name). Unknown chain names cannot reach the resolver: the row's `referenced_resources()`
  makes them finalize-time miss-policy errors, and reachability plus provider instantiation run in
  the `secret-config` kind's `validate(registry)` hook (a small framework extension: finalize runs
  each kind's optional hook over the complete acyclic graph).
- **Prompt-once identity**: the resolver instance carries the per-command resolved-value cache
  (eager-resolve fills it; later renders hit it without re-prompting). Today that identity comes
  from Config carrying ONE resolver. As-built (refined at the maintainer's suggestion): the
  gathering happens once after finalize -- `build_registry`'s standard path (`manifests=None`) is
  memoized per Config object, so every default caller shares one frozen Registry, and the resolver
  is memoized per Registry instance. Prompt-once follows from registry identity with no "equal rows
  across builds" assumption, redundant registry rebuilds disappear, and an explicitly built registry
  (tests, custom orchestration) gets its own resolver matching its own rows.
- `Config.secret_resolver` is removed along with `_build_secret_resolver` and the parse-time
  chain-kind validation in `_load_secret_config`. As built the checks land at `build_registry`
  finalize (not lazily at first `resolver_for`): every resource-touching command validates the chain
  and reachability when it builds the registry, which is closer to the original parse-time semantics
  than the interim assembly-time relocation. Tests pinning the parse-time error relocate
  accordingly.
- Consumer repoint (~15 sites, as built): every consumer passes a registry --
  `resolver_for(registry)` has no config parameter. Command entries build the registry once (the
  per-config singleton makes repeats free) and thread it through the deep paths;
  `compute_needed_secrets` / `resolve_for_command` take the registry instead of the config. Every
  consumer imports `resolver_for` function-locally from `agentworks.secrets.providers`, giving the
  test stub a single seam.

## Git credential provider alias (TOML side)

`[git_credentials.<name>]` accepts `provider` as an alias for `type` (`provider` wins when both
present) so every today-valid config still loads; manifests already accept only `provider`. `type`
dies with the TOML surface in Phase 5. The `GitCredentialConfig.type` field itself is NOT renamed in
this phase (blast radius belongs to the field's consumers; the vocabulary alignment on the operator
surface is what R9 requires now).

## Inspection follow-through

- `agw resource list` shows `secret-provider` rows (built-in) and `secret-backend` rows with their
  provider references counted; `agw resource describe secret-provider env-var` lists the backends
  referencing it.
- `agw secret describe` / doctor compute per-backend conventions by asking the provider-instantiated
  sources from `resolver_for`, so future config-bearing providers render with no display changes.

## Tests

Provider registry lookup and instantiation; test-only-provider config validation (good config, bad
field, missing required, defaults) and resolution end to end; custom backend (provider env-var)
declared via manifest and placed in the chain; reserved-name rejection for `env-var`/`prompt`
operator manifests; multiple backends sharing a provider; chain naming an unknown backend (errors at
`build_registry` finalize via the secret-config row's edges); legacy TOML `[secret_backends.*]` rows
still resolving through the existing path; prompt-once identity (one registry, one resolver
instance); the secret-config kind (published row, default chain edges, bare-registry sentinel);
bundle publishes the two built-in backends with per-file built-in origins; git-credential TOML
`provider` alias (alias works, both-present precedence, `type` still works); describe/doctor
rendering; regression: the shipped sample config and a maximal today-valid TOML config load
unchanged.
