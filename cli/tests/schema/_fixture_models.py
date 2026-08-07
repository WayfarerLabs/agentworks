"""Fixture models for the walker suites.

These mirror the config shapes the framework actually has (a git
credential's token, azure's ``service_principal``, aws's
``credentials``, proxmox's ``token_secret``, a template's ``inherits``
list, a platform union) so the parity assertions can land before the
real models exist. Step 2.3 authors the real ones and re-points the
parity tests at them.
"""

from __future__ import annotations

import socket
from typing import Annotated, ClassVar, Literal

from pydantic import AfterValidator, Discriminator, Field
from pydantic.json_schema import SkipJsonSchema

from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr, ResourceRef, ScalarShorthand, SecretRef
from agentworks.schema.reference import RefRelationship


class GithubLike(AgwModel):
    """A token-sourcing git-credential provider's config."""

    token: Annotated[str, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]
    api_url: str | None = None


class PrincipalLike(AgwModel):
    """Azure's ``service_principal`` block."""

    client_id: str
    tenant_id: str
    secret: Annotated[
        str,
        SecretRef(usage="the Azure service-principal client secret", default_template="azure-client-secret"),
    ]


class AzureLike(AgwModel):
    """A platform whose secret lives one level down, in an optional block."""

    region: str
    service_principal: PrincipalLike | None = None


class AwsCredentialsLike(AgwModel):
    """Aws's ``credentials`` block."""

    access_key_id: str
    access_key_secret: Annotated[
        str,
        SecretRef(usage="the AWS secret access key", default_template="aws-secret-access-key"),
    ]


class AwsLike(AgwModel):
    """A platform with an optional nested credentials block."""

    region: str
    credentials: AwsCredentialsLike | None = None


class ProxmoxLike(AgwModel):
    """A platform whose secret is a top-level field with a constant default."""

    api_url: str
    template_vmid: int
    token_secret: Annotated[str, SecretRef(usage="the Proxmox API token", default_template="proxmox-token")]


class CredsLike(AgwModel):
    """One credential block, reused by two sibling fields below."""

    secret: Annotated[str, SecretRef(usage="the credential secret")]


class DiamondLike(AgwModel):
    """Two sibling fields of the SAME nested model type.

    The shape that an accumulating visited set would silently reduce to
    its first field.
    """

    primary: CredsLike
    fallback: CredsLike


class TemplateLike(AgwModel):
    """A template with an inheritance list and a used template."""

    inherits: list[
        Annotated[
            str,
            ResourceRef(kind="vm-template", usage="a parent template", relationship=RefRelationship.INHERITS),
        ]
    ] = Field(default_factory=list)
    image: Annotated[str, ResourceRef(kind="vm-template", usage="the base image")] | None = None
    replicas: int = 1


class LimaArm(AgwModel):
    """The union arm that names no Resource."""

    name: Literal["lima"]
    vm_host: str | None = None


class ProxmoxArm(AgwModel):
    """The union arm that names a secret."""

    name: Literal["proxmox"]
    token_secret: Annotated[str, SecretRef(usage="the Proxmox API token", default_template="proxmox-token")]


class RenamedArm(AgwModel):
    """An arm answering to two tags, as a renamed capability keeping its
    old name would."""

    name: Literal["aws-ec2", "ec2"]
    access_key_secret: (
        Annotated[
            str,
            SecretRef(usage="the AWS secret access key", default_template="aws-secret-access-key"),
        ]
        | None
    ) = None


class NumericallyTaggedArm(AgwModel):
    """An arm whose tag is not a string: unaddressable here by design."""

    version: Literal[1]
    token_secret: Annotated[str, SecretRef(usage="an unreachable secret")] | None = None


class OtherNumericallyTaggedArm(AgwModel):
    """The second arm of the numerically tagged union."""

    version: Literal[2]


class SiteLike(AgwModel):
    """A discriminated union spelled with ``Annotated[..., Discriminator]``."""

    platform: Annotated[LimaArm | ProxmoxArm, Discriminator("name")]


class FieldDiscriminatedSite(AgwModel):
    """The same union spelled with ``Field(discriminator=...)``."""

    platform: LimaArm | ProxmoxArm = Field(discriminator="name", default=LimaArm(name="lima"))


class OptionalUnionSite(AgwModel):
    """The union spelled with the ``Annotated`` INSIDE the optional.

    The third legal spelling, and the one an author reaches for when the
    whole block is optional (a harness integration on a session
    template). Pydantic validates it identically to the other two, so a
    lookup that misses the discriminator here would be a silently wrong
    graph rather than an error.
    """

    platform: Annotated[LimaArm | ProxmoxArm, Discriminator("name")] | None = None


class OneArmSite(AgwModel):
    """A discriminated union with a SINGLE arm.

    ``Union[(X,)]`` is ``X``, so the annotation collapses to a bare model
    while pydantic still dispatches on the tag. A classifier that read the
    collapsed form as an ordinary nested block would lose the tag, which
    is exactly what a capability kind with one registered implementation
    produces.
    """

    platform: Annotated[LimaArm, Discriminator("name")]


class UndiscriminatedSite(AgwModel):
    """A union with no discriminator at all: no arm is addressable."""

    platform: LimaArm | ProxmoxArm | None = None


class RenamedArmSite(AgwModel):
    """A union one of whose arms answers to two tags."""

    platform: Annotated[LimaArm | RenamedArm, Discriminator("name")]


class TaggedCollectionSite(AgwModel):
    """Collections whose ELEMENTS are a discriminated union of models.

    Not a shape the framework ships (all four discriminated unions are
    top-level capability configs), and one any capability or plugin author
    can write. Left unclassified, its elements read as an undiscriminated
    union, which no walker expands: a secret named inside one would be
    absent from the dependency graph with nothing reported, and every
    human surface would render the field as an opaque list of tables.
    """

    platforms: list[Annotated[LimaArm | ProxmoxArm, Discriminator("name")]] = Field(default_factory=list)
    platforms_by_name: dict[str, Annotated[LimaArm | ProxmoxArm, Discriminator("name")]] = Field(default_factory=dict)


class FieldTaggedCollectionSite(AgwModel):
    """The same shape with the element's tag spelled ``Field(discriminator=)``.

    Pydantic accepts both spellings on an element exactly as it accepts
    both on a field, so a lookup that read only one would be a silently
    wrong graph rather than an error.
    """

    platforms: list[Annotated[LimaArm | ProxmoxArm, Field(discriminator="name")]] = Field(default_factory=list)


class NumericallyTaggedSite(AgwModel):
    """A union tagged by something other than a name."""

    thing: Annotated[NumericallyTaggedArm | OtherNumericallyTaggedArm, Discriminator("version")] | None = None


class MultiArmMarked(AgwModel):
    """A marked field whose union has two non-``None`` arms.

    Pydantic keeps the marker inside the arm rather than lifting it onto
    the field, so this is the same lookup asymmetry as the union
    spellings above, one layer down.
    """

    secret: Annotated[str, SecretRef(usage="the multi-arm secret", default_template="multi-arm-secret")] | int = 0


class CatalogEntryLike(AgwModel):
    """One entry of a size catalog."""

    cpus: int
    memory: float
    size: str


class CatalogLike(AgwModel):
    """A platform carrying collections of tables and of names.

    ``vm_sizes`` is a list of tables, which a field reference that could
    not expand it would render as an opaque "list". The credential
    collections are the same shape with a marked field inside, where a
    dropped element is a dropped graph edge.

    NOT a stand-in for the shipped catalogs, though it is named after
    them: azure's ``vm_sizes`` and aws's ``instance_types`` are spelled
    ``Annotated[list[X], Field(min_length=1)] | None = None``, which the
    walker reaches through two peels this plainer spelling never
    exercises. Those two are pinned directly, in
    ``test_fields.test_the_shipped_optional_catalog_shape_expands_its_element``.
    """

    vm_sizes: list[CatalogEntryLike] = Field(default_factory=list)
    accounts: list[CredsLike] = Field(default_factory=list)
    accounts_by_name: dict[str, CredsLike] = Field(default_factory=dict)
    extra_secrets: dict[str, Annotated[str, SecretRef(usage="an extra secret")]] = Field(default_factory=dict)
    templates: tuple[Annotated[str, ResourceRef(kind="vm-template", usage="a tagged template")], ...] = ()


class SelfReferential(AgwModel):
    """A model reachable from itself: the walk must terminate.

    Reachable both directly and through a collection, since the guard has
    to hold on both routes.
    """

    secret: Annotated[str, SecretRef(usage="the node secret")] | None = None
    child: SelfReferential | None = None
    children: list[SelfReferential] = Field(default_factory=list)


class UnmarkedLike(AgwModel):
    """A model with nothing marked: it implies no edges at any depth."""

    name: str
    port: int = 22
    nested: LimaArm | None = None


class NeverResolved(AgwModel):
    """A model whose annotation never resolves to anything at all."""

    child: NeverDefinedAnywhere | None = None  # type: ignore[name-defined]  # noqa: F821
    secret: Annotated[str, SecretRef(usage="an unreachable secret", default_template="never-resolved")] | None = None


class ResolvesToUnbuildable(AgwModel):
    """A model whose annotation RESOLVES, to a type pydantic has no
    schema for.

    The nastier half of the unbuildable case: the rebuild attempt raises
    ``PydanticSchemaGenerationError`` rather than reporting failure, and
    a walker reaching this model through the annotation graph could not
    have screened for it.
    """

    sock: UnbuildableAlias | None = None
    secret: Annotated[str, SecretRef(usage="an unreachable secret", default_template="unbuildable")] | None = None


# Deliberately AFTER the model above: the annotation is unresolvable when
# the class is created and resolvable, to something unbuildable, by the
# time a walker asks for the model's fields.
UnbuildableAlias = socket.socket


class StringRoot(AgwRootModel[str]):
    """A backend mapping that is a bare string."""


class MappingRoot(AgwRootModel[GithubLike]):
    """A root model wrapping a mapping-shaped model."""


class AccountRefLike(AgwModel):
    """The table arm of a string-or-table mapping."""

    account: NonEmptyStr
    reference: NonEmptyStr


class StringOrTableRoot(AgwRootModel[NonEmptyStr | AccountRefLike]):
    """A backend mapping that is a bare string OR a table: the shipped
    onepassword shape, and the framework's one UNdiscriminated union
    (nothing tags a bare string)."""


class ScalarOrBlockLike(AgwModel):
    """Unions of some scalars and ONE model, at both depths.

    The field-level spelling is the shipped onepassword mapping written
    somewhere a root model is not needed. The element-level ones are not
    shipped and any capability or plugin author can write one; classified
    for the reason a collection of TAGGED blocks is, since a walker that
    stopped at the element would render it as an opaque "table" while the
    emitted schema spelled its properties out.

    The arm is a MARKED model on purpose, which is what makes this fixture
    pin the boundary as well as the shape: the field-documentation stream
    expands the block, and reference extraction does not walk it, so a
    secret named in here implies no graph edge. That is what
    ``test_extract_totality`` records this model as edgeless for.
    """

    mapping: str | CredsLike | None = None
    mappings: dict[str, str | CredsLike] = Field(default_factory=dict)
    mapping_list: list[str | CredsLike] = Field(default_factory=list)


class SelfReferentialUnion(AgwModel):
    """A scalar-or-block union whose block is reachable from itself.

    The block a union OFFERS expands like any other nested block, so it
    takes the stream's own path guard; without one the walk would recur
    until the interpreter gave up and take every surface down with it.
    """

    name: str = ""
    child: str | SelfReferentialUnion | None = None


class ShorthandLike(AgwModel):
    """A model an operator may also write as one bare scalar: the shipped
    ``EnvEntry`` shape, with the same two fields it folds between."""

    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")

    value: str | None = None
    secret: Annotated[str, SecretRef(usage="the shorthand entry's secret")] | None = None


class ShorthandTemplatedLike(AgwModel):
    """A shorthand model with an OWNER-TEMPLATED field beside the folded
    one.

    The order pin: the fold has to happen before owner-templated defaults
    are filled, since the fill acts on a mapping and a shorthand value is
    not one yet. Folded second, this model's ``token`` would resolve for
    an operator who wrote the table form and silently not for one who
    wrote the scalar.
    """

    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")

    value: str | None = None
    token: Annotated[str, SecretRef(usage="the shorthand token", default_template="shorthand-{owner_name}")] | None = (
        None
    )


class ShorthandHolder(AgwModel):
    """The three ways a shorthand model is held.

    One authored shorthand has to reach every one of them, because the
    shipped case is the middle one (five spec models hold an ``EnvEntry``
    table) and nothing about the declaration is per-field.
    """

    entry: ShorthandLike | None = None
    entries: dict[str, ShorthandLike] = Field(default_factory=dict)
    entry_list: list[ShorthandLike] = Field(default_factory=list)


def _shouty(value: str) -> str:
    if not value.isupper():
        raise ValueError(f"invalid key {value!r} (must be upper case)")
    return value


ShoutyKey = Annotated[str, AfterValidator(_shouty)]


class TableWithConstrainedKeys(AgwModel):
    """A table whose KEYS are constrained, which is how an env table
    validates its variable names. Pydantic reports a key's failure with a
    trailing ``[key]`` marker segment."""

    env: dict[ShoutyKey, NonEmptyStr] = Field(default_factory=dict)


class MappingValueLike(AgwModel):
    """A table whose VALUES are an undiscriminated union of three
    shapes: the secret ``backend_mappings`` shape, and the one place
    pydantic reports a single mistake as one error per member."""

    backend_mappings: dict[str, str | dict[str, object] | Literal[False]] = Field(default_factory=dict)


class FrameworkFielded(AgwModel):
    """A row carrying framework fields beside the operator's.

    ``SkipJsonSchema`` is the one marker that takes a field out of BOTH
    emitted schema and the field-reference stream, which is what lets a
    declared-resource row be its own spec model.
    """

    name: str
    cpus: int | None = None
    origin: SkipJsonSchema[str | None] = None
    declared_at: SkipJsonSchema[str] = "synthesized"


#: Every fixture model the totality suite throws garbage at.
ALL_FIXTURES = (
    GithubLike,
    PrincipalLike,
    AzureLike,
    AwsCredentialsLike,
    AwsLike,
    ProxmoxLike,
    CredsLike,
    DiamondLike,
    CatalogEntryLike,
    CatalogLike,
    TemplateLike,
    LimaArm,
    ProxmoxArm,
    SiteLike,
    FieldDiscriminatedSite,
    OptionalUnionSite,
    RenamedArmSite,
    NumericallyTaggedSite,
    UndiscriminatedSite,
    OneArmSite,
    TaggedCollectionSite,
    FieldTaggedCollectionSite,
    MultiArmMarked,
    SelfReferential,
    UnmarkedLike,
    NeverResolved,
    ResolvesToUnbuildable,
    StringRoot,
    MappingRoot,
    AccountRefLike,
    StringOrTableRoot,
    ScalarOrBlockLike,
    SelfReferentialUnion,
    ShorthandLike,
    ShorthandTemplatedLike,
    ShorthandHolder,
    TableWithConstrainedKeys,
    MappingValueLike,
    FrameworkFielded,
)
