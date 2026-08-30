"""Azure DevOps Git credential provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.git_credential.base import (
    CredentialPayload,
    GitCredentialProvider,
    HttpsCredentialScope,
    ManagedHelper,
    StoredCredential,
    require_line_safe_credential_input,
)
from agentworks.schema import AgwModel, NonEmptyStr, SecretRef
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext


AzDOOrg = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+$")]
"""An Azure DevOps organization name."""


class AzDOSecretSource(AgwModel):
    """Acquire the final credential from a declared secret."""

    mode: Literal["secret"]
    """Resolve provider input through Agentworks' declared secret system."""
    secret: Annotated[
        NonEmptyStr,
        SecretRef(usage="the Azure DevOps credential input", default_template="git-token-{owner_name}"),
    ]
    """The secret input this provider reads; defaults to `git-token-<credential-name>`."""


class AzureCliSource(AgwModel):
    """Acquire a fresh credential from the target user's Azure CLI."""

    mode: Literal["az-cli"]
    """Acquire each credential from the target user's active Azure CLI identity."""


AzDOSource = Annotated[AzDOSecretSource | AzureCliSource, Field(discriminator="mode")]


class AzDOConfig(AgwModel):
    """Azure DevOps credential acquisition and organization scope."""

    name: Literal["azdo"]
    """Select the Azure DevOps credential provider."""
    source: AzDOSource
    """How this provider acquires its credential."""
    org: AzDOOrg = Field(examples=["my-org"])
    """The Azure DevOps organization this credential may serve."""


_AZURE_DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
_AZ_FAILURE_HINT = (
    "agentworks: Azure CLI credential acquisition failed; check that 'az' is installed and on PATH "
    "for this user, then authenticate an identity with access to the configured Azure DevOps organization"
)

_AZ_READINESS = """
command -v az >/dev/null 2>&1 || exit 20
az account show --output none >/dev/null 2>&1 || exit 21
"""


def _azure_cli_helper(org: str) -> bytes:
    return f"""#!/bin/sh
set -u
command -v az >/dev/null 2>&1 || exit 1
token=$(az account get-access-token \
    --resource {_AZURE_DEVOPS_RESOURCE} \
    --query accessToken --output tsv 2>/dev/null) || exit 1
case "$token" in
    ''|*'
'*) exit 1 ;;
esac
printf 'username={org}\\npassword=%s\\n\\n' "$token"
""".encode()


class AzDOCredentialProvider(GitCredentialProvider):
    """Produces stored or Azure-CLI-backed Azure DevOps credentials."""

    contract_version: ClassVar[int] = 3
    name: ClassVar[str] = "azdo"
    description: ClassVar[str] = "Azure DevOps HTTPS credentials from a secret or Azure CLI"
    config_model: ClassVar[type[AzDOConfig]] = AzDOConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Azure DevOps",
        overview="""
        Authenticates HTTPS git operations against one Azure DevOps organization. An
        explicit `source` selects either a declared secret or the active target-user
        Azure CLI identity. Enabled runup checks that CLI identity read-only and warns
        without blocking helper installation.

        The configured organization is both the credential's scope and the username
        returned to Git. The Azure CLI identity must already belong to that organization
        and have repository access.
        """,
    )

    @property
    def config(self) -> AzDOConfig:
        return self._config_as(AzDOConfig)

    def credential_scopes(self) -> tuple[HttpsCredentialScope, ...]:
        # Preserve the released AzDO translation exactly: dev.azure.com
        # plus the configured organization as one path-prefix segment.
        return (HttpsCredentialScope("dev.azure.com", (self.config.org,)),)

    def validate_inputs(self, ctx: RunContext) -> None:
        source = self.config.source
        if isinstance(source, AzDOSecretSource):
            self._secret_input(ctx, source)

    @staticmethod
    def _secret_input(ctx: RunContext, source: AzDOSecretSource) -> str:
        return require_line_safe_credential_input(ctx.secret(source.secret), secret_name=source.secret)

    def runup(self, ctx: RunContext) -> None:
        source = self.config.source
        if isinstance(source, AzureCliSource):
            self._check_cli_readiness(ctx)
            return
        token = self._secret_input(ctx, source)
        self._verify_token(token, secret_name=source.secret)

    def _check_cli_readiness(self, ctx: RunContext) -> None:
        from agentworks import output
        from agentworks.errors import StateError
        from agentworks.ssh import SSHError

        target = ctx.admin_target() or ctx.agent_target()
        if target is None:
            raise StateError("Azure CLI runup requires a current user target")
        try:
            result = target.run(_AZ_READINESS, check=False, timeout=10)
        except SSHError:
            output.warn(f"Could not check Azure CLI readiness for git-credential/{self.owner_name}")
            return
        if result.returncode == 0:
            output.detail(f"Verified Azure CLI readiness for git-credential/{self.owner_name}")
        elif result.returncode == 20:
            output.warn(
                f"Azure CLI credentials are currently unavailable for git-credential/{self.owner_name}; "
                "the target user must install 'az' on PATH and authenticate it as the intended identity"
            )
        else:
            output.warn(
                f"Azure CLI credentials are currently unavailable for git-credential/{self.owner_name}; "
                "the target user must authenticate 'az' as the intended identity"
            )

    def _verify_token(self, token: str, *, secret_name: str) -> None:
        import base64

        from agentworks import output

        basic = base64.b64encode(f":{token}".encode()).decode()
        result = self._probe_static_credential(
            f"https://dev.azure.com/{self.config.org}/_apis/connectionData",
            {
                "Authorization": f"Basic {basic}",
                "Accept": "application/json",
                "User-Agent": "agentworks",
            },
            secret_name=secret_name,
            reject_statuses=(401, 203),
            host_label="Azure DevOps",
        )
        if result is not None:
            output.detail(f"Verified git token for git-credential/{self.owner_name}")

    def credential_material(self, ctx: RunContext) -> CredentialPayload:
        source = self.config.source
        if isinstance(source, AzDOSecretSource):
            password = self._secret_input(ctx, source)
            payload: StoredCredential | ManagedHelper = StoredCredential(self.config.org, password)
        else:
            payload = ManagedHelper(_azure_cli_helper(self.config.org), _AZ_FAILURE_HINT)
        return payload

    def review_remote(self, url: str) -> list[str]:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != "dev.azure.com":
            return []
        if parts.username and parts.username != self.config.org:
            return [
                f"the git remote {url!r} embeds username {parts.username!r}, not the "
                f"configured {self.config.org!r} organization; use a plain HTTPS remote "
                "or the configured organization as its username"
            ]
        return []
