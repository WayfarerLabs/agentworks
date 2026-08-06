# LLD: Step 2.1, the Schema Foundation (with step 2.2, the error bridge, folded in)

Date: 2026-08-06

Status: DRAFT, awaiting review. Companion to [frd.md](frd.md), [hla.md](hla.md) (Components 1 and
5), [plan.md](plan.md) (steps 2.1 and 2.2). Builds on
[descriptor-adoption-lld.md](descriptor-adoption-lld.md) section 7, which settled the config-schema
contract (`config_model_for(consuming_kind)`) this foundation supplies models to. Contradictions
found against HEAD or against the upstream artifacts are in section 11, not designed around
silently.

Step 2.1 builds `resources/schema/`: the framework-wide model vocabulary every later step derives
from. Nothing in this step is wired into decode, finalize, or the registry; steps 2.3 and 2.5 do the
wiring. That is deliberate and is what keeps 2.1 always-green: the package lands complete, with unit
tests, alongside the machinery it replaces, and each later step deletes one hand-rolled surface.

**This LLD also covers step 2.2, the error bridge.** The fold decision and its justification are
section 7.

## 1. What this step delivers, and what it does not

Delivered by 2.1:

- `AgwModel` / `AgwRootModel`: the shared strict, frozen, closed-world bases (section 2).
- `SecretRef` / `ResourceRef` field markers and their `x-agw-ref` JSON Schema encoding (section 3).
- `extract_references(model_cls, blob, owner)`: the total, never-raising, source-agnostic reference
  extractor (section 4), including the owner-templated secret-name derivation FR18 requires (section
  5).
- `iter_field_docs(model_cls)`: the ordered field-reference stream (section 6).
- The error bridge: `pydantic.ValidationError` to owner-framed `ConfigError` (section 7).
- The pydantic dependency, the mypy plugin, and strict mypy green repo-wide (section 2.4).

NOT delivered by 2.1, deliberately:

- No capability config models, no registration surface, no `config_schema` descriptor field. That is
  step 2.3, and the descriptor LLD already records the trigger (`descriptor-adoption-lld.md` section
  7).
- No kind spec models, no decode swap. Step 2.5.
- No call sites: `Capability.validate` / `Capability.dependencies` /
  `SecretBackend.validate_mapping` all still exist and still run after 2.1. The old and new regimes
  coexist for exactly the length of steps 2.3 and 2.5, which is the shortest bridge the always-green
  constraint allows.
- No renderer, no describe surface, no emission CLI. Steps 2.7 and 2.8 consume what lands here.

The package is `cli/agentworks/resources/schema/`, beside `resources/reference.py` (the reference
types it produces) and `resources/kind.py`. It has no import dependency on `capabilities/`,
`manifests/`, or `plugins/`, which is what lets those import it without touching the cycle
discipline the descriptor table lives under (`capabilities/descriptor.py` module docstring).

**The one edge that matters is the one this design creates**, and it runs in a single direction:
`resources/schema/` imports `resources/reference.py`, never the reverse. `reference.py` is a leaf
today (stdlib plus a `TYPE_CHECKING` block, `resources/reference.py:49-55`) and it stays one. That
constraint is why `RefRelationship` is defined in `reference.py` rather than in the schema package
(section 3.1): it is a DEFAULT on `ConfigReference`, so it is needed at runtime, and importing it
from `resources/schema/markers.py` would make `reference.py` import the schema package whose
`extract` module imports `ConfigReference` back. Anything reaching `reference` first would then die
on a partially-initialized module, and it would only appear to work when the schema package happened
to import first, which is the worst kind of cycle: order-dependent and invisible until it is not.
The rule for this package is therefore stated positively: **`resources/schema/` may import
`resources/reference.py`; nothing under `resources/schema/` may be imported by it.**

## 2. The base model

### 2.1 Spelling

```python
# resources/schema/base.py
from pydantic import BaseModel, ConfigDict, RootModel

_SHARED = {
    "frozen": True,                  # FRD "frozen/immutable declaration objects remain the norm"
    "strict": True,                  # no silent coercion; "8" is not 8, 1 is not True
    "validate_default": True,        # a declared default is checked, not trusted (section 2.2)
    "use_attribute_docstrings": True,   # the authored description lives under the field (2.3)
    "revalidate_instances": "always",   # a nested model instance is re-checked, never trusted
}

_AGW_MODEL_CONFIG = ConfigDict(extra="forbid", **_SHARED)   # FR12: closed world
_AGW_ROOT_MODEL_CONFIG = ConfigDict(**_SHARED)              # no extra: see below


class AgwModel(BaseModel):
    """Base for every agentworks schema model: kind specs and capability
    config alike. Strict, frozen, closed-world."""

    model_config = _AGW_MODEL_CONFIG


class AgwRootModel[T](RootModel[T]):
    """Base for a modeled surface whose value is NOT a mapping."""

    model_config = _AGW_ROOT_MODEL_CONFIG
```

**The two configs must be separate, and this is not a style preference.** `RootModel` refuses
`extra` outright: its `__init_subclass__` raises `PydanticUserError` with code `root-model-extra`
("RootModel does not support setting model_config['extra']"), so a shared config carrying
`extra="forbid"` fails at class definition and `base.py` does not import. Splitting is the only
spelling that runs. **Verified by execution against pydantic 2.13.4** (lead, 2026-08-06): defining a
`RootModel[str]` subclass with a config carrying `extra="forbid"` raises exactly that
`PydanticUserError`. The same run settled the strict-conversion question below: `int` IS accepted
for a `float` field under `strict=True` (`M(x=1).x` yields `1.0`).

Closed-world is not weakened by the split, because a root model has no keys to be unknown: FR12's
"unknown keys are hard errors" is a property of MAPPING-shaped surfaces, and every mapping-shaped
surface extends `AgwModel`. A root model's strictness is its root type, which `strict=True` already
enforces: `AgwRootModel[str]` rejects a table, and a root model whose root is itself a nested
`AgwModel` inherits that model's `extra="forbid"` for the mapping it wraps. So the closed-world
guarantee reaches every key an operator can write, which is what FR12 needs.

`AgwRootModel` is not speculative generality: it is required at 2.3 by the secret-backend kind.
`MappingValue` is `str | dict[str, object] | Literal[False]` (`secrets/base.py:23`), so env-var's
mapping is a bare string and onepassword's is a string OR a table
(`plugins/onepassword/backend.py:281-300`). A `BaseModel` cannot be a bare string, so those two
`mapping_model` registrations are root models or they are nothing. (The `Literal[False]` arm is NOT
part of what a backend model has to express: the generic opt-out is filtered by the caller before
any backend sees its mapping, `secrets/base.py:133-134`. 2.3 should model `str` and `str | table`
respectively, and leave `False` where it is handled.) Recording the base here rather than letting
2.3 invent one is what keeps ONE set of shared settings, with the single documented divergence
above.

### 2.2 What each setting buys, and the one carve-out

- **`extra="forbid"`** is FR12 in one line, and it reaches every surface that HAS keys: kind specs,
  capability config, and nested models (root models carry it through whatever `AgwModel` their root
  wraps, section 2.1). This retires `_warn_unexpected_keys` (`config/loaders_core.py:52`), whose
  warn-and-load-anyway behavior is the footgun FR12 names. The retirement itself happens kind by
  kind in 2.5; 2.1 only makes the replacement exist.
- **`frozen=True`** matches the frozen-dataclass discipline the registry already relies on, which is
  the FRD's stated constraint and the whole justification. (Not a hashability argument: pydantic
  generates `__hash__` from field values, so any model with a `list` field raises `TypeError` when
  hashed, and half the 2.3 inventory has one, `extra_args`, `required_commands`, `writable_dirs`,
  `instance_types`. 2.3's union cache keys on a TYPE, which needs no instance hashability.)
- **`strict=True` is a deliberate TIGHTENING, taken knowingly.** The manifest frontend is YAML
  through pyyaml's safe loader, which already yields real `int` / `float` / `bool` / `str` / `None`
  / `list` / `dict`, so there is nothing legitimate left to coerce: a quoted `"8"` where an integer
  belongs is an operator mistake and lax mode would silently accept it.
  - For most of the inventory this preserves shipped behavior exactly, because today's hand-rolled
    validators are already `isinstance`-strict in the same sense: codex, claude-code, shell, lima,
    github, azdo, env-var, and onepassword all type-check with `isinstance`
    (`plugins/codex/harness_integration.py:361-381` is representative), and aws and azure even
    exclude `bool` from `int`, which is pydantic's strict semantics.
  - **It is a real break for proxmox, and the doc says so rather than claiming otherwise.**
    `plugins/proxmox/platform.py:93-94` validates `template_vmid` with
    `int(str(config["template_vmid"]))`, so `template_vmid: "9000"` loads today and becomes an error
    against a strict `int` field. And `api_url`, `node`, `token_id`, `storage`, `bridge`, `pool`,
    and `verify_ssl` get NO type check at all today (`_REQUIRED_KEYS` is a presence check,
    `platform.py:90-92`), with `verify_ssl` consumed as `bool(self._cfg("verify_ssl", True))`
    (`platform.py:131`), so `verify_ssl: "no"` currently means TRUE and becomes an error. Both are
    breaks worth taking rather than papering over with a `Field(strict=False)` carve-out: the second
    one is a config that silently does the opposite of what the operator wrote, which is exactly
    FR12's stated target. **This needs an operator-facing breaking-change note** when the proxmox
    model lands in 2.3 (quote your numbers, spell your booleans), and 2.3's commit carries the
    marker, exactly as phases 1 and 2.4 do for their breaks. The plan now carries this as its own
    2.3 box (added by the lead, 2026-08-06).
  - **The float question resolves cleanly:** `int` IS accepted for a `float` field in strict mode,
    so `memory: 8` validates against `memory: float` with no carve-out needed. Where a field
    genuinely wants a lenient rule anyway, the opt-in is per field
    (`Annotated[float, Field(strict=False)]`) with a comment saying why, never a relaxation of
    `_AGW_MODEL_CONFIG`. One global posture, local exceptions a reader can see.
- **`validate_default=True`** means a declared default is validated rather than trusted. Note what
  it does NOT do: it does not fire at class definition, it fires when a model is validated with the
  field omitted, so a wrong default surfaces the first time a document leaves that field out (which,
  for a defaulted field, is the common case and usually the first test). FR15 makes defaults
  load-bearing, so having them checked at all is what matters.
- **`revalidate_instances="always"`** exists so that binding a validated model instance at construct
  (Component 2) cannot smuggle an unvalidated nested instance past the boundary. It costs a re-walk
  of a few small documents.

### 2.3 Descriptions come from attribute docstrings

`use_attribute_docstrings=True` makes this the authored form:

```python
class LimaConfig(AgwModel):
    vm_host: str | None = None
    """SSH host running limactl for a remote-Lima site (e.g. 'user@host'). Omit for a local site."""
```

Decision: attribute docstrings are the primary description channel, and an explicit
`Field(description=...)` wins where one is given. Reasons: it matches how this repo already
documents class-level attributes (`capabilities/base.py:290-301` documents `contract_version`
exactly this way), it keeps the description physically attached to the field, and it means FR6's
"per-field operator-facing description" costs an author nothing beyond the comment they would write
anyway.

Two consequences to state plainly, because they are constraints on every later author:

- The mechanism reads source via `inspect.getsource`, so a model built with `create_model` gets no
  docstring descriptions. Authored models are therefore always written out as classes. Nothing in
  this effort needs `create_model` (union assembly in 2.3 builds a discriminated union TYPE, not a
  model), so this is a rule, not a limitation we are working around.
- A field with neither a docstring nor an explicit description yields `description=None` in the
  stream and renders as an undocumented field. It is not an error at 2.1. Whether the renderer or a
  conformance check should require descriptions on registered models is 2.3's call; noted, not
  decided here.

### 2.4 The pydantic pin, the mypy plugin, and the gate

Checked at authoring time (2026-08-06, PyPI): **latest stable is pydantic 2.13.4**, whose
`requires-python` is `>=3.9`. This repo's floor is `requires-python = ">=3.12"`
(`cli/pyproject.toml:5`), so there is no conflict.

- Pin: `"pydantic>=2.13.4,<3"` in `cli/pyproject.toml`'s `[project].dependencies`. Lower bound at
  the checked-latest stable (we want its bug fixes and its `use_attribute_docstrings` behavior);
  upper bound excludes the next major, since a v3 would be a re-litigation of this whole design.
- **Re-check at implementation, do not trust this line.** The standing repo rule
  (`.claude/rules/latest-stable-versions.md`) applies: whoever implements 2.1 re-queries the latest
  stable and pins that, recording the checked version and date in the commit message. If it has
  moved past 2.13.4, the new number is the pin and this paragraph is stale, which is fine.
- pydantic v2 brings `pydantic-core` (a compiled wheel), `annotated-types`, and `typing-extensions`
  transitively. The repo already ships compiled dependencies (the azure SDKs, boto3's stack), so
  this changes no packaging story.
- **The mypy plugin is enabled**: `plugins = ["pydantic.mypy"]` under `[tool.mypy]`, plus

  ```toml
  [tool.pydantic-mypy]
  init_forbid_extra = true
  init_typed = true
  warn_required_dynamic_aliases = true
  ```

  `strict = true` stays exactly as it is (`cli/pyproject.toml:81`), and **strict mypy must stay
  green repo-wide**: the plugin is additive checking, never a relaxation. If a model shape cannot be
  expressed under strict mypy, the model shape is wrong, not the config.

- The full standing gate (`ruff check`, `ruff format --check`, `mypy .` strict, `pytest -q`,
  `./scripts/lint-files.sh`) is green at every 2.1 commit, as for every step.
- `pydantic` moves from the SDD cspell dictionary to the root `.cspell.json` in the same commit that
  first imports it, per the plan's 2.1 box (it is now permanent code, not SDD prose).

## 3. The field markers and their `x-agw-*` encoding

### 3.1 The vocabulary

Two spellings over one record, as `Annotated` metadata:

```python
# resources/reference.py, beside ResourceReference and ConfigReference (see section 1)
class RefRelationship(Enum):
    """What the referring model MEANS by pointing at the target."""

    USES = "uses"          # a runtime need: the target must resolve for the referrer to work
    INHERITS = "inherits"  # source composition: the target's declaration is merged in (FR17)


# resources/schema/markers.py
from agentworks.resources.reference import RefRelationship


@dataclass(frozen=True, kw_only=True)
class RefMarker:
    kind: str                          # target resource kind: "secret", "vm-template", ...
    usage: str                         # prose, verbatim into ResourceReference.usage
    default_template: str | None = None
    relationship: RefRelationship = RefRelationship.USES

    def __post_init__(self) -> None: ...          # template vocabulary check, section 5.2
    def __get_pydantic_json_schema__(self, schema, handler): ...   # section 3.2


@dataclass(frozen=True, kw_only=True)
class ResourceRef(RefMarker):
    """The field names a resource of a fixed kind."""


@dataclass(frozen=True, kw_only=True)
class SecretRef(RefMarker):
    """The field names a secret, optionally with an owner-templated default name."""

    kind: str = "secret"
```

Spelled at a field:

```python
token: Annotated[str, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]
```

`kw_only=True` on all three is what lets `SecretRef` give `kind` a default without the
non-default-after-default ordering problem; the repo already uses `kw_only` frozen dataclasses
(`vms/sites.py:47`).

Settled details:

- **`RefRelationship` is defined in `resources/reference.py`, not in the schema package.** It is the
  reference vocabulary's word, both the marker and `ConfigReference` need it, and only this
  direction avoids the cycle section 1 describes. `markers.py` imports it; `reference.py` imports
  nothing of ours.
- **`usage` is required on both markers**, with no default. It is the prose that ends up on the
  target's `ReferenceEntry` and in `agw resource describe`'s "Referenced by:" section
  (`resources/reference.py:71-77`), so a marker without it degrades an operator-visible surface.
  Every producer today supplies one ("the auth token", "the Azure service-principal client secret",
  "the Proxmox API token"), so requiring it preserves shipped output.
- **`relationship` is carried, not consumed, at 2.1.** The plan's 2.3 requirement is explicit that
  FR17 must key on the RELATIONSHIP and not on the target kind (`plan.md:289-295`:
  `isinstance(ref, TemplateReference)` really means "points at a template" and would misclassify a
  future uses-a-template edge). The marker is the only layer that can express the distinction, so it
  expresses it here; which traversals filter on it is 2.3's and 2.5's call. Carrying one defaulted
  enum field is the cheapest way to keep that door open, and leaving it out would force 2.5's
  `inherits: list[str]` field to be indistinguishable from a uses edge at the moment of extraction,
  which is exactly the bug FR17 was raised against.
- **`default_template` lives on the base, not only on `SecretRef`.** It is one optional field, the
  walker treats it uniformly, and today's only owner-templated defaults happen to be secrets. The
  alternative (duplicating the template machinery if a non-secret kind ever wants it) costs more
  than the field does.

**Not doing this, and why.** No `ResourceRef(kind=...)` validation against `KIND_REGISTRY` at marker
construction: `resources/schema/` must not import the kind registry (it would invert the dependency
this package exists below). A registered-kind check belongs to the registration-time conformance
pass in 2.3, which already has the descriptor in hand.

### 3.2 The JSON Schema encoding

The marker implements `__get_pydantic_json_schema__`, which is pydantic's native hook for
`Annotated` metadata. That means the semantics survive into emitted schema automatically, from the
one authored marker, with no second thing to write and nothing to keep in sync:

```jsonc
"token": {
  "type": "string",
  "description": "the personal access token secret",
  "x-agw-ref": {
    "kind": "secret",
    "usage": "the auth token",
    "default_template": "git-token-{owner_name}",
    "relationship": "uses"
  }
}
```

Settled:

- **One extension key, `x-agw-ref`, holding an object.** Not a spray of `x-agw-kind` / `x-agw-usage`
  / `x-agw-default-template` top-level keys. One key keeps the vocabulary enumerable, keeps a
  marker's facts together, and gives a future version a single place to grow. The `x-agw-` PREFIX is
  reserved for agentworks vocabulary generally; `x-agw-ref` is the only member of it at 2.1, and a
  new member needs a consumer, not just an idea.
- **Keys inside the object are snake_case**, matching how agentworks spells every field everywhere
  else, rather than importing JSON Schema's camelCase convention into a namespace we own. One casing
  convention per project beats matching the neighborhood of an extension point.
- `relationship` is always emitted, including the default `"uses"`, so a consumer never has to know
  our default to read the schema.
- `x-` prefixed keys are ignored by conforming validators, so this cannot confuse
  yaml-language-server or any editor tooling (FR9); it only adds hover-visible facts.

## 4. `extract_references`

### 4.1 Signature

```python
# resources/schema/extract.py
@dataclass(frozen=True)
class RefOwner:
    """WHO declared the blob being extracted from: a (kind, name) address."""

    kind: str
    name: str

    @property
    def display(self) -> str:
        return f"{self.kind}/{self.name}"


def extract_references(
    model_cls: type[BaseModel],
    blob: object,
    owner: RefOwner,
) -> tuple[ConfigReference, ...]:
    """Every resource reference ``blob`` implies under ``model_cls``, read
    structurally from the model's reference-marked fields. Total: never
    raises, for any ``blob`` whatsoever."""
```

Three settled points about the signature:

- **`owner` is a typed `(kind, name)` record, not the display string capability code passes today.**
  Today `owner: str` is `"git-credential/prod"` and the git-credential layer splits the kind back
  off with `credential_name_from_owner` (`capabilities/git_credential/base.py:52-56`) to build
  `git-token-<name>`. A template needs the NAME, so handing the walker a pre-joined string and
  asking it to re-split is the string surgery FR18 exists to delete. `RefOwner.display` reproduces
  today's string exactly, so error framing is unchanged. This deviates from the HLA's `owner` (a
  bare string); see section 11.
- **`blob: object`, not `Mapping[str, object]`.** The secret-backend mapping surface is
  `str | dict[str, object] | Literal[False]` (`secrets/base.py:23`), so the extractor must accept a
  non-mapping blob and answer honestly (no refs) rather than reject it. `object` is also what makes
  the totality contract meaningful: there is no input the caller has to pre-screen.
- **`model_cls: type[BaseModel]`, not `type[AgwModel]`.** Root models are `RootModel`, and both
  bases derive from `BaseModel`; widening here avoids a union in the signature of the most-called
  function in the package.

Return type is `tuple[ConfigReference, ...]`, the EXISTING sourceless reference record
(`resources/reference.py:93-105`), extended with one field:

```python
@dataclass(frozen=True)
class ConfigReference:
    kind: str
    name: str
    usage: str
    relationship: RefRelationship = RefRelationship.USES   # added by 2.1
```

Reusing it rather than minting an `ExtractedRef` is deliberate: `ConfigReference` is already
sourceless by design (the owning resource attaches itself as source via `sourced_references`,
`reference.py:167-193`), which is exactly the extractor's contract, and minting a parallel type
would leave two shapes for one concept through 2.3 and 2.5. Its docstring is updated in the same
commit: it is now "implied by a modeled blob", not "implied by a capability's config block". Whether
the type should be RENAMED once it is no longer capability-specific is a residual question for the
lead (section 11).

### 4.2 The totality contract, and how it is enforced

**Contract: `extract_references` never raises, for any `(model_cls, blob, owner)`.** A blob it
cannot make sense of contributes no edges. This is the property the whole graph depends on: the
registry builds edges in pass 1 and validates in pass 7 (`resources/registry.py:329-460`), so a
config with both a malformed blob and a cycle must report the cycle (R9.3). It is also the contract
today's hand-rolled `dependencies` classmethods carry, in prose, one capability at a time
(`plugins/azure/platform.py:456-484`, `plugins/aws/platform.py:314-342`,
`plugins/proxmox/platform.py:64-86`, `capabilities/git_credential/base.py:64-86`).

Enforced by construction, not by a blanket guard:

- The walker performs only membership tests, `isinstance` checks, `Mapping.get`, and a template
  substitution over a pre-validated placeholder set (section 5.2). It parses nothing, imports
  nothing lazily, and invokes NO user code: it reads `model_fields` metadata and raw values, never a
  validator, never `model_validate`, never a capability method.
- **Recursion carries a PATH-SCOPED visited set: push the model class on descent, pop on return.**
  Not an accumulating set. An accumulating set terminates self-reference just as well but silently
  drops diamonds: a model with `primary: CredsModel` and `fallback: CredsModel` would walk the first
  and skip the second, so `fallback`'s secret would never become an edge. That is precisely the
  silent-missing-edge outcome this section argues is worse than a crash, and it would defeat FR18
  one indirection down. Path scoping cuts only a genuine cycle (a model reachable from itself),
  which is the only case that cannot terminate.
- Every value read from the blob is checked before use; an edge whose NAME cannot be derived (the
  field is present but is not a non-empty string) is omitted, exactly as today.
- **An incomplete model contributes nothing rather than raising.** `model_fields` raises on a model
  with an unresolved forward reference, and the walker recurses into nested classes it discovers
  from annotations, so it can reach a model it did not build. It therefore checks
  `__pydantic_complete__` first, attempts `model_rebuild(raise_errors=False)` once, and contributes
  nothing if the model is still incomplete. That branch should be unreachable in practice, and 2.3's
  registration conformance is what makes it so: a registered config model with an unresolved forward
  reference is refused at registration, where the author can see it, rather than degrading a graph
  edge at runtime. Recorded here as a requirement ON 2.3, not as a hope.

**Deliberately NOT a blanket `except Exception`.** A catch-all would convert a bug in the walker
into silently missing graph edges, which is a worse failure than a crash: the graph would build,
finalize would pass, and a secret would go unresolved with no message anywhere. Totality is a
property of the code plus the property test (section 9), not a suppression.

> **Implementation record (2026-08-06), reviewed and folded.** Four things about the walk that this
> section did not anticipate, all found by review or by building it:
>
> - **Metadata lives in three legal places**, not one: lifted onto the field, inside the `Annotated`
>   around a union, and inside a union arm. Reading only the first meant
>   `Annotated[A | B, Discriminator("x")] | None` silently lost its discriminator (no refs, no arms)
>   while pydantic validated it happily, which is a wrong graph from a working model. One shared
>   `spine_metadata` now reads all three, which also repaired `marker_of` under a multi-arm union
>   and constraint lookup under the inner-annotated spelling.
> - **An arm may answer to several tags** (`Literal["aws-ec2", "ec2"]`, exactly what a renamed
>   capability keeping its old name looks like). Only the first was read, so the old name was
>   unaddressable. Arms are enumerated per tag value.
> - **Non-string discriminators stay unsupported, deliberately.** Pydantic accepts them; widening
>   `tag` to `object` would push through to `FieldDoc.union_arms[].tag`, which presenters render
>   directly. Stated and fixture-pinned rather than accidental.
> - **`extract_references` could raise after all** (see 4.2's contract):
>   `model_rebuild(raise_errors= False)` suppresses only `PydanticUndefinedAnnotation`, so a forward
>   reference resolving to an unbuildable type escaped as `PydanticSchemaGenerationError`. Verified
>   by the lead: a parent holding such a child is incomplete at definition, so the walker's own
>   rebuild is exactly where it surfaces. Fixed by catching the two NAMED pydantic errors around
>   that single call, which is not the blanket guard 4.2 forbids: it handles a documented failure
>   mode of the one pydantic call the walker makes, rather than swallowing a walker bug. Confirmed
>   at HEAD: extraction returns `()` and `iter_field_docs` raises `StateError`.
>
> Also settled here: `RefOwner` lives in `markers.py`, not `extract.py` as section 8 says, because
> `base.py` needs it for the templated fill and importing `extract` from `base` would invert the
> layering. A private `_shape.py` holds the field classification both walkers share, which is what
> keeps them from drifting on a shape.

### 4.3 The walk

Per field of `model_cls.model_fields`, in declaration order:

- **Marked scalar** (`Annotated[str, SecretRef(...)]`): value present and a non-empty `str`, emit
  it; value absent or `None`, emit the template default when one is declared and nothing otherwise;
  value present in any other shape, omit (the edge's identity is destroyed).
- **Marked list** (`list[Annotated[str, ResourceRef(...)]]`): emit one ref per element that is a
  non-empty `str`, skip other elements, contribute nothing for a non-list value. No template
  default: a list has no single default identity.
- **Optional wrapper** (`X | None`): unwrapped, then handled as its inner shape.
- **Nested model field**: recurse with the raw sub-value when it is a mapping; contribute nothing
  otherwise.
- **Discriminated union field**: read the raw tag; when it names an arm, recurse into that arm's
  model; an absent or unknown tag contributes nothing.
- **Unmarked field**: nothing, at any depth.
- **Collection OF MODELS** (`list[Model]`, `tuple[Model, ...]`, `dict[str, Model]`): **GAP, added
  2026-08-06 after implementation surfaced it.** The enumeration above covers a marked list of
  SCALARS and a single nested model, but not a collection whose elements are models, so as first
  built such a field contributes no references and does not expand in `iter_field_docs`. The
  extraction half is LATENT (no shipped capability puts a marked field inside a model collection),
  but the field-docs half is LIVE and operator-visible: aws-ec2's `instance_types` and azure-vm's
  `vm_sizes` are exactly operator-overridable catalogs of models (`plugins/aws/platform.py:144`,
  `plugins/azure/platform.py:309`), so their entries' fields (`cpus`, `memory_gib`, `type`, `arch`)
  would render as an opaque blob, which FR10 forbids ("every field with its type, required/default,
  and description"). Both walkers must recurse per element, with the element index or mapping key
  carried in the `FieldDoc` path. The path-scoped guard makes this safe: a self-referential model
  nested through a list still terminates. Tracked as its own plan box under step 2.1 rather than
  deferred, because discovering it at 2.8 means reworking the renderer's input contract after the
  guide effort has begun consuming it.
- **A root model** (`model_cls` is an `AgwRootModel`, so its only `model_fields` entry is `root`):
  **contributes no references, by construction.** The signature was widened to `type[BaseModel]` so
  root models pass rather than trip an assertion, and this is the honest answer for what happens
  next: no shipped backend mapping implies an agentworks resource (env-var's mapping is an env var
  name and onepassword's is an external `op://` reference, both returning `()` today at
  `secrets/env_var.py:65-69` and `plugins/onepassword/backend.py:303-307`), so there is nothing to
  extract and nothing to invent. If a future backend's mapping does name a resource, the answer is
  to mark the field inside a root model whose root IS a nested `AgwModel`, which the nested-model
  rule above already walks; the bare-scalar root carries no references.

Two notes. The discriminated-union arm is selected from the RAW tag value, never from the capability
registry: the union type carries its own arms, so the walk stays a pure function of the model
(section 4.4). And absence-versus-malformed is the distinction that reproduces shipped behavior
byte-for-byte: azure emits its edge when `service_principal.secret` is absent (default) and omits it
when the field is present but not a non-empty string (`plugins/azure/platform.py:469-478`).

### 4.4 FR21 door (a): source-agnostic by signature

The function's inputs are the model, the blob, and the owner. It is **not** given, and must never be
given: the registry, the `Config`, the graph, a `SourceLocation`, a "declared versus effective"
flag, the capability registry, or anything naming a manifest file. A future persisted instance spec,
validated against the same model, extracts identically by construction, because the extractor cannot
tell the difference and has no parameter through which to learn it.

Two consequences worth stating so a later step does not erode them:

- **The two-stage extraction 2.3 needs is a CALLER concern, not a walker parameter.** The HLA has
  structural refs extracted per declared blob (feeding the graph the merge walks) and secret refs
  read off the effective blob (`hla.md:229-231`). Both are the same function called with a different
  blob. If someone proposes an `effective: bool` parameter, that is door (a) closing; the answer is
  two call sites.
- **The layer-stack merge (FR21 door b) is likewise not here.** Merging is 2.3's, over blobs, and
  produces a blob this function then reads. Nothing in the extractor knows a chain exists.

## 5. FR18: structural secret-name extraction (issue #311)

### 5.1 What changes

Today, four capabilities each hand-roll the same shape: read a config key, fall back to a
capability-specific default constant or a string-derived name, guard the malformed case, return a
`ConfigReference`. The four defaults are `git-token-<credential name>`
(`capabilities/git_credential/base.py:59-61`), `azure-client-secret`
(`plugins/azure/platform.py:227`), `aws-secret-access-key` (`plugins/aws/platform.py:74`), and
`proxmox-token` (`plugins/proxmox/platform.py:38`). Three are constants; one is owner-derived, and
it is the one that needed `credential_name_from_owner`.

Under this foundation the entire derivation is the marker:

```python
class GitHubConfig(AgwModel):                       # authored in 2.3
    token: Annotated[str, SecretRef(usage="the auth token",
                                    default_template="git-token-{owner_name}")]
```

> **Correction (implementation, 2026-08-06): there is NO `_TEMPLATED` sentinel, and the fill is a
> `mode="before"` validator, not `mode="after"` as section 5.2 first specified.** Two independent
> reasons, both verified by execution. The base is `frozen=True`, so assigning in an after-validator
> raises `ValidationError` (`frozen_instance`). And an after-validator can only fill a field that
> already validated, which would force every templated field to carry a placeholder default of the
> right type, making it report `required=False` with a junk `default` in `FieldDoc` and putting a
> fake default into every generated sample, contradicting section 6.1's separate `default_template`
> field. The before-validator fills the raw mapping for keys that are absent or explicitly `null`
> and raises `StateError` when the owner is missing from context, so a templated field stays a plain
> REQUIRED field with no sentinel. Everything this section promises holds; only the mechanism
> differs.

and the three constant cases are just `default_template="azure-client-secret"` (a template with no
placeholder), so ONE mechanism covers both. The capability's `dependencies` classmethod, its default
constant, and its guard code all delete in 2.3.

**Renaming or adding a secret field changes extraction with no other edit**, because extraction
reads `model_fields`. Rename `token` to `pat` and the extracted ref follows the field; add a second
`SecretRef` field and a second ref appears. Pinned by the test in section 9, which is the plan's 2.1
box for #311.

### 5.2 The owner template: vocabulary, validation, and where it is applied

**Placeholder vocabulary, closed:** `{owner_name}` and `{owner_kind}`. That is the whole set. A
template is checked when the MARKER is constructed (`RefMarker.__post_init__`), by parsing it with
`string.Formatter().parse` and raising `StateError` on anything it cannot render. `parse` yields
`(literal, field_name, format_spec, conversion)` per replacement, and **all three of the last fields
are checked, not just the name**:

- `field_name` outside `{owner_name, owner_kind}` is rejected, naming the offending placeholder.
- a non-empty `format_spec` is rejected: `"git-token-{owner_name:d}"` has a legal field name and
  raises `ValueError` at `str.format` time, which is the one thing section 4.2 promises cannot
  happen inside the extractor.
- a non-`None` `conversion` (`{owner_name!r}`) is rejected for the same reason, and because a
  quote-wrapped secret name is never what an author meant.
- an empty or positional `field_name` (`"{}"`, `"{0}"`) is rejected: it is `IndexError` at render
  time under the keyword-only substitution the extractor uses.

Two things follow: an author's mistake fails at import of the module that declares the model (loud,
immediate, before any registration), and rendering at extraction time cannot raise, which is what
lets section 4.2 promise totality without a guard. This is the invariant-in-code rule: the
vocabulary is enforced, not documented.

**Where the template is applied: at every point that needs the name, from one declaration.**

1. **In extraction** (`extract_references`), when the marked field is absent from the raw blob. The
   graph gets the same edge it gets today, without validation having run. This is the totality path.
2. **In validation**, so the typed instance carries the resolved name and FR15 holds. The model
   cannot know the owner by itself, so the owner rides pydantic's validation context: the base model
   carries a `model_validator(mode="after")` that fills any unset templated field from
   `info.context["owner"]`. `resources/schema/` exposes the one helper that builds it:

   ```python
   def validation_context(owner: RefOwner) -> dict[str, object]:
       """The context every model_validate call in the framework passes."""
   ```

   A model with a templated field validated WITHOUT an owner in context raises `StateError` (a
   framework bug: a call site forgot the context), never `ConfigError` (which would blame the
   operator for our mistake). Models with no templated field ignore the context entirely, so a
   context-free `model_validate` stays legal for them.

The consequence for 2.3, stated here so it is not rediscovered: after this, a git-credential
provider's `secret_name` is a plain field read off the validated model. The
`if self._secret_refs: ... else: default_token_secret(...)` fallback
(`capabilities/git_credential/base.py:151-158`) is deleted, not ported, because the model layer has
already resolved the name (FR15: "consumers error on unset rather than defaulting locally").

**Not doing this, and why.** No `{owner_display}` placeholder: resource names cannot contain `/`
(`capabilities/git_credential/base.py:53-55` states it outright, which is what makes its
owner-string split exact), so a template rendering `git-credential/prod` into a secret name could
only produce a name that never resolves.

## 6. `iter_field_docs`

### 6.1 Signature and record

```python
# resources/schema/fields.py
_UNSET: Final = object()   # "no default", distinct from a declared default of None


@dataclass(frozen=True, kw_only=True)
class FieldDoc:
    path: tuple[str, ...]          # ("service_principal", "secret"); leaf name is path[-1]
    annotation: object             # the resolved annotation, markers stripped
    required: bool
    default: object                # _UNSET when there is none
    default_template: str | None   # the owner-templated default, unrendered (no owner here)
    description: str | None
    choices: tuple[object, ...]    # Literal / Enum members, in declaration order; () when open
    constraints: Mapping[str, object]   # normalized: {"min_length": 1, "ge": 0, "pattern": "..."}
    ref: RefMarker | None          # the field's reference semantics, verbatim
    nested_model: type[BaseModel] | None    # set when this field opens a nested block
    union_arms: tuple[UnionArm, ...]        # empty unless the field is a discriminated union


@dataclass(frozen=True, kw_only=True)
class ModelDoc:
    """A model's own identity, for the heading above its fields."""

    model: type[BaseModel]
    title: str                     # the model's schema title
    description: str | None        # first paragraph of the class docstring


@dataclass(frozen=True, kw_only=True)
class UnionArm:
    tag: str                       # the discriminator value ("lima", "azure-vm", ...)
    doc: ModelDoc                  # the arm's own identity, so a list of arms reads as prose


def iter_field_docs(model_cls: type[BaseModel]) -> Iterator[FieldDoc]: ...


def model_doc(model_cls: type[BaseModel]) -> ModelDoc: ...


def render_type(annotation: object) -> str:
    """The operator-facing type rendering ("string", "list of string",
    "integer"). Separate so a presenter may use it or ignore it."""
```

`choices`, `constraints`, and `ModelDoc` are here because this record is a cross-SDD coordination
point (section 6.2), and widening it later means renegotiating with the onboarding child SDD rather
than editing a file. All three have concrete day-one consumers:

- **`choices`**: `Literal` and `Enum` fields are already all over the 2.3 inventory (aws `arch`,
  claude-code `permission_mode`, and every union discriminator is itself a `Literal`). Without this
  field, each of the three presenters calls `typing.get_args` on the annotation itself, three
  implementations of one thing, or `render_type` bakes the alternatives into a string the guide
  cannot re-lay-out. Both outcomes are the drift FR13 exists to prevent.
- **`constraints`**: normalized to plain keys and plain values (`min_length`, `max_length`, `ge`,
  `gt`, `le`, `lt`, `pattern`, `multiple_of`), so a presenter never has to know that pydantic stores
  them as `annotated_types.Ge` objects. The bridge's `string_too_short` normalization (section 7.4)
  implies models will carry these, and an operator reading a field reference that omits "at least 1
  character" is reading an incomplete reference.
- **`ModelDoc`**: describe and the guide both want a heading with the model's own prose, and an arm
  handle carrying only `(tag, model)` forces every presenter to dig the docstring out itself. Making
  `UnionArm` carry a `ModelDoc` rather than a bare class means "one arm rendered, alternatives
  listed" (FR10) can list the alternatives WITH their one-line descriptions, which is the actual
  operator need.

### 6.2 Settled behavior

- **Order is declaration order**, which is `model_fields` order, and determinism is part of the
  contract: rendered samples must be stable across runs or the FR13 tests are worthless.
- **Nested models expand inline, depth-first.** The nested field itself is yielded (with
  `nested_model` set), followed by its children at a longer `path`. A presenter renders indentation
  from `len(path)`; a flat presenter joins `path` with dots. This is why the stream carries a path
  rather than a bare name.
- **Union arms do NOT expand inline.** The field yields its arms as handles, and the presenter
  decides: FR10 wants one arm rendered with the alternatives listed, describe may want all of them,
  the guide may want a table. Recursion is `iter_field_docs(arm.doc.model)`. Keeping the choice out
  of the walker is the seam that lets four presentations differ without four walkers.
- **The record is presentation-free.** It carries the annotation, not a rendered string;
  `render_type` is a separate exported helper so a presenter may adopt or replace our rendering. No
  markdown, no ANSI, no CLI vocabulary anywhere in the record. This is a hard rule, because of the
  next point.
- **The guide is an EXTERNAL consumer.** The roadmap's onboarding child SDD composes `agw guide`
  topic pages from these same sources (`hla.md:301-311`, `plan.md:444-452`). So `FieldDoc` is a
  shared source, not a CLI-layer detail: it lives in `resources/schema/`, and any change to its
  shape is a cross-SDD coordination point, not a local refactor. That is also why `ref` carries the
  marker itself rather than a flattened copy of its fields: one vocabulary for both consumers.
- **Same PATH-SCOPED recursion guard as the extractor** (push on descent, pop on return), and it
  matters here for the same reason in a different costume: an accumulating visited set would render
  a nested block for `primary: CredsModel` and then emit NOTHING for `fallback: CredsModel`, so the
  generated sample would be missing a whole section of the config an operator has to write.
- **An incomplete model raises `StateError` here**, unlike the extractor, which contributes nothing
  (section 4.2). The asymmetry is deliberate: extraction is total by contract and its caller cannot
  handle an exception, while `iter_field_docs` is a developer-facing walker whose caller is a
  renderer, and a silently truncated field reference is worse than a loud failure at the moment
  someone tries to render a model that cannot be built.

### 6.3 Three presentations, plus emission as a sibling

The plan and the task frame this as "one walker, four presentations": sample renderer, describe,
JSON Schema emission, and the guide. **Emission is not a consumer of this stream**, and should not
be: HLA Component 6 specifies emission as `model_json_schema` over the models (`hla.md:270-271`),
which is pydantic's own generator, and re-deriving JSON Schema from `FieldDoc` would mean writing a
second schema generator to keep in sync with the first. The honest statement is **one authored
source (the model), two derivation mechanisms**: `iter_field_docs` for human presentations (sample,
describe, guide), `model_json_schema` plus the marker's schema hook (section 3.2) for machine
consumption. Drift between the two is prevented by the marker being the single authored carrier of
ref semantics, and pinned by the round-trip test in section 9 that asserts the same marker facts
appear in BOTH the stream and the emitted schema. See section 11 for the HLA wording this revises.

## 7. The error bridge (step 2.2), and the fold decision

### 7.1 The call: FOLD it into this LLD

The plan permits it if small (`plan.md:247`). It is small, and more importantly it is not separable
from this document. Reasons, in order of weight:

1. **The bridge's input is defined here.** Its whole job is translating the `ValidationError` that
   `AgwModel` raises. A separate LLD would open by restating this document's base model, marker
   vocabulary, and owner framing before it could say anything, and the two would then have to be
   read together anyway.
2. **A strict base model with no error rendering is an incomplete solution.** Landing `AgwModel`
   without the bridge would put a surface on main whose errors are raw pydantic text ("Input should
   be a valid string [type=string_type, input_value=8, input_type=int]"), which is a regression
   against every hand-rolled message it replaces. The two belong in one package and one step's
   definition of done.
3. **The severity plumbing the plan lists is not new machinery** (section 7.5), so what remains of
   2.2's design surface is one translation module: roughly a `loc`-to-path renderer, a small
   message-normalization table, and two entry points.

**What this implies for the plan, for the lead to apply** (this agent does not edit `plan.md`): step
2.2's first box ("`error-bridge-lld.md` (may fold into 2.1's LLD if small)") is satisfied by this
document and should be checked with a pointer to it. Step 2.2's SECOND box (the implementation plus
the representative-mistakes corpus test) stays exactly as written and is still its own step. The
fold is of the design document, not of the work.

### 7.2 The module, and the name collision

`resources/schema/errors.py`. Two entry points, one pure and one throwing:

```python
def render_validation_error(
    exc: PydanticValidationError,
    *,
    model_cls: type[BaseModel],
    owner: RefOwner,
) -> list[str]:
    """One operator-facing line per validation error, owner-framed and
    normalized. Pure: no raising, no I/O, reusable as diagnostic text."""


def config_error_from(
    exc: PydanticValidationError,
    *,
    model_cls: type[BaseModel],
    owner: RefOwner,
    location: SourceLocation | None = None,
    hint: str | None = None,
) -> ConfigError:
    """The same rendering, framed by ``location`` and aggregated into the
    ConfigError the caller raises (section 7.4)."""
```

**The collision, flagged because it will bite otherwise:** `agentworks.errors.ValidationError`
already exists (`errors.py:62`) and is a DIFFERENT thing (invalid user input at the command
surface). The bridge imports pydantic's as
`from pydantic import ValidationError as PydanticValidationError`, and it produces `ConfigError`,
which is what every config-shape error in the codebase already is. It never produces the agentworks
`ValidationError`; renaming either type is out of scope.

`model_cls` is a parameter, not a convenience: `extra_forbidden` errors are where we can beat
pydantic's default message by naming the valid fields, which is what today's messages do ("unknown
azure-vm platform field(s): ...", `plugins/azure/platform.py:492`), and that list comes off the
model.

### 7.3 `loc` to owner-framed path

A pydantic `loc` is a tuple of `str` (field names, extra keys) and `int` (list indices), with a
discriminated union inserting the selected arm's tag as a segment. Rendering rules:

- Join string segments with `.`; render an int segment as `[i]` appended to the previous segment.
- **Drop a segment that is a union arm tag.** For `spec.platform: {name: lima, vm_host: 8}` pydantic
  reports `('platform', 'lima', 'vm_host')`; the operator wrote no `lima` key, so the rendered path
  is `platform.vm_host`. The bridge detects this by walking the model alongside the loc, so it drops
  the tag only when it really is one.
- Prefix with the owner: the final line is `<owner.display>.<path>: <message>`, e.g.
  `vm-site/lab.platform.vm_host: must be a string`. That is FR12's "owner-scoped framing
  (`<owner>.<field>: ...`)" and it is a punctuation mark away from today's shape
  (`f"{owner}.{key} is required for the aws-ec2 platform and must be a non-empty string"`,
  `plugins/aws/platform.py:348`). We adopt the FRD's colon form uniformly rather than preserving
  each validator's private phrasing.
- A root-model error has an empty loc, so the line is `<owner.display>: <message>`.

### 7.4 Message normalization, and framing by SourceLocation

**Normalization is an explicit, small table keyed on pydantic's `type`**, and anything not in the
table falls through to pydantic's own message verbatim. Falling through is the honest default: a
fabricated paraphrase of an error type we have not considered is worse than a slightly clinical
correct one. Day-one entries, chosen to match shipped phrasing:

- `missing`: `is required`
- `extra_forbidden`: `unknown field; expected one of: a, b, c` (the list read off `model_cls`)
- `string_type` / `int_type` / `bool_type` / `float_type`: `must be a string` / `an integer` /
  `a boolean` / `a number`
- `list_type`: `must be a list`; `dict_type`, `model_type`, and `model_attributes_type`:
  `must be a table`
- `string_too_short`: `must not be empty`, but ONLY when `min_length == 1`
- `literal_error`: `must be one of: <pydantic's own rendered alternatives>`
- `union_tag_not_found`: `name is required`; `union_tag_invalid`:
  `unknown name 'x'; registered: ...`

> **Corrections from the 2.2 implementation (2026-08-06), each verified by execution against
> pydantic 2.13.4.** This table was written from memory of pydantic's error types and four entries
> were wrong in ways that would have shipped bad messages:
>
> - **`union_tag_missing` does not exist**; the real type is `union_tag_not_found`. As spelled the
>   entry would never have fired, and a missing capability name would have fallen through to "Unable
>   to extract tag using discriminator 'name'".
> - **`model_attributes_type` was missing.** A discriminated-union field given a bare scalar
>   (`platform: lima` instead of a table) reports it, not `model_type`. That is the likeliest single
>   operator mistake at 2.3, so it would have been the worst message the bridge emits.
> - **`string_too_short` is guarded to `min_length == 1`.** A flat "must not be empty" is a FALSE
>   paraphrase for a floor of 3, and the never-fabricate rule forbids that more strongly than the
>   table entry requires it. Above 1 it falls through to pydantic's exact wording.
> - **`literal_error`'s alternatives come pre-rendered** in `ctx["expected"]`
>   (`"'arm64' or 'x86_64'"`). Re-deriving them from the annotation would be a second enumeration to
>   keep in sync with pydantic's own.
> - Two smaller facts: `ctx["discriminator"]` arrives PRE-QUOTED, so it is unquoted before use or
>   every message reads `unknown 'name' 'lmia'`; and a root model's errors carry no `root` loc
>   segment, so the walk must start inside what the root wraps or the first segment eats the path.

The last entry is the capability-name case; it is listed here because the bridge owns the rendering,
but the registered-options text only becomes real in 2.3 when unions are assembled, and R9.2's hard
finalize error for an unregistered name is unchanged either way (`frd.md:153-157`).

**Aggregation, and why the bridge must own the location framing.** Pydantic reports every error in
one exception; today's validators raise on the first. Rendering all of them is a strict improvement
and matches phase 1's aggregated-error style, but it cannot be combined with leaving
`SourceLocation` framing at the call sites, because **both existing framings assume a single-line
message**:

- Decode frames as a PREFIX on the string it is handed: `f"{location.file}:{location.line}: {msg}"`
  (`manifests/envelope.py:52-53`). Given a 5-line body, lines 2 to 5 come out unlocated.
- The finalize validate pass stringifies the whole exception and appends the origin:
  `f"{exc} ({format_origin_location(origin)})"` (`resources/registry.py:513-521`). Given a 5-line
  body, the location is glued to the LAST line.

So an unqualified "the bridge aggregates, the call sites frame" would regress FR12's "file/position
context at least as good as today's" as a direct consequence of an improvement, which is not a trade
worth making. **Settled: the bridge frames the batch itself**, which is also what the HLA already
specifies ("the manifest document's `SourceLocation` (file, line) frames the whole batch",
`hla.md:270-274`). `config_error_from` takes the location:

```python
def config_error_from(exc, *, model_cls, owner, location: SourceLocation | None = None,
                      hint: str | None = None) -> ConfigError: ...
```

and renders:

- **One error, with a location:** `<file>:<line>: <owner>.<path>: <message>`. Byte-identical in
  shape to what decode produces today, so the common case changes nothing.
- **One error, no location:** `<owner>.<path>: <message>`.
- **Several errors:** a located header naming the count, then one indented line per error:

  ```text
  ~/.config/agentworks/resources/sites.yaml:12: vm-site/lab: 3 problems
    platform.vm_host: must be a string
    platform.cpus: is required
    platform.regions: unknown field; expected one of: cpus, memory, region, vm_host
  ```

  Every line is under one location header, which is exactly what neither call-site framing can do.

The cap is **10 rendered lines** with a trailing `... and N more`, and the header always states the
TRUE count, so a capped batch never hides how bad the document is.

**What this asks of 2.3 and 2.5, named so it is not missed.** Bridge-produced errors must not also
pass through the finalize pass's suffix wrapper (`resources/registry.py:513-521`), or they get
framed twice. That wrapper still has to exist during the transition, because hand-rolled
`ConfigError`s from unmigrated validators still arrive unframed. So 2.3 calls the bridge with the
location it already has, inside a branch that does not re-wrap, and **that branch is deleted when
the last hand-rolled validator dies in 2.5**, leaving one framing for everything. This is a bounded
fork with a stated deletion trigger, which is the price of not regressing the multi-error case; the
alternative (keeping two divergent framings forever) is the half-migrated state that costs more.

Field-level line numbers within a document remain explicitly out of scope, matching the HLA
(`hla.md:271-274`): the header carries document-level `file:line`, each line carries the full field
path.

### 7.5 Severity: there is nothing to plumb

The plan calls for "the severity plumbing for fold-gated validation (the bridge raises for
READY+ENABLED resources)". Read against HEAD, that gate already exists and is not the bridge's:
`Registry.finalize` pass 7 runs `_validate_resources`, which skips disabled rows and not-ready rows
and only then calls the throwing check (`resources/registry.py:466-521`). The bridge raises when it
is called; WHO calls it is the fold's decision, already implemented.

So the settled position is: **2.2 adds no severity mechanism.** What it adds is the pure
`render_validation_error` alongside the throwing `config_error_from`, which is the actual
requirement behind "the same rendering is reusable as diagnostic text elsewhere": doctor rows and
describe want the lines without the exception. Section 11 records this as a plan-wording finding
rather than a design change.

## 8. Package layout

```text
cli/agentworks/resources/schema/
    __init__.py      # the public surface: AgwModel, AgwRootModel, SecretRef, ResourceRef,
                     # RefOwner, extract_references, iter_field_docs, FieldDoc, render_type,
                     # validation_context, render_validation_error, config_error_from
    base.py          # AgwModel, AgwRootModel, the shared ConfigDict, the template validator
    markers.py       # RefRelationship, RefMarker, SecretRef, ResourceRef, the schema hook
    extract.py       # RefOwner, extract_references
    fields.py        # FieldDoc, UnionArm, iter_field_docs, render_type
    errors.py        # the bridge
```

Six small modules rather than one file, because four of them have distinct external consumers (2.3
imports `base` and `extract`; 2.7 imports `markers`; 2.8 and the guide import `fields`; 2.3 and 2.5
import `errors`) and the repo's file-size guidance is 500 lines. `__init__.py` is the import surface
so consumers write `from agentworks.resources.schema import AgwModel` and internal module boundaries
stay ours to move.

**Docs.** `resources/schema/` gets a module docstring set carrying the contract, and the SDD-doc
impermanence rule applies: this file is deleted when the SDD locks, so nothing permanent may cite
its path. What must be PROMOTED to permanent homes at step 2.9, recorded here so the sweep has a
list: the marker vocabulary and the `x-agw-ref` encoding (into `capabilities/README.md` and
`plugins/README.md`, beside the `config_model` registration contract), the `FieldDoc` record shape
and its guide-shared status (into a `resources/schema/README.md` or the package docstring, since the
onboarding child SDD will cite it), and the totality contract for `extract_references` (into its own
docstring, where it already belongs).

**One of those is not just a promotion candidate, it is a guardrail that must survive in code:**
section 4.4's rule that two-stage extraction is a CALLER concern and that an `effective: bool`
parameter would close FR21 door (a). It goes into `extract_references`'s own docstring at 2.9, in
those terms, because the next person to want declared-versus-effective behavior will be reading that
signature and not this file, and this file will be gone.

## 9. Test plan

`tests/resources/schema/`, unit-level throughout: 2.1 wires nothing, so there is no integration
surface to exercise yet.

**Base model.** Unknown key rejected on a model and on a nested model. For root models the assertion
is the positive one (section 2.1), not an unknown-key one, since a `RootModel[str]` has no keys:
`AgwRootModel[str]` rejects a table, and an `AgwRootModel` wrapping a nested `AgwModel` rejects an
unknown key INSIDE that mapping, which is what makes closed-world reach every key an operator can
write. Frozen: mutation raises. Strict: `"8"` is not accepted for `int`, `1` is not accepted for
`bool`, `None` is not accepted for a non-optional field, and `8` IS accepted for `float` (pinning
the resolved question in section 2.2 so nobody re-opens it). `validate_default`: a model whose
declared default violates its own field raises on `model_validate({})`, the omission case, NOT at
class definition. Attribute docstrings surface as descriptions, and an explicit
`Field(description=...)` wins over a docstring.

**Markers and the JSON Schema round trip.** For a fixture model with a scalar `SecretRef`, a
`ResourceRef`, a nested model containing a `SecretRef`, and a list of refs: `model_json_schema()`
carries `x-agw-ref` with all four keys at the right property path in every case, including inside a
nested `$defs` entry and inside each union arm. The same test asserts the same facts read off
`iter_field_docs`, which is the anti-drift pin between the two derivation mechanisms (section 6.3).
Plus: a marker whose `default_template` names an unknown placeholder raises `StateError` at
construction, naming the placeholder.

**`extract_references` totality.** Two layers, no new test dependency:

- An explicit adversarial corpus: `None`, `0`, `""`, `False`, a list where a table belongs, a table
  where a string belongs, deeply nested garbage, a mapping with non-string keys, a union tag that is
  a number, a union tag naming no arm, values of every wrong type at every marked field.
- A seeded generator producing several thousand random nested blobs (fixed seed, so failures
  reproduce) over the fixture models. The assertion is threefold: no call raises, every returned
  `name` is a non-empty `str`, and every returned `kind` matches a marker on the model.

  Hypothesis would express this more directly, but adding a test dependency is a decision outside
  this step's scope and a seeded generator is reproducible and sufficient. Recorded as a deliberate
  choice, not an oversight.

**The diamond regression (the path-scoped guard, section 4.2).** A fixture model with two sibling
fields of the SAME nested model type (`primary: CredsModel`, `fallback: CredsModel`), each with a
`SecretRef`, extracts BOTH secrets. This is the test that fails under an accumulating visited set,
and it is written as a named regression rather than folded into the walk tests, because the failure
it guards is silent. Its `iter_field_docs` twin asserts both nested blocks are yielded, in order,
with distinct paths.

**`extract_references` parity with today.** One test per shipped derivation, asserting the new
walker returns what the old classmethod returns for the same blob: github token (absent, overridden,
malformed), azure `service_principal.secret` (absent table, absent key, malformed key), aws
`credentials.access_key_secret`, proxmox `token_secret`. These are written against fixture models
mirroring the real config shapes, so they can land at 2.1 before the real models exist, and 2.3
re-points them at the real models.

**FR18 structural extraction (the plan's #311 box).** Over a fixture capability model: renaming the
`SecretRef` field changes the extracted reference and nothing else in the test changes; adding a
second `SecretRef` field yields a second reference; both with no edit outside the model class. This
is the test that would fail if anyone reintroduced string-scraping.

**`iter_field_docs`.** Declaration order preserved; nested paths correct and depth-first; union arms
yielded as handles and NOT expanded inline, each carrying its arm's `ModelDoc` title and
description; `required` / `default` / `_UNSET` reported correctly including a field whose declared
default is `None`; `choices` populated for a `Literal` field, an `Enum` field, and a discriminator,
and empty for an open `str`; `constraints` normalized to plain keys for `min_length`, `ge`, and
`pattern` (asserting no `annotated_types` object leaks into the record); `render_type` output for
scalar, optional, list, and union annotations; a self-referential model terminates; an incomplete
model raises `StateError`.

**The bridge (this is step 2.2's box, listed here because the design is here).** The FRD's
representative-mistakes corpus as a pinned test: unknown key (asserting the valid-field list
appears), wrong type, missing required field. Each asserts owner framing (`<owner>.<path>: ...`).
Framing is tested at BOTH cardinalities, since that is where the design nearly broke (section 7.4):
a single error with a location renders as one line whose prefix matches decode's shipped shape, and
a MULTI-error batch renders a located header plus indented lines with **every** line reachable from
one location (the assertion is that no error line is unlocated, not merely that nothing is doubled).
Plus: the header states the true count when the 10-line cap trims the body; the union-arm tag
segment is dropped from the rendered path; an unmapped pydantic error type falls through verbatim;
`render_validation_error` raises nothing and returns the same lines the exception carries. The
bad-capability-name entry lands in 2.3 (it needs assembled unions) and the old-sibling-shape entry
in 2.4, exactly as the plan sequences them (`plan.md:261-264`, `plan.md:371-373`).

**Gate.** `mypy .` strict green with the pydantic plugin enabled is itself a test of the base
model's typing behavior, and it is the one that catches a model shape that cannot be expressed under
strict typing.

## 10. Implementation sequence

Each is one commit with the full gate green after it. 2.1 is additive throughout, so there is no red
window and no ordering hazard.

1. **Dependency and tooling.** Pin pydantic (re-check latest stable first), enable the mypy plugin,
   promote `pydantic` to the root cspell dictionary. Gate green with no code change.
2. **`base.py` plus tests.** `AgwModel`, `AgwRootModel`, the shared `ConfigDict`.
3. **`markers.py` plus tests.** `RefRelationship` into `resources/reference.py` (section 1's
   direction rule), the markers, template-vocabulary validation including the format-spec and
   conversion rejections, the JSON Schema hook and its round-trip test.
4. **`extract.py` plus tests.** `RefOwner`, `extract_references`, the `ConfigReference.relationship`
   field, the totality / diamond / parity suites, the FR18 structural test. The templated-default
   validator on the base model lands here too (it and the extractor are the two readers of
   `default_template`, and splitting them would land half a mechanism).
5. **`fields.py` plus tests.** `iter_field_docs`, `FieldDoc`, `render_type`.
6. **`errors.py` plus tests.** The bridge and the representative-mistakes corpus (step 2.2's
   implementation box).

## 11. Contradictions and residual decisions for the lead

**Contradictions found against HEAD or the upstream artifacts.** The first two were folded into
`hla.md` by the lead on 2026-08-06 (commit `4421cee4`) and are kept here as the record of why.

1. **The HLA had emission consuming `iter_field_docs` while Component 6 derived it from
   `model_json_schema`.** Both cannot be the mechanism. Settled in section 6.3: emission is a
   SIBLING derivation from the same authored models, not a consumer of the stream, because deriving
   JSON Schema from `FieldDoc` means writing a second schema generator. The marker's schema hook
   plus the round-trip test are what keep the two honest. **Corrected upstream** (`hla.md:144-153`).
2. **The HLA said the secret template "derives the default from the owner at decode time".**
   Capability blobs are explicitly NOT validated at decode: decode passes them through raw and
   finalize owns their validation (`manifests/decode.py:18-23`, and the HLA's own Component 3 timing
   table). Settled: the template is applied at VALIDATION time, wherever that is for the surface
   (decode for kind-owned fields in 2.5, finalize for capability config in 2.3), plus in extraction,
   which runs before and independently of both. **Corrected upstream** (`hla.md:129-134`).
3. **The plan's "severity plumbing" for the bridge describes machinery that already exists.**
   `Registry.finalize` pass 7 already gates the throwing check on READY and ENABLED
   (`resources/registry.py:466-521`). Section 7.5 settles that 2.2 adds no severity mechanism; the
   real requirement behind that phrase (rendering reusable as diagnostic text) is met by the pure
   `render_validation_error` entry point. Worth correcting in the plan's 2.2 wording so nobody
   builds a mechanism to satisfy a sentence.
4. **`agentworks.errors.ValidationError` collides with pydantic's** (`errors.py:62`). Not a design
   problem, but it is a trap for every file that touches both; section 7.2 fixes the import
   spelling. No rename proposed.
5. **The plan and the FRD still spelled the template placeholder `{owner}`** after section 5.2
   closed the vocabulary to `{owner_name}` / `{owner_kind}`. The plan's line is the exact
   instruction a 2.3 implementer follows when authoring the github model, so it mattered more than
   the wording usually would. **Corrected upstream** (`frd.md:74`, `plan.md:318`, commit
   `5d4c06e4`).

**Gaps this LLD fills** (gaps rather than contradictions: the artifacts do not state a wrong thing,
they leave a thing unstated).

- **`owner`'s type.** The HLA and the plan write `extract_references(model_cls, blob, owner)` with
  no type on `owner`, and today's capability code passes a `"kind/name"` display string that the
  git-credential layer then splits apart (`capabilities/git_credential/base.py:52-56`). Section 4.1
  decides: `owner` is a typed `RefOwner(kind, name)` with a `display` property reproducing today's
  string exactly, because a template needs the NAME and re-splitting a string we joined ourselves is
  the string surgery FR18 exists to delete. Nothing upstream needs correcting.
- **The modeling consequence of onepassword's union mapping.** `plan.md:322` does record the shape
  ("mapping is itself a union: `op://` string or account/reference table"); what it leaves unstated
  is that a union of string-or-table cannot be a `BaseModel` at all, and neither can env-var's bare
  string. So the two `mapping_model` registrations at `plan.md:310` are root models or they are
  nothing. `AgwRootModel` lands here, in the foundation, rather than being improvised in 2.3, and
  section 2.1 also records that `Literal[False]` is NOT part of what those models express (the
  generic opt-out is filtered at `secrets/base.py:133-134` before any backend sees a mapping).

**Residual decisions for the lead.**

- **`AgwModel` as the base's name.** Alternatives considered: `SchemaModel` (says where it lives,
  not what it promises), `StrictModel` (names one of four properties), `DeclModel` (matches the
  `...Decl` suffix but reads wrong on capability config models, which are not decls). `AgwModel` is
  short, which matters because every model in the codebase will spell it, and `agw` is already the
  project's own short form. Cheap to overturn: it is a rename before any model exists.
- **Whether `ConfigReference` gets renamed in 2.3.** After this step it is "a reference implied by a
  modeled blob", not "by a capability's config block". Renaming touches every producer and consumer,
  and 2.3 is deleting all the producers anyway, so 2.3 is the cheap moment if we want it. This LLD
  updates the docstring and leaves the name.
- **The 10-line aggregation cap** (section 7.4) is a judgment call with no precedent in the codebase
  to copy. Easy to change; flagged so it is a decision rather than a default.
- **The strict-mode proxmox break** (section 2.2) is the one place this foundation changes what an
  operator's existing config does: `template_vmid: "9000"` stops loading, and `verify_ssl: "no"`
  stops silently meaning true. Taken as a break rather than a carve-out, consistent with the
  standing "if we need to break the schema, now is the time", but it is an operator-facing change
  and it needs the breaking-change marker plus an upgrade note when the proxmox model lands in 2.3.
  Flagged because it is the kind of thing that should be a decision, not a discovery in a release.
  Now tracked as its own 2.3 plan box (commit `5d4c06e4`), which also asks whether the migrator
  emits quoted scalars; that check belongs with the model, not here.
- **The framing fork in the finalize pass** (section 7.4): bridge-produced errors bypass
  `resources/registry.py:513-521`'s suffix wrapper while unmigrated hand-rolled validators still use
  it. The fork is what buys located multi-error output without waiting for 2.5, and it is deleted
  when the last hand-rolled validator goes. If the lead would rather have a single-line joined
  message and no fork, that is the tradeoff to overturn here, not in 2.3.
- **Requiring descriptions on registered models** (section 2.3). A registration-time conformance
  check could refuse a model with an undocumented field, which would make FR10's "complete generated
  skeleton" promise structural. It belongs to 2.3's conformance pass if we want it; not decided
  here, and not assumed.
- **`use_attribute_docstrings` requires source availability.** It is a constraint on every future
  model author (no `create_model` for authored models). Flagged rather than buried: if the lead
  prefers explicit `Field(description=...)` everywhere, the flag comes off and the cost is a
  duplicated description channel.
