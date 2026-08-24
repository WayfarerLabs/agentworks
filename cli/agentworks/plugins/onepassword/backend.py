"""Resolve and preview 1Password references through the bounded ``op`` CLI."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, Field

from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.capabilities.secret_backend.client import (
    BackendFailed,
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
    ResolutionIntent,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.errors import ConfigError, StateError
from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.graph import Readiness


_OP_BINARY = "op"
_MONOTONIC = time.monotonic

_SIGNED_OUT_MARKERS = (
    "not currently signed in",
    "no account found",
    "session expired",
    "account is not signed in",
)
_NOT_FOUND_MARKERS = (
    "isn't an item",
    "isn't a field",
    "no such item",
    "no such field",
)
_CONNECTIVITY_MARKERS = (
    "connection refused",
    "network is unreachable",
    "temporary failure in name resolution",
    "dial tcp",
)


class AppAuthenticationImpact(StrEnum):
    """How app authentication counts for zero-impact preview."""

    OPERATOR_ACTION = "operator-action"
    NONE = "none"


@dataclass(frozen=True, slots=True, repr=False)
class _BoundedRead:
    value: str | None = None
    failure: FailureReason | None = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.failure is None):
            raise ValueError("onepassword read must carry exactly one outcome")

    def __repr__(self) -> str:
        return "_BoundedRead(value=<redacted>)"


def _bounded_read(args: list[str], *, timeout: float) -> _BoundedRead:
    """Run one bounded read and discard provider-native failure text."""
    if timeout <= 0:
        return _BoundedRead(failure=FailureReason.DEADLINE_EXCEEDED)
    try:
        completed = subprocess.run(  # noqa: S603 - explicit argv, no shell
            [_OP_BINARY, *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _BoundedRead(failure=FailureReason.DEADLINE_EXCEEDED)
    except OSError:
        return _BoundedRead(failure=FailureReason.CONNECTIVITY)

    if completed.returncode == 0:
        if "\0" in completed.stdout:
            return _BoundedRead(failure=FailureReason.MALFORMED_VALUE)
        return _BoundedRead(value=completed.stdout)

    stderr = completed.stderr.lower()
    if any(marker in stderr for marker in _SIGNED_OUT_MARKERS):
        failure = FailureReason.AUTHENTICATION
    elif any(marker in stderr for marker in _NOT_FOUND_MARKERS):
        failure = FailureReason.LOOKUP_REJECTED
    elif any(marker in stderr for marker in _CONNECTIVITY_MARKERS):
        failure = FailureReason.CONNECTIVITY
    else:
        failure = FailureReason.EXTERNAL
    return _BoundedRead(failure=failure)


def _check_op_uri(uri: str) -> str:
    """Reject a locally malformed ``op://vault/item/field`` reference."""
    prefix = "op://"
    if not uri.startswith(prefix):
        raise ValueError("onepassword reference must start with 'op://'")
    path = uri[len(prefix) :].split("?", 1)[0]
    segments = path.split("/")
    if len(segments) < 3 or not all(segments[:3]):
        raise ValueError("onepassword reference requires non-empty vault, item, and field segments")
    return uri


OpUri = Annotated[NonEmptyStr, AfterValidator(_check_op_uri)]


class OnePasswordSourceConfig(AgwModel):
    """Shared config for one 1Password source."""

    name: Literal["onepassword"]
    account: NonEmptyStr | None = None
    """Optional 1Password account shorthand passed to ``op read``."""
    timeout: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    """Maximum seconds for the complete source turn."""
    app_authentication_impact: AppAuthenticationImpact = AppAuthenticationImpact.OPERATOR_ACTION
    """Whether app authentication counts as operator action during preview."""


class OnePasswordMapping(AgwRootModel[OpUri]):
    """One native ``op://`` lookup address."""


def _known_unattended_authentication() -> bool:
    """Return whether ambient provider facts prove a non-app auth mode."""
    if os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        return True
    return bool(os.environ.get("OP_CONNECT_HOST") and os.environ.get("OP_CONNECT_TOKEN"))


class _OnePasswordClient:
    def __init__(
        self,
        *,
        config: OnePasswordSourceConfig,
        intent: SecretClientIntent,
    ) -> None:
        self._config = config
        self._intent = intent
        self._deadline = _MONOTONIC() + config.timeout

    def _read(self, request: SecretLookupRequest) -> _BoundedRead:
        mapping = request.mapping
        if not isinstance(mapping, OnePasswordMapping):
            return _BoundedRead(failure=FailureReason.INVALID_MAPPING)
        args = ["read", "--no-newline"]
        if self._config.account is not None:
            args += ["--account", self._config.account]
        args.append(mapping.root)
        timeout = max(0.0, self._deadline - _MONOTONIC())
        return _bounded_read(args, timeout=timeout)

    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        if not isinstance(self._intent, PreviewIntent):
            raise StateError("resolution client cannot perform preview")
        if (
            self._intent.impact is OperatorImpact.NONE
            and self._config.app_authentication_impact is AppAuthenticationImpact.OPERATOR_ACTION
            and not _known_unattended_authentication()
        ):
            return {
                request.name: PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED) for request in requests
            }
        results: dict[str, BackendPreview] = {}
        for request in requests:
            read = self._read(request)
            if read.value is not None:
                results[request.name] = PreviewAvailable()
            else:
                assert read.failure is not None
                results[request.name] = PreviewFailed(read.failure)
        return results

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        if not isinstance(self._intent, ResolutionIntent):
            raise StateError("preview client cannot perform resolution")
        results: dict[str, BackendResolution] = {}
        for request in requests:
            read = self._read(request)
            if read.value is not None:
                results[request.name] = BackendResolved(read.value)
            else:
                assert read.failure is not None
                results[request.name] = BackendFailed(read.failure)
        return results


class _OnePasswordContext(AbstractContextManager[SecretSourceClient]):
    def __init__(
        self,
        *,
        config: OnePasswordSourceConfig,
        intent: SecretClientIntent,
    ) -> None:
        self._config = config
        self._intent = intent

    def __enter__(self) -> SecretSourceClient:
        return _OnePasswordClient(
            config=self._config,
            intent=self._intent,
        )

    def __exit__(self, *args: object) -> None:
        return None


class OnePasswordBackend(SecretBackend):
    """Resolve secret values from 1Password via ``op read``."""

    contract_version: ClassVar[int] = 1
    config_model: ClassVar[type[AgwModel]] = OnePasswordSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = OnePasswordMapping
    name: ClassVar[str] = "onepassword"
    description: ClassVar[str] = "resolves via the 1Password CLI (op read op://vault/item/field)"
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="1Password",
        overview="""
        Reads a secret through the `op` CLI. Each secret needs an explicit
        `backend_mappings.<source-name>` reference. Account selection, timeout, and
        preview app-authentication impact belong to the source config.

        Actual resolution always attempts the bounded provider read. Global
        `--non-interactive` disables terminal input only and does not suppress app,
        biometric, browser, or device approval.
        """,
    )
    supports_tty_interaction: ClassVar[bool] = False

    @classmethod
    def backend_readiness(cls) -> Readiness:
        import shutil

        from agentworks.resources.graph import Readiness

        if shutil.which("op") is None:
            return Readiness.blocked("op CLI not installed")
        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        if not isinstance(mapping, OnePasswordMapping):
            return LookupDescription(LookupDisposition.NOT_APPLICABLE, None)
        return LookupDescription(LookupDisposition.CANDIDATE, mapping.root)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        if not isinstance(config, OnePasswordSourceConfig):
            raise ConfigError("onepassword received the wrong source config model")
        return _OnePasswordContext(config=config, intent=intent)
