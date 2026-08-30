"""Provider materialization and per-user Git credential reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agentworks import output
from agentworks.git_credentials.reconcile import reconcile_user_git_credentials
from agentworks.git_credentials.state import (
    UserCredentialState,
    build_user_credential_state,
    validate_credential_scope_claims,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.git_credential.base import (
        CredentialPayload,
        GitCredentialProvider,
        HttpsCredentialScope,
    )
    from agentworks.config import Config
    from agentworks.git_credentials.nodes import GitCredentialNode
    from agentworks.resources import Registry
    from agentworks.transports import Transport


class _WarnLogger(Protocol):
    def step(self, name: str) -> None: ...

    def warning(self, msg: str) -> None: ...


@dataclass(frozen=True)
class CredentialRequest:
    """One holding credential node and its scoped operation context."""

    node: GitCredentialNode
    context: RunContext
    scopes: tuple[HttpsCredentialScope, ...]

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def provider(self) -> GitCredentialProvider:
        return self.node.provider


def credential_requests(
    nodes: Iterable[GitCredentialNode],
    scoped_ctx: Callable[[tuple[str, ...]], RunContext],
) -> tuple[CredentialRequest, ...]:
    """Prepare static scopes and resolved inputs before target mutation."""
    requests = tuple(
        CredentialRequest(node, scoped_ctx(node.secret_refs()), node.provider.credential_scopes()) for node in nodes
    )
    validate_credential_scope_claims((request.name, request.scopes) for request in requests)
    for request in requests:
        request.provider.validate_inputs(request.context)
    return requests


def credential_redactions(
    nodes: Iterable[GitCredentialNode],
    resolved_values: Mapping[str, str],
) -> tuple[str, ...]:
    """Return declared input values solely for immutable log redaction."""
    names = dict.fromkeys(name for node in nodes for name in node.secret_refs())
    return tuple(resolved_values[name] for name in names)


def announce_git_credentials(nodes: Iterable[GitCredentialNode]) -> None:
    """Echo each Git credential participating in the operation preflight."""
    for node in nodes:
        output.info(f"Checking git-credential/{node.name}...")


def materialize_credential_state(
    requests: Iterable[CredentialRequest],
    config: Config,
    logger: _WarnLogger | None = None,
) -> UserCredentialState:
    """Run provider-owned runup/materialization and compile surviving output."""
    from agentworks.errors import TokenRejectedError

    materials: list[tuple[str, tuple[HttpsCredentialScope, ...], CredentialPayload]] = []
    for request in requests:
        if config.defaults.runup_git_credentials:
            output.detail(f"Performing runup test for git-credential/{request.name}...")
            try:
                request.node.runup(request.context)
            except TokenRejectedError as exc:
                msg = (
                    f"git credential {request.name!r} rejected; skipping it "
                    f"(fix its configured source and reinit): {exc}"
                )
                output.warn(msg)
                if logger is not None:
                    logger.warning(msg)
                continue
        materials.append(
            (
                request.name,
                request.scopes,
                request.provider.credential_material(request.context),
            )
        )
    return build_user_credential_state(materials)


def configure_user_git_credentials(
    target: Transport,
    requests: Iterable[CredentialRequest],
    config: Config,
    logger: _WarnLogger,
) -> None:
    """Materialize and unconditionally reconcile one target user's full state."""
    from agentworks.ssh import SSHError

    logger.step("Git credentials")
    output.info("Reconciling git credentials...")
    state = materialize_credential_state(requests, config, logger)
    try:
        reconcile_user_git_credentials(target, state)
    except SSHError as exc:
        msg = f"git credential reconciliation failed: {exc}"
        logger.warning(msg)
        output.warn(msg)
        return
    output.detail("Git credentials reconciled")


def remote_advisories(registry: Registry, url: str) -> list[str]:
    """Ask every ready declared provider to review one remote URL."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return []
    from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY

    seen: set[str] = set()
    advisories: list[str] = []
    for name, credential in registry.iter_kind_items("git-credential"):
        if not registry.graph.is_ready("git-credential", name):
            continue
        provider_class = GIT_CREDENTIAL_PROVIDER_REGISTRY.get(credential.provider.name)
        if provider_class is None:
            continue
        provider = provider_class(name, credential.provider.config, description=credential.description)
        for message in provider.review_remote(url):
            if message not in seen:
                seen.add(message)
                advisories.append(message)
    return advisories


__all__ = [
    "announce_git_credentials",
    "CredentialRequest",
    "UserCredentialState",
    "build_user_credential_state",
    "configure_user_git_credentials",
    "credential_redactions",
    "credential_requests",
    "materialize_credential_state",
    "reconcile_user_git_credentials",
    "remote_advisories",
]
