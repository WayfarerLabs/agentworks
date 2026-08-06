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
from typing import Annotated, Literal

from pydantic import Discriminator, Field

from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr, ResourceRef, SecretRef
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


class UndiscriminatedSite(AgwModel):
    """A union with no discriminator at all: no arm is addressable."""

    platform: LimaArm | ProxmoxArm | None = None


class RenamedArmSite(AgwModel):
    """A union one of whose arms answers to two tags."""

    platform: Annotated[LimaArm | RenamedArm, Discriminator("name")]


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

    ``vm_sizes`` is the shipped shape (azure's ``vm_sizes``, aws's
    ``instance_types``): a list of tables, which a field reference that
    could not expand it would render as an opaque "list". The credential
    collections are the same shape with a marked field inside, where a
    dropped element is a dropped graph edge.
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
    MultiArmMarked,
    SelfReferential,
    UnmarkedLike,
    NeverResolved,
    ResolvesToUnbuildable,
    StringRoot,
    MappingRoot,
    AccountRefLike,
    StringOrTableRoot,
)
