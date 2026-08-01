"""Git-credential-domain node implementation.

The ``git-credential`` consuming-resource node applies the translation
rule to its DECLARED resource: the decl's provider reference becomes
the HELD instance (constructed, not edged; the holder composes its
readiness with the thin one-line fan-in), and the decl's
``secret``-kind references become the node's ``secret_refs``. Moved
here from ``vms/nodes.py`` once a second domain (agents) consumed it:
domains implement their own nodes, and the credential node belongs to
the git-credentials domain, not to any one consumer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.git_credential.base import GitCredentialProvider
    from agentworks.orchestration.node import Node
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry


class GitCredentialNode:
    """The ``git-credential`` consuming-resource node: holds its
    provider instance, composes its readiness (the thin one-line
    fan-in), and folds the instance's declared secrets into its own
    ``secret_refs``. Built by :func:`git_credential_node`.
    """

    def __init__(
        self,
        name: str,
        provider: GitCredentialProvider,
        secret_refs: tuple[ResourceReference, ...],
        registry: Registry,
    ) -> None:
        self._name = name
        self._provider = provider
        self._secret_refs = secret_refs
        self._registry = registry

    @property
    def key(self) -> str:
        return f"git-credential/{self._name}"

    @property
    def provider(self) -> GitCredentialProvider:
        """The held instance, for the orchestrator's domain ops
        (``helper_entry`` / ``credential_lines``). Ops stay
        un-unified; holding is not hiding."""
        return self._provider

    def deps(self) -> tuple[Node, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        return tuple(ref.name for ref in self._secret_refs)

    def config_secret_refs(self) -> tuple[ResourceReference, ...]:
        # The provider's token secret, as declared. The preflight sweep
        # predicts resolvability over these; the credential itself does
        # not, because how a secret gets a value is the operation's
        # concern and not the credential's.
        return self._secret_refs

    def preflight(self, ctx: RunContext) -> None:
        # The credential checks that its own declarations are INTACT
        # (the names its config named reach real registry rows), which
        # is registry consistency and legitimately its concern; then the
        # held instance's own world checks.
        from agentworks.orchestration.secrets import require_declared_refs

        require_declared_refs(self.key, self._secret_refs, self._registry)
        self._provider.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        self._provider.runup(ctx)


def git_credential_node(registry: Registry, name: str) -> GitCredentialNode:
    """Build the ``git-credential/<name>`` node from its DECLARED
    resource: the decl's provider reference becomes the held instance
    (constructed, not edged), and its ``secret``-kind references become
    the node's ``secret_refs``.
    """
    from agentworks.resources.access import git_credential
    from agentworks.vms.initializer import resolve_git_credential_providers

    decl = git_credential(registry, name)
    if decl is None:
        raise NotFoundError(
            f"git credential '{name}' not found in config",
            entity_kind="git-credential",
            entity_name=name,
        )
    provider = resolve_git_credential_providers(registry, [name])[name]
    # Read the credential's config-implied secret edges off the retained graph
    # (the single access path, R11) rather than re-walking the decl.
    secret_refs = tuple(ref for ref in registry.graph.edges_of("git-credential", name) if ref.kind == "secret")
    return GitCredentialNode(name, provider, secret_refs, registry)
