"""Core-driven capability config: validation, extraction, and the union.

A capability DECLARES the shape of its config and the framework does the
rest. Nothing here invokes capability code to validate a blob or to derive
what it references, which is what the base-class docstrings promised all
along and what keeps a misbehaving plugin out of the finalize pass.

Three things live here, and every consuming resource needs all three:

- :func:`validate_capability_config`, the throwing shape check, raising the
  error bridge's framed ``ConfigError``;
- :func:`capability_config_references`, the total, never-raising edge
  extraction;
- :func:`capability_config_union`, the tagged union over a kind's
  registered models, which is what makes the "unknown name" message and
  (later) emitted schema possible.

**The union is a root model, built per** ``kind`` **and cached on the
registry's contents.** See :func:`capability_config_union` for why the
cache is keyed that way rather than invalidated.

**What a caller passes as ``config`` is what the capability's own model
validates**, and its shape follows the kind's dispatch:

- a TAGGED kind (vm-platform, git-credential-provider,
  harness-integration) is selected by a ``name`` key INSIDE its table, so
  ``config`` is that whole table, tag included, exactly as the operator
  wrote it and exactly as the host row carries it
  (:class:`~agentworks.schema.CapabilityBlock`);
- a MAP-KEYED kind (secret-backend) is selected by the key its value sits
  under, so ``config`` is that value, which need not be a mapping at all
  (env-var's is a bare string).

So a TAGGED kind needs no ``name`` argument at all: the tag inside the
table is what selects the implementation, and reading it here rather than
taking the caller's copy is what makes the two unable to disagree. A
map-keyed kind passes ``name``, because its config carries no tag.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Union, cast

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from agentworks.capabilities.descriptor import descriptor_for, descriptor_for_impl
from agentworks.capabilities.retired_shapes import retired_shape_error
from agentworks.errors import StateError
from agentworks.schema import (
    AgwRootModel,
    config_error_from,
    extract_references,
    validation_context,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor
    from agentworks.resources.reference import ConfigReference
    from agentworks.schema import RefOwner
    from agentworks.source_location import SourceLocation

#: Assembled unions, keyed by ``kind`` PLUS the arms the union would be
#: built from. Never evicts; see :func:`capability_config_union` for both
#: choices and what bounds the size.
_UNION_CACHE: dict[tuple[str, frozenset[tuple[str, type[BaseModel]]]], type[BaseModel]] = {}


def selected_name(kind: str, config: object, name: str | None) -> str | None:
    """Which implementation of ``kind`` this config selects.

    For a TAGGED kind that is the tag inside the table, never the
    caller's ``name``: two sources for one fact is two sources that can
    disagree, and a disagreement here would look up one implementation
    and validate against another's schema, which is a silent wrong
    answer. Tolerant by design: a missing or non-string tag names no
    implementation, which is what the dangling capability edge already
    reports (R9.2).

    For a MAP-KEYED kind (secret-backend) the config carries no tag and
    the outer map key is the only source, so ``name`` is required; its
    absence is a framework bug, not an operator mistake.
    """
    discriminator = descriptor_for(kind).config_schema.discriminator
    if discriminator is None:
        if name is None:
            raise StateError(
                f"the {kind} capability kind dispatches its config by map key, so a caller must say which "
                f"implementation the config belongs to"
            )
        return name
    tag = config.get(discriminator) if isinstance(config, Mapping) else None
    return tag if isinstance(tag, str) else None


def capability_config_model(kind: str, name: str) -> type[BaseModel] | None:
    """The config model ``kind``/``name`` offers, or ``None`` when no such
    implementation is seated on this host.

    ``None`` rather than an error, matching what every consuming resource
    already does with an unknown capability name: the resource's dangling
    capability edge is what reports it, as a hard finalize miss (R9.2), and
    reporting it twice in different vocabularies would be worse than once.
    """
    impl = _seated_impl(descriptor_for(kind), name)
    return None if impl is None else offered_model(impl)


def validate_capability_config(
    *,
    kind: str,
    config: object,
    owner: RefOwner,
    name: str | None = None,
    location: SourceLocation | None = None,
    provenance: Mapping[str, RefOwner] | None = None,
) -> BaseModel | None:
    """Validate ``config`` as one ``kind`` implementation's config; return
    the validated instance, or ``None`` when no such implementation is
    seated.

    See the module docstring for what ``config`` is and when ``name`` is
    needed: for a tagged kind it is the whole table the operator wrote,
    tag included, and the tag is the selector.

    Raises the error bridge's framed ``ConfigError``, which already carries
    ``location``: a caller must NOT wrap the result with a location of its
    own, or the operator reads it twice.

    ``provenance`` is for a config a caller ASSEMBLED by merging an
    inheritance chain: it maps each top-level key to the owner that
    declared it, so an error on an inherited key names that owner instead
    of blaming the leaf. Every non-inheriting surface omits it, its config
    being its own declaration.
    """
    descriptor = descriptor_for(kind)
    selected = selected_name(kind, config, name)
    if selected is None:
        return None
    impl = _seated_impl(descriptor, selected)
    if impl is None:
        return None
    # Before validation, so a pre-migration document gets its exact
    # rewrite rather than the unconnected pair of problems the model layer
    # would answer it with. Framed with the same ``location`` the
    # validation below is given, because it is an error about the same
    # document. Release-scoped; see the module it lives in.
    retired_shape_error(getattr(impl, "retired_shape", None), config, owner, location)
    hint = reference_hint(kind, selected)
    if descriptor.config_schema.discriminator is None:
        model = offered_model(impl)
        return _validated(model, config, owner=owner, location=location, hint=hint, provenance=provenance)
    union = capability_config_union(kind)
    validated = _validated(union, config, owner=owner, location=location, hint=hint, provenance=provenance)
    # The union is a root model, so the thing the capability was written
    # against is what it wraps, never the wrapper.
    return cast("BaseModel", validated.root)  # type: ignore[attr-defined]


def validate_own_config(
    impl: type,
    config: Mapping[str, object],
    *,
    owner: RefOwner,
) -> BaseModel:
    """Validate ``config`` against the config ``impl`` itself offers.

    The construct-time path, where the implementation CLASS is already in
    hand: there is no name to look up and no arm to select, so this skips
    the registry and the union entirely. ``config`` is the capability's
    OWN config, untagged, because a constructor that already knows its
    class has no use for a selector; the tag an arm model declares is
    supplied from ``impl.name``, which is the same fact the class is.

    A ``config`` that carries the tag anyway must agree with the class.
    Neither silent resolution is acceptable: letting the class win
    discards a key a caller wrote, and letting the config win would
    validate against a schema the caller did not think it was using. That
    can only be a framework mistake (a call site handing over a table
    where a config belongs), so it is a ``StateError``.
    """
    retired_shape_error(getattr(impl, "retired_shape", None), config, owner)
    descriptor = descriptor_for_impl(impl)
    discriminator = descriptor.config_schema.discriminator if descriptor is not None else None
    payload: Mapping[str, object] = config
    if discriminator is not None:
        own = str(impl.name)  # type: ignore[attr-defined]
        declared = config.get(discriminator)
        if declared is not None and declared != own:
            raise StateError(
                f"{impl.__name__} was handed a config tagged {declared!r}, which is not the capability it is; "
                f"pass the capability's own config here, not the host's tagged table"
            )
        payload = {**config, discriminator: own}
    return _validated(offered_model(impl), payload, owner=owner, location=None)


def capability_config_references(
    *,
    kind: str,
    config: object,
    owner: RefOwner,
    name: str | None = None,
) -> tuple[ConfigReference, ...]:
    """Every Resource reference ``config`` implies as one ``kind``
    implementation's config.

    Total and never raising, for any inputs whatsoever: the graph is built
    before anything is validated, so a config nobody can make sense of has
    to contribute no edges rather than sink the walk.

    What is read is exactly what would be VALIDATED, tag and all. That is
    not a change of substance from the raw-blob rule this used to state:
    the rule existed because the tagged form was a synthesis the caller
    did not have, and now it is the table the operator wrote. An arm
    model's tag field carries no reference marker, so it contributes
    nothing either way.
    """
    selected = selected_name(kind, config, name)
    model = None if selected is None else capability_config_model(kind, selected)
    if model is None:
        return ()
    return extract_references(model, config, owner)


def resolved_capability_modes(
    *,
    kind: str,
    config: object,
    name: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """The tag each discriminated-union field of ``config``'s
    implementation resolves to, as ``(field, tag)`` pairs in declaration
    order: a written tag off the blob, an omitted field's off its
    declared default.

    This is what makes an IMPLICIT mode choice visible without opening
    the manifest: the mode unions carry declared defaults (azure and
    aws's ``auth``, lima's ``placement``), so a document that writes
    nothing has still resolved to an arm, and a reviewer reading a
    health or inspection surface should see which. Read structurally off
    the declared model, like extraction: no validation runs and no
    capability code is invoked.

    Total and never raising: an unknown implementation, a blob that is
    not a table, or a written value whose tag is missing or malformed
    contributes no pair (validation is where that becomes an error), so
    a rendering caller degrades to what it already showed.
    """
    from agentworks.schema._shape import model_fields_of, shape_of

    selected = selected_name(kind, config, name)
    model = None if selected is None else capability_config_model(kind, selected)
    fields = None if model is None else model_fields_of(model)
    if fields is None:
        return ()
    modes: list[tuple[str, str]] = []
    for field_name, field in fields.items():
        shape = shape_of(field)
        if not shape.arms or shape.discriminator is None:
            continue
        written = isinstance(config, Mapping) and field_name in config
        value: object = config[field_name] if written and isinstance(config, Mapping) else field.default
        if isinstance(value, Mapping):
            tag = value.get(shape.discriminator)
        else:
            tag = getattr(value, shape.discriminator, None)
        if isinstance(tag, str) and tag:
            modes.append((field_name, tag))
    return tuple(modes)


def capability_config_union(kind: str) -> type[BaseModel]:
    """The discriminated union over every registered ``kind``
    implementation's config.

    A root model wrapping the union type rather than a bare
    ``TypeAdapter``, because the error bridge frames against a model: as a
    root model, a failure's leading tag segment (``('lima', 'placement')``)
    is recognized as the tag it is and dropped, so an operator reads
    ``vm-site/lab.placement`` rather than a path with our dispatch mechanism
    in it. The model is generated rather than authored, which is legal
    precisely because it declares no fields of its own: every field, and
    every field description, comes from the authored arms.

    **The cache key is the union's own ARMS**, rather than ``kind`` alone
    with an invalidation protocol. Every mutator of a capability registry
    (plugin seating, ``seated_plugin``'s snapshot/restore, a test
    installing a fixture capability) would otherwise have to remember to
    invalidate, and a forgotten invalidation is a stale union: a capability
    validated against ANOTHER capability's schema, which is a silent wrong
    answer rather than a crash. Keying on what the union would be BUILT
    from makes that impossible by construction.

    Keying on the registry MAPPING alone would not, quite: a seated class
    whose ``config_model`` changed would keep its cache entry, same name
    and same class object, and go on being validated against the model it
    used to offer. Unreachable in production, where ``config_model`` is a
    ClassVar set at class definition, but the whole reason to prefer this
    over invalidation is that the alternative fails silently, so a residual
    silent path is not one to leave open. Resolving the models costs what
    :func:`_build_union` already pays per arm on a miss, against rebuilding
    a pydantic union, which is the expensive half.

    The cache never evicts. Its size is bounded by the distinct arm sets a
    process ever sees, which is one per kind plus one per test that seats a
    fixture capability; a deliberate choice, not an oversight.
    """
    descriptor = descriptor_for(kind)
    discriminator = descriptor.config_schema.discriminator
    if discriminator is None:
        raise StateError(
            f"the {kind} capability kind dispatches its config by map key, not by a tagged union, "
            f"so there is no union to assemble"
        )
    arms = _arms(descriptor)
    key = (kind, frozenset(arms.items()))
    cached = _UNION_CACHE.get(key)
    if cached is not None:
        return cached
    union = _build_union(descriptor, tuple(arms.values()), discriminator)
    _UNION_CACHE[key] = union
    return union


def registered_implementations(kind: str) -> dict[str, type]:
    """Every implementation of ``kind`` this build has, keyed by the name
    that selects it, in registration order.

    The enumeration the DOCUMENTATION surfaces need: the sample renderer
    lists a capability's alternatives and the field reference documents one
    of them, and both have to answer for an implementation whose plugin is
    not enabled, because enablement is a property of a published row rather
    than of the registry.

    It lives here, beside the other reads of a capability registry, rather
    than in the renderer, and that is the same consolidation the four
    consuming resources already made: the sanctioned registry read stays
    one call site instead of becoming two. Availability is never what
    it asks (see the module docstring); it asks what exists to describe.
    """
    return {name: impl_class(seated) for name, seated in descriptor_for(kind).registry().items()}


def registered_implementation(kind: str, name: str) -> type | None:
    """The implementation CLASS registered as ``kind``/``name``, or ``None``.

    Nothing is constructed: the declaration a documentation surface reads
    (``config_model``, ``description``, ``prose``) is class-level.
    """
    return _seated_impl(descriptor_for(kind), name)


def offered_model(impl: type) -> type[BaseModel]:
    """The config model ``impl`` offers.

    Read through ``config_for`` when the implementation has it, never off
    ``config_model`` directly, so a capability that overrides the hook is
    honored everywhere the framework asks. That is what lets a capability
    whose methods run at several levels arrive as an ordinary
    registration.

    Two shapes, because the four kinds' implementation contracts are not
    uniform, which is the same code fact registration conformance already
    branches on. The three ABC kinds inherit ``Capability.config_for``;
    the Protocol kind declares only ``config_model``, because a per-secret
    backend mapping is not a level a capability is driven at and never
    will be.
    """
    resolve = getattr(impl, "config_for", None)
    if callable(resolve):
        return cast("type[BaseModel]", resolve())
    model = getattr(impl, "config_model", None)
    if model is None:
        raise StateError(
            f"{impl.__name__} declares no config_model, so the framework has no schema to validate its config against"
        )
    return cast("type[BaseModel]", model)


def _seated_impl(descriptor: CapabilityKindDescriptor, name: str) -> type | None:
    """The implementation CLASS seated under ``name``, or ``None``."""
    seated = descriptor.registry().get(name)
    return None if seated is None else impl_class(seated)


def _arms(descriptor: CapabilityKindDescriptor) -> dict[str, type[BaseModel]]:
    """The config model every registered implementation of this kind
    offers, keyed by the name it is registered under.

    Both the cache key and the union's arms come from this one read, so the
    key cannot describe a union different from the one it would build.
    """
    return {name: offered_model(impl_class(seated)) for name, seated in descriptor.registry().items()}


def _build_union(
    descriptor: CapabilityKindDescriptor,
    arms: tuple[type[BaseModel], ...],
    discriminator: str,
) -> type[BaseModel]:
    if not arms:
        raise StateError(
            f"no {descriptor.kind} implementation is registered, so its config union has no arms; "
            f"the built-in rows publish unconditionally, so this means registration did not run"
        )
    # Built from a runtime value, so mypy cannot see a type here and
    # neither could it if this were spelled any other way: the arms come
    # from a registry a plugin contributes to. The shape is checked
    # instead at registration (conformance check five proves every arm
    # extends the kind's base and tags itself) and by the union tests.
    union: Any = Annotated[Union[arms], Field(discriminator=discriminator)]  # noqa: UP007
    return cast(
        "type[BaseModel]",
        type(
            f"{_class_name(descriptor.kind)}Config",
            (AgwRootModel[union],),
            {"__module__": __name__, "__doc__": f"The config of any registered {descriptor.kind}."},
        ),
    )


def impl_class(seated: object) -> type:
    """What a registry holds, as the CLASS that carries the declaration.

    One kind's registry holds a constructed instance rather than the class
    (secret-backend, the descriptor-carried interim exception), and the
    declaration is class-level either way.
    """
    return seated if isinstance(seated, type) else type(seated)


def _class_name(kind: str) -> str:
    return "".join(part.capitalize() for part in kind.split("-"))


def reference_hint(kind: str, name: str) -> str:
    """Where an operator goes to see the capability config shape they got
    wrong.

    The counterpart of the declarable-kind path's sample hint
    (``manifests.decode``): a kind's own fields are best seen as a document
    to edit, while a capability's config is a block inside someone else's
    document, so the field reference is the surface that answers it. Built
    from the kind and the name for the same reason the sample hint is built
    from the kind: a hand-kept per-capability steer would be a second
    description of a shape that is already rendered.
    """
    return f"`agw resource describe-kind {kind}/{name}` prints this implementation's fields"


def _validated(
    model: type[BaseModel],
    payload: object,
    *,
    owner: RefOwner,
    location: SourceLocation | None,
    hint: str | None = None,
    provenance: Mapping[str, RefOwner] | None = None,
) -> BaseModel:
    try:
        return model.model_validate(payload, context=validation_context(owner))
    except PydanticValidationError as exc:
        raise config_error_from(
            exc,
            model_cls=model,
            owner=owner,
            location=location,
            hint=hint,
            provenance=provenance,
        ) from exc
