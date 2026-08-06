# LLD: Step 2.0, Capability-Kind Descriptor Adoption

Date: 2026-08-05

Status: REVIEWED, UNDER IMPLEMENTATION (section 10 steps 1-3 landed 2026-08-05). Companion to
[frd.md](frd.md), [hla.md](hla.md) (Component 0), [plan.md](plan.md) (step 2.0). Authority:
`../2026-08-04-next-steps/capability-descriptor-contract.md` (the design decision record; where it
says "settled" this LLD builds, where it lists open questions for the wave 2 seed this LLD settles
them). Deviations found during implementation are recorded inline against the section they revise.

Step 2.0 collapses the capability-kind switchboard onto a single descriptor table. Seven sites today
independently enumerate the four capability kinds (`vm-platform`, `harness-integration`,
`git-credential-provider`, `secret-backend`); each becomes a derived view of one frozen table. The
work is mechanical and always-green: no schema modeling here, no behavior change, one site per
commit with the full gate green after each. Phase 2's per-kind modeling (2.1/2.3/2.5) then registers
one config model per capability implementation, resolved by consuming kind (section 7).

## 1. The `CapabilityKindDescriptor` record

One frozen, typed, core-owned record per kind. Illustrative shape (day-one fields per the contract;
final types are the implementer's, verified against HEAD):

```python
class RegistryPolicy(Enum):
    CLASS_BY_NAME = "class-by-name"                    # registry stores the impl class, keyed by name
    CONSTRUCTED_SINGLETON = "constructed-singleton"    # interim (secret-backend only); wave 3 removes

@dataclass(frozen=True)
class HostSurface:
    """How a capability kind is selected inside a declarable kind's spec (site 6)."""
    host_kind: str                                     # "vm-site" | "git-credential" | "session-template"
    naming_field: str                                  # "platform" | "provider" | "harness_integration"
    config_field: str                                  # "platform_config" | ...
    legacy_string_shape: Literal["accept-warn", "reject"]  # 2.4 flips accept-warn -> reject

@dataclass(frozen=True)
class CapabilityKindDescriptor:
    kind: str
    contract_version: int                              # day-one (operator ruling); check is trivial until it matters
    implementation_contract: type                      # Capability (ABC) for three; SecretBackend (Protocol) for secret-backend
    registry_policy: RegistryPolicy
    registry: Callable[[], MutableMapping[str, object]]    # lazy accessor (cycle-safe, see section 2)
    required_operations: frozenset[str]                # domain-op names the framework depends on
    # (no config field at 2.0; see the deferred list below and section 7)
    entry_factory: Callable[[str, object, "Origin | None"], object]   # builds the read-only row (section 8)
    kind_strategy: "ResourceKind"                      # the SAME object registered in KIND_REGISTRY[kind]
    readiness: Callable[[str, object], "Readiness"]    # capability-NODE host-support readiness (section 5)
    publisher_source: str                              # built-in Origin source label, preserved byte-for-byte
    manifest_section: HostSurface | None               # None for map-keyed secret-backend
    # Deferred fields, recorded as comments with their trigger (contract's minimal-by-rule):
    #   config_schema            -> step 2.3, when the first config model registers. Holds the kind's model
    #                               CONTRACT (the base every impl's model must extend), which step 2.1 defines,
    #                               so it cannot be typed before then. Implementations declare the model, not
    #                               the descriptor; resolution is keyed by consuming kind (section 7).
    #   consumer_gating          -> the first NEW consuming surface that consolidates gating (waves 3, 4).
    #                               Wave 2 changes no gating behavior, so no field yet.
    #   migration_participation  -> only if wave 2 rules `agw resource migrate` survives AND should derive
    #                               from the live descriptor. Counterargument stands: the migrator is a
    #                               deliberately independent frozen oracle and may never derive from live wiring.
```

Two adjustments to the contract's illustrative list, settled here:

- `union_assembly` is NOT a stored per-record field. Assembly is uniform across kinds (the framework
  builds one discriminated union per `(kind, consuming kind)` pair at the registration boundary and
  caches it), so it has no per-kind variation to carry. It is a framework operation owned by
  Component 3 (step 2.3), not a descriptor field. The contract's field list is explicitly
  "illustrative, not final code," so this is a settlement, not a contradiction.
- The config field is ABSENT at 2.0, not present-and-empty (revised 2026-08-05 when slots were
  rescinded; see section 7). No models exist until step 2.3, the field cannot be typed until step
  2.1 defines the base model, and the original reason to carry it early ("wave 4 adds slots without
  reshaping the record") died with the slot mechanism. It is recorded as a deferred field with its
  trigger instead.

Snapshot/restore needs no field: it iterates the table (section 5), so participation is membership.

## 2. The single table, and where it lives

`CAPABILITY_DESCRIPTORS`: the four records, the only capability-kind enumeration the seven sites
read. Each capability package contributes its own record (mirroring today's per-package
`publish_to`), and a small accessor collects them. Because early modules (`resources/graph.py`,
`plugins/adapters.py`, `manifests/decode.py`) are imported before the capability packages, the table
is resolved LAZILY, exactly as `_CAPABILITY_REGISTRY_LOADERS` (`resources/graph.py:586`) already
does: the `registry` field is a callable, and consumers read the table inside their functions, not
at module import. This inherits the existing cycle discipline rather than inventing a new one.

> **Spelling, as implemented (2026-08-05).** The table is reached through a `@cache`d accessor
> `capability_descriptors()` (plus `descriptor_for(kind)`) in `capabilities/descriptor.py`, not a
> module-level `CAPABILITY_DESCRIPTORS` constant: a constant cannot satisfy this section's own lazy
> requirement without importing the four capability packages at module load, which is the dependency
> inversion the lazy discipline exists to prevent. The four package imports live inside the accessor
> body, mirroring `_CAPABILITY_REGISTRY_LOADERS`. The accessor returns a tuple in today's registry
> order (vm-platform, harness-integration, git-credential-provider, secret-backend), which is the
> order step 4's snapshot tuple wants. Where sections 3, 10, and 12 write `CAPABILITY_DESCRIPTORS`,
> read `capability_descriptors()`. The lazy discipline buys CYCLE SAFETY, not import deferral:
> anything importing `resources.kinds` now transitively loads the capability packages.
>
> **The same constraint applied one level down at every derived site**, so each of these is a cached
> accessor rather than the module-level constant the LLD writes, for the identical reason (the
> consuming module loads before the capability packages): `_CAPABILITY_KINDS` reads as
> `_capability_kinds()`, `_CAPABILITY_REGISTRY_LOADERS` as `_capability_registry_loaders()`,
> `CAPABILITY_ADAPTERS` as `capability_adapters()`, and `CAPABILITY_FIELDS` as `capability_fields()`
> (with an internal `_host_surfaces()`). Caching is safe because every cached value derives only
> from frozen module-level records and caches ACCESSORS rather than registries, so plugin seating
> and snapshot/restore stay fully visible. Read sections 3, 6, 10, and 12 through these spellings.

`KIND_REGISTRY` stays the all-kinds runtime map; the descriptor is the capability-kind SWITCHBOARD
enumeration. Their relationship is pinned, not merged: the guard (section 12) asserts
`descriptor.kind_strategy is KIND_REGISTRY[descriptor.kind]` and that the descriptor kinds equal
`KIND_REGISTRY`'s `category == "capability"` kinds. The four capability
`KIND_REGISTRY[...] = ...(...)` lines (`vms/kinds.py:170`,
`capabilities/harness_integration/kinds.py:71`, `capabilities/git_credential/kinds.py:112`,
`secrets/kinds.py:219`) STAY where they are (co-located with each kind's definition, like every
declarable kind); the descriptor references the same strategy object. This keeps `KIND_REGISTRY`'s
populator local while making the descriptor the single thing the switchboard derives from. (Lead
decision, confirmed: leave them co-located; the reasoning is in section 13.)

## 3. The generic adapter

The four hand-written five-method adapters (`plugins/adapters.py:91-208`) collapse into one
`_DescriptorAdapter(descriptor)` implementing the existing `CapabilityAdapter` Protocol
(`adapters.py:46`). `CAPABILITY_ADAPTERS` becomes derived:

```python
CAPABILITY_ADAPTERS = {d.kind: _DescriptorAdapter(d) for d in CAPABILITY_DESCRIPTORS}
```

The five methods read descriptor fields, branching only on `registry_policy` for the one asymmetry:

```python
def peek(self, name):        return self.d.registry().get(name)
def matches(self, occ, cls): return (type(occ) is cls) if self.d.registry_policy is CONSTRUCTED_SINGLETON else (occ is cls)
def prepare(self, cls):      return cls() if self.d.registry_policy is CONSTRUCTED_SINGLETON else cls
def seat(self, name, load):  self.d.registry()[name] = load
def build_row(self, name, origin):
    seated = self.d.registry().get(name)
    if seated is None: raise StateError(_unseated_message(self.d.kind, name))
    return self.d.entry_factory(name, seated, origin)
```

The three design points the current adapters pin (instance trap confined to `prepare`,
exact-identity `matches`, `build_row` reads the seated impl) are preserved by construction: they are
exactly the `registry_policy` branch plus the seated-read. Domain operations stay domain-owned: the
adapter never touches `VMPlatform.create`, `SecretBackend.batch_get`, credential fill, etc. Those
live on each kind's own interface and are invoked by their domains, never through the descriptor
(contract's "what stays domain-owned").

## 4. Registration-time conformance

The descriptor makes trust-but-verify enforceable at registration, replacing the `type`-and-`cast`
seam (the `cast("type[VMPlatform]", ...)` in each adapter's `seat` today plus the loose
`isinstance(impl, type)` gate). The checks run inside `register_plugin`'s pass 1
(`_validate_descriptor`, `plugins/registration.py:60`), BEFORE any registry mutation, so atomic
seating (prepare-all-then-seat) is unchanged. Per impl, keyed by its kind's descriptor:

1. **Implementation-contract conformance**, branched by contract shape (the contract is NOT uniform,
   verified at HEAD):
   - The three ABC kinds (`Capability`-derived):
     `issubclass(impl, descriptor.implementation_contract)`.
   - The secret-backend kind: `issubclass` is NOT usable. `SecretBackend` (`secrets/backends.py:58`)
     is a plain `Protocol`, not `@runtime_checkable`, and its `name`/`description`/`interactive` are
     `@property` members (`backends.py:95-102`); `issubclass()` against a Protocol with non-method
     members raises `TypeError` even after adding `@runtime_checkable`. So the Protocol kind is
     conformance-checked STRUCTURALLY, by the required metadata (check 2) and required operations
     (check 4) being present on the impl, which is the real enforcement for a Protocol anyway. The
     descriptor's `implementation_contract` for secret-backend is documentary (the target Protocol)
     rather than an `issubclass` argument.
2. **Required metadata present**: `name` (non-empty, `/`-free `str`) and `description` (`str`),
   readable at class level. Concrete impls expose these as class attributes uniformly, including the
   secret backends whose Protocol declares them as properties (verified: `secrets/env_var.py:42-43`,
   `secrets/prompt.py:39-40`, `plugins/onepassword/backend.py:248-249` all set `name`/`description`
   as class attributes), so the check reads them off the class without constructing.
3. **Side-effect-free constructibility check** (settled below).
4. **Required operations implemented**: every name in `descriptor.required_operations` is present
   and callable on the impl.
5. **Config model conformance**: every registered config model conforms to the kind's model
   contract. Does not exist at 2.0 (no models, and no config field on the record); arrives with step
   2.3 alongside the deferred `config_schema` field. Nothing is claimed-but-empty, because an
   implementation either declares a model or does not.
6. **`contract_version` compatibility**: `impl` declares a `contract_version`; the check compares it
   to the kind's supported version. Trivial from day one (single version), so the discipline
   predates the first incompatible change.

### The constructibility check, settled

The check is purely STRUCTURAL and NEVER calls `impl(...)`:

- `inspect.isabstract(impl) is False` (no unimplemented `@abstractmethod`), decisive for the three
  ABC kinds. The abstract domain ops live on the per-kind capability bases, not on the shared
  `Capability` ABC (`capabilities/base.py:276`, which has zero abstractmethods, only ClassVars and
  concrete no-op preflight/runup defaults): `vm_platform/base.py` (6 abstract ops),
  `harness_integration/base.py` (3), `git_credential/base.py` (3). `isabstract` is thus decisive
  because a concrete impl must implement its kind base's abstract ops.
- The metadata and required-operation attributes (checks 2 and 4) are present, which for the
  Protocol kind (secret-backend, no ABC to leave abstract) is the real structural enforcement.

Keeping the check construction-free is the whole point: it never constructs regardless of registry
policy (only check 1 branches by contract shape, above; constructibility itself is uniform), so it
is independent of the interim singleton. The `impl()` call that DOES happen for secret-backend is
the `CONSTRUCTED_SINGLETON` `prepare` step (pass 2, `registration.py:126`), the registry-payload
construction, not the conformance check. That call stays fallible-and-caught as today
(`registration.py:127`), and it dies during the precheck (no mutation), preserving atomicity. When
wave 3 removes the singleton policy, the constructibility check does not change, because it never
depended on construction.

## 5. Readiness, publisher, registry loaders, snapshot

**Registry loaders** (`_CAPABILITY_REGISTRY_LOADERS`, `graph.py:586`) ARE the descriptor's
`registry` field: `{d.kind: d.registry for d in CAPABILITY_DESCRIPTORS}`. `_impl_for`
(`graph.py:528`) reads it unchanged.

**Graph kind set** (`_CAPABILITY_KINDS`, `graph.py:368`):
`frozenset(d.kind for d in CAPABILITY_DESCRIPTORS)`.

**Readiness dispatch** (`_capability_node_readiness`, `graph.py:472`): the per-kind if-branches
become `descriptor_for(kind).readiness(name, _impl_for(kind, name))`. The `readiness` callable
reproduces today's exact behavior AND strings per kind:

- vm-platform: `lambda name, impl: _from_unsupported_reason(name, impl.unsupported_reason())`,
  preserving `f"platform '{name}' is unsupported here: {reason}"` (`graph.py:489`).
- secret-backend: `lambda name, impl: impl.not_ready()` (instance, no config arg, `graph.py:491`).
- harness-integration, git-credential-provider: `lambda name, impl: Readiness.ready()`.

This is the capability-NODE readiness (config-independent host support), distinct from the
config-dependent `Capability.not_ready(config)` a consuming resource (vm-site) uses. The callable
shape `(name, impl) -> Readiness` also accommodates wave 3's secret-source readiness choice (the
contract's last open question, which is wave 3's call to record, not this LLD's).

**Publisher** (`publisher_source`, plus the generic publisher): the four `publish_to` functions
(`capabilities/vm_platform/__init__.py:91`, `harness_integration/__init__.py:113`,
`git_credential/__init__.py:60`, `secrets/backends.py:200`), each an instance of the
skip-plugin-seated idiom, collapse into ONE generic publisher parameterized by the descriptor
(`kind`, `registry`, `entry_factory`, `publisher_source`). `bootstrap.py:100-103` iterates the
descriptors calling it; the other bootstrap publishers (builtin manifests, config, operator
manifests) are untouched. Two details preserved byte-for-byte: the built-in `Origin` source label is
the descriptor's `publisher_source` (including secret-backend's `"agentworks.secrets"`, not
`"...backends"`), and iteration is sorted by name (harmonizing to the harness/git convention; verify
no test pins vm-platform/secret-backend insertion order, adjust in the same commit if one does).

**Snapshot/restore** (`_capability_registries`, `plugins/registration.py:184`, consumed by
`seated_plugin` at `:170`): `tuple(d.registry() for d in CAPABILITY_DESCRIPTORS)`. Order is the
table order; `seated_plugin`'s clear-and-update loop is unchanged. No descriptor field: membership
is participation.

## 6. Manifest decode derivation, and the decode-fork boundary

Site 6 is the capability-config tagged-fold ENUMERATION, and only that. Two things are distinct:

- **(A) The tagged-fold dispatch** (`CAPABILITY_FIELDS`, `decode.py:80`, and the hardcoded
  `if doc.kind == "session-template": _normalize_session_harness_selector` branch, `decode.py:224`).
  This enumerates capability-hosting surfaces. It IS a switchboard site and derives here.
- **(B) The per-kind `_decode_*` spec decoders** (the phase-1 interim fork with the migrator oracle,
  `decode.py:243` onward). These decode each DECLARABLE kind's spec, are per-declarable-kind (not a
  capability-kind enumeration), and are replaced by kind-spec MODELS in step 2.5. Step 2.0 does NOT
  touch them. This is the coordinated boundary with 2.5: 2.0 absorbs (A); 2.5 owns (B) in full.

Derivation of (A): `CAPABILITY_FIELDS` today has exactly two entries (`vm-site`, `git-credential`),
NOT session-template, which is handled by the separate `_normalize_session_harness_selector` branch.
To reproduce that byte-for-byte, `CAPABILITY_FIELDS` derives ONLY from descriptors whose fold is the
sibling-pair accept-warn path, i.e.
`manifest_section is not None and manifest_section.legacy_string_shape == "accept-warn"`:

```python
CAPABILITY_FIELDS = {
    d.manifest_section.host_kind: (d.manifest_section.naming_field, d.manifest_section.config_field)
    for d in CAPABILITY_DESCRIPTORS
    if d.manifest_section is not None and d.manifest_section.legacy_string_shape == "accept-warn"
}
```

That filter is load-bearing: without it the harness-integration descriptor (whose `manifest_section`
is non-None but `legacy_string_shape == "reject"`, session-template having hardened in wave 1) would
add a third entry and route session-template through the accept-warn fold, a real behavior change.
secret-backend contributes nothing regardless (`manifest_section is None`; `backend_mappings` is
map-keyed). The decode DISPATCH replaces the hardcoded `if doc.kind == "session-template"` branch
with a lookup over all descriptors' `manifest_section.host_kind`, selecting the fold by
`legacy_string_shape` (accept-warn -> `_normalize_capability_field`, reject ->
`_normalize_session_harness_selector`), so both host kinds route to their current fold and messages
are unchanged.

The two fold FUNCTIONS and their exact messages stay unchanged at 2.0, because
`tests/manifests/test_capability_shape.py` pins them (e.g. `match="spec.{field} is a tagged table"`,
and the session-template selector's distinct "not a supported YAML field" rejection). Their behavior
difference (session-template REJECTS the legacy string shape, hardened in wave 1;
vm-site/git-credential ACCEPT-and-warn it until 2.4) is carried as
`manifest_section.legacy_string_shape`, which drives which fold the dispatch selects, so no message
changes and the gate stays green. UNIFYING the two folds into one message-templated function belongs
to 2.4 (where the accept-warn path is being removed and its messages reworked into the
hard-error-naming-the-rewrite anyway). Settled: 2.0 derives the enumeration and dispatch; 2.4
unifies the folds; 2.5 owns the `_decode_*` decoders. `KIND_SECTIONS` (`decode.py:49`) and
`KIND_REGISTRY` legitimately enumerate ALL resource kinds and are untouched.

## 7. Config schemas, keyed by consuming kind (slots RESCINDED)

> **Slots are rescinded** (operator ruling, 2026-08-05, roadmap note
> `roadmap-note-config-schemas.md`; `capability-descriptor-contract.md` on `main` is updated to
> match). This section previously specified a named-slot mechanism with a reserved `DEFAULT_SLOT`.
> It is replaced by what follows.

Each capability IMPLEMENTATION registers exactly one config model, which is what phase 2's original
plan specified before the seed notes introduced slots. There is no slot vocabulary, no reserved
default name, and nothing extra to spell for the common case. The premise, verified against HEAD
before adopting the change: no implementation needs more than one model today. Secret backends have
no backend-level config channel at all (`plugins/onepassword/backend.py:11`, citing ADR 0016), so
`mapping_model` is genuinely their single model; and harness-integration has exactly one hosting
surface today (session-template), because agent recipes carry no harness config. Slots had zero
exercised cases.

**Resolution is keyed by CONSUMING RESOURCE KIND, from day one.** The framework never asks an
implementation for "its schema"; it asks for "your schema for `<consuming kind>`". The capability
base supplies the default:

```python
class Capability:
    config_model: ClassVar[type[BaseModel]]          # the common case: one model

    @classmethod
    def config_model_for(cls, consuming_kind: str) -> type[BaseModel]:
        return cls.config_model                      # the argument is ignored by all-but-one kind
```

An author of a vm-platform writes `config_model = LimaConfig` and never sees the parameter, so the
keying costs the simple kinds nothing.

**The default REJECTS an unexpected consuming kind** (operator, 2026-08-05). It does not blindly
return `config_model`: it first checks `consuming_kind` against the kind's known consuming resource
kind (`manifest_section.host_kind` for the three hosted kinds, `secret` for map-keyed
secret-backend) and raises a framework error naming both kinds if they disagree. Without that check
the parameter is decorative for three of the four kinds, and a wrong-kind bug returns a
plausible-but-wrong model instead of failing, which is exactly the silent-wrong-answer class FR12's
strict direction exists to eliminate. With it, the keying is load-bearing from day one rather than
becoming real only when wave 4 arrives. Every framework consumer of a config model (validation at
the finalize fold, union assembly in 2.3, emission in 2.7, sample and describe rendering in 2.8)
passes the consuming kind, which it always has: it is walking a consuming resource of a known kind.

Why key it now rather than when wave 4 needs it: wave 4's harness-integration keys its schema by
hosting surface (vm-template including the admin attachment, agent-template, workspace-template,
session-template, per the roadmap's `scope-participation-contract.md`), and that becomes a local
override of `config_model_for` in the harness package rather than a signature change rippling
through four framework sites, at least one of which (the emission and rendering sources) the
onboarding child's guide surface will be consuming by then. Changing a signature another child SDD
depends on is the expensive version of this move; the cheap version is one ignored parameter today.
Support for a scope is carried by the integration's implementation, never by schema presence.

Union assembly is therefore per `(kind, consuming kind)` pair. At wave 2 every kind has exactly one
consuming kind, so this reduces to today's per-kind union; wave 4 adds pairs without reshaping the
mechanism.

At 2.0 there is nothing to build: no models exist. The descriptor carries no config field at all,
only a deferred-field comment recording the trigger (step 2.3, when the first model registers) and
this shape, in the same style as `consumer_gating` and `migration_participation`. The field cannot
be typed before step 2.1 defines the base model, which is the second reason not to carry an empty
one through 2.0.

**Not done, deliberately:** `manifest_section` stays singular. It drives exactly one thing today,
the accept-warn `CAPABILITY_FIELDS` decode derivation (section 6), and that legacy sibling-shape
fold is killed by 2.4 and 2.5 well before wave 4 introduces a second hosting surface. When emission
and rendering need the surface set, the honest source is the capability's declared models, not this
field.

## 8. Entry factory (unification question, settled)

Today's four Entry rows split: `VMPlatformEntry` and `SecretBackendEntry` carry `description`;
`HarnessIntegrationEntry` and `GitCredentialProviderEntry` do not (name/origin only). Settled: the
four dataclasses do NOT unify at 2.0. Each descriptor carries a small `entry_factory` closure that
builds its CURRENT Entry type, preserving today's rows byte-for-byte:

```python
# vm-platform:              lambda name, impl, origin: VMPlatformEntry(name, description=impl.description, origin=origin)
# harness / git-credential: lambda name, impl, origin: HarnessIntegrationEntry(name, origin=origin)   # impl.description ignored
# secret-backend:           lambda name, impl, origin: SecretBackendEntry(name, description=impl.description, origin=origin)
```

`build_row` calls it with the real origin; the generic publisher calls it with `origin=None` (the
publisher passes the real origin to `registry.add` separately, matching today). Unifying the rows
into one generic `CapabilityRow(name, description, origin)` WOULD change row content (harness/git
rows would gain a description they lack today), which breaks the always-green guarantee and the
`expects_description` parametrization at `tests/plugins/test_plugin_framework.py:254-257`. That is a
row-semantics change, a different artifact from the switchboard collapse; it belongs to the
row/model work (2.5 or later), not this mechanical step. The latent inconsistency (two carry
description, two do not) is recorded here as a follow-up candidate, deferred loudly rather than
silently absorbed.

## 9. Secret-backend interim exception and the `_VMPlatformKind` move

- **Interim exception**: `secret-backend`'s descriptor carries
  `registry_policy = CONSTRUCTED_SINGLETON`, documented in-record as the interim exception wave 3
  removes. It is the one kind whose registry stores a constructed instance
  (`secrets/backends.py:188`); the generic adapter's `registry_policy` branch confines the whole
  asymmetry (`prepare` constructs, `matches` compares `type(occ) is cls`, readiness asks the
  instance). Nothing else in the codebase special-cases secret-backend after adoption; wave 3 flips
  the field and deletes the branch's second arm.
- **`_VMPlatformKind` move**: `_VMPlatformKind` (`vms/kinds.py:140`) moves into the capability
  package (new `capabilities/vm_platform/kinds.py`, mirroring `harness_integration/kinds.py` and
  `git_credential/kinds.py`), so all four capability strategies live beside their capability code
  and the descriptor references them uniformly. Pure relocation: the
  `KIND_REGISTRY["vm-platform"] = ...` registration moves with it, `resources/kinds/__init__.py`
  imports the new location, and `vms/kinds.py` keeps
  `_VMTemplateKind`/`_AdminTemplateKind`/`_VMSiteKind`. Always-green; its own early commit (a
  symmetry precondition, section 10).

## 10. The always-green derivation sequence

Each step is one commit with the full gate green after it (`ruff`, `ruff format --check`, `mypy`
strict, `pytest -q`, `lint-files`). Ordered least-entangled first; adapters and decode (the
meatiest) last:

1. **Move `_VMPlatformKind`** into `capabilities/vm_platform/kinds.py` (section 9). Pure relocation.
2. **Introduce the table.** `CapabilityKindDescriptor`, `RegistryPolicy`, `HostSurface`, and
   `CAPABILITY_DESCRIPTORS` (four records contributed per-package, lazily accessed), populated
   entirely from existing wiring. ADDITIVE: no site derives yet. A table-level self-test proves each
   record's fields match the live wiring (registry object identity,
   `kind_strategy is KIND_REGISTRY[kind]`, source labels, host pairs) and that the built-in impls
   pass the section-4 conformance checks.
3. **Wire registration-time conformance** into `register_plugin`'s pass 1 (`_validate_descriptor`,
   `registration.py:60`, which today checks none of section 4). Behavior-ADDITIVE: every shipped
   built-in and the one shipped plugin (onepassword) conform, so the gate stays green; the negative
   conformance tests (section 12) land with this commit. Distinct from the derivations, so it is its
   own step rather than folded into the table introduction.
4. **Snapshot/restore tuple** (`_capability_registries`) derives from the table.
5. **Bootstrap publication**: collapse the four `publish_to` (entry points `secrets/__init__.py:37`
   fronting `backends.py:200`, plus the three capability-package `publish_to`s) into the generic
   publisher; `bootstrap.py` iterates the table. NOT purely mechanical: this harmonizes
   publish-iteration to sorted-by-name (section 5); first verify no test pins vm-platform or
   secret-backend insertion order, and adjust in this same commit if one does.
6. **Registry loaders** (`_CAPABILITY_REGISTRY_LOADERS`) derive from the `registry` field.
7. **Graph kind set + readiness dispatch** (`_CAPABILITY_KINDS`, `_capability_node_readiness`)
   derive.
8. **Adapter table**: replace the four adapters with `_DescriptorAdapter`; `CAPABILITY_ADAPTERS`
   derives.
9. **Manifest decode** (site 6, part A): `CAPABILITY_FIELDS` (accept-warn-filtered, section 6) and
   the session-template dispatch derive; fold functions/messages unchanged.
10. **Flip the guard test** and reconcile the sibling drift guards (section 12).

The migrator's kind-participation flags stay hand-maintained throughout (section 11); no commit.

## 11. The migrator stays hand-maintained

`agw resource migrate`'s kind-participation flags are NOT derived. The migrator is a deliberately
independent frozen oracle (phase 1 relocated the TOML loaders into it precisely so its verification
is independent of live wiring); deriving from the live descriptor would defeat that independence.
This is the deferred `migration_participation` field's whole reason for existing: created only if
wave 2 later rules that migrate both survives and should derive (2.4's open decision), against the
counterargument that it stays independent. Step 2.0 creates no such field and touches no migrator
code.

## 12. Test plan

**Guard-test flip.** `test_capability_adapters_keys_match_the_capability_category_kinds`
(`tests/plugins/test_plugin_framework.py:279`) flips from "detect an omitted site" to "assert every
site derives from the descriptor." It keeps the original assertion (descriptor kinds equal
`KIND_REGISTRY`'s `category == "capability"` kinds) and adds, over `CAPABILITY_DESCRIPTORS`:

- `set(CAPABILITY_ADAPTERS) == {d.kind for d in descriptors}` and each adapter's registry IS the
  descriptor's registry.
- `set(_CAPABILITY_KINDS) == {d.kind for d in descriptors}`.
- `set(_CAPABILITY_REGISTRY_LOADERS) == {d.kind for d in descriptors}` and each loader IS
  `d.registry`.
- the snapshot tuple's registries ARE the descriptors' registries, in table order.
- `CAPABILITY_FIELDS` equals the accept-warn-filtered comprehension of section 6 (the same filter,
  so the guard asserts exactly two entries, `vm-site` and `git-credential`, not three).
- `d.kind_strategy is KIND_REGISTRY[d.kind]` for every descriptor (no drift).
- non-vacuity: the table has exactly the four known kinds (so a scan that silently sees nothing
  fails loudly).

Because each site is now BUILT from the table, these assertions are near-tautological by
construction, which is the point: they regression-lock the derivation and fail the moment someone
reintroduces an independent enumeration at a site. The bootstrap publisher, having no static
structure to compare, is pinned indirectly: publishing into an empty registry and checking the row
set equals the descriptor kinds' built-ins.

**Conformance tests.** A test that each shipped built-in impl passes the section-4 checks, and
negative tests that a non-conforming fixture impl (wrong base, missing metadata, abstract, missing
required op) is rejected at `register_plugin` with a `PluginError` naming the plugin, before any
registry mutation (atomicity preserved).

**Sibling drift-guard reconciliation.** `tests/agents/test_recipe_gate_drift.py` and
`tests/sessions/test_harness_integration_gate_drift.py` are CONSUMER-GATING guards (the deferred
`consumer_gating` territory; wave 2 changes no gating behavior). Their BEHAVIOR is untouched. Both
docstrings cross-reference "the `CAPABILITY_ADAPTERS.keys()` adapter-drift test" as the pattern they
mirror (`test_recipe_gate_drift.py:13`, `test_harness_integration_gate_drift.py:11`); those
references are reconciled to name the flipped guard and its "derives from the descriptor" framing.
That is the whole reconciliation: docstring pointers, no assertion changes.

## 13. Contradictions and residual decisions for the lead

- **"Only capability-kind enumeration" vs the four `KIND_REGISTRY` writes. DECIDED (lead,
  2026-08-05): leave them co-located, guard-pinned.** Section 2 keeps the four capability
  `KIND_REGISTRY[...] = ...` lines beside their kinds (like every declarable kind) and pins them
  equal via the guard. Rationale: routing them through a central loop
  (`for d in CAPABILITY_DESCRIPTORS: KIND_REGISTRY[d.kind] = d.kind_strategy`) would reintroduce the
  exact import-order coupling the lazy-table discipline (section 2) exists to avoid, and would make
  capability-kind registration asymmetric with declarable-kind registration. The descriptor
  references the same strategy object (`kind_strategy is KIND_REGISTRY[kind]`), so there is one
  strategy per kind, referenced not duplicated; the "only enumeration" the contract targets is the
  SWITCHBOARD (the seven derived sites), and the guard makes divergence structurally impossible,
  which is the actual goal.
- **Heterogeneous `implementation_contract`** (ABC for three, `@runtime_checkable` Protocol for
  secret-backend) is a code fact, not a contradiction; the conformance check (section 4) handles
  both by design (`isabstract` for the ABCs, structural attribute presence for the Protocol).
- **Latent Entry inconsistency** (two rows carry `description`, two do not, section 8): recorded as
  a deferred follow-up, not fixed here, because fixing it changes row content and breaks
  always-green.
- **Graph read-side access modes are an explicitly held door (roadmap guide-surface note,
  2026-08-05).** The roadmap's guide surface renders against the resource graph in a gated read-only
  access mode whose rule ("gated modes expose only already-materialized data") leans on
  post-finalize immutability staying a registry/fold property (FR21 door c). Nothing here narrows
  that: the descriptor table is a static core-owned enumeration, `registry` is a lazy callable on
  the descriptor (not a power wired onto graph nodes), and readiness dispatch is a fold-time
  concern. Future descriptor edits must not attach lazily-computing powers to graph nodes. If
  per-kind guide topic content ever wants a typed registration point, a descriptor field beside the
  deferred `config_schema` is the natural home; noted only, decided no earlier than wave 4.
- No hard contradiction between the contract and HEAD was found. The kind names in the contract's
  illustrative record (`vm-platform`, `harness-integration`) match HEAD; the host/capability naming
  split (vm-site hosts vm-platform, git-credential hosts git-credential-provider, session-template
  hosts harness-integration) is consistent across `decode.py`, the kind strategies, and the
  adapters.
