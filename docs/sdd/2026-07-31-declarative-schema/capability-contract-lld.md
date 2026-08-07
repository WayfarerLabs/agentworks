# LLD: Step 2.3, the Capability Contract Flip

Date: 2026-08-06

Status: IMPLEMENTED (2026-08-06). Deviations found by building it are recorded inline against the
section they revise, and gathered in section 14.

Originally: DRAFT, awaiting review. Companion to [frd.md](frd.md) (FR5, FR8, FR12, FR15, FR18,
FR21), [hla.md](hla.md) (Components 0, 2, and 3), [plan.md](plan.md) (step 2.3). Builds on
[schema-foundation-lld.md](schema-foundation-lld.md) (the model vocabulary, the walkers, and the
error bridge, all implemented) and [descriptor-adoption-lld.md](descriptor-adoption-lld.md) section
7 (the config contract, settled per facet by the operator ruling of 2026-08-06). Authority for the
facet vocabulary: `../2026-08-04-next-steps/scope-participation-contract.md` and
`capability-descriptor-contract.md`. Contradictions found against HEAD or the upstream artifacts are
in section 14, not designed around silently.

Step 2.3 is the contract flip: capabilities stop being INVOKED to validate their own config and
start DECLARING it. After this step no capability code runs during validation or reference
extraction on any surface, `Capability.validate` / `Capability.dependencies` /
`SecretBackend.validate_mapping` are gone, and every capability's operations read typed fields off a
validated model instead of `config.get(...)`.

## 1. What this step delivers, and what it does not

Delivered:

- The registration surface: `config_model` on every capability implementation, uniform across all
  four kinds (section 2).
- The `config_for()` hook every core read of a capability's config goes through, which is the seam
  wave 4's per-facet offering arrives on. The facet vocabulary and the `facet` parameter shipped
  here originally and were removed on 2026-08-07 as mechanism with no consumer; the per-facet
  CONTRACT stands (section 3).
- The descriptor's `config_schema` field (the kind's model contract) and registration-time
  conformance check five (section 4).
- Union assembly per `kind`, cached on the arms the union would be built from (section 5).
- The interim tagged-table synthesis that bridges phase-1 decode to the tagged union (section 6).
- The core-owned validation and extraction entry points, and the retirement of every hand-rolled
  validator and `dependencies` classmethod (section 7).
- The model inventory: thirteen capability config models, three of them root models (section 9).
- Construction binding the validated model instance, and the per-capability typed-ops migration
  (section 10).
- Two bridge extensions the first authored model validators require (section 8).

NOT delivered by 2.3, deliberately, each with its reason:

- **Effective-config validation at finalize, per-key merge provenance, and the FR17 traversal
  split.** These are the plan's step-2.3 boxes for the inheritance surface, and they are a distinct
  body of work: moving session-template validation from resolve time to finalize needs the merge to
  run over registry rows, a provenance channel through the bridge, and a relationship-typed edge
  that every runtime-need traversal then filters on. Section 12 settles the design so it is not
  re-litigated, and states plainly what is left to build. What 2.3 DOES do there is repoint
  `sessions/templates.py::_validate_merged` at the core entry point so no capability code is invoked
  from it either; its TIMING is unchanged. Flagged for the lead in section 14 as the one scope call
  in this LLD. **Delivered by step 2.3b (2026-08-06); section 12 now records what was built and the
  one place the design did not survive contact.**
- **The old-sibling-shape hard error.** Step 2.4. Decode still normalizes both shapes; 2.3 consumes
  whatever decode produces.
- **Kind spec models and the decode swap.** Step 2.5. The interim synthesis of section 6 exists
  exactly because decode still hands us a naming field plus a sibling blob, and it is deleted there.
- **The operator upgrade note** for the deliberate breaks (section 9.6). The plan puts the note in
  2.9; 2.3 carries the breaking-change marker on its commit, and section 9.6 is the material the
  note is written from.
- **The FR15 defaulting sweep.** Step 2.6. One exception, argued in section 10.3: three proxmox
  consumer-side defaults move onto the model here, because the ops migration rewrites those exact
  lines and leaving a fallback behind would mean touching them twice.

## 2. The registration surface

### 2.1 One attribute, one spelling, all four kinds

```python
class Capability(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    owner_kind: ClassVar[str]
    contract_version: ClassVar[int]

    config_model: ClassVar[type[BaseModel]]
    """The config this capability offers. Declared, never defaulted."""
```

`SecretBackend` (a Protocol, `secrets/backends.py:58`) declares the same member, for the same reason
its `contract_version` is spelled on every backend: Protocol bodies are not inherited by structural
implementers.

**Settled: the attribute is `config_model` everywhere, including secret backends.** The HLA says
backends "declare `mapping_model` the same way" (`hla.md:190`) and the plan says the backend
`mapping_model` "registers as that kind's config model" (`plan.md:350`). Both were written before
step 2.0 collapsed the switchboard onto the descriptor. Two names would put a per-kind branch back
into the two places 2.0 just made kind-agnostic (the conformance pass and the model lookup), which
is the exact shape of duplication the descriptor exists to end, and the `mapping_` prefix's own
justification (`validate_mapping`'s sibling) is being deleted in this step. The kind's model
CONTRACT still differs (section 4), which is where the real asymmetry belongs. Recorded as a
deviation from the HLA's wording in section 14.

**Settled: `config_model` is REQUIRED, not defaulted.** Defaulting it to an empty model on the base
would make "I accept no config" the thing an author gets by FORGETTING, which is how the retired
base `validate` behaved and is precisely what makes an unmigrated plugin look migrated. The same
reasoning already governs `contract_version` (`capabilities/base.py:290-301`); this follows it
rather than inventing a second rule.

**Settled: there is no SHARED empty model, because at wave 2 it would have no user.** The plan asks
for one (`plan.md:339`), and the shape it describes is right, but it does not survive contact with
the tag: a kind whose config is dispatched by a discriminated union needs every model to carry the
`name` field, so a shared model with no fields cannot be an arm of it. The one shipped capability
that accepts no config (wsl2) therefore declares a two-line model carrying only its tag, and every
other shipped capability has real fields. A shared empty model becomes real the moment a map-keyed
or untagged surface wants one, and inventing it now, with nothing to use it, is speculative
generality. Recorded in section 14.

### 2.2 Models live beside their implementation

`LimaConfig` is in `lima.py`, `ProxmoxConfig` in `plugins/proxmox/platform.py`, and so on, exactly
where `name` and `description` already live. This is not a filing preference:
`use_attribute_docstrings` (schema-foundation LLD 2.3) makes the field's operator-facing description
part of the class body, so the model IS the documentation, and separating it from the code it
documents is how the drift FR13 targets gets reintroduced. Where a platform module is already large,
the model and its nested models go in a sibling `config.py` under the same package rather than a
shared models module.

## 3. The facet contract, and why no facet code ships

### 3.1 The contract

Config is offered per FACET. A **facet** is the level a capability is DRIVEN at, pairing one level's
API methods with that level's config: `vm`, `user`, `workspace`, `session`. Four levels, fixed and
core-owned, not extensible; a change here is an ordinary contract change, as the scope-participation
contract already says of scopes.

A capability offers a fixed set of facet configs the same way it offers a fixed set of API methods,
and CONSUMERS choose which facet they drive, so a producer never has to know who is asking.

**Facets are not scopes, and core owns the mapping.** The roadmap's scopes are `vm`, `admin`,
`agent`, `workspace`, `session`. Admin and agent both resolve to `USER`; session start and resume
share `SESSION`. That mapping lives in the CORE code that drives each level, so a vm-template's
admin attachment and an agent template get the same answer by construction. A capability only ever
sees a facet, and nothing in `capabilities/` may spell a scope.

**Config presence is NOT the support claim.** Asking a capability for its config at a facet asks
what SHAPE the config has there, never whether the capability implements that level. A capability
may support a facet and offer an empty config there, and a capability that offers a config at a
facet has claimed nothing about implementing it. Support is carried by the implementation, per the
scope-participation contract. Wiring the two together would reinvent the rescinded slot mechanism
under a new name, so `config_for` is documented in exactly these terms and no core code may read it
as a support signal.

### 3.2 The hook that ships

```python
class Capability(ABC):
    @classmethod
    def config_for(cls) -> type[BaseModel]:
        """The config model this capability offers."""
        return cls.config_model
```

`config_model = LimaConfig` and nothing else, for all thirteen shipped capabilities. What the hook
buys is that every core read of a capability's config goes THROUGH it rather than off `config_model`
directly (`capabilities/config.py:offered_model`), so the first capability that answers with
something other than its declaration is an ordinary registration rather than a framework change.
That indirection is the whole of the seam wave 4 needs, and it is pinned by a test over a fixture
capability that overrides the hook.

**The association is DECLARED DATA, readable before any method runs.** `config_model` is a ClassVar
and `config_for` is a classmethod over class-level data; core reads both at finalize, with no
instance and no operation invoked. That is the ruling's "readable at finalize" requirement, and it
constrains wave 4: a per-facet capability must DECLARE its offered models as data, not compute them,
because registration conformance reads `config_model` directly and may not invoke implementation
code (`capabilities/conformance.py:_config_model_error`).

### 3.3 No facet vocabulary and no facet parameter ship

This step originally shipped a `capabilities/facets.py` (a `Facet` enum and a `facet_config`
resolver that hard-errored on an unoffered facet) plus a `facet` parameter on nine signatures. Both
were removed on 2026-08-07 under the roadmap lead's **no mechanism without a consumer** direction:
no production code named a facet, nothing passed the parameter, and a vocabulary with no consumer is
a signature every reader has to decode and no reader can use.

**This is a removal of unused mechanism, not a reversal of the contract.** Section 3.1 stands as
settled. Wave 4 reintroduces the parameter ADDITIVELY, in the same change that brings the
harness-integration kind whose methods run at several levels and the consumers that would pass it,
so the declaration and the call site's facet still arrive together. That pairing is what the
vocabulary existed to enable, and it is exactly what the vocabulary could not deliver on its own.

The alternative considered and rejected at the time was naming a facet per hosting surface now
(vm-site as `VM`, session-template as `SESSION`, git-credential as `USER`, secret-backend as none),
which would have made wave 4's session-template call site free. It would also have had wave 2 ASSERT
a scope-to-facet mapping that the ruling gives to core-at-wave-4, on evidence that does not exist
yet: a git credential is plausibly user-level and a per-secret backend mapping is plausibly no level
at all, and neither guess has a consumer that would catch it being wrong. That reasoning is why the
parameter was defaulted rather than threaded; the same reasoning carried one step further is why it
is now absent: a default no call site passes is the same guess with a signature attached.

## 4. The descriptor's `config_schema`, and conformance check five

The deferred field (`capabilities/descriptor.py:184-204`) is created here, holding the kind's model
CONTRACT: what any model an implementation offers must be.

```python
@dataclass(frozen=True)
class ConfigContract:
    """What a model offered for this kind must be."""

    base: type[BaseModel]
    """``AgwModel`` where the config is mapping-shaped; ``AgwRootModel``
    where it is not (secret-backend, section 9.4)."""

    discriminator: str | None
    """The tag field carrying the capability's own name, for a kind whose
    config is dispatched by a DISCRIMINATED UNION (``"name"``, FR8). ``None``
    for a kind dispatched by a MAP KEY (secret-backend's
    ``backend_mappings``), whose models carry no tag because the key
    already is one."""
```

Per kind:

| kind                      | `base`         | `discriminator` |
| ------------------------- | -------------- | --------------- |
| `vm-platform`             | `AgwModel`     | `"name"`        |
| `harness-integration`     | `AgwModel`     | `"name"`        |
| `git-credential-provider` | `AgwModel`     | `"name"`        |
| `secret-backend`          | `AgwRootModel` | `None`          |

**Conformance check five** (`capabilities/conformance.py`, the sixth reason slot; the numbering
follows the descriptor LLD's section 4 list) runs at registration, before any registry mutation:

1. `config_model` is present and is a `type` deriving from `contract.base`.
2. When `contract.discriminator` is set, the model declares that field as a `Literal` whose values
   include the implementation's own `name`. A mismatch would make the arm unaddressable from a
   manifest while everything else looked fine, which is exactly the class of silent failure
   registration conformance exists for.
3. The model is COMPLETE: `model_fields` resolves. The schema-foundation LLD (section 4.2) records
   this as a requirement ON 2.3 rather than a hope, because an unresolvable forward reference in a
   registered model degrades a graph edge at runtime instead of failing where the author can see it.

Check five reads `config_model` directly rather than calling `config_for`, and that is deliberate:
conformance must not invoke plugin code. A per-facet capability (wave 4) therefore has to conform by
declaring its offered models as DATA the check can read rather than as a computation only the
capability can run, which is a constraint wave 4 inherits from this choice.

**Not doing this, and why.** No check that every field carries a description. The schema-foundation
LLD left it open (its section 11) and it stays open: it is a real quality gate but it belongs with
the renderer (2.8) that would consume the description, where a failure is legible ("this field would
render blank") rather than abstract. Recorded, not decided.

## 5. Union assembly

### 5.1 Shape and placement

For each kind whose `ConfigContract.discriminator` is set, the framework assembles one discriminated
union over the registered implementations' offered models:

```python
# capabilities/config.py
def capability_config_union(kind: str) -> type[BaseModel]:
    """The tagged union over every registered ``kind`` implementation's
    config, cached."""
```

Arms are `impl.config_for()` for every name in the kind's live registry, in registry order; the
discriminator is `contract.discriminator`.

> **Correction (implementation): the union is a generated ROOT MODEL, not the bare `TypeAdapter`
> this section first specified.** The bridge frames against a MODEL, and that is what buys the
> operator-facing path: as a root model, a failure's leading tag segment (`('lima', 'vm_host')`) is
> recognized as the tag it is and dropped, so an operator reads `vm-site/lab.vm_host` rather than a
> path with our dispatch mechanism in it. Verified by execution against pydantic 2.13.4 before
> adopting it; the same run confirmed the arm's own field list survives into an unknown-key message
> and that a bad tag renders `unknown name 'nope'; registered: ...`. Generating a model rather than
> authoring one is legal precisely because it declares no fields of its own (the step 2.1 rule is
> about attribute docstrings, which only authored fields have): every field, and every field
> description, comes from the authored arms.

Assembly happens at the existing post-registration boundary in the sense that matters: it is
performed lazily on first use and every use is downstream of `build_registry` (`bootstrap.py:52`),
which seats plugins before it publishes and finalizes. Nothing is built at import.

### 5.2 The cache key is the union's own arms

```python
_UNION_CACHE: dict[tuple[str, frozenset[tuple[str, type[BaseModel]]]], type[BaseModel]] = {}
```

**Settled: cache on what the union would be BUILT from rather than on `kind` alone with an
invalidation protocol.** The alternative needs every mutator of a capability registry to remember to
invalidate: plugin seating, `seated_plugin`'s snapshot/restore (`plugins/registration.py:170`), and
every test that installs a fixture capability. A forgotten invalidation is a stale union, which
presents as a capability that validates against another capability's schema, which is a
silent-wrong-answer bug rather than a crash. Keying on the arms makes staleness structurally
impossible: the key and the arms come from one read, so the key cannot describe a union different
from the one it would assemble. The cost is resolving the models per lookup (O(registered
capabilities), a dozen attribute reads), which is what building the union already pays per arm on a
miss, against rebuilding a pydantic union, which is the expensive part. The HLA's "cached on the
kind's registry entry" is satisfied in substance; the mechanism is this LLD's call, and the reason
to differ is stated rather than assumed.

> **Correction (review, 2026-08-06): the key was first the registry MAPPING, and that left one
> silent path open.** It covered every case enumerated above, and missed a seated class whose
> `config_model` changed: same name, same class object, so the entry survived and the capability
> went on being validated against the model it no longer offered. Reproduced before fixing.
> Unreachable in production, where `config_model` is a ClassVar set at class definition, and closed
> anyway, because the entire argument for keying over invalidating is that invalidation fails
> silently; a residual silent path undercuts that rather than sitting beside it.

The cache never evicts. Its size is bounded by the distinct arm sets a process ever sees, which is
one per kind plus one per test that seats a fixture capability: a deliberate choice, recorded so it
is not mistaken for an oversight.

### 5.3 What the union is for

Two consumers, and no third:

- **Validation of a capability-embedded blob** (section 7.2), which is where the "unknown `name`"
  message comes from.
- **Schema emission** (2.7), which expresses the union as `oneOf` plus a discriminator.

Reference extraction does NOT go through the union: it walks the impl's own model against the raw
blob (section 7.3). Constructing a capability does not either: the class is in hand, so the arm is
already selected (section 10.1).

Every capability offers exactly one config, so the key is `(kind, arms)` and this is a per-kind
union. Wave 4's per-facet offering widens the key rather than replacing the machinery: the arms half
already makes staleness impossible, and a facet is one more thing the arms are read at.

## 6. The interim tagged-table synthesis

FR8's manifest shape is one tagged table (`spec.platform: {name: lima, vm_host: ...}`). Decode on
`main` still produces the naming field and the config blob as two fields on the decl
(`VMSiteDecl.platform` / `.platform_config`, `vms/sites.py:58-59`, and the same shape on
`GitCredentialConfig` and `SessionTemplate`), because 2.5 owns the decode swap. So the core
synthesizes the tagged table at the validation boundary:

```python
def tagged_config(name: str, blob: Mapping[str, object], *, discriminator: str) -> dict[str, object]:
    """The tagged table ``blob`` will BE once 2.5's decode produces it
    directly. Interim: deleted when the last decoder becomes a kind spec
    model."""
```

**Settled: a `name` key already present in the blob is a hard error, not an override in either
direction.** Both silent resolutions are wrong, and one of them is dangerous:

- Tag-wins (`{**blob, discriminator: name}`) silently DISCARDS a key the operator wrote. Today that
  key is a loud unknown-field error on every shipped capability.
- Blob-wins (`{discriminator: name, **blob}`) lets `platform_config.name` select a DIFFERENT union
  arm than the `platform` field names, so a site declared `platform: lima` would validate, and
  construct, against another platform's schema. That is a silent wrong answer produced by a
  compatibility shim, which is the worst thing this step could ship.

So the synthesis raises a `ConfigError` naming the collision and the fix. The error is interim by
construction: under 2.5's tagged decode the operator writes ONE table in which `name` is the
selector, and the collision cannot be expressed.

The function has exactly one deletion trigger, stated in its docstring: when 2.5's decode hands the
tagged table straight through, the call sites pass the table and this function goes.

## 7. Core-driven validation and extraction

### 7.1 The entry points

One new core module, `capabilities/config.py`, holding everything a consuming resource needs and
nothing a capability does:

```python
def capability_config_model(kind: str, name: str) -> type[BaseModel] | None:
    """The registered implementation's offered config model, or ``None``
    when no implementation of that name is seated on this host."""


def validate_capability_config(
    *,
    kind: str,
    name: str,
    blob: Mapping[str, object],
    owner: RefOwner,
    location: SourceLocation | None = None,
) -> BaseModel | None:
    """Validate ``blob`` as ``kind``/``name``'s config and return the
    validated instance. Raises the bridge's framed ``ConfigError``.
    ``None`` when no such implementation is seated (the dangling
    capability edge is what reports that; R9.2)."""


def capability_config_references(
    *,
    kind: str,
    name: str,
    blob: Mapping[str, object],
    owner: RefOwner,
) -> tuple[ConfigReference, ...]:
    """The references ``blob`` implies. Total, never raising, for any
    inputs; ``()`` when no such implementation is seated."""
```

`capabilities/config.py` may import `resources/schema/` and `capabilities/descriptor.py`; it is
imported by the four consuming-resource modules, by the migrator, and by `capabilities/base.py`.
That direction is the same one the descriptor table already established, and it holds because
`resources/schema/` imports nothing of ours but `resources/reference.py`.

### 7.2 Validation

`validate_capability_config` resolves the descriptor, looks up the seated implementation, and:

- for a tagged kind, validates `tagged_config(name, blob)` through `capability_config_union(kind)`,
  returning the selected arm instance;
- for a map-keyed kind, validates `blob` through `impl.config_for()`.

Both pass `validation_context(owner)` so owner-templated `SecretRef` defaults resolve (FR18), and
both translate a `pydantic.ValidationError` through `config_error_from(...)` with the location the
caller already has.

**The framing fork the schema-foundation LLD asked for (its section 7.4), settled.** The bridge owns
its own file:line framing, so a bridge-produced error must not also pass through the finalize pass's
origin-suffix wrapper (`resources/registry.py:517-522`) or it is framed twice. The wrapper still has
to exist while unmigrated hand-rolled validators raise unframed `ConfigError`s (every kind decoder,
until 2.5). Mechanism:

```python
# resources/schema/errors.py
class FramedConfigError(ConfigError):
    """A ConfigError that already carries its own location framing.
    Callers must not re-frame it; ``config_error_from`` produces only
    this type."""
```

and the wrapper re-raises it untouched. Marking the error rather than the call site is what makes
the rule hold through the four consuming resources plus construction plus the migrator, without each
of them having to know about a wrapper three layers away. The `except ConfigError` branch, its
`format_origin_location` call, and this subclass all die together in 2.5 when the last hand-rolled
validator does; that is stated in both docstrings, and it is a bounded fork with a named trigger
rather than a second permanent framing.

### 7.3 Extraction

`capability_config_references` is `extract_references(model, blob, owner)` and nothing else. The
blob is the RAW declared mapping, never the tagged synthesis: the tag is a kind-owned selector, the
model's `name` field carries no marker, and passing the raw blob keeps extraction a pure function of
`(model, blob, owner)` (FR21 door (a), schema-foundation LLD 4.4).

Totality is inherited, not re-promised: the walker never raises, and the only code between it and
the caller is a registry lookup that returns `None` on a miss.

### 7.4 Who calls what

| call site                                    | before                                    | after                                                                |
| -------------------------------------------- | ----------------------------------------- | -------------------------------------------------------------------- |
| `vms/sites.py:94`                            | `capability.dependencies(...)`            | `capability_config_references(kind="vm-platform", ...)`              |
| `vms/sites.py:152`                           | `capability.validate(...)`                | `validate_capability_config(kind="vm-platform", ...)`                |
| `git_credentials/credential.py:104` / `:149` | provider `dependencies` / `validate`      | the same two, `kind="git-credential-provider"`                       |
| `sessions/template.py:109` / `:129`          | integration `dependencies` / `validate`   | the same two, `kind="harness-integration"`                           |
| `secrets/base.py:105`                        | `backend.would_attempt(...)`              | unchanged (section 7.5)                                              |
| `secrets/base.py:142`                        | `backend.validate_mapping(...)`           | `validate_capability_config(kind="secret-backend", ...)`             |
| `sessions/templates.py:183`                  | integration `validate` on the merged blob | `validate_capability_config(...)`, same timing (section 12)          |
| `migrate/planning.py:514` / `:554` / `:602`  | capability `validate` pre-write           | `validate_capability_config(...)` with a labeled owner (section 7.6) |
| `capabilities/base.py:325` / `:327`          | `validate` + `dependencies` at construct  | the same two, through the model (section 10.1)                       |

The gating each consuming resource performs is UNCHANGED. In particular `SecretDecl.validate` keeps
its enabled-backend filter (R9.9: a mapping addressed to a present-but-disabled backend stays inert)
and its `False` opt-out skip; only the line that reaches into the backend changes. The finalize pass
kept its READY-and-ENABLED scope (R3/R9.4) untouched at this step, which changed WHO validates and
never WHEN. That scope was removed outright later in the effort (2026-08-07, review finding 16):
validation is now unconditional, because readiness is computed from config the validate pass has not
yet checked, so gating the pure check on the environmental one let a typo suppress its own error.

The enabled-backend filter described above went the same way, later the same day, under the same
reasoning applied one level down: an operator must not be able to accumulate invalid configuration
that blows up the moment they enable the underlying resource. `SecretDecl.validate_config` now
validates EVERY declared mapping, disabled backend or not, and the enabled-backend set it needed is
gone from the whole `validate_config` signature (`declared_resource.py` and the three other
overrides included), so no implementation can reach for an environmental verdict. Two properties
that the filter conflated are now separate and only one of them tracks enablement: a mapping to a
disabled backend is still INERT for resolution, dropped from the chain by `active_backends`, so it
is never selected or resolved through. The `False` opt-out skip stays, and is NOT the same defect:
it is loop-owned vocabulary that names no model to check against and reads identically on every
host, so skipping it is a fact about the document. A mapping to an ABSENT backend also stays as it
was, validating vacuously because no seated implementation means no model exists to judge it, and
reported once by the secret's dangling `secret-backend` edge as a hard finalize miss (R9.11).

### 7.5 What is deleted, and what survives

Deleted outright:

- `Capability.validate` and `Capability.dependencies` (`capabilities/base.py:335`, `:355`), and all
  eleven overrides.
- `SecretBackend.validate_mapping` (`secrets/backends.py:131`) and its three implementations.
- `SecretBackend.dependencies` (`secrets/backends.py:157`) and its three implementations. It has
  **zero production callers**: `SecretDecl.dependencies` builds its `secret -> secret-backend` edges
  from `would_attempt` plus the explicit mapping keys and never consults it. It is dead Protocol
  surface, so it is deleted rather than ported, and `SecretDecl.dependencies` gains the core
  extraction call in its place (section 9.4) so the kind is not the one exception to core-driven
  extraction.
- `capabilities/git_credential/base.py`'s `credential_name_from_owner`, `default_token_secret`,
  `token_dependency`, and `validate_token_field` (`:52`, `:59`, `:64`, `:89`). All four exist to
  derive and check one templated secret name from a display string; the `SecretRef` marker with
  `default_template="git-token-{owner_name}"` is that whole derivation, and `RefOwner` carries the
  name so nothing re-splits a string we joined ourselves (FR18).
- Every per-capability parser that exists only to validate: `_validated_scope` (`github.py:42`),
  `_parse_service_principal` and `_parse_size_catalog` (`azure/platform.py:249`, `:319`),
  `_parse_credentials` and `_parse_instance_catalog` (`aws/platform.py:106`, `:180`),
  `_validate_op_uri` / `_ref_from_table`'s validation half (`onepassword/backend.py`), plus the
  `_REQUIRED_KEYS` / `_OPTIONAL_KEYS` / `_*_FIELDS` constant pairs that fed them.

Survives, and must not be confused with the above:

- **`Capability.not_ready(config)`** (`capabilities/base.py:380`) keeps taking a RAW mapping. This
  is load-bearing: the readiness fold (finalize pass 4) runs BEFORE the validate pass (pass 7) and
  is total over unvalidated config by contract. Typing its parameter would make the fold depend on
  validity, which is the B1 loop the readiness refactor exists to avoid, and would turn a malformed
  blob into a permanent readiness reason. `LimaPlatform.not_ready` therefore keeps
  `config.get("vm_host")`, and its docstring says why in those terms.
- **`SecretBackend.would_attempt` / `describe_lookup` / `batch_get` / `not_ready`**: domain
  operations over the raw mapping, invoked at resolve time, not validation. Unchanged.
- **`HarnessIntegration.merge_config`**: capability-owned MERGE semantics (shell unions
  `required_commands`), not validation. It runs on raw declared blobs before validation by design
  (`shell.py:113-124` says so). Unchanged here; section 12 records where it belongs long term.

### 7.6 Owner framing, and the migrator's TOML vocabulary

`RefOwner(kind, name)` renders `kind/name`, which is the address every consuming resource already
frames with (the punctuation shifts, since the bridge adopts the FRD's uniform colon form:
`[azure].region is required` becomes `[azure].region: is required`). The migrator is the one caller
that frames differently and deliberately: it reports in the operator's TOML vocabulary
(`[azure].region is required ...`, `migrate/planning.py:514`) because the operator is looking at a
TOML file that has not been rewritten yet.

**Settled: `RefOwner` gains an optional `label`,** with `display` returning
`label or f"{kind}/{name}"`. The alternative (make the migrator adopt `vm-site/<name>` framing)
changes an operator-facing message for no gain, in the one command whose entire job is to speak
about the old file. One optional field on a record that already exists is cheaper than either a
second owner type or a regressed message. Note this widens a type the schema foundation owns; it is
additive and the default reproduces today's behavior exactly.

## 8. Two bridge extensions

Step 2.3 authors the first model VALIDATORS in the codebase (github's mutual exclusion,
onepassword's shape guard, prompt's refusal), and the first UNTAGGED union (onepassword's mapping).
The bridge as shipped handles neither well. Both fixes are general and belong in the bridge, not in
the models.

- **`value_error` normalization.** Pydantic renders a validator's `ValueError` as
  `"Value error, <message>"` and puts the exception in `ctx["error"]`. Verified against 2.13.4. Left
  alone, every authored cross-field rule reads
  `git-credential/prod: Value error, repos and owner are mutually exclusive ...`. The bridge adds
  one table entry rendering `str(ctx["error"])`, falling through to pydantic's text when the context
  is not there. This is not inventing phrasing (the rule the module forbids): the message is the
  author's own, and the prefix is pydantic's presentation, exactly like the pre-quoted discriminator
  the bridge already unquotes.
- **Untagged-union arm segments.** For `AgwRootModel[str | OnePasswordAccountRef]` given `5`,
  pydantic reports two errors whose locs are `('str',)` and `('OpTable',)` (verified). The operator
  wrote neither segment, and the second is an internal class name. This is the same problem the
  bridge already solves for a discriminated union's tag (`_AtTag`, `errors.py:277`), one shape over:
  `_shape.py` exposes an untagged union's members, and the bridge drops the arm segment while
  continuing the walk INTO the named arm when it is a model, so a bad key in the table arm renders
  as `bogus: unknown field; expected one of: account, reference` with the field list intact. Without
  this, the field list is lost (the container walk gives up) and onepassword's messages regress.

Both are additive to a module with a corpus test; the corpus gains an entry for each.

## 9. The model inventory

Re-enumerated against HEAD on 2026-08-06 rather than trusted from the plan. Thirteen models plus one
shared empty model plus five nested models. Field types below are the AUTHORED shape; every model
extends `AgwModel` (or `AgwRootModel`, section 9.4) and inherits strict / frozen / closed-world.

### 9.1 vm-platform (5)

- **`lima`** (`LimaConfig`): `name: Literal["lima"]`, `vm_host: str | None = None`.
- **`wsl2`** (`Wsl2Config`): `name: Literal["wsl2"]` and nothing else. It has no `validate` override
  today, so the retired base rejected every key, which a model carrying only its tag reproduces
  exactly. This is the plan's "empty-config capabilities register the shared empty model" box, in
  the only form a tagged kind admits (section 2.1).
- **`azure-vm`** (`AzureVMConfig`): `subscription_id`, `resource_group`, `region` (required
  non-empty strings); `vm_sizes: list[AzureVMSize] | None = None`;
  `service_principal: AzureServicePrincipal | None = None`.
  - `AzureVMSize`: `cpus: PositiveInt`, `memory: PositiveInt`, `size: NonEmptyStr`.
  - `AzureServicePrincipal`: `tenant_id`, `client_id` (required non-empty strings),
    `secret: Annotated[str, SecretRef(usage="the Azure service-principal client secret", default_template="azure-client-secret")]`.
- **`proxmox`** (`ProxmoxConfig`): `api_url`, `node`, `token_id` (required non-empty strings);
  `template_vmid: int`; `storage: str = "local-lvm"`, `bridge: str | None = None`,
  `pool: str = "agentworks"`, `verify_ssl: bool = True`;
  `token_secret: Annotated[str, SecretRef(usage="the Proxmox API token", default_template="proxmox-token")]`.
- **`aws-ec2`** (`AwsEC2Config`): `region` (required non-empty string);
  `subnet_id: str | None = None`; `instance_types: list[AwsInstanceType] | None = None`;
  `credentials: AwsCredentials | None = None`.
  - `AwsInstanceType`: `cpus: PositiveInt`, `memory: PositiveInt`, `type: NonEmptyStr`,
    `arch: Literal["x86_64", "arm64"]`.
  - `AwsCredentials`: `access_key_id: NonEmptyStr`,
    `access_key_secret: Annotated[str, SecretRef(usage="the AWS secret access key", default_template="aws-secret-access-key")]`,
    `assume_role_arn: str | None = None`.

### 9.2 harness-integration (3)

- **`shell`** (`ShellConfig`): `command: str | None`, `resume_command: str | None`,
  `required_commands: list[str] | None`.
- **`claude-code`** (`ClaudeCodeConfig`): `permission_mode: str | None`, `model: str | None`,
  `extra_args: list[str] | None`. The VALUE sets stay unvalidated on purpose (Claude-owned, drift
  between releases); the comment saying so moves onto the field docstrings.
- **`codex`** (`CodexConfig`): `model`, `sandbox`, `approval_policy`, `profile`,
  `approvals_reviewer` (`str | None`); `network`, `web_search`, `disable_strict_config`
  (`bool | None`); `writable_dirs`, `extra_args` (`list[str] | None`).

### 9.3 git-credential-provider (2)

- **`github`** (`GitHubConfig`): `repos: list[GitHubRepo] | None`, `owner: GitHubName | None`,
  `token: Annotated[str, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]`,
  plus one model validator for the repos/owner mutual exclusion. `GitHubRepo` and `GitHubName` are
  `Annotated[str, ...]` pattern constraints carrying today's `_NAME_RE` charset (interpolated
  verbatim into gitconfig headers, so the charset is load-bearing, not cosmetic). The shipped
  `repo`-singular hint (`github.py:52-55`) is preserved as a field-level `ValueError` from a
  validator on an alias-free extra key? No: see section 14, residual 3.
- **`azdo`** (`AzDOConfig`): `org: AzDOOrg` (required), `token` as above.

### 9.4 secret-backend (3, root models)

`MappingValue` is `str | dict[str, object] | Literal[False]` (`secrets/base.py:23`), so a backend's
config is not mapping-shaped and cannot be a `BaseModel`. All three extend `AgwRootModel`, per the
schema-foundation LLD's section 2.1.

- **`env-var`** (`EnvVarMapping`): `AgwRootModel[NonEmptyStr]`.
- **`onepassword`** (`OnePasswordMapping`): `AgwRootModel[OpUri | OnePasswordAccountRef]`, with a
  `mode="before"` shape guard that raises the shipped one-line message for a value that is neither a
  string nor a table. `OpUri` is `Annotated[str, ...]` carrying today's `op://vault/item/field`
  check; `OnePasswordAccountRef` is an `AgwModel` of `account` and `reference`.
- **`prompt`** (`PromptMapping`): prompt has no mapping vocabulary and rejects EVERY value,
  including `{}`. `typing.Never` is not expressible as a pydantic root (verified: 2.13.4 raises
  `PydanticSchemaGenerationError`), so this is an `AgwRootModel[object]` whose `mode="before"`
  validator always raises, carrying the shipped message. Prompt-local rather than shared: the
  message has to name the backend to be worth reading, so a shared model could only say something
  vaguer than what it replaces.

> **Correction (implementation): a backend mapping's errors frame with the mapping KEY.** A root
> model's errors carry no field path of their own, and a secret may map several backends, so
> `secret/<name>` alone would leave an operator reading "must not be empty" with no way to tell
> which mapping to fix. The owner therefore carries a `label` of
> `secret/<name>.backend_mappings.<backend>`, the second user of the field section 7.6 added for the
> migrator, and strictly better than the `secret 'name'` framing it replaces.

**The generic `False` opt-out is NOT modeled**, in any of the three. It is filtered by the caller
before a backend ever sees a mapping (`secrets/base.py:133-134`), so putting a `Literal[False]` arm
in a backend's model would declare a value that cannot reach it and would emit a schema arm that is
a lie.

`SecretDecl.dependencies` gains the extraction call over each mapping's model, replacing the dead
`SecretBackend.dependencies` it never called. Every shipped backend's mapping is an external
identifier implying no agentworks resource, so the extracted set is `()` today and the change is
behavior-neutral; what it buys is that secret-backend stops being the one kind whose config
references are structurally underivable.

### 9.5 The shared vocabulary

`NonEmptyStr` (`Annotated[str, Field(min_length=1)]`) and `PositiveInt`
(`Annotated[int, Field(gt=0)]`) live in `schema/base.py` beside the model bases. Ten of the thirteen
models want one or both, and the alternative is ten spellings of `min_length=1` whose drift nobody
would notice. `PositiveInt` also carries the bool-is-an-int concern for free: pydantic's strict mode
rejects `True` for an `int` field (verified), which is what today's `isinstance(cpus, bool)` guards
do by hand.

> **Correction (implementation): every `SecretRef`-marked field is `NonEmptyStr`, not `str`.** The
> owner template fills a field that is ABSENT or `null`, and an empty string is neither, so a bare
> `str` would accept `token: ""` where every shipped validator rejects it. Found by the
> git-credential parity test, which is what those tests are for. A related floor is why the two
> operator-overridable catalogs keep `min_length=1` on the LIST itself: selection does `max()` over
> the catalog when nothing fits, so an empty catalog is a site on which no VM can be created.
> Contrast github's `repos`, where the shipped non-empty rule is dropped deliberately (section 14).

### 9.6 The two deliberate breaks, and a third the plan under-states

Carried on the commit's breaking-change marker; the operator note is 2.9's box, written from here.

1. **Proxmox loses lax scalars.** `template_vmid: "9000"` stops loading
   (`plugins/proxmox/platform.py:94` does `int(str(...))` today) and `verify_ssl: "no"` stops
   silently meaning `True` (`platform.py:131` does `bool(self._cfg("verify_ssl", True))`, and a
   non-empty string is truthy). `api_url`, `node`, `token_id`, `storage`, `bridge`, and `pool` gain
   a type check they have never had. No `Field(strict=False)` carve-outs: the second case is config
   that does the opposite of what the operator wrote, which is FR12's stated target.
2. **Explicit `secret: null` flips meaning** for azure `service_principal.secret`, aws
   `credentials.access_key_secret`, and proxmox `token_secret`. The model rule is that absent OR
   `None` yields the owner template, matching what git-credential's `token_dependency` already does,
   so `null` goes from "no edge" to "the default-named edge".
3. **`null` also flips VALIDATION for all THREE, not azure alone.** The plan (`plan.md:366-376`)
   names azure specifically, on the grounds that `_parse_service_principal`
   (`plugins/azure/platform.py:279`) raises today with a message telling the operator to omit the
   key. Checked at HEAD: `plugins/aws/platform.py:129-134` and `plugins/proxmox/platform.py:97-102`
   raise on exactly the same input, with the same "omit the key to use the default" advice. So all
   three today tell an operator to omit the key and, after this step, silently accept `null` as the
   default. The note must name all three; an operator who followed any of those errors' advice will
   not otherwise connect the two. Flagged in section 14 for the lead, since the plan's box is the
   note's source material.

One further check the plan's box asks for: **the migrator does not emit quoted scalars.** Its
emission passes TOML-native values through ruamel, so a `template_vmid = 9000` in TOML lands as an
integer in YAML. A `template_vmid = "9000"` in TOML lands quoted, and now fails the migrator's own
pre-write validation (section 7.4) rather than producing a manifest that will not load, which is the
better of the two failures and needs no separate handling.

## 10. Construction and the typed-ops migration

### 10.1 Construction binds the validated instance

```python
class Capability(ABC):
    def __init__(self, owner_name: str, config: Mapping[str, object]) -> None:
        self.owner_name = owner_name
        owner = RefOwner(kind=self.owner_kind, name=owner_name)
        model = type(self).config_for()
        self._config = validate_config(model, config, owner=owner, capability=type(self).name)
        self._secret_refs = tuple(
            ref for ref in extract_references(model, config, owner) if ref.kind == "secret"
        )
```

The construct-time invariant is UNCHANGED in substance ("an instance is config-valid by
construction"); what changes is that the check is `model_validate` and its result is kept instead of
discarded. Validation happens against the model directly, not through the union: the class is in
hand, so the arm is already chosen.

The constructor takes no facet (section 3.3). Construction is exactly where wave 4 must say which
level it is building for, so a facet argument arrives here then; it is absent now because no caller
could pass a meaningful one, and a defaulted parameter no call site passes buys nothing over adding
it with the consumers that need it.

> **Correction (implementation): the tag synthesis at construct derives from the impl's KIND, which
> may be absent.** `validate_own_config` needs the kind's discriminator to know whether to
> synthesize, and it reads it through `descriptor_for_impl`, which answers by asking which kind's
> implementation contract the class satisfies. A bare `Capability` subclass satisfies none, so that
> lookup returns `None` rather than raising and such a class validates its blob as written. Not a
> tolerance: registration refuses any implementation that does not derive from its kind's contract,
> so the only classes reaching it with no kind are ones that never register (a test double
> exercising the base's own contract).

**Typed access.** `Capability.config` becomes a read-only property returning `AgwModel`; each
implementation overrides it with its own return type:

```python
class LimaPlatform(VMPlatform):
    @property
    def config(self) -> LimaConfig:
        return self._config_as(LimaConfig)
```

`_config_as` is a small generic helper on the base that `isinstance`-checks and raises `StateError`
otherwise, so the narrowing is an ENFORCED invariant rather than a `cast` (principle: enforce, do
not document). Considered and rejected: making `Capability` generic in its config model. It reads
better at the declaration and worse everywhere else, because strict mypy's `disallow_any_generics`
would then require `type[VMPlatform[Any]]` at every bare reference across the registries, the
adapters, the graph stamping, and the site resolver. Four lines per capability against a repo-wide
annotation ripple is not a close call.

`VMPlatform.platform_config` (`vm_platform/base.py:152`) retires in favor of the typed `config`; its
fifteen call sites are the ops migration below anyway.

### 10.2 The ops migration, per capability

The hidden bulk the plan warns about. Enumerated from HEAD; every one becomes an attribute read that
mypy checks:

- **proxmox** (`_cfg` and its 11 call sites, `platform.py:120-217`, `:402`): `self._cfg("api_url")`
  becomes `self.config.api_url`, and `_cfg` itself is deleted.
  `int(str(self._cfg("template_vmid")))` at `:215` becomes `self.config.template_vmid`, which is
  where the strict break earns its keep.
- **azure** (6 reads, `platform.py:629-631`, `:665-667`):
  `str(self.platform_config["subscription_id"])` becomes `self.config.subscription_id`.
  `_parse_service_principal` and `_parse_size_catalog` become attribute reads plus the built-in
  catalog default; `_select_vm_size` keeps its selection logic (it is domain behavior, not
  validation) and takes the typed catalog.
- **aws** (7 reads, `platform.py:385`, `:436-437`, `:498-499`, `:909`): same shape.
- **lima** (`platform.py:149`): `self.platform_config.get("vm_host")` becomes `self.config.vm_host`,
  and `_vm_host_ssh`'s `str(...)` coercion goes with it.
- **shell** (`shell.py:138`, `:143`): `_command_field`'s isinstance dance is deleted; `start` /
  `resume` read `self.config.command` / `.resume_command`.
- **claude-code** (`harness_integration.py:193-199`) and **codex** (`:732-760`): the flag-emission
  loops read typed optionals. Codex's `_FLAG_FIELDS` table stays (it maps a field name to a CLI
  flag, which is emission, not validation) but is keyed against model attributes.
- **azdo** (`azdo.py:69`): `self._org` and its `assert isinstance` are replaced by
  `self.config.org`.
- **github** (`github.py:130`, `:175`): `_validated_scope` re-parsing at construct is deleted;
  `store_username` and `helper_entry` read `self.config.repos` / `.owner`.
- **git-credential base** (`base.py:151-158`): `secret_name` becomes `self.config.token`. The
  `if self._secret_refs: ... else: default_token_secret(...)` fallback is DELETED, not ported: the
  model layer has already resolved the name (FR15's "consumers error on unset rather than defaulting
  locally"), and the schema-foundation LLD names this exact fallback as the one 2.3 removes.

### 10.3 Three proxmox defaults move onto the model

`storage` (`"local-lvm"`), `pool` (`"agentworks"`), and `verify_ssl` (`True`) are consumer-side
fallbacks today, spelled as the second argument of `_cfg`. `_cfg` is being deleted and those exact
lines rewritten, so the choice is between declaring the defaults on the model now or writing
`self.config.pool or "agentworks"` and deleting it again at 2.6. Declaring them here is strictly
cheaper and is what the model layer is for. This is the only FR15 work 2.3 does; the request-side
literals the 2.6 box enumerates (cpus / memory / disk / swap on four platforms) are untouched, and
they are a different mechanism (request defaults, not config defaults).

## 11. Test plan

`tests/capabilities/test_capability_config.py` for the new core module, plus reworks of the two
shipped contract tests.

**The contract itself.** `test_capability_config_contract.py` is reworked from "capabilities are
invoked to validate" to declare-and-receive: a fixture capability declaring a model gets its config
validated, its references extracted, and a typed instance bound, with no method of its own invoked
(pinned by a fixture capability carrying both retired methods, each raising if called: deleting the
base's declarations is not the same promise as nothing calling them, and only the second one is what
keeps a plugin's code out of the finalize pass). It keeps its end-to-end shape (real manifests
through `build_registry`), because that is what proves the finalize pass reaches the new path.

**`test_capability_base.py`** pins construction: the bound instance is the validated model, a
malformed blob raises at construct with owner framing, and `_secret_refs` comes from extraction.

**The config hook.** A fixture capability that overrides `config_for` is honored by `offered_model`,
which is what proves the core reads the hook rather than `config_model` and so that wave 4's
per-facet offering is additive. The ordinary case (the base hook answering with the declaration) is
pinned beside it.

**Conformance check five.** Negative tests at `register_plugin`, one per defect: a model that is not
a `type`, one that does not extend the kind's `base`, one whose `name` Literal disagrees with the
implementation's `name`, one that is incomplete. Each rejected before any registry mutation, with
the plugin named, matching the pattern step 2.0 established.

**Union assembly.** The union's arms equal the registered models; seating a fixture plugin changes
the union without any invalidation call (the contents-keyed cache, section 5.2); an unknown `name`
renders "unknown name 'x'; registered: ...".

**Tagged synthesis.** A blob carrying its own `name` key is a hard error naming the collision; the
synthesized table selects the arm the naming field named, and nothing else can change that
selection.

**Extraction parity, per shipped derivation.** The schema-foundation LLD landed these against
fixture models mirroring the real shapes (its section 9); 2.3 re-points them at the real models,
which is what makes them parity tests rather than self-tests: github token (absent / overridden /
malformed), azure `service_principal.secret` (absent table / absent key / malformed / explicit
null), aws `credentials.access_key_secret`, proxmox `token_secret`. The explicit-null cases assert
the NEW behavior and are the regression pins for break 2 of section 9.6.

**The breaks, pinned as tests, not as prose.** `template_vmid: "9000"` raises; `verify_ssl: "no"`
raises; `secret: null` yields the default-named edge and validates, on all three platforms.

**Fold-gated severity** (the plan's box): a broken blob on a disabled plugin's resource loads with
the row marked and errors on enable; a broken blob on an enabled, ready resource is a load error; an
unregistered capability name stays a hard finalize error (R9.2). All three are behavior this step
must PRESERVE, so the tests are written against the new path and assert today's outcomes.

**Message parity.** The FR12 corpus gains the bad-capability-name entry (which needed assembled
unions, per the schema-foundation LLD's section 9), the `value_error` entry, and the untagged-union
entry.

## 12. The inheritance surface: what was settled, and what step 2.3b built

Recorded here in full so the remaining work was a build, not a re-design. Everything below the
"Settled" heading was written for 2.3; the "As built" subsection records step 2.3b (2026-08-06),
including the one place the design did not survive contact.

**Settled.**

- **Validation runs on the EFFECTIVE blob, never on a partial declared one** (FR12, operator
  decision 2026-08-02). A declared blob on an inheriting surface may be legitimately partial, so a
  model's required fields would wrongly reject a child that a parent completes. Chain length is one
  everywhere but session templates, so this is one uniform rule.
- **Extraction stays two-staged, as two CALLS, never a parameter** (schema-foundation LLD 4.4):
  structural references come off each DECLARED blob and feed the graph the merge walks; secret
  references come off the EFFECTIVE blob so a child overriding a parent's secret name does not
  over-declare. `extract_references` must never learn a `declared`-versus-`effective` flag; that is
  FR21 door (a) closing.
- **The inheritance edge is typed by RELATIONSHIP, not by target kind.** `TemplateReference`
  (`resources/reference.py`) types the TARGET ("points at a template") and coincides with
  inheritance only because `inherits` is currently the sole reason to point at one. FR17 must key on
  `RefRelationship.INHERITS`, which step 2.1 already put on the marker and on `ConfigReference` for
  exactly this reason. `isinstance(ref, TemplateReference)` would silently misclassify a future
  uses-a-template edge and reintroduce the conflation one level down.
- **`merge_config` is capability-owned merge semantics and stays on the capability.** It is not
  validation, and it runs on raw declared blobs by design.

**The interim constraint, enforced (added by review, 2026-08-06).** Until the merged blob is what
validates, a harness-integration config model may not declare a REQUIRED field beyond its tag and
its owner-templated references: a child template's declared blob is legitimately partial, so the
finalize pass would fail it with nothing in the error naming inheritance as the cause. Nothing
breaks today only because no shipped model has one, which is exactly why it is refused at class
definition (`HarnessIntegration.__init_subclass__`) rather than written down: the trap is invisible
from the author's side, since the model looks right and every test of the capability alone passes.
That method is deleted by the step below, which is what makes the constraint's expiry real.
**Deleted 2026-08-06 by step 2.3b, as scheduled.**

**Left to build, and it is a real body of work:** resolving each inheritance chain over registry
rows inside finalize, a per-key provenance channel so the bridge can name the template that declared
a bad key, repointing the runtime-need traversals (the secret resolve union, preflight's
resolvability sweep, dependency listings) to filter on `INHERITS`, and the policy call on whether
readiness or enablement propagates across an inheritance edge (today session templates opt out of
the fold entirely, so nothing is decided by accident). Until then
`sessions/templates.py::_validate_merged` keeps its resolve-time timing, having been repointed at
the core entry point so no capability code runs there either.

**Why it is not in this step.** It shares no code with the contract flip: the flip is about WHO
validates, this is about WHICH BLOB and WHEN. Landing them together would produce one change whose
two halves fail independently, and the flip is the one the other eleven boxes of step 2.3 depend on.
Flagged for the lead as the scope call of this LLD (section 14).

### 12.1 As built (step 2.3b, 2026-08-06)

**The one thing that did not survive contact: the traversal split alone is a REGRESSION, and the
effective-config rule has to reach all four inheriting kinds, not only session-template capability
config.** Everything above is written about the harness blob, on the reasoning that "chain length is
one everywhere but session templates". That is true of CAPABILITY CONFIG and false of the graph. The
edges that actually flow through the one runtime-need traversal today are the non-capability ones:
`vm-template`'s `env` block and its `tailscale_auth_key`, `agent-template`'s `git_credentials` and
`user_install_commands`, `workspace-template`'s `env`. Before this step a child reached all of them
by CROSSING the inheritance edge, so:

- excluding the edge without any other change makes `collect_secrets_for` on a child stop returning
  its inherited env secrets, which is a silent under-answer on a live surface; and
- keeping the edge leaves FR17 unmet in shipped code, not hypothetically:
  `vm-template.tailscale_auth_key` IS an owner-defaulted secret name on an inheriting kind.
  Reproduced at HEAD before the change, with a child overriding it: `collect_secrets_for` returned
  both `kid-auth-key` and the parent's `tailscale-auth-key`.

So the two halves are one change and were built as two commits with a green step between them,
ordered so no window under-reports:

1. **Effective-declaration edges.** Each of the four inheriting kinds resolves its own chain at
   finalize and emits its runtime-need refs off the merged result, keeping `inherits` itself as the
   declared `INHERITS` edge. The traversals still crossed at this point, so the graph over-reported
   exactly as before rather than losing anything.
2. **The traversal split.** `DependencyGraph.runtime_reachable_from` is the closure over `USES`
   edges only and `collect_secrets_for` reads it; the recipe use-gate takes the other closure (see
   the gate paragraph below, which corrects what this step first shipped).

Details worth having in one place:

- **The seam is the build context, renamed.** `BuildContext` carries the published rows and became
  `FinalizeContext`, because the validate pass is handed the same object: a row's edges and its
  shape check are two readings of one merged declaration and must not be computed from two different
  ones. `enabled_backends` deliberately did NOT move onto it: that set is only known after the fold,
  so a field for it would read empty during the build walk, which is the
  silently-wrong-at-one-call-site shape this effort keeps finding. It has since been removed from
  the signature entirely (section 7.4), which settles the question the other way: no implementation
  of `validate_config` gets an enablement verdict at all, from the context or from anywhere else.
- **Totality needed two mechanisms, neither of them a bare `except ConfigError`.**
  `InheritanceCycleError` (new, `errors.py`, raised by all four resolvers' cycle guards through one
  shared constructor) lets the finalize view degrade on a cyclic chain and nothing else; the
  degraded value is provably never observed, because a degraded row implies a loop among present
  nodes and finalize's cycle pass raises before the graph is built (pinned by a test on
  `vm-template`, whose degraded edge set loses the auth-key secret). And the harness merge is
  reached by name (`merged_config`), falling back to the base contract's own child-wins default for
  an unregistered name, because the build walk merges a chain before any name has been checked.
  Verified by a real failure during implementation: a blanket `except ConfigError` around the merge
  swallowed the unknown-integration-name case and the miss policy then never fired, which is exactly
  the silent-wrong-answer class.
- **A bare `FinalizeContext()` degrades to "this declaration alone", not to nothing.** Each emitter
  merges itself into the rows it was handed (`{**context.rows_of(kind), self.name: self}`), a no-op
  during the real walk. Found the same way: without it, four existing tests that call
  `dependencies(BuildContext())` directly went silently empty rather than failing loudly.
- **`ResolvedVMTemplate.dependencies` is deleted.** It was a second derivation of the edge set the
  declared template now computes, with no production caller.
- **Provenance** is per top-level key of the merged blob, recording the LAST declarer in merge order
  (the one child-wins keeps) and restricted to the keys that survived. The bridge renders it as an
  `(inherited from <owner>)` tail and suppresses it when the declarer is the owner already at the
  head of the line. It fires when the CHILD's row is the one the validate pass reaches first; the
  parent's own row reports the same key on its own account, which is what makes the tail necessary
  rather than redundant.
- **Of FR17's three named traversals, only one reads the graph, and the other two were already
  right.** The secret resolve union comes off a plan's NODES (`orchestration.secrets.secret_union`),
  which hold already-resolved templates, and preflight's resolvability prediction runs over each
  node's own `config_secret_refs`; neither crosses an edge at all, so neither needed changing and
  both are asserted in the regression test so that stays true. The graph-reading one is
  `collect_secrets_for`, whose live caller is the `secret` kind's per-session `instances` projection
  (`agw secret describe`'s "Used by"), not the eager-resolve path its docstring claimed.
  **Dependency LISTINGS were deliberately left crossing**: `describe`'s "Referenced by:" and the
  REFS count read `dependents_of`, which is inbound and one hop, and a parent template genuinely IS
  referenced by its children ("a parent template" is the usage prose an operator wants before
  deleting it). Excluding it there would hide a true structural fact rather than fix an attribution.
- **`_validate_merged` retired**, and `_resolve` now validates nothing at all: finalize checks the
  shape and construction re-validates the blob it binds, so the resolve-time call was the third copy
  and the one at the wrong time.
- **The producing half moved reference ATTRIBUTION onto the inheritor, and that had to be paid for**
  (found by review, 2026-08-06). Three places answered "who wants this target?" with the edge's
  `source`: the miss-policy error, an auto-declared row's `Origin`, and its synthesized description.
  A child now publishes the runtime needs of its merged declaration, so `source` is the child even
  when an ancestor wrote the name, and an operator told
  `vm-template 'kid' references unknown apt-package 'nope'` opens a file with no `apt_packages` in
  it. Worse, the parent emits the same edge, so which row got blamed depended on the order the build
  walk reached them in, and a file rename could move it. Fixed by carrying `declared_by` on
  `ResourceReference` and `ReferenceEntry`, read through a `declarer` property that falls back to
  `source`, so a non-inheriting producer says and means nothing different. Per-key declarers come
  from `resources/inheritance.py`, which holds the chain's SHAPE (its merge ORDER, and which layer
  last declared a key) once for all four kinds even though none of them share a merge rule; the
  order is a contract, so it is pinned by folding the layers and getting each resolver's own merged
  result. The harness block is attributed at BLOCK granularity, to the layer that selected the
  integration, because a `ConfigReference` carries no field to be finer with; that is exact for
  every shape a template can currently write, since a config block without a selector beside it does
  not load, and closing the remaining case would need `extract_references` to carry its field path.
  **The tests publish the CHILD first**, which is the ordering that exposes it: with the parent
  first the answer comes out right by accident.

**The policy call, made explicitly (FR17 left it to this LLD).** ENABLEMENT propagates across an
inheritance edge; READINESS does not. Enablement, because a template resolver compiles the parent's
declaration into the recipe the use-gate is about to act on, so a disabled parent is not a runtime
need the child happens to have, it is source the child is made of. Readiness, because no template
kind implements `not_ready`, so every template row folds to a ready verdict and an inheritance edge
changes nothing; a future inheriting kind that grows a hook is handed every out-edge's state and
decides for itself, which is R4's rule and not a traversal's to decide in advance.

**The gate is a UNION of two closures, not the full one** (corrected by review, 2026-08-06). The
first pass kept `ensure_recipe_enabled` on the crosses-everything closure, reasoning that it was
already right to cross inheritance. It was, before the producing half landed; afterwards it also
reached an ancestor's own leaves, including ones the child OVERRODE, so disabling
`secret/tailscale-auth-key` refused a child that had renamed it. Reproduced. The recipe is
`composed_from` (the INHERITS closure: the ancestor rows the resolver compiles in, which is what
carries the enablement policy) plus `runtime_reachable_from` (what the row needs, which since the
producing half already includes everything it inherited AND still uses). What the union deliberately
omits is an ancestor's standalone needs. It failed SAFE, which is why it took a review to find and
why the LLD had recorded the old behavior as settled: worth remembering that "fails safe" and
"nobody will notice" are the same sentence.

`reachable_from` was deleted with that change rather than left with no caller. Its only property was
crossing everything, which after the split is not a question anyone asks, and a query with that
shape is what a future consumer reaches for without deciding. Both closures now name the
relationships they CROSS, so a third `RefRelationship` joins neither until someone decides, and
`test_every_relationship_has_a_closure` fails until they do.

**One operator-visible semantic this creates, flagged for the lead.** Validation is per ROW over
that row's own chain, so a base template whose blob only a CHILD completes is now a load error in
its own right. That is the honest reading of FR12 (any template is directly namable at
`session create`, so "abstract base template" is not a thing the schema supports) and it is pinned,
but it is only reachable now that a required field is declarable at all, so nothing shipped changes
today. If the lead wants abstract bases, that is a new concept (an `abstract: true` marker, or
validation scoped to leaves) and should be decided before a capability ships a required field.

## 13. Implementation sequence

Each is one commit with the full gate green after it. Always additive-then-subtractive: the core
path lands and is exercised beside the old one, then the old one is deleted per kind, so there is no
red window.

1. **Vocabulary and contract.** `Facet` and `facet_config` (both since removed, section 3.3),
   `NonEmptyStr` / `PositiveInt`, `FramedConfigError`, the two bridge extensions (section 8) with
   their corpus entries, `RefOwner.label`. Additive; nothing consumes them yet.
2. **The descriptor's `config_schema` and conformance check five.** `ConfigContract` on the four
   records, the check, its negative tests. Still additive: no implementation declares a model, so
   the check is scoped to implementations that do (a model-less impl is refused only from step 4
   onward, when the attribute becomes required).
3. **The core module.** `capabilities/config.py`: the union cache, `tagged_config`,
   `validate_capability_config`, `capability_config_references`, `validate_config`. Unit-tested
   against fixture capabilities; no production call site yet.
4. **Per kind, one commit each, in this order: git-credential-provider, vm-platform,
   harness-integration, secret-backend.** Each commit adds that kind's models, flips its consuming
   resource's two call sites plus construction, migrates its ops to typed fields, deletes that
   kind's hand-rolled validators, and updates its tests. git-credential first because it is the
   smallest surface that exercises everything (a templated `SecretRef`, a cross-field validator, a
   pattern-constrained scalar, and the token-helper deletion); secret-backend last because it is the
   only root-model kind and the only map-keyed one.
5. **Retire the base contract.** Delete `Capability.validate` / `Capability.dependencies`,
   `SecretBackend.validate_mapping` / `dependencies`, and their descriptor `required_operations`
   entries; make `config_model` required at conformance. This commit is what makes the flip
   irreversible, and it is deliberately last so that every kind is already off the old path when it
   lands.
6. **The remaining callers.** The migrator's three pre-write checks and
   `sessions/templates.py::_validate_merged`. Separate because they are not per-kind and their
   framing decisions (section 7.6, section 12) are their own review surface.
7. **Docs.** `capabilities/README.md` and `capabilities/harness_integration/README.md` document
   declare-and-receive; the standing "may be deprecated" notes on the retired API are deleted with
   the API. The full permanent-doc promotion is 2.9's box; what lands here is the correction of text
   that would otherwise describe a contract that no longer exists.

Step 2.3b's own sequence, appended when it landed (2026-08-06; rationale in section 12.1):

1. **Type the inheritance edge by relationship.** `ResourceReference.relationship`, defaulted to
   `USES`; `inherits_reference` as the one spelling, used by all four inheriting kinds;
   `sourced_references` carries a modeled reference's relationship through. Additive, no consumer.
1. **Effective-declaration edges.** `FinalizeContext` carries the published rows; each inheriting
   kind emits its runtime needs off its merged declaration. The traversals still cross at this
   point, so the graph over-reports exactly as before rather than losing anything: this is what
   makes the green step between the two halves honest rather than merely compiling.
1. **The traversal split.** `runtime_reachable_from`, the secret walk repointed at it, the
   enablement-propagation policy stated and pinned, and the FR17 regression test.
1. **Effective-config validation at finalize.** The merged harness blob validates in the finalize
   pass with per-key provenance through the bridge; `_validate_merged` and the interim
   `HarnessIntegration.__init_subclass__` guard are both deleted.
1. **Docs.** This section, section 12.1, `capabilities/README.md`, and
   `capabilities/harness_integration/README.md`.

## 14. Contradictions and residual decisions for the lead

**Contradictions found against HEAD or the upstream artifacts.**

0. **The schema package could not stay under `resources/`, and this step moved it.** The step 2.1
   LLD places it at `resources/schema/` "beside `resources/reference.py` (the reference types it
   produces)" and states the direction rule in those terms. That does not survive contact with the
   first capability config models. Importing ANY module under `agentworks.resources` runs
   `resources/__init__.py`, which imports `resources.kinds`, which imports all four capability
   packages; a capability module declares its config model at CLASS-DEFINITION time, so it must
   import the schema package at module level, and that import closes the cycle. Verified by
   execution: with `capabilities/base.py` importing `agentworks.resources.schema`,
   `python -c "import agentworks.capabilities.git_credential.base"` fails with
   `cannot import name 'Capability' from partially initialized module`. Settled by moving the
   package to a top-level `agentworks/schema/`, which is the same constraint `declared_resource.py`
   and `source_location.py` already sit at top level for, and their docstrings already state it. Two
   pieces moved with it so the package is a real leaf: `RefRelationship` and `ConfigReference` now
   live in `schema/reference.py` and are re-exported by `resources/reference.py` (the 2.1 LLD's
   direction rule, one level out, with the same reasoning), and `format_file_path` moved into
   `source_location.py` so the bridge can render an operator-facing path without importing
   `resources/render.py`. Pinned by a boundary test that scans the package's imports AND
   subprocess-imports each capability base on its own, both directions, because a source scan alone
   would miss the cycle and an import alone would miss a dependency nobody exercises.

1. **The plan under-states break 2: `secret: null` flips VALIDATION for all three cloud platforms,
   not azure alone.** `plan.md:373-376` names azure's `_parse_service_principal` as "the more
   visible half" and implies aws and proxmox only change their edge. At HEAD,
   `aws/platform.py:129-134` and `proxmox/platform.py:97-102` raise on explicit `null` with the same
   omit-the-key advice. Section 9.6 records all three. This matters because the 2.9 upgrade note is
   written from that box.
2. **The HLA and the plan spell the secret-backend attribute `mapping_model`** (`hla.md:190`,
   `plan.md:350`). Section 2.1 settles it as `config_model`, uniform across kinds, with the
   reasoning (two names put a per-kind branch back into the two sites step 2.0 made kind-agnostic).
   The substance the artifacts ask for is unchanged: the backend's per-secret mapping model IS that
   kind's registered config.
3. **"Empty-config capabilities register the shared empty model"** (`plan.md:339`) cannot hold
   literally for a kind with a discriminator: a model with no fields has no `name` field, so it
   cannot be a union arm. wsl2 declares a model carrying only its tag, which is the box's intent.
   The consequence worth stating is that at wave 2 a SHARED empty model would have zero users, so
   none is built (section 2.1); one becomes real when a map-keyed or untagged surface wants it.
4. **`SecretBackend.dependencies` is dead surface**, not a contract half. `secrets/backends.py:157`
   documents it as "the `secret-backend` half of the capability contract's `dependencies`/`validate`
   split", and the secret-backend descriptor requires it (`secrets/kinds.py:260`), but it has zero
   production callers: `SecretDecl.dependencies` derives its edges from `would_attempt` and the
   explicit mapping keys. Deleted rather than ported (section 7.5), with core extraction wired in
   its place so the kind is not an exception.
5. **`Capability.validate`'s docstring is stale about where it is invoked** ("manifest decode with
   `file:line` framing; legacy TOML loaders", `capabilities/base.py:361-362`). Decode has not called
   it since phase 1 (`manifests/decode.py:18-23` says so outright). Moot in this step, since the
   method is deleted, but worth knowing that the shipped documentation of the retiring contract was
   already wrong about it.
6. **The descriptor's deferred-field comment still describes the RESCINDED consuming-kind design**
   (`capabilities/descriptor.py:193-199`: "Resolution is keyed by CONSUMING RESOURCE KIND from day
   one"). That was superseded by the facet ruling of 2026-08-06. The comment is replaced when the
   field is created (step 13.2). Noted because it is the only place on `main` where the rescinded
   design still reads as settled.
7. **The step 2.1 LLD's `resources/schema/` placement did not survive the first capability models.**
   Recorded as contradiction 0 above, with the verified reproduction. The package is
   `agentworks/schema/` now, and a boundary test keeps it a leaf.
8. **github's `repos: []` is accepted where the shipped validator rejected it.** A deliberate
   loosening, pinned by a test that says so: an empty list and an absent field mean the same thing
   to every consumer of that field (`store_username` and `helper_entry` both test truthiness), so a
   floor would only buy an error an operator can hit by writing something inert. The two vm-platform
   CATALOGS keep their floor for the opposite reason (section 9.5), which is why the two cases are
   worth stating together.

9. **The flip left eight DEAD exemptions in the graph guard's allow-lists** (four registry-read,
   four dependencies), found by review and measured by removing each entry and re-running the
   scanners: none was dead at the step's base commit. A guard that was exactly tight was loosened
   while this LLD's own comment claimed the exempted surface had shrunk. Deleted, and
   `test_every_exemption_is_load_bearing` now fails any entry whose removal changes nothing, so the
   same rot cannot follow the next migration silently.
10. **A one-arm discriminated union collapses**, which this step's assembled union did not account
    for. `Union[(X,)]` is `X`, so a capability kind with a single registered implementation produced
    a bare model; pydantic still dispatched on the tag, but the shape classifier read it as a nested
    block, so the bridge rendered the tag as a field the operator never wrote and lost the arm's
    field list. Latent only because every shipped kind currently has two or more implementations.
    Fixed in the classifier, where the shape rule belongs.
11. **The FR17 traversal split's blast radius is wider than the artifacts describe** (found by step
    2.3b, 2026-08-06; full account in section 12.1). The plan's FR17 survey concludes that "what is
    missing is only the consuming half", and the HLA and this LLD both discuss the inheritance
    surface as a capability-config question, on the reasoning that chain length is one everywhere
    but session templates. That holds for capability config and not for the graph: the edges that
    actually cross an inheritance edge in the shipped runtime-need traversal are `vm-template`'s env
    and auth key, `agent-template`'s credentials and install commands, and `workspace-template`'s
    env. Implementing the consuming half alone silently drops a child's inherited env secrets;
    implementing neither leaves FR17 unmet against a shipped owner-defaulted secret name
    (`tailscale_auth_key`). Both reproduced. The producing half (every inheriting kind emitting the
    runtime needs of its EFFECTIVE declaration) is therefore part of the same change, and the FR17
    boxes cannot be read as scoped to session templates.

**Residual decisions for the lead.**

- **The scope call: effective-config validation, provenance, and the FR17 traversal split are left
  to a follow-on step** (section 12), against the plan's step-2.3 boxes. The reason is that they
  share no code with the contract flip and would double this step's review surface, and the flip is
  what every other 2.3 box depends on. If the lead wants them in 2.3, the sequence is section 13
  plus three more commits and the step roughly doubles. Stated here rather than absorbed silently,
  because it is the one place this LLD narrows what the plan asked for. **Resolved: taken as step
  2.3b and delivered 2026-08-06 (section 12.1).**
- **Abstract base templates are now a load error, and that is a new ruling** (section 12.1, last
  paragraph). Latent until a capability declares a required field, so it costs nothing to overturn
  today and gets expensive once one ships.
- **~~`facet_config` raises `StateError`, not `ConfigError`.~~** Moot: `facet_config` was removed on
  2026-08-07 (section 3.3). The question returns with wave 4's resolver, and the answer proposed
  then was that an unoffered facet is a consumer asking a producer for a level it does not serve, a
  framework mistake rather than an operator's. It becomes live again if the lead ever expects a
  facet to be operator-selectable.
- **github's `repo`-singular hint.** Today `_validated_scope` special-cases the singular misspelling
  with a bespoke message (`github.py:52-55`). Under a closed-world model it becomes the generic
  unknown-field error, which names `repos` in its field list, so the information survives but the
  targeted nudge does not. Recovering it would mean a model validator that inspects unknown keys,
  which fights `extra="forbid"`. Recommendation: let it go and let the field list carry it; flagged
  because it is a deliberate small regression in help quality on a real operator mistake.
- **`RefOwner` gains an optional `label`** (section 7.6) so the migrator keeps its TOML vocabulary.
  This widens a record the schema foundation owns. Additive and defaulted, but the lead may prefer
  the migrator adopt `kind/name` framing and the field not exist.
- **The union cache keys on registry CONTENTS** rather than on `kind` alone with invalidation
  (section 5.2), which differs from the HLA's "cached on the kind's registry entry". Cheap to
  overturn; the reason to prefer it is that the failure mode of the alternative is silent.
- **`_config_as` narrowing versus a generic `Capability`** (section 10.1). Four lines per capability
  against `type[VMPlatform[Any]]` repo-wide under strict mypy. If the floor moves to 3.13, PEP 696
  type-parameter defaults would make the generic option cheap, and this is worth revisiting then.
