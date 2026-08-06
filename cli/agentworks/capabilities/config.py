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

**The union is a root model, built per** ``(kind, facet)`` **and cached on
the registry's contents.** See :func:`capability_config_union` for why the
cache is keyed that way rather than invalidated, and
:func:`tagged_config` for the interim step 2.5 deletes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Union, cast

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from agentworks.capabilities.descriptor import descriptor_for, descriptor_for_impl
from agentworks.errors import ConfigError, StateError
from agentworks.schema import (
    AgwRootModel,
    config_error_from,
    extract_references,
    validation_context,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor
    from agentworks.capabilities.facets import Facet
    from agentworks.resources.reference import ConfigReference
    from agentworks.schema import RefOwner
    from agentworks.source_location import SourceLocation

#: Assembled unions, keyed by ``(kind, facet)`` PLUS the arms the union
#: would be built from. Never evicts; see :func:`capability_config_union`
#: for both choices and what bounds the size.
_UNION_CACHE: dict[tuple[str, Facet | None, frozenset[tuple[str, type[BaseModel]]]], type[BaseModel]] = {}


def capability_config_model(kind: str, name: str, facet: Facet | None = None) -> type[BaseModel] | None:
    """The config model ``kind``/``name`` offers at ``facet``, or ``None``
    when no such implementation is seated on this host.

    ``None`` rather than an error, matching what every consuming resource
    already does with an unknown capability name: the resource's dangling
    capability edge is what reports it, as a hard finalize miss (R9.2), and
    reporting it twice in different vocabularies would be worse than once.
    """
    impl = _seated_impl(descriptor_for(kind), name)
    return None if impl is None else offered_model(impl, facet)


def validate_capability_config(
    *,
    kind: str,
    name: str,
    blob: Mapping[str, object],
    owner: RefOwner,
    facet: Facet | None = None,
    location: SourceLocation | None = None,
) -> BaseModel | None:
    """Validate ``blob`` as ``kind``/``name``'s config; return the validated
    instance, or ``None`` when no such implementation is seated.

    Raises the error bridge's framed ``ConfigError``, which already carries
    ``location``: a caller must NOT wrap the result with a location of its
    own (see ``FramedConfigError``).
    """
    descriptor = descriptor_for(kind)
    impl = _seated_impl(descriptor, name)
    if impl is None:
        return None
    contract = descriptor.config_schema
    if contract.discriminator is None:
        model = offered_model(impl, facet)
        return _validated(model, blob, owner=owner, location=location)
    union = capability_config_union(kind, facet)
    tagged = tagged_config(name, blob, discriminator=contract.discriminator, owner=owner)
    validated = _validated(union, tagged, owner=owner, location=location)
    # The union is a root model, so the thing the capability was written
    # against is what it wraps, never the wrapper.
    return cast("BaseModel", validated.root)  # type: ignore[attr-defined]


def validate_own_config(
    impl: type,
    blob: Mapping[str, object],
    *,
    owner: RefOwner,
    facet: Facet | None = None,
) -> BaseModel:
    """Validate ``blob`` against the config ``impl`` itself offers.

    The construct-time path, where the implementation CLASS is already in
    hand: there is no name to look up and no arm to select, so this skips
    the registry and the union entirely. The tagged synthesis still runs
    for a kind whose models carry a tag, because the model's own tag field
    is required either way; a class of no registered kind has no such
    contract and validates its blob as written.
    """
    descriptor = descriptor_for_impl(impl)
    model = offered_model(impl, facet)
    discriminator = descriptor.config_schema.discriminator if descriptor is not None else None
    payload: object = blob
    if discriminator is not None:
        payload = tagged_config(str(impl.name), blob, discriminator=discriminator, owner=owner)  # type: ignore[attr-defined]
    return _validated(model, payload, owner=owner, location=None)


def capability_config_references(
    *,
    kind: str,
    name: str,
    blob: Mapping[str, object],
    owner: RefOwner,
    facet: Facet | None = None,
) -> tuple[ConfigReference, ...]:
    """Every Resource reference ``blob`` implies as ``kind``/``name``'s
    config.

    Total and never raising, for any inputs whatsoever: the graph is built
    before anything is validated, so a blob nobody can make sense of has to
    contribute no edges rather than sink the walk.

    The RAW blob is what is read, never the tagged synthesis: the tag is a
    kind-owned selector that no model marks as a reference, and passing the
    raw blob keeps extraction a pure function of ``(model, blob, owner)``.
    """
    model = capability_config_model(kind, name, facet)
    if model is None:
        return ()
    return extract_references(model, blob, owner)


def capability_config_union(kind: str, facet: Facet | None = None) -> type[BaseModel]:
    """The discriminated union over every registered ``kind``
    implementation's config at ``facet``.

    A root model wrapping the union type rather than a bare
    ``TypeAdapter``, because the error bridge frames against a model: as a
    root model, a failure's leading tag segment (``('lima', 'vm_host')``)
    is recognized as the tag it is and dropped, so an operator reads
    ``vm-site/lab.vm_host`` rather than a path with our dispatch mechanism
    in it. The model is generated rather than authored, which is legal
    precisely because it declares no fields of its own: every field, and
    every field description, comes from the authored arms.

    **The cache key is the union's own ARMS**, rather than ``(kind, facet)``
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
    arms = _arms(descriptor, facet)
    key = (kind, facet, frozenset(arms.items()))
    cached = _UNION_CACHE.get(key)
    if cached is not None:
        return cached
    union = _build_union(descriptor, tuple(arms.values()), discriminator)
    _UNION_CACHE[key] = union
    return union


def tagged_config(
    name: str,
    blob: Mapping[str, object],
    *,
    discriminator: str,
    owner: RefOwner,
) -> dict[str, object]:
    """The one tagged table this config WILL BE once decode produces it
    directly (``platform: {name: lima, vm_host: ...}``).

    INTERIM, with one deletion trigger: decode still hands a consuming
    resource a naming field and a sibling config blob, and step 2.5's kind
    spec models make it hand over the tagged table instead. Then the
    callers pass that table and this function goes.

    A ``name`` key already in the blob is a hard error rather than an
    override in either direction, because both silent resolutions are
    wrong and one is dangerous: letting the tag win DISCARDS a key the
    operator wrote (today a loud unknown-field error), and letting the blob
    win lets ``platform_config.name`` select a different capability's
    schema than ``platform`` names, which is a silent wrong answer produced
    by a compatibility shim. Under the tagged shape the collision cannot be
    expressed at all, which is why this error is as interim as the
    function.
    """
    if discriminator in blob:
        raise ConfigError(
            f"{owner.display}: {discriminator!r} is the field that names the capability, so it cannot also "
            f"appear inside its config block (got {blob[discriminator]!r})",
            entity_kind=owner.kind,
            entity_name=owner.name,
            hint=f"remove {discriminator!r} from the config block; the capability is already named beside it",
        )
    return {discriminator: name, **blob}


def offered_model(impl: type, facet: Facet | None = None) -> type[BaseModel]:
    """The config model ``impl`` offers at ``facet``.

    Two shapes, because the four kinds' implementation contracts are not
    uniform, which is the same code fact registration conformance already
    branches on. The three ABC kinds inherit ``Capability.config_for``,
    which is the override point for a capability whose methods run at
    several levels; the Protocol kind declares only ``config_model``,
    because a per-secret backend mapping is not a level a capability is
    driven at and never will be.
    """
    resolve = getattr(impl, "config_for", None)
    if callable(resolve):
        return cast("type[BaseModel]", resolve(facet))
    model = getattr(impl, "config_model", None)
    if model is None:
        raise StateError(
            f"{impl.__name__} declares no config_model, so the framework has no schema to validate its config against"
        )
    return cast("type[BaseModel]", model)


def _seated_impl(descriptor: CapabilityKindDescriptor, name: str) -> type | None:
    """The implementation CLASS seated under ``name``, or ``None``."""
    seated = descriptor.registry().get(name)
    return None if seated is None else _impl_class(seated)


def _arms(descriptor: CapabilityKindDescriptor, facet: Facet | None) -> dict[str, type[BaseModel]]:
    """The config model every registered implementation of this kind offers
    at ``facet``, keyed by the name it is registered under.

    Both the cache key and the union's arms come from this one read, so the
    key cannot describe a union different from the one it would build.
    """
    return {name: offered_model(_impl_class(seated), facet) for name, seated in descriptor.registry().items()}


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


def _impl_class(seated: object) -> type:
    """What a registry holds, as the CLASS that carries the declaration.

    One kind's registry holds a constructed instance rather than the class
    (secret-backend, the descriptor-carried interim exception that wave 3
    removes), and the declaration is class-level either way.
    """
    return seated if isinstance(seated, type) else type(seated)


def _class_name(kind: str) -> str:
    return "".join(part.capitalize() for part in kind.split("-"))


def _validated(
    model: type[BaseModel],
    payload: object,
    *,
    owner: RefOwner,
    location: SourceLocation | None,
) -> BaseModel:
    try:
        return model.model_validate(payload, context=validation_context(owner))
    except PydanticValidationError as exc:
        raise config_error_from(exc, model_cls=model, owner=owner, location=location) from exc
