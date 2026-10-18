"""One access-local Azure credential, retained before selected token work."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.errors import AgentworksError, LimitExceededError, StateError
from agentworks.plugins.azure.config import AzureAmbientAuth
from agentworks.plugins.azure.network import AzureError

if TYPE_CHECKING:
    from azure.identity import ClientSecretCredential, DefaultAzureCredential, InteractiveBrowserCredential

    from agentworks.capabilities.base import RunContext
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.azure.config import AzureAuth, AzureServicePrincipalAuth

_ARM_SCOPE = "https://management.azure.com/.default"


class _OwnedAzureCredential:
    """Selected credential custody, serialized by the retaining access's lock.

    One failed attempt permanently refuses authentication on this helper. Only
    terminal public-close retry remains. A normally held ready credential is a
    dependency, not uncertain per-read retirement. Public close does not prove
    drainage of SDK-internal processes, browser work or every socket.
    """

    def __init__(self, auth: AzureAuth, ctx: RunContext, site_name: str, vm_name: str) -> None:
        self._auth = auth
        self._ctx = ctx
        self._site_name = site_name
        self._vm_name = vm_name
        self._credential: ClientSecretCredential | DefaultAzureCredential | InteractiveBrowserCredential | None = None
        self._failed = False

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def has_credential(self) -> bool:
        return self._credential is not None

    @property
    def cleanup_incomplete(self) -> bool:
        return self._failed and self.has_credential

    def credential(self, deadline: Deadline) -> object:
        provider_locator_remaining(deadline, vm_name=self._vm_name)
        if self._failed:
            raise self._refusal()
        if self._credential is not None:
            return self._credential
        # Fail closed before any lazy constructor, secret lookup or token work.
        self._failed = True
        failure_kind: str | None = None
        try:
            if isinstance(self._auth, AzureAmbientAuth):
                self._select_ambient(deadline)
            else:
                self._select_principal(self._auth, deadline)
            provider_locator_remaining(deadline, vm_name=self._vm_name)
        except AgentworksError:
            raise
        except Exception as exc:
            failure_kind = type(exc).__name__
        if failure_kind is not None:
            # Raise outside the handler so persisted tracebacks carry no SDK
            # exception context containing credential inputs.
            raise AzureError(
                f"could not authenticate the Azure credential for vm-site '{self._site_name}'",
                detail=failure_kind,
                entity_kind="vm-site",
                entity_name=self._site_name,
            )
        self._failed = False
        assert self._credential is not None
        return self._credential

    def _select_ambient(self, deadline: Deadline) -> None:
        from azure.core.exceptions import ClientAuthenticationError
        from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

        remaining = provider_locator_remaining(deadline, vm_name=self._vm_name)
        self._credential = DefaultAzureCredential(
            process_timeout=min(10, self._integer_timeout(remaining)),
            connection_timeout=remaining,
            read_timeout=remaining,
        )
        provider_locator_remaining(deadline, vm_name=self._vm_name)
        try:
            self._credential.get_token(_ARM_SCOPE)
        except ClientAuthenticationError:
            pass
        else:
            return
        # Fallback work is outside the SDK exception handler: its errors and
        # original controls must not inherit a credential-bearing SDK context.
        provider_locator_remaining(deadline, vm_name=self._vm_name)
        if not self._retire():
            raise self._refusal()
        remaining = provider_locator_remaining(deadline, vm_name=self._vm_name)
        self._integer_timeout(remaining)
        output.info("No Azure credentials found, opening browser for login...")
        remaining = provider_locator_remaining(deadline, vm_name=self._vm_name)
        timeout = self._integer_timeout(remaining)
        self._credential = InteractiveBrowserCredential(
            timeout=timeout, connection_timeout=remaining, read_timeout=remaining
        )

    def _select_principal(self, auth: AzureServicePrincipalAuth, deadline: Deadline) -> None:
        from azure.core.exceptions import ClientAuthenticationError
        from azure.identity import ClientSecretCredential

        client_secret = self._ctx.secret(auth.secret)
        remaining = provider_locator_remaining(deadline, vm_name=self._vm_name)
        failure_kind: str | None = None
        try:
            self._credential = ClientSecretCredential(
                auth.tenant_id,
                auth.client_id,
                client_secret,
                connection_timeout=remaining,
                read_timeout=remaining,
            )
            provider_locator_remaining(deadline, vm_name=self._vm_name)
            self._credential.get_token(_ARM_SCOPE)
        except (ClientAuthenticationError, ValueError) as exc:
            failure_kind = type(exc).__name__
        if failure_kind is not None:
            raise AzureError(
                f"could not authenticate the Azure service principal for "
                f"vm-site '{self._site_name}' (client {auth.client_id} in tenant "
                f"{auth.tenant_id}, secret '{auth.secret}')",
                detail=failure_kind,
                entity_kind="vm-site",
                entity_name=self._site_name,
                hint=(
                    f"Check auth.tenant_id / auth.client_id and the value of the '{auth.secret}' secret "
                    "(an expired client secret is the usual cause; `az ad app credential list` shows expiry). "
                    "An unreachable Entra ID token service also produces an authentication failure."
                ),
            )

    def _integer_timeout(self, remaining: float) -> int:
        seconds = math.floor(remaining)
        if seconds < 1:
            raise LimitExceededError(
                "Azure credential timeout cannot represent the remaining deadline",
                entity_kind="vm",
                entity_name=self._vm_name,
            )
        return seconds

    def _refusal(self) -> StateError:
        return StateError(
            "Azure credential selection cannot admit further work",
            entity_kind="vm",
            entity_name=self._vm_name,
        )

    def _retire(self) -> bool:
        if self._credential is not None:
            try:
                self._credential.close()
            except Exception:
                return False
            else:
                self._credential = None
        return True

    def close(self) -> bool:
        """Stop authentication and retry only the same original's public close."""
        self._failed = True
        return self._retire()
