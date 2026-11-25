"""Source and lookup declarations for Azure Key Vault secrets."""

from __future__ import annotations

import re
from typing import Annotated, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field

from agentworks.schema import AgwModel, AgwRootModel, ScalarShorthand

_VAULT_AUTHORITIES = {
    "vault.azure.net": "login.microsoftonline.com",
    "vault.usgovcloudapi.net": "login.microsoftonline.us",
    "vault.azure.cn": "login.chinacloudapi.cn",
}
_VAULT_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]")


def _validate_vault_url(value: str) -> str:
    """Validate an operator-authored vault endpoint before credentials can reach it."""
    parts = urlsplit(value)
    hostname = parts.hostname or ""
    vault_name, _, suffix = hostname.partition(".")
    if (
        parts.scheme != "https"
        or parts.netloc != hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or suffix not in _VAULT_AUTHORITIES
        or _VAULT_NAME.fullmatch(vault_name) is None
        or "--" in vault_name
    ):
        raise ValueError("expected an HTTPS Azure Key Vault vault endpoint")
    return value.rstrip("/")


VaultUrl = Annotated[str, AfterValidator(_validate_vault_url)]
SecretName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9-]{1,127}$")]
SecretVersion = Annotated[str, Field(pattern=r"^[a-fA-F0-9]{32}$")]


class AzureKeyVaultAmbientAuth(AgwModel):
    """Use Azure's default credential chain."""

    mode: Literal["ambient"]


class AzureKeyVaultSourceConfig(AgwModel):
    """One vault and the identity used to read it."""

    name: Literal["azure-keyvault"]
    vault_url: VaultUrl = Field(examples=["https://example-vault.vault.azure.net"])
    """The vault's HTTPS DNS endpoint."""
    auth: AzureKeyVaultAmbientAuth = AzureKeyVaultAmbientAuth(mode="ambient")
    """Authentication for this source; defaults to the ambient credential chain."""


class AzureKeyVaultLookup(AgwModel):
    """The Azure secret to read, optionally pinned to one version."""

    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="name")

    name: SecretName = Field(examples=["npm-token"])
    """The Azure secret name; a bare string means this field."""
    version: SecretVersion | None = None
    """The Azure version identifier; omitted means the latest version."""


class AzureKeyVaultMapping(AgwRootModel[AzureKeyVaultLookup]):
    """A secret name or a table with name and optional version."""


def vault_authority(vault_url: str) -> str:
    """Select the identity authority for an already validated vault endpoint."""
    suffix = (urlsplit(vault_url).hostname or "").partition(".")[2]
    return _VAULT_AUTHORITIES[suffix]
