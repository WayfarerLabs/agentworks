"""Read Azure Key Vault secrets through operation-local SDK clients."""

from __future__ import annotations

import concurrent.futures
import logging
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from agentworks.capabilities.secret_backend import (
    BackendFailed,
    BackendMissing,
    BackendPreview,
    BackendResolution,
    BackendResolved,
    FailureReason,
    IndeterminateReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    OperatorImpact,
    PreviewAvailable,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewIntent,
    PreviewMissing,
    ResolutionIntent,
    SecretBackend,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.errors import ConfigError, StateError, UserAbort
from agentworks.plugins.azure.keyvault_config import (
    AzureKeyVaultMapping,
    AzureKeyVaultSourceConfig,
    vault_authority,
)
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from types import TracebackType

    from azure.keyvault.secrets import SecretClient
    from pydantic import BaseModel

    from agentworks.resources.graph import Readiness
    from agentworks.schema import AgwModel, AgwRootModel


@contextmanager
def _quiet_provider_logging() -> Iterator[None]:
    """Contain Azure/MSAL diagnostics during this synchronous provider operation.

    HTTP logging is also disabled per client. Identity emits independent
    credential failure logs, and caller-installed child handlers must not
    expose those diagnostics even when Agentworks debug mode is enabled.
    """
    names = {"azure", "msal", "msal_extensions"}
    names.update(
        name for name in logging.Logger.manager.loggerDict if name.startswith(("azure.", "msal.", "msal_extensions."))
    )
    saved: list[tuple[logging.Logger, list[logging.Handler], bool]] = []
    for name in names:
        logger = logging.getLogger(name)
        saved.append((logger, logger.handlers, logger.propagate))
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    try:
        yield
    finally:
        for logger, handlers, propagate in saved:
            logger.handlers = handlers
            logger.propagate = propagate


def _provider_failure(exc: Exception) -> BackendFailed | BackendMissing:
    """Project structured provider input without retaining diagnostic text."""
    from azure.core.exceptions import (
        ClientAuthenticationError,
        HttpResponseError,
        ServiceRequestError,
        ServiceResponseError,
    )

    if isinstance(exc, ClientAuthenticationError):
        return BackendFailed(FailureReason.AUTHENTICATION)
    if isinstance(exc, (ServiceRequestError, ServiceResponseError)):
        return BackendFailed(FailureReason.CONNECTIVITY)
    if isinstance(exc, HttpResponseError):
        code = exc.error.code if exc.error is not None else None
        if exc.status_code == 404 and code == "SecretNotFound":
            return BackendMissing()
        if exc.status_code == 401:
            return BackendFailed(FailureReason.AUTHENTICATION)
        if exc.status_code in (400, 403, 404):
            return BackendFailed(FailureReason.LOOKUP_REJECTED)
    return BackendFailed(FailureReason.EXTERNAL)


class _AzureKeyVaultClient:
    def __init__(self, config: AzureKeyVaultSourceConfig, intent: SecretClientIntent) -> None:
        self._config = config
        self._intent = intent
        self._resources = ExitStack()
        self._sdk_client: SecretClient | None = None

    def _client(self) -> SecretClient:
        """Open provider resources only after the selected method starts."""
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        if self._sdk_client is None:
            allow_impact = not isinstance(self._intent, PreviewIntent) or self._intent.impact is OperatorImpact.ALLOW
            credential = DefaultAzureCredential(
                authority=vault_authority(self._config.vault_url),
                exclude_interactive_browser_credential=not allow_impact,
                exclude_visual_studio_code_credential=not allow_impact,
                exclude_broker_credential=not allow_impact,
                process_timeout=10,
                connection_timeout=10,
                read_timeout=30,
                retry_total=2,
                logging_enable=False,
            )
            self._resources.callback(credential.close)
            self._sdk_client = SecretClient(
                self._config.vault_url,
                credential,
                verify_challenge_resource=True,
                connection_timeout=10,
                read_timeout=30,
                retry_total=2,
                logging_enable=False,
            )
            self._resources.callback(self._sdk_client.close)
        return self._sdk_client

    def _read(self, request: SecretLookupRequest) -> BackendFailed | BackendMissing | BackendResolved:
        mapping = request.mapping
        if not isinstance(mapping, AzureKeyVaultMapping):
            return BackendFailed(FailureReason.INVALID_MAPPING)
        try:
            value = self._client().get_secret(mapping.root.name, mapping.root.version).value
        except (UserAbort, concurrent.futures.CancelledError):
            raise
        except Exception as exc:
            return _provider_failure(exc)
        if type(value) is not str or "\0" in value:
            return BackendFailed(FailureReason.MALFORMED_VALUE)
        return BackendResolved(value)

    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        if not isinstance(self._intent, PreviewIntent):
            raise StateError("resolution client cannot perform preview")
        results: dict[str, BackendPreview] = {}
        with _quiet_provider_logging():
            for request in requests:
                result = self._read(request)
                if isinstance(result, BackendResolved):
                    results[request.name] = PreviewAvailable()
                elif isinstance(result, BackendMissing):
                    results[request.name] = PreviewMissing()
                else:
                    if self._intent.impact is OperatorImpact.NONE and result.reason is FailureReason.AUTHENTICATION:
                        results[request.name] = PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)
                    else:
                        results[request.name] = PreviewFailed(result.reason)
        return results

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        if not isinstance(self._intent, ResolutionIntent):
            raise StateError("preview client cannot perform resolution")
        with _quiet_provider_logging():
            return {request.name: self._read(request) for request in requests}

    def close(self) -> None:
        with _quiet_provider_logging():
            self._resources.close()


class _AzureKeyVaultContext(AbstractContextManager[SecretSourceClient]):
    def __init__(self, config: AzureKeyVaultSourceConfig, intent: SecretClientIntent) -> None:
        self._config = config
        self._intent = intent
        self._client: _AzureKeyVaultClient | None = None

    def __enter__(self) -> SecretSourceClient:
        self._client = _AzureKeyVaultClient(self._config, self._intent)
        return self._client

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._client is not None:
            self._client.close()


class AzureKeyVaultBackend(SecretBackend):
    """Resolve declared secrets from Azure Key Vault."""

    name: ClassVar[str] = "azure-keyvault"
    description: ClassVar[str] = "reads Azure Key Vault secrets using Azure credentials"
    contract_version: ClassVar[int] = 3
    supports_tty_interaction: ClassVar[bool] = False
    config_model: ClassVar[type[AgwModel]] = AzureKeyVaultSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = AzureKeyVaultMapping
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Azure Key Vault",
        overview="""
        Reads declared secrets from a configured vault. Each mapping selects an
        Azure secret name and optionally a version. Authentication belongs to
        the source. Preview never returns the secret value.

        See Microsoft's Azure Key Vault and Azure Identity documentation for
        vault permissions and provider authentication setup.
        """,
    )

    @classmethod
    def backend_readiness(cls) -> Readiness:
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        if not isinstance(mapping, AzureKeyVaultMapping):
            return LookupDescription(LookupDisposition.NOT_APPLICABLE, None)
        identifier = mapping.root.name
        if mapping.root.version is not None:
            identifier += "/" + mapping.root.version
        return LookupDescription(LookupDisposition.CANDIDATE, identifier)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        if not isinstance(config, AzureKeyVaultSourceConfig):
            raise ConfigError("azure-keyvault received the wrong source config model")
        return _AzureKeyVaultContext(config, intent)
