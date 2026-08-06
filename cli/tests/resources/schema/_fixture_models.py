"""Fixture models for the walker suites.

These mirror the config shapes the framework actually has (a git
credential's token, azure's ``service_principal``, aws's
``credentials``, proxmox's ``token_secret``, a template's ``inherits``
list, a platform union) so the parity assertions can land before the
real models exist. Step 2.3 authors the real ones and re-points the
parity tests at them.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Discriminator, Field

from agentworks.resources.reference import RefRelationship
from agentworks.resources.schema import AgwModel, AgwRootModel, ResourceRef, SecretRef


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


class UntaggedArm(AgwModel):
    """A union arm with no literal tag: unaddressable from a raw blob."""

    token_secret: Annotated[str, SecretRef(usage="an unreachable secret")]


class SiteLike(AgwModel):
    """A discriminated union spelled with ``Annotated[..., Discriminator]``."""

    platform: Annotated[LimaArm | ProxmoxArm, Discriminator("name")]


class FieldDiscriminatedSite(AgwModel):
    """The same union spelled with ``Field(discriminator=...)``."""

    platform: LimaArm | ProxmoxArm = Field(discriminator="name", default=LimaArm(name="lima"))


class UndiscriminatedSite(AgwModel):
    """A union with no discriminator at all: no arm is addressable."""

    platform: LimaArm | ProxmoxArm | None = None


class SelfReferential(AgwModel):
    """A model reachable from itself: the walk must terminate."""

    secret: Annotated[str, SecretRef(usage="the node secret")] | None = None
    child: SelfReferential | None = None


class UnmarkedLike(AgwModel):
    """A model with nothing marked: it implies no edges at any depth."""

    name: str
    port: int = 22
    nested: LimaArm | None = None


class StringRoot(AgwRootModel[str]):
    """A backend mapping that is a bare string."""


class MappingRoot(AgwRootModel[GithubLike]):
    """A root model wrapping a mapping-shaped model."""


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
    TemplateLike,
    LimaArm,
    ProxmoxArm,
    SiteLike,
    FieldDiscriminatedSite,
    UndiscriminatedSite,
    SelfReferential,
    UnmarkedLike,
    StringRoot,
    MappingRoot,
)
