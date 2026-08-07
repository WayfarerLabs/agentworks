# Step 2.5 LLD: kind spec models replace the decoders

> Status: written 2026-08-06, for plan step 2.5. Reviewed by: pending. IMPLEMENTED 2026-08-06;
> section 15 records where the design met contact and what it settled differently.
>
> Every pydantic claim below was verified by execution against the pinned 2.13.4 in this workspace,
> not read from documentation or memory. Where a verified result is load-bearing, the observed
> output is quoted. Every code claim cites `file:line` at the state described (HEAD unless marked,
> since step 2.4 is in flight in this tree and rewrote parts of `manifests/decode.py`).

## 1. What this step delivers, and what it does not

**Delivers.** One model per declarable resource kind, replacing the thirteen `_decode_*` functions;
`manifests/decode.py` reduced to an envelope-to-model adapter with no per-kind knowledge; the
declared-resource rows themselves as frozen models; the capability host surface carrying the tagged
table the operator actually writes, which retires the last two interim forks step 2.3 left behind;
unknown keys as hard errors on every kind spec (FR12); `metadata.expires` (FR20); and the migrator's
row normalization taught the new row shape.

**Does not.** This step changes what VALIDATES a declaration and what the row IS. It does not change
what the graph contains:

- The four inheriting kinds keep their hand-rolled `dependencies()` exactly as step 2.3b built them
  (effective-declaration edges, `declared_by` attribution, `INHERITS` typing). Section 7 states the
  rule that keeps them intact and why moving them to structural extraction is not this step's work.
- Capability config models are step 2.3's and are not touched. What changes is the SHAPE the core is
  handed (section 4), not who validates or when.
- No JSON Schema is emitted (2.7) and no sample or describe surface is rendered (2.8). This step's
  obligation to those steps is that the model it points them at is exactly the operator's spec
  surface, with no framework fields in it. Section 2 is about nothing else.
- Settings sections stay hand-loaded (2.10, FR14). Every shared leaf validator that settings still
  use survives; section 10 says which, against a plan box that says otherwise.
- The migrator's oracle (`migrate/toml_resources.py`) keeps its own hand-rolled loaders. It is a
  deliberately independent frozen oracle (descriptor LLD section 11), and the whole point of the
  decode-parity test is that the two sides are written twice. Section 8.3.
- No kind gains name validation it does not have today. Only `secret` and `vm-site` validate their
  own names at load (`decode.py` `_decode_secret`, `_decode_vm_site` at HEAD); that stays true.

## 2. The shape of a kind spec

### 2.1 One model class per kind, not two

The decl class becomes the model. `VMTemplate` extends a modeled `DeclaredResource` and declares its
own spec fields; there is no separate `VMTemplateSpec`.

The alternative (a spec model plus a decl the assembler builds from it) was considered and rejected:
it puts two authored field lists per kind in the tree, and the assembler that copies one into the
other is exactly the hand-rolled per-kind function this step exists to delete. It would also mean
every optional field a future author forgets to thread is silently dropped rather than caught, which
is the failure mode of today's decoders (`_decode_agent_template` at HEAD threads sixteen fields by
hand).

What made the single class viable is that the two objections to it both have pydantic-native
answers, both verified:

- **Emission must see the spec surface only.** `pydantic.json_schema.SkipJsonSchema` removes a field
  from `model_json_schema()` output. Verified: a model with `declared_at` and `origin` annotated
  `SkipJsonSchema[...]` emits `{"properties": {"name": ..., "cpus": ...}}` with neither field
  present, and `additionalProperties: false` intact.
- **The doc stream must see it too.** `SkipJsonSchema` survives into `FieldInfo.metadata` as a
  `SkipJsonSchema` instance (verified by inspecting `model_fields`), so `iter_field_docs` can skip
  exactly the same set with a one-line predicate rather than an exclusion list it would have to keep
  in sync. That is the one change this step makes to the 2.1 package's walkers, and section 9 lists
  it with the two bridge fixes.

So the emission surface is `model_json_schema(VMTemplate)` and the render surface is
`iter_field_docs(VMTemplate)`, and both are the spec and only the spec, with no filtering at the
call site and no post-processing of pydantic's output. Post-processing emitted schema was refused at
2.1 (schema-foundation LLD 6.3: deriving schema from the doc record would be a second generator) and
is refused here for the same reason.

### 2.2 `DeclaredResource` becomes the modeled metadata base

```python
class DeclaredResource(AgwModel):
    """Common metadata every declared resource carries."""

    name: str
    description: str | None = None
    expires: SkipJsonSchema[Expiry | None] = None
    declared_at: SkipJsonSchema[SourceLocation] = SYNTHESIZED
    origin: SkipJsonSchema[Origin | None] = None
```

Notes, each of which is a decision:

- **It stays in `agentworks/declared_resource.py`.** The module docstring's reason (a top-level
  module because `agentworks.resources.__init__` eagerly imports every kind module, closing a cycle)
  is unchanged, and `agentworks.schema` is a true leaf: its only agentworks imports are
  `agentworks.errors` and `agentworks.source_location` (verified by import audit over
  `agentworks/schema/*.py`; `RefRelationship` and `ConfigReference` live at
  `agentworks/schema/reference.py` precisely so the package imports nothing under `resources/`). So
  the base can extend `AgwModel` without introducing a cycle.
- **`declared_at: SourceLocation` and `origin: Origin | None` keep their stdlib-dataclass types.**
  Verified: under `strict=True` with `revalidate_instances="always"`, a pydantic model accepts an
  already-constructed frozen dataclass instance for such a field, and REJECTS a dict for it with
  `dataclass_exact_type`. That rejection is a small bonus: an operator who writes `declared_at:` in
  a spec gets an error rather than a framework field they can set. The good message for that case is
  still section 2.3's guard, because `dataclass_exact_type` explains nothing.
- **`expires` is FR20, modeled once here rather than per kind.** The plan phrases it as "on the
  shared envelope `metadata`", and this base IS that: `name` and `description` are already the
  metadata block's two fields, carried on the row, and the envelope injects them (`decode.py`
  `decode_document` at HEAD injects `doc.description` into the spec dict today). Adding `expires` to
  the same base is what makes "every kind inherits it uniformly" true by construction. A separate
  `Metadata` model was considered and rejected: it would declare `name` and `description` a second
  time, and whether `description` is required differs per kind (`secret` requires it,
  `secrets/base.py:55`), which a shared metadata model cannot express while the row can.
- **`_METADATA_KEYS` in `manifests/envelope.py:23` is derived from this base**, not hand-listed: the
  envelope's accepted metadata keys are the base's operator-writable fields (the ones NOT carrying
  `SkipJsonSchema`, plus `expires`). Stated as a set built from `model_fields` so a fourth metadata
  field cannot be accepted by one layer and rejected by the other. Today they are two hand-kept
  lists and they already agree only by luck.
- **`Expiry` is a lax datetime with a shape guard.** Verified against pyyaml's safe loader:
  `expires: 2026-01-01T00:00:00Z` yields a `datetime`, `expires: 2026-01-01` yields a `date`, and
  `expires: "2026-01-01"` yields a `str`. Under `strict=True` a `datetime` field accepts only the
  first (`datetime_type` for the other two), so this is one of the base model's sanctioned per-field
  carve-outs (`schema/base.py:24-27`): `Annotated[datetime, Field(strict=False)]`. Lax mode accepts
  all three (verified). It also accepts an INT as a unix timestamp: `expires: 12` validated to
  `1970-01-01T00:00:12+00:00`. That is nonsense as an expiry and is refused by a `BeforeValidator`
  that admits only `str | date | datetime`, so the carve-out is a widening of the accepted
  spellings, never of the accepted types.

### 2.3 The one guard decode keeps: metadata may not be written in `spec`

`extra="forbid"` closes the spec surface against unknown keys, but the metadata fields ARE fields of
the row, so `spec: {name: other}` would be accepted and would silently override the envelope. Today
there is exactly this guard for one field (`decode.py`, `decode_document`: "description belongs in
metadata.description, not in spec"). It generalizes to the whole base and is derived from it:

```python
_ROW_METADATA_FIELDS = frozenset(DeclaredResource.model_fields)

reserved = _ROW_METADATA_FIELDS & set(spec)
if reserved:
    raise ConfigError(
        f"{doc.where}: {', '.join(sorted(reserved))} belong(s) in metadata, not in spec"
    )
```

One derived guard, one message, and it cannot fall behind a new metadata field. It is the only
per-document check decode performs before handing the payload to the model.

### 2.4 The `ResourceKind` protocol carries the model, so decode enumerates nothing

`_DECODERS` (`decode.py:804` at HEAD) is a per-declarable-kind table in the manifest layer. Under
the descriptor step's rule (derive the switchboard, do not hand-maintain it), the model belongs on
the kind strategy, which is where every other per-kind fact already lives:

```python
class ResourceKind(Protocol):
    ...
    # Optional by CATEGORY, like ``instances``: every declarable kind
    # declares ``model``, no capability kind has one. Decode reads it,
    # and 2.7's emission iterates the same attribute.
```

Declared as an optional attribute in the protocol's prose, following the shipped `instances`
precedent (`resources/kind.py:146-167`, which documents why an optional hook is absent-on-class
rather than a Protocol member), and pinned by a test asserting every `declarable` kind has a `model`
and no `capability` kind does. `_DECODERS` is deleted. `KIND_SECTIONS` (`decode.py:52`) is the
migrator's table over all kinds and stays, exactly as the descriptor LLD ruled for step 2.0.

## 3. Decode after the swap

### 3.1 What `decode_document` becomes

```python
def decode_document(doc: Document, issues: list[str]) -> Any:
    model = KIND_REGISTRY[doc.kind].model
    payload = {**doc.spec, **_metadata_payload(doc)}   # after the section 2.3 guard
    owner = RefOwner(kind=doc.kind, name=doc.name)
    try:
        resource = model.model_validate(payload, context=validation_context(owner))
    except PydanticValidationError as exc:
        raise config_error_from(
            exc, model_cls=model, owner=owner, location=doc.location, hint=_sample_hint(doc.kind)
        ) from exc
    issues.extend(advisory_issues(resource, doc))
    return resource
```

That is the whole decoder. Everything a kind used to say about itself is on its model.

Three things this preserves deliberately:

- **`validation_context(owner)` is passed unconditionally**, though no kind spec field carries an
  owner template (section 7.2). A model with no templated field ignores the context
  (`schema/base.py:105-112`), and passing it always is what makes a future templated field on a kind
  spec work without anyone remembering this call site.
- **The bridge owns the framing.** `config_error_from` already prefixes `file:line` and renders a
  multi-problem batch with every line located (`schema/errors.py:125-167`). Today decode prefixes
  `doc.where` onto a single-line message (`decode_document`'s `except AgentworksError` branch),
  which leaves lines 2..N of any multi-line message unlocated. That branch goes.
- **A uniform hint.** Every kind-spec validation failure carries
  ``hint="see `agw resource sample <kind>`"``. That is FR16's pointer discipline applied at the one
  place an operator is already looking at a shape they got wrong, and it is what pays for the two
  hand-written steers this step drops (the vm-site "e.g. lima, wsl2, azure-vm, aws-ec2, proxmox"
  enumeration and the git-credential provider list): the sample surface lists them live, and this
  file's hand-kept enumeration is one of the drift sources FR13 exists to kill. The hint's naming
  follows whatever 2.8 settles for the sample surface; if that name is still open at implementation
  time, use `agw resource sample`, which is shipped.

### 3.2 What the envelope keeps

`manifests/envelope.py` is unchanged in shape: `apiVersion` / `kind` / `metadata` / `spec` checking,
the unknown-kind message with its kebab-case hint, the non-declarable-kind refusal, and the
`_NO_SELECTOR_KINDS` rule stay hand-rolled. Two edits only: `_METADATA_KEYS` derives from the row
base (2.2), and `metadata.expires` passes through to the payload.

The document envelope is NOT modeled here. It could be (`apiVersion: Literal["agentworks/v1"]` is a
tempting one line), but its errors are the best in the codebase already, it must be able to name the
kind BEFORE any kind model is in hand, and 2.7 is the step that actually needs a document-level
schema. Modeling it now would be speculative work whose only consumer is a step that has not
designed its emission surface yet. Recorded as 2.7's call, not deferred silently.

### 3.3 Advisory issues: two structural passes replace six hand-enumerated warnings

`ManifestSet.issues` (`manifests/loader.py:104`) is the load-time advisory channel, warned once per
request (`bootstrap.py:136`). Today decode fills it from four hand-enumerated helpers. Three of the
four classes retire outright and two survive, structurally derived:

**Retires: unknown-key warnings.** `_warn_unexpected_keys` on kind specs is FR12's warn-to-error
flip, done by `extra="forbid"`. See section 10 for what survives of that helper.

**Survives, derived from the markers: non-conforming secret names.**
`_warn_nonconforming_secret_name` (`config/loaders_core.py:79`) and
`_warn_nonconforming_derived_secret` (`:111`) are hand-wired at four call sites, and both docstrings
say outright that deriving the coverage structurally from the `ConfigReference(kind="secret")` edges
is what issue #311 wants. Step 2.1 closed #311 for extraction; this closes it for the warning:

```python
def advisory_issues(resource: DeclaredResource, doc: Document) -> list[str]:
    owner = RefOwner(kind=doc.kind, name=doc.name)
    refs = extract_references(type(resource), doc.spec, owner)
    refs += _hosted_capability_references(resource, doc, owner)
    return [
        _nonconforming(ref) for ref in refs
        if ref.kind == "secret" and not _conforms(ref.name)
    ] + _env_hygiene_issues(resource)
```

Both helpers are deleted from the decode side. The message changes: it frames by owner and usage
(`vm-template/base: secret name 'Bad_Name' for the Tailscale auth key does not follow ...`) rather
than by a hand-written location string, which is the framing every other operator-facing message in
this effort uses. The derived-name case (#308, a `git-credential` whose name makes
`git-token-<name>` non-conforming) needs no separate helper at all: `extract_references` renders the
marker's owner template for an absent field (`schema/extract.py:151-156`), so the derived name
appears in `refs` and is checked by the same line.

`_hosted_capability_references` is `capability_config_references(kind=..., config=..., owner=...)`
for a kind with a host surface, and it has one honest soft edge: a capability seated by a plugin has
not been imported yet when manifests load (`bootstrap.build_registry` seats plugins at
`bootstrap.py:110`, after `load_manifests` ran in `load_request_registry`), so its blob's secret
names are not warned about. That is a missed ADVISORY line, never a wrong answer, and the finalize
validate pass still checks the blob's shape. It is strictly more coverage than today, where the only
capability-blob case wired at all is a literal `token_secret` key name test (`_decode_vm_site` at
HEAD).

**Survives, derived from the field type: env hygiene.** `_parse_env_table` emits two advisory lines
(an `AGENTWORKS_*` key that the runtime prelude will override, and a value containing a newline that
SSH `SetEnv` cannot transport, `config/loaders_core.py:185-202`). Neither can come out of a model
validator, which has no channel but an exception. They move into `_env_hygiene_issues`, which finds
env tables by FIELD ANNOTATION (the shared `EnvTable` type, section 5.1), not by a hand-listed set
of env-bearing kinds. Same rule as the secret check above, stated once: **an advisory check is
derived from a declared type or a declared marker, never from an enumeration of kinds.** That is
what keeps the sixth env-bearing kind from being the one nobody remembers.

### 3.4 The legacy sibling shape survives the swap, and this is where

Step 2.4's box requires that its hard error not degrade into a generic unknown-key error once the
models land, and it is right to: under the models, `platform: lima` plus `platform_config: {...}`
would surface as a type error on `platform` plus an unknown key `platform_config`, which tells an
operator nothing about the rewrite.

2.4 lands `_fold_capability_table` (in flight in this tree), which raises the exact-rewrite error
and then splits the tagged table into the internal sibling pair. Section 4 deletes the SPLIT,
because the row carries the tagged table directly. The ERROR does not go with it. It becomes one
derived pre-validation guard in decode, over the same descriptor table the fold derives from today:

```python
def _reject_legacy_shape(surface: HostSurface, spec: Mapping[str, object], where: str) -> None:
    """The 0.14 sibling pair, refused by name with its rewrite. Deleted
    when the shape is far enough in the past to be a plain unknown key."""
```

Consequences, both deliberate:

- `HostSurface.config_field` SURVIVES, and its docstring changes. Today it reads "the internal
  sibling field holding the capability's config blob ... decode splits it into this pair"
  (`capabilities/descriptor.py:126` as edited by 2.4 in flight). After this step there is no
  internal pair and no split, and the field's only remaining job is to let the guard name the legacy
  field. A field whose docstring describes a mechanism that no longer exists is a name that lies to
  every future reader, so this LLD requires the rewrite, not just the survival.
- The guard is the deletion trigger's home: it says in its docstring that it and
  `HostSurface.config_field` go together, and when. This is the only compatibility surface this step
  leaves behind, and it is one function.

## 4. The capability host surface: the row carries the tagged table

### 4.1 Why this is 2.5's work and not a nice-to-have

Step 2.3 shipped `tagged_config` (`capabilities/config.py:212`) whose docstring names one deletion
trigger: "decode still hands a consuming resource a naming field and a sibling config blob, and step
2.5's kind spec models make it hand over the tagged table instead. Then the callers pass that table
and this function goes." The capability-contract LLD section 6 says the same. The function cannot be
deleted while `VMSiteDecl` holds `platform: str` plus `platform_config: dict`
(`vms/sites.py:58-59`), because the union arms carry a real `name` field and something has to put it
back.

So the row's shape changes, and the shape it changes to is the one the operator writes.

### 4.2 `CapabilityBlock`

```python
# agentworks/schema/block.py
class CapabilityBlock(BaseModel):
    """A tagged capability table: ``name`` selects the implementation and
    every other key is that implementation's own config, validated later
    against its declared model (never here)."""

    model_config = ConfigDict(extra="allow", **_SHARED_SETTINGS)

    name: NonEmptyStr

    @property
    def config(self) -> dict[str, object]:
        """The capability-owned keys: everything but the tag."""
```

Verified behavior, all four points load-bearing: extras survive validation and re-validation
(`revalidate_instances="always"` kept them), `model_dump()` returns the whole tagged mapping
(`{'name': 'lima', 'vm_host': 'h', 'nested': {'a': 1}}`), the emitted schema is
`{'properties': {'name': ...}, 'required': ['name'], 'additionalProperties': True}`, and an omitted
tag renders through the bridge as `vm-site/lab.platform.name: is required`.

- **`extra="allow"` is a deliberate, visible local exception to the base's closed world**, which is
  the shape the base model's docstring sanctions (`schema/base.py:24-27`). It is not a hole: the
  extras are a different owner's surface, and they are closed-world checked against that owner's
  model at finalize. Spelling the exception in one shared class, once, is what keeps it from being
  spelled per host kind.
- **It lives in the schema package** (`agentworks/schema/block.py`), because the decl classes need
  it at class-definition time and `agentworks.schema` is the only model-vocabulary package that is a
  proven import leaf. `agentworks/capabilities/` is not: `capabilities/__init__.py` imports
  `capabilities.base`, which a domain module cannot pull in at import time without inverting the
  layering the capabilities package docstring states.
- **It carries no capability-kind marker.** The association from `(declarable kind, spec field)` to
  capability kind already exists on the descriptor's `HostSurface` (`host_kind`, `naming_field`), so
  2.7's union splice reads it there. Adding a second carrier would be a second thing to keep in sync
  with the first.

### 4.3 What the rows look like, and the call-site churn that pays for it

| row                              | before                                           | after                                          |
| -------------------------------- | ------------------------------------------------ | ---------------------------------------------- |
| `VMSiteDecl` (`vms/sites.py:58`) | `platform: str`, `platform_config: dict`         | `platform: CapabilityBlock`                    |
| `GitCredentialConfig` (`:64`)    | `provider: str`, `provider_config: dict`         | `provider: CapabilityBlock`                    |
| `SessionTemplate` (`:63`)        | `harness_integration: str \| None` + config blob | `harness_integration: CapabilityBlock \| None` |

Reads become `.platform.name` and `.platform.config`. The churn is bounded and mechanical, and mypy
finds all of it. Counted, not estimated: ten `.platform` reads on the DECL (seven in `vms/sites.py`
at `:80`, `:98`, `:132`, `:135`, `:166`, `:271`, `:296`; `vms/manager/power.py:128`;
`doctor.py:373`, `:377`), five `.provider` reads, all inside `git_credentials/credential.py`, and
three `.harness_integration` reads on a declared row (`sessions/templates.py:212-215`). Most
`vm_node.site.platform` reads are NOT in this set: `VMSiteNode.platform` is the constructed platform
instance (`vms/nodes.py:73-78`), not the decl's field, and it is untouched.

Nothing about the merge or resolve layers changes: `ResolvedSessionTemplate` keeps its
`(name, config)` pair (`sessions/templates.py:55-56`), because the resolved layer is not a manifest
surface.

One semantic simplifies rather than moving: today `harness_integration_config` without
`harness_integration` is a hand-written error (`_decode_session_template` at HEAD, "a blob with no
owner"). Under one block the state is inexpressible, so the error and its test retire.

### 4.4 The core entry points take the tagged mapping

```python
def validate_capability_config(*, kind: str, config: Mapping[str, object], owner: RefOwner, ...)
def capability_config_references(*, kind: str, config: Mapping[str, object], owner: RefOwner, ...)
def capability_config_model(kind: str, name: str, facet: Facet | None = None)   # unchanged
```

`name` is read off the mapping for the seated-implementation lookup (tolerantly: a non-string tag
means no implementation, which is what the dangling capability edge already reports under R9.2). The
callers pass `decl.platform.model_dump()`.

`capability_config_references` passing the TAGGED mapping to `extract_references` is not a
regression against the capability-contract LLD's 7.3 rule ("the RAW blob is what is read, never the
tagged synthesis"). That rule existed because the synthesis was a fabrication; after this step the
tagged table IS the raw blob, and the arm model's `name` field carries no marker, so it contributes
nothing. The docstring updates to say so rather than being left describing the interim.

`validate_own_config` (`capabilities/config.py:109`) loses its synthesis branch the same way, and
`Capability.__init__` (`capabilities/base.py:347`) is handed the tagged mapping by its four
construction sites: `vms/sites.py:297`, `git_credentials/__init__.py:81`,
`vms/initializer/credentials.py:104`, and `sessions/nodes.py:362`.

## 5. The shared modeled vocabulary

Authored once in the domain that owns each concept, not per kind.

### 5.1 The env table, and `EnvEntry` becomes a model

The operator surface is two shapes per key (`config/loaders_core.py:161-172`):

```yaml
env:
  PLAIN: a value
  FROM_SECRET: { secret: my-secret }
```

`EnvEntry` becomes an `AgwModel` with a `mode="before"` validator turning the bare string into
`{"value": ...}` (verified: accepted under `strict=True`, since before-validators run ahead of type
checking), and `EnvTable = dict[EnvVarName, EnvEntry]` is the shared field type.

Three settlements:

- **`EnvEntry.key` is deleted.** It duplicates the map key, and nothing enforces that the two agree:
  `{"A": EnvEntry(key="B", ...)}` is constructible today. An invariant that is neither enforced nor
  needed should be deleted rather than documented, and here deleting the field IS the enforcement.
  Its three uses are all inside `env/entry.py` (the two `__post_init__` messages and the
  `usage=f"the {self.key} env var"` string at `:71`); `env_references` (`:78`) already iterates
  `env.items()` and passes the key down. The churn is real (fourteen test modules construct
  `EnvEntry(...)`) but it is mechanical and LOUD: under `extra="forbid"` a leftover `key=` kwarg is
  an immediate validation error, never a silent acceptance.
- **`EnvVarName` is `Annotated[str, AfterValidator(...)]`, not `Field(pattern=...)`.** Verified: the
  pattern form renders as `String should match pattern '^[A-Za-z_][A-Za-z0-9_]*$'`, while the
  validator form reproduces today's message verbatim, because the bridge reads a validator's own
  exception out of the error context (`schema/errors.py:317-325`). Verified output:
  `vm-template/base.env.1BAD.[key]: invalid env var name '1BAD' (must match /^[A-Za-z_][A-Za-z0-9_]*$/)`.
  The trailing `[key]` segment is a bridge gap, fixed in section 9.
- **The emitted schema needs one hand-written fragment, and only one.** `EnvEntry`'s
  before-validator is invisible to `model_json_schema`, which emits the object form alone
  (verified). So `EnvEntry` carries a `__get_pydantic_json_schema__` returning
  `anyOf: [string, <the object>]`. This is the single place in this step where a schema fact is
  written by hand rather than derived, it sits three lines from the validator that implements it,
  and it is pinned by a test that validates both shapes against the emitted schema. Called out
  rather than buried, because FR13 says schema facts live in exactly one authored place and this is
  the exception that proves it: here the authored place is the type, and both the validator and the
  schema hook are its two faces.

### 5.2 Name and length caps

`validate_name` (`config/validation.py:83`) stays the single naming rule, wrapped for the model
layer by one shared annotation factory:

```python
def ResourceName(max_length: int) -> object:
    """``name`` with the caller's per-kind cap. The cap is never defaulted
    here: each kind's ceiling is derived at the module that owns its sink."""
```

The wrapper converts `agentworks.errors.ValidationError` into `ValueError`, because it is NOT a
`ValueError` subclass (`errors.py:62`: it extends `AgentworksError`) and an exception that is
neither is not caught by pydantic, so it would escape `model_validate` and bypass the bridge
entirely, losing the batch framing for that one error class. Verified error taxonomy, not assumed.

Two kinds override the inherited `name` with it, matching today exactly: `secret` with
`MAX_SECRET_NAME_LENGTH`, `vm-site` with `MAX_FREEFORM_NAME_LENGTH`. Verified that a subclass may
re-declare an inherited field, including making an optional one required.

### 5.3 Choice sets: the `Literal` becomes the source

`named-console-template` validates `tmux_layout` against `VALID_TMUX_LAYOUTS`
(`sessions/layouts.py:23`), a runtime tuple, which a `Literal` cannot be built from. Rather than
restate the values (drift) or fall back to a validator (which leaves `FieldDoc.choices` empty and so
leaves `describe` unable to list the layouts, FR10), the direction inverts:

```python
TmuxLayout = Literal["even-horizontal", ..., AW_SESSION_VERTICAL_LAYOUT_VALUE]
VALID_TMUX_LAYOUTS = get_args(TmuxLayout)
```

One authored list, the tuple derived from it, `choices` populated for free, and the bridge's
`literal_error` branch already renders "must be one of: ..." (`schema/errors.py:306-311`).

### 5.4 The secret backend-mapping value

`backend_mappings: dict[str, str | dict[str, object] | Literal[False]]`. Verified: a string, a table
and `false` all validate under `strict=True`; `true` and a number are refused.

But the refusal renders badly today, and that is a bridge gap this step is the first to hit (section
9): pydantic reports one error per union member, so `true` produces three problems and the bridge
renders them as three lines whose paths carry pydantic's member labels
(`secret/npm.backend_mappings.b.str: must be a string`,
`secret/npm.backend_mappings.b.dict[str,any]: must be a table`, ...). Today it is one line: "must be
a string, inline table, or false". The bridge fix restores one line; the model does NOT gain a guard
validator to paper over it, because the same defect would then be waiting for the next union.

The `true` case keeps its own specific message ("boolean must be `false` (opt-out); `true` is not a
valid value") through a `BeforeValidator` on the value type, since that message teaches something
the alternatives list does not.

### 5.5 String lists

`_require_string_list` (`config/loaders_core.py:44`) becomes `list[str]`, and there is nothing else
to say: strict mode rejects a bare string for a list field, which is the mistake that helper existed
to catch. The known trap applies and is honored throughout: `tuple[str, ...]` is NOT satisfied by a
YAML list under `strict=True` (verified: `tuple_type`), so every operator-writable sequence in every
kind spec is a `list`.

## 6. The thirteen kinds

Order is the plan's (smallest first). Each entry names only what is not mechanical.

1. **apt-package** (`apt/apt.py:70`). `apt: list[str]` required, `apt_sources: list[str] = []`.
2. **apt-source** (`:50`). Four required strings plus `key_dearmor: bool = False`; `source_file`
   keeps its simple-filename rule (`_SAFE_FILENAME_RE`, `:109`) as a constrained annotation.
3. **system-install-command** / **user-install-command** (`install_commands.py:48`, `:59`). The two
   classes are field-identical today and become two thin subclasses of one shared spec base, which
   is a duplication this step is well placed to remove. Two carried behaviors: the at-most-one-of
   `test_exec` / `test_file` / `test_dir` rule becomes a `mode="after"` model validator, and the
   `test` steer ("'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'.", `:80`)
   becomes a `mode="before"` validator, because as a plain unknown key it would lose the remedy. Two
   strict-mode breaks, both deliberate: `str(data[key]).strip()` no longer coerces a non-string, and
   an empty string no longer silently becomes `None`.
4. **workspace-template** (`workspaces/template.py`). `env` plus five optional scalars.
5. **named-console-template** (`sessions/template.py:30`). `tmux_layout: TmuxLayout` (5.3).
6. **admin-template** (`vms/admin.py:26`). Concrete defaults throughout (`username="agentworks"`,
   `shell="bash"`, `dotfiles_destination="~/.dotfiles"`, ...), which move verbatim onto the model.
   This kind is already FR15-shaped and is the cheapest place to prove the pattern before 2.6.
7. **agent-template** (`agents/template.py:28`). All-optional `None = inherit`. Cross-field:
   `validate_mise_settings(packages, lockfile, install_before, ...)` becomes a `mode="after"`
   validator, called with `self.mise_install_before or "7d"` to reproduce exactly what the decoder
   does today (it validates with the `"7d"` default but STORES `None`, `_decode_agent_template` at
   HEAD, and that asymmetry is load-bearing for inheritance). **Behavior change to name in the
   operator note:** `username` and `git_force_safe_directory` are in this kind's accepted key set
   today (`_AGENT_TEMPLATE_KEYS` derives from `_USER_CONFIG_KEYS`) but are NOT fields of
   `AgentTemplate`, so an operator who writes either gets no warning and no effect. They become hard
   unknown-key errors. This is FR12 working as designed and it is also the clearest single argument
   for the flip.
8. **vm-template** (`vms/template.py:53`). `tailscale_auth_key` carries `SecretRef` WITHOUT a
   `default_template`; section 7.2 is why. Strict-mode break: `cpus`/`memory`/`disk`/`swap` are
   `int(spec[...])` today, so `cpus: "4"` loads and `cpus: true` loads as `1`; both become errors.
   Same family as the proxmox break the plan already records at 2.3, same operator note.
9. **secret** (`secrets/base.py:30`). `description` required (an override of the base's optional
   field), `hint`, `backend_mappings` (5.4). Name capped at `MAX_SECRET_NAME_LENGTH`.
10. **git-credential** (`git_credentials/credential.py:59`). `provider: CapabilityBlock`. The
    `token` steer ("'token' is provider config now: move it into the spec.provider table") is kept
    as a `mode="before"` validator; the `type` steer is dropped in favour of the unknown-key
    message, which for this kind reads "unknown field; expected one of: provider" and says the same
    thing.
11. **vm-site** (`vms/sites.py:49`). `platform: CapabilityBlock`; name capped at
    `MAX_FREEFORM_NAME_LENGTH`; the platform-shadow rule (a site named after a known platform must
    declare it) becomes a `mode="after"` validator, which is legitimate because it is a cross-field
    rule over `name` and `platform.name` and because it reads the registry at exactly the moment
    decode does today.
12. **session-template** (`sessions/template.py:45`). `harness_integration: CapabilityBlock | None`
    (4.3). This kind is already strict at its boundary (`_raise_unexpected_keys`), so it is the one
    kind whose unknown-key behavior does not change.

Two shapes recur and are handled once, not twice: the `env` table (5.1) and `inherits: list[str]`.
`inherits` elements carry
`ResourceRef(kind=<the kind itself>, usage="a parent template", relationship=INHERITS)` for
documentation and emission; section 7.1 is why they do not yet produce the edge.

`description` on the four apt / install-command rows: today it is `str = field()` (required on the
class) but the loaders default it to `""` (`apt.py:126`), so a manifest omitting
`metadata.description` gets an empty string. Those four rows drop the override and inherit the
base's `str | None = None`, unifying "no description" on one value. The check that this is invisible
is a test-plan item (7.3 of section 12), because `_polish_auto_declared_description`
(`resources/registry.py:832`) tests it for truthiness and both values are falsy.

## 7. Reference extraction and defaulting across the swap

### 7.1 `dependencies()` does not move, and that is the point

Every inheriting kind's `dependencies()` reads its EFFECTIVE declaration and attributes each edge to
the layer that declared it (`vms/template.py:88-135`, and the three siblings). That is step 2.3b's
work, closed 2026-08-06 at 4441 tests, and it is not re-derivable from `extract_references` today:
the extractor is a pure function of `(model, blob, owner)` and takes no chain, by design (FR21 door
(a), `schema/extract.py:56-64`), while the merged declaration is a typed row, not a blob.

So this step's obligation is a NON-regression, stated plainly: **the models must not change what
`dependencies()` returns for any input.** Concretely that means three things, each with a test:

- the merged-declaration resolvers keep receiving rows whose fields hold exactly what they hold
  today (which is why `None = inherit` optionality is preserved field for field in section 6);
- markers added to kind spec fields are inert with respect to the graph in this step, and the LLD
  says so rather than leaving a reader to assume the edges moved;
- the `INHERITS` typing of the `inherits` edge, and the `USES` typing of everything else, is
  unchanged.

Making kind-spec extraction structural is a genuinely good follow-up (it would delete four near
identical `dependencies` bodies) but it is a GRAPH change, it needs the merged declaration expressed
as a blob or the extractor taught about typed rows, and doing it inside the decoder swap would mean
one change whose two halves fail independently. Deferred loudly: section 14 carries it as a named
residual with the reason, not as a TODO.

### 7.2 Owner-templated defaults must not reach an inheriting kind's declared row

This is the trap this step most needs to state, because the mechanism is automatic and the damage is
silent.

`AgwModel` fills any field whose marker declares a `default_template` and whose value is absent or
`None` (`schema/base.py:88-132`). On a capability config model that is exactly right. On an
INHERITING kind's declared row it is wrong, and `vm-template.tailscale_auth_key` is the live case:
the declared field is `str | None` with `None` meaning "inherit" (`vms/template.py:80-86`), and the
default `"tailscale-auth-key"` belongs to the RESOLVED layer (`vms/templates.py:44`). A
`default_template` on the declared field would fill every template's row with the literal default,
so a child that inherits its parent's override would silently stop doing so, and every template in
the config would declare an edge to the default secret.

**Settled: a kind spec's reference markers carry `kind`, `usage` and `relationship`, never
`default_template`.** Owner templating stays a capability-config mechanism, where the validated blob
IS the effective blob. The rule is enforced, not just written down: a construction-time check on the
declarable kinds' models refuses a `default_template` on any field of a row that inherits, so the
next author gets a failure at import of their module rather than a config that quietly stops
inheriting. Pinned by a negative test.

FR15's resolved-layer defaulting is unaffected: 2.6 enumerates consumer-side fallbacks for MODELED
fields, and the resolved template layer is not modeled by this step.

### 7.3 What FR17 depends on, verified unchanged

The FRD's correction (frd.md:119-130) names the four non-capability edges that actually cross an
inheritance edge: `vm-template`'s `env` and `tailscale_auth_key`, `agent-template`'s
`git_credentials` and `user_install_commands`, `workspace-template`'s `env`. All four are produced
by the hand-rolled `dependencies()` bodies this step does not touch, from fields this step preserves
field for field. The regression test step 2.3b left behind (a child overriding a parent's default
secret name) is re-run against the model-backed rows as a per-kind box, so the swap cannot regress
it silently.

## 8. The rows are models: what that breaks

Three sites reflect over the row as a dataclass. All three are found by one grep and all three are
real.

### 8.1 The shared replace helper

`dataclasses.replace` fails on a pydantic model (`dataclasses.is_dataclass` is False), and the
capability marker rows (`VMPlatformEntry` and friends) stay frozen dataclasses and still flow
through the same code, so the branch cannot simply flip. One shared helper, homed beside
`DeclaredResource` because that module is the leaf both callers can already reach:

```python
def replace_fields(row: Any, **updates: Any) -> Any:
    """``row`` with ``updates`` applied, for a frozen dataclass or a frozen
    model. Framework-supplied values only: the model path does not
    re-validate, exactly as ``dataclasses.replace`` does not."""
```

Verified: `model_copy(update=...)` works on a frozen model and does not re-validate.

Call sites: `Registry.add`'s origin stamping (`resources/registry.py:189`) and
`_polish_auto_declared_description` (`:852`).

### 8.2 `strip_source_fields`, the plan's explicit checkbox

`migrate/verify.py:59` returns `resource` unchanged when `dataclasses.is_dataclass` is False. Under
models it would no-op on every declarable row while continuing to work on the capability rows, so
the migrator's registry-equivalence check would start comparing `origin` and `declared_at` and would
fail on every migration, and `test_decode_parity.py` would fail with it. Loud rather than silent,
which is the one mercy here, but it is squarely the plan's checkbox and it is fixed the same way:
the dataclass branch becomes a `replace_fields` call with a model branch beside it, and the field
probing (`hasattr(resource, field)`) is unchanged because `hasattr` works on both.

Verification that the fix is right, not just green: `normalized_rows` is compared with `==`
(`verify.py:53`), and a pydantic model's `__eq__` compares class and field values, so the oracle's
row and the manifest's row still compare exactly as two dataclasses did.

### 8.3 The oracle stays hand-rolled, and the parity test stays a real test

`migrate/toml_resources.py` constructs the SAME row classes from flat TOML with its own hand-rolled
loaders, and `test_decode_parity.py` compares the two sides. The rows becoming models does not make
that a tautology: the oracle's assembly is still written separately, so the test still checks the
emission mapping. What DOES change is that the oracle's constructions are now validated, so its
`str()` / `int()` coercions must produce the model's types (they do; TOML yields real scalars) and
its `_parse_env_table` must stop passing `key=` to `EnvEntry` (section 5.1).

The apt / install-command loaders are the one place decode and the oracle share code today
(`decode.py:781-801` delegates to `apt._load_apt_sources` and its three siblings, and
`migrate/toml_resources.py:701` imports the same four). That sharing ends on the decode side: the
models replace the delegation, the loaders survive for the oracle alone. Their four kinds' parity
entries stop being tautological as a side effect, which is a small improvement nobody has to pay
for.

## 9. What this step owes the 2.1 / 2.2 package

Three changes, all general, all in a package earlier steps froze. Each is here because 2.5 is the
first surface to hit it, and each is pinned by a corpus entry rather than by a unit test alone.

**(a) `iter_field_docs` skips `SkipJsonSchema` fields.** One predicate over `FieldInfo.metadata`
(verified present). Without it, every rendered sample and every describe view would list `origin`
and `declared_at` as fields an operator should fill in.

**(b) The bridge drops a trailing `[key]` segment.** Verified: a constrained dict KEY reports
`loc = ('env', '1BAD', '[key]')`, which the bridge renders as `env.1BAD.[key]`. The `[key]` marker
is pydantic's way of saying the failure is in the key rather than the value; the key itself is
already the preceding segment, so the marker is noise the operator never wrote. This is the same
class of fix as the union-arm tag drop the bridge already performs (`schema/errors.py:376-387`).

**(c) The bridge collapses an undiscriminated union's alternatives into one line.** Verified against
the secret mapping model: `backend_mappings: {b: true}` produces THREE problems (`string_type`,
`dict_type`, `literal_error`) whose rendered paths carry pydantic's internal member labels
(`...b.str`, `...b.dict[str,any]`). Today the same input produces one line naming the three
alternatives, so leaving this would be an error-quality regression, which FR12 forbids in terms.

The rule, stated so it stays narrow: problems that share a loc prefix and end exactly at the union
position are ONE problem ("the value is none of the alternatives"), rendered once from the union's
members. Problems that go DEEPER than the union position (a failure inside a model arm, e.g.
onepassword's `account.reference: is required`) stay as they are, because those are informative and
collapsing them would lose the only useful thing in the batch.

**Amended 2026-08-07 (review finding 22).** The rule as written is incomplete, and shipping it alone
left a real defect: when a deeper problem exists, the SHALLOW alternatives line has to be DROPPED,
not kept beside it. Keeping both is what made every malformed onepassword table lead with
`Input should be a valid string`, a complaint about whichever arm sorted first, printed directly
above the real diagnosis. The paragraph above even cites `account.reference: is required` without
noticing the noise beside it. What shipped: an arm that got past the SHAPE check and then failed on
CONTENT silences the other arms' shape rejections, because those arms were never the one the
operator meant. Where no arm got past the shape check, the collapse to "must be a string or a table"
is exactly right and is preserved. This affects the shipped `validate_capability_config` path, not
only a plugin's own revalidation. The `_AtUnion` cursor already identifies the union position, so
this is a grouping over `_problems`, not a new walk.

Extension (c) also fixes the member-label paths for free, which is why the models do not get a guard
validator to route around the problem (section 5.4).

## 10. The full deletion list

Every item below is deleted BY this step, not merely made unreachable. Grouped by why.

**The phase-1 interim decode fork (the per-kind decoders).** Scoped to 2.5 by the descriptor LLD
section 6 ("2.0 absorbs (A); 2.5 owns (B) in full") and confirmed against the code:

- the thirteen `_decode_*` functions and the `_DECODERS` table (`decode.py:804` at HEAD);
- the six per-kind key-set constants (`_SECRET_KEYS`, `_VM_TEMPLATE_KEYS`, `_USER_CONFIG_KEYS`,
  `_AGENT_TEMPLATE_KEYS`, `_WORKSPACE_TEMPLATE_KEYS`, `_SESSION_TEMPLATE_KEYS`);
- `_doc_decls` (`decode.py:219` at HEAD) and the apt / install-command delegation it exists for;
- decode's `except AgentworksError` re-framing branch, replaced by the bridge's own framing (3.1);
- the module docstring's fork narrative, replaced by the adapter's.

**The step 2.3 interim forks**, both of which name 2.5 as their trigger in their own docstrings:

- `tagged_config` (`capabilities/config.py:212`) and its collision error, inexpressible once the
  operator writes one table (4.1);
- `validate_own_config`'s synthesis branch (`:127-131`);
- `FramedConfigError` (`schema/errors.py:84`) and the finalize pass's `except FramedConfigError`
  re-raise plus the `except ConfigError` origin-suffix wrapper around it
  (`resources/registry.py:526-542`). Their stated trigger is "when the last hand-rolled validator
  does (step 2.5)". Verified that this step is that moment: after it, every resource `validate()`
  goes through `validate_capability_config`, and every decode error goes through
  `config_error_from`, so nothing raises an unframed `ConfigError` from a resource any more and the
  two framings become one. `format_origin_location`'s other callers (`resources/render.py`) are
  unaffected.

**The hand-enumerated advisory helpers** (3.3), on the decode side:

- `_warn_nonconforming_secret_name` and `_warn_nonconforming_derived_secret`
  (`config/loaders_core.py:79`, `:111`) in full, including the migrator-oracle callers, which move
  to the same structural check. Both docstrings' "tracked in issue #311" note is discharged.

**The capability-fold plumbing** (4.2, 3.4):

- `_fold_capability_table` (2.4's, in flight) minus its legacy-shape error, which becomes
  `_reject_legacy_shape`;
- the sibling-pair split itself, and with it `platform_config` / `provider_config` /
  `harness_integration_config` as row fields.

**What does NOT retire, against a plan box that says it does.** Plan step 2.5 says "the
`_warn_unexpected_keys` machinery retires with the last kind". It does not: three settings loaders
still call it (`config/loaders_core.py:244` for `[operator]`, `config/loaders_secrets.py:87` for
`[secret_config]`, `config/loaders_sessions.py:29` for `[session.config]`), and
`_raise_unexpected_keys` has two more (`loaders_core.py:314`, `:352`) plus the oracle
(`migrate/toml_resources.py:525`). What retires with the last kind is every KIND-SPEC use. The
helpers themselves retire at 2.10 (FR14) if that step lands, and stay otherwise. Same for
`_parse_env_table` and `_require_string_list`, which survive for the oracle. Reported in section 14.

## 11. Implementation sequence

Always-green, additive then subtractive, one commit per numbered item with the full gate passing
after each. This is a thirteen-kind swap, so the pattern is bedded in on the smallest kinds first,
as the plan orders it.

1. **Foundation edits, additive.** `SkipJsonSchema` skipping in `iter_field_docs`, the two bridge
   extensions with their corpus entries (section 9), `CapabilityBlock`, `replace_fields`, the
   `ResourceName` annotation factory. Nothing consumes them yet; the bridge extensions land with the
   error-corpus entries that prove them.
2. **`DeclaredResource` becomes a model**, with every row still a dataclass. This does not compile
   as a single step (a dataclass cannot extend a `BaseModel`), so it lands WITH item 3 for the first
   kind and the base is introduced in that commit. Recorded here rather than pretended away.
3. **apt-package and apt-source**, models and decode swap together, plus `replace_fields` wired at
   its three sites and `strip_source_fields` taught the model shape. The first commit is the biggest
   because it carries the base; every later kind is a small diff against a proven pattern.
4. **system-install-command and user-install-command**, including the shared spec base (section
   6.3).
5. **workspace-template**, which introduces `EnvTable` and the `EnvEntry` model, its `key` removal,
   and the env-hygiene advisory pass. The env work rides the smallest env-bearing kind on purpose.
6. **named-console-template** (the `Literal` inversion), then **admin-template**, then
   **agent-template** (the mise cross-field validator), then **vm-template** (the first `SecretRef`
   on a kind spec, and the enforced no-`default_template` rule).
7. **secret**, which introduces the mapping-value union and depends on bridge extension (c).
8. **The three host kinds, one commit each: git-credential, vm-site, session-template.** Each
   carries its `CapabilityBlock` row change and its consumers. `tagged_config` and
   `validate_own_config`'s synthesis are deleted in the LAST of the three, when no caller is left.
9. **The sweep.** `_DECODERS`, the key-set constants, `_doc_decls`, `FramedConfigError` and the
   finalize-pass fork, the two nonconforming-name helpers, the decode module docstring.

Item 8 is where the branch is most exposed: three commits during which some rows carry a block and
others a pair. The core entry points take the tagged mapping from the first of the three, and the
two not-yet-migrated hosts keep calling through a two-line adapter that spells `{"name": n, **blob}`
at the call site. That adapter exists for two commits and is deleted in the third; it is named here
so nobody mistakes it for a survivor of `tagged_config`.

## 12. Test plan

Unit tests live beside their model (`tests/vms/`, `tests/agents/`, ...); the cross-cutting ones stay
in `tests/manifests/`.

**Per kind, twelve times (the box each kind's commit checks off).**

1. Every field round-trips from a manifest document to the row, including the shapes the old decoder
   coerced (a `list` where a list belongs, a bare string where one belongs).
2. An unknown spec key is a hard error naming the valid fields (FR12), replacing that kind's
   warn-mode assertion where one exists.
3. Each of the kind's semantic validators fails with the message the decoder produced, asserted on
   the text, not on the exception type.
4. A metadata field written inside `spec` is refused by the section 2.3 guard, with `name` and
   `description` both covered (the guard is derived, so one kind proving all four fields is enough,
   but `description` is asserted per kind because it is the field operators actually mistake).

**Cross-cutting.**

1. **`test_decode_parity.py` passes unchanged.** This is the highest-value single signal in the
   step: the migrator's independently written oracle and the model must produce equal rows for every
   kind it covers. Any change to that file's expectations is a finding to explain, not a fixup.
2. **Every bundled sample and built-in manifest still loads** (`tests/manifests/test_samples.py`,
   `manifests/package.py:112`'s issue-free assertion). Under closed-world specs this becomes a real
   check that the samples describe fields that exist; a sample line that has been wrong all along
   will surface here, and fixing it is in scope.
3. **The apt / install-command `description` change is invisible** on `agw resource list` and
   `agw resource describe` for a row that declares none (section 6, the `""`-to-`None` unification).
4. **The migrator round-trips.** A migrate run over a fixture TOML config verifies clean, which
   exercises `strip_source_fields` against model rows end to end rather than through a unit test of
   the helper.
5. **FR17 does not regress:** step 2.3b's inheriting-surface regression test (a child overriding a
   parent's default secret name) re-run against model-backed rows, plus an assertion that no
   kind-spec marker carries a `default_template` (7.2), proven non-vacuous by adding one to a
   fixture model and watching the check fire.
6. **The error corpus grows by five entries**, each asserting owner framing and file/line at least
   as good as today's: a constrained dict key (bridge fix b), a union with no matching alternative
   (bridge fix c), a missing required spec field, a metadata field written in `spec`, and the legacy
   sibling shape (3.4, which is 2.4's corpus entry re-run against the model regime, and is the entry
   that proves 2.4's hardening survived this swap).
7. **`metadata.expires`** validates on any kind from all three accepted spellings and rejects both a
   malformed string and a bare number (2.2's verified lax-mode surprise), which is the FR20 box.
8. **The `ResourceKind.model` derivation** is pinned: every declarable kind has a model, no
   capability kind does, and decode reads it rather than a table (the 2.0 guard-test pattern: assert
   derivation, not agreement).

**Non-vacuity.** Three of these tests iterate registry contents and would pass on an empty
collection (6, 9's marker sweep, 12). Each gets the count guard the 2.0 step's review established.

## 13. What 2.9 promotes

SDD docs are not permanent. This step's contract that must survive in permanent homes:

- **The metadata / spec split rule** (framework fields ride the row and are refused inside `spec`;
  the emitted schema and the doc stream are the spec surface only): into
  `agentworks/declared_resource.py`'s module docstring, where the base itself lives, and referenced
  from `docs/guides/resources.md`.
- **`ResourceKind.model` as the per-kind schema authority**: into the protocol docstring in
  `resources/kind.py`, beside the `instances` precedent it follows.
- **`CapabilityBlock`'s semantics** (the tag is kind-owned, the extras are the capability's and are
  closed-world checked against the capability's own model at finalize): into
  `capabilities/README.md`, beside the `config_model` registration contract 2.9 already rewrites.
- **The no-`default_template`-on-an-inheriting-row rule** (7.2): into the enforcing check's own
  docstring, because the person who needs it will be reading the failure, not this file.
- **The advisory-checks-are-derived rule** (3.3): into `manifests/decode.py`'s module docstring.
- **`_reject_legacy_shape`'s deletion trigger and its tie to `HostSurface.config_field`** (3.4).

New SDD vocabulary for the SDD-local `.cspell.json`: none required beyond what is already there
(`frozenset`, `jsonschema`, `unbuildable`). Confirm at implementation; anything that lands in
permanent code goes to the root dictionary instead.

## 14. Contradictions and residual decisions for the lead

**Contradictions found against the upstream artifacts, with evidence.**

1. **The schema package is not where three artifacts say it is.** HLA Component 1 (`hla.md:149`),
   plan step 2.1, and schema-foundation LLD section 8 all specify `resources/schema/`. It shipped at
   `cli/agentworks/schema/` and had to: importing any submodule of `agentworks.resources` runs its
   `__init__`, which eagerly imports every domain kind module (the reason recorded at
   `agentworks/declared_resource.py`'s module docstring), so a package under `resources/` could not
   be the leaf the design requires. The code is right and the docs are stale. Two permanent files
   still carry the wrong path: `cli/pyproject.toml`'s pydantic dependency comment
   ("agentworks/resources/schema/") and `agentworks/schema/reference.py`'s docstring ("a leaf that
   `resources/schema/` imports"). This LLD proposes fixing both as opportunistic cleanups in item 1
   of the sequence, and 2.9's promotion sweep must use the real path.
2. **Plan 2.5's `_warn_unexpected_keys` box is wrong** (section 10). Six non-kind callers survive.
   Suggested plan wording: "every kind-spec use of `_warn_unexpected_keys` retires; the helper
   itself survives for the settings sections until 2.10."
3. **`tagged_config`'s deletion requires a row-shape change no plan box mentions.** The plan's 2.5
   boxes describe models, decode, unknown keys, the migrator, frozen rows and `expires`. Deleting
   `tagged_config` on its stated trigger forces `VMSiteDecl` / `GitCredentialConfig` /
   `SessionTemplate` to carry the tagged block and forces about twenty read sites plus the three
   core entry-point signatures (section 4). That work is real and belongs in the plan as its own box
   under 2.5 rather than arriving inside "decode swap".
4. **The plan's frozen-model box has a consequence it does not name:** `EnvEntry` must become a
   model and lose its `key` field (5.1), touching the env package, the migrator oracle, and fourteen
   test modules. Cheap and mechanical, but it is not implied by the words "decl classes are frozen
   models" and the lead should see it before the diff arrives.
5. **FR20's wording versus where `expires` lands.** The FRD says "modeled once on the shared
   envelope `metadata`". It is modeled once on the shared ROW base, which already carries `name` and
   `description`, and the envelope derives its accepted metadata keys from it (2.2). Equivalent in
   effect and strictly better against drift, but it is a reading of the requirement rather than its
   literal shape, so it is flagged rather than assumed.
6. **Two bridge defects reached main and this step is the first consumer to hit them** (section 9 b
   and c), both verified by execution. They are edits to a module steps 2.1 and 2.2 signed off on.
   Flagged so the lead can decide whether they land here or as their own hardening commit; this LLD
   assumes here, because a step that shipped a worse error than the code it replaced would violate
   FR12 on the day it landed.

**Residual decisions, deliberately not made here.**

1. **Structural extraction for kind specs is deferred, loudly** (7.1). It would delete four
   near-identical `dependencies()` bodies, and it is a graph change with its own regression surface.
   Recommended as a follow-up step after 2.6, not folded into this swap. If the lead wants it inside
   phase 2, it needs its own plan box and its own review.
2. **The non-conforming-secret-name check stays a WARNING** (3.3). The principled alternative is to
   make it a hard error carried by the `SecretRef` marker itself, which would be structural, uniform
   across kind specs and capability config, and would close the #279 / #308 family completely. It is
   not taken here for one reason: it is an operator-visible break that no plan box asks for, in a
   step that already carries three (agent-template's two dropped keys, vm-template's integer
   coercion, and the install-command string coercion). Cheap to overturn: the check is one function
   and the escalation is a one-line change of where it is raised from. Recommended for 2.9's
   operator note as a "next release" warning if the lead wants it.
3. **A metadata-sourced field's error path reads `<owner>.description`, not
   `<owner>.metadata.description`** (2.2). It is parity, not a regression (today's message is
   `secrets.<name>.description is required ...`, equally silent about metadata), and the uniform
   sample hint points at the shape. The mechanisms that would fix it (a validation alias spelling
   the fake path, or a per-field path prefix threaded through the bridge) both cost more than the
   cosmetic buys. Flagged as a known small roughness rather than fixed.
4. **The document envelope is not modeled** (3.2). 2.7 is the step with a reason to want a
   document-level schema; deciding its shape now would be speculative.
5. **The `agw resource sample` hint text hard-codes a surface name that 2.8 may rename.** The plan
   records the describe surface's name as still open and raised to the operator. This step uses the
   shipped `agw resource sample` spelling; if 2.8 renames, the hint is one constant.

## 15. Where the design met contact

Written during implementation. Everything above stands except where named here.

**Two designs did not survive, and both were caught by shipped tests.**

1. **The per-kind name cap is declared data the decoder reads, not a validator on the `name` field**
   (against section 5.2). Section 5.2 has `secret` and `vm-site` override `name` with a capped
   annotation, "matching today exactly". It does not match: a model validates on every construction,
   while `validate_name` runs only at DECODE today, and three shipped tests pin that `SecretDecl`
   must accept a non-conforming name outside the manifest path
   (`test_add_rejects_names_containing_slash`,
   `test_secretdecl_construction_tolerates_nonconforming_operator_name`,
   `test_nonconforming_secret_name_still_resolves`). Issue #279's decision was warn at the operator
   boundary and stay tolerant at runtime, so an auto-declared secret carries whatever name the
   reference that summoned it used. The row declares `NAME_MAX_LENGTH` and decode applies it to what
   an operator wrote; `name_check`, the annotation factory section 5.2 proposed, was written and
   then deleted.

2. **`validate_capability_config` keeps a `name` parameter, for the map-keyed kind only** (against
   section 4.4). Dropping `name` outright works for the three TAGGED kinds and not for
   `secret-backend`, which dispatches by an outer map key and whose config is the mapping VALUE (a
   bare string for env-var). `selected_name(kind, config, name)` is the one place that decides: the
   tag for a tagged kind (so the caller's copy cannot disagree with it, which would look up one
   implementation and validate against another's schema), the caller's `name` for a map-keyed one,
   and a `StateError` when a map-keyed kind is called without one.

**Three things the design did not anticipate.**

1. **`DeclaredResource.validate` had to be renamed `validate_config`.** `BaseModel` already has a
   (deprecated) `validate` classmethod meaning something else, so the old name resolved on EVERY row
   rather than on the three that define the hook, and the finalize pass's
   `getattr(resource, "validate", None)` would have called pydantic's with this method's arguments.
   A runtime bug, not a typing complaint.

2. **`Origin` and the naming rule both had to be relocated to top-level leaves.** A model resolves
   its FIELD annotations at class-definition time, so a row carrying `origin: Origin` and a row
   whose `name` cap comes from `MAX_SECRET_NAME_LENGTH` both need those names without running a
   package that imports the row back. `agentworks/origin.py` and `agentworks/naming.py` join
   `declared_resource.py` and `source_location.py` for exactly the reason those two are already
   there. Both moved whole (no shim); `agentworks.resources` goes on re-exporting `Origin`.

3. **The metadata / spec split needed a structural home, not just `SkipJsonSchema`.** Section 2.2
   marks `expires`, `declared_at` and `origin` but leaves `name` and `description` visible, and
   derives `_METADATA_KEYS` as "not `SkipJsonSchema`, plus `expires`", which is a special case
   admitting there is a third category. There is: a row carries SPEC fields, ENVELOPE fields, and
   FRAMEWORK fields, and only the first is spec surface. `EnvelopeMetadata` is the base that says
   which, `METADATA_FIELDS` is its field set, and every one of its fields carries `SkipJsonSchema`.
   The first kind found the live defect: without it, an operator who mistypes a spec key is answered
   with "unknown field; expected one of: apt, apt_sources, declared_at, description, name, origin",
   offering `origin` as something they could have written.

**Two guards the swap made dead, and deleted rather than carried.**

1. **The kind-owned shadow checks** (`_decode_vm_site` and `_decode_git_credential`'s "may not
   contain kind-owned field(s)"). Their premise was that a `platform` key inside `platform_config`
   could silently re-pick the capability. Inside ONE tagged table it cannot: `name` is the selector
   and is a real field of the block, so a stray `platform` key is config the platform does not
   accept, and the platform's own model says so at finalize. Their tests now pin that refusal.

2. **`tagged_config`'s collision error** went with the function, as section 4.1 predicted, and
   `validate_own_config` gained a StateError in its place: a construct-time caller passes the
   capability's OWN config, untagged, and one carrying a tag for a different capability is a
   framework mistake rather than an operator one.

**Two operator-visible improvements found in passing, beyond the three breaks section 6 records.**

- `inherits: parent` written without the list used to load as `['p','a','r','e','n','t']`, because
  the decoder spelled `list(...)` around it.
- `metadata.expires: 12` would have validated to 1970 under a lax datetime; section 2.2 predicted
  this and the `BeforeValidator` refuses it.

**Two smaller settlements.**

- `EnvEntry` gains a public `CapabilityBlock.of(name, **config)` sibling on the block class, because
  an open model's extra fields are not in its `__init__` signature and every caller that ASSEMBLES a
  block (the migrator's oracle, tests) has a name and a mapping in hand.
- The migrate hint's third surface: `_LEGACY_SIBLING_SHAPES` gained `session-template`. Decode
  attaches the hint from one generic guard over every host surface, and the migrator covered two, so
  a hand-typed `harness_integration: shell` was answered with a command that printed "nothing to
  migrate" for the exact document that had just failed to load. A test pins the invariant (every
  host surface decode can refuse is one the migrator's upgrade covers) rather than the entry.

## 16. What the step-2.5 review corrected

The review found one wrong answer and five degraded messages. Two of its findings contradict claims
made above, so those are corrected here rather than left standing.

**Section 3.3's "strictly more coverage than today" is false, and the soft edge was a regression.**
That section calls a plugin capability's blob contributing no advisory at manifest-load time "a
missed ADVISORY line, never a wrong answer". For `vm-site.token_secret` it is LESS coverage: the
check it replaces was not platform-gated, so it fired for every site. And it was not confined to
plugins seating late in `build_registry`: a plugin's impls seat when `agentworks.plugins` is
imported, which no caller of `load_manifests` is obliged to have done, so doctor (the surface this
advisory exists for) reported `Config is valid` for a non-conforming `token_secret` on an ENABLED
proxmox site. `_hosted_capability_references` imports the index itself now, which makes the advisory
a property of the document rather than of who loaded it first.

**Section 6's shadow-guard deletion argument is right but incomplete.** Deleting the guard is
correct (a stray `platform` key inside one tagged table cannot re-pick the capability, so it is the
platform's model's business), but its replacement is readiness-gated: `_validate_resources` skips a
not-ready or disabled node, so the refusal only runs on a host where that platform can run. A `wsl2`
site carrying junk loads clean on Linux. Consistent with R9.4 and accepted as a narrowing; named in
`VMSiteDecl.validate_config` so the next reader does not have to rediscover it.

**Three fields transcribed the wrong requiredness.** Section 6 says `apt-package`'s `apt` is
required. It reads that way on the dataclass and NOT on the operator surface, because the loader
read it through `_require_list`, whose `get(key, [])` defaults it. The mismatch dead-ended the
migrator: a config.toml that hard-errors on load carried a remediation that aborted at verification
and rolled back. `vm-template.tailscale_auth_key` lost its non-empty guard, which was the one wrong
answer in the step: `None` means inherit and the merge overrides on `is not None`, so an empty
string replaced the resolved default with the name of no secret at all. And a declared `secret`
needs a non-empty description (it is the prompt text), which is checked at decode for the same
reason the name cap is: the framework builds secret rows with an empty one on purpose.

**Two message regressions, both fixed in the bridge rather than the field.** A pattern-constrained
string rendered pydantic's raw text; `string_pattern_mismatch` joins the normalization table, which
also improves five pre-existing fields across two capabilities. An orphan or mixed `*_config` blob
lost its fold steer, because `_reject_legacy_shape` returned early unless the naming field was a
string.

**The lens, which is the durable part.** In three of these the test was rewritten to assert the
degraded output, which stops it being a guard. FR12 makes error quality a non-regression
requirement, so when output changes the question is whether the new message is at least as good, and
when the honest answer is no, the fix is the code.
