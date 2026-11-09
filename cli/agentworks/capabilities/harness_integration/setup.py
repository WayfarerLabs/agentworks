"""Facet-specific native setup invocations, independent of session construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agentworks import output
from agentworks.artifacts.model import ArtifactInputs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.db import VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.execution.files import Files
    from agentworks.harness_setup.model import NativeClaim, SetupFacet, SetupRecord
    from agentworks.transports import Transport


@dataclass(frozen=True, kw_only=True)
class SetupInvocation:
    """Core supplies execution and acknowledges confirmed ownership changes.

    ``access`` runs requests as the identity being set up (the admin for VM,
    admin and workspace setup, the agent for its own), and ``files`` is that
    access's file operations under setup's per-request bound; use it for
    every file operation. Checkpoint the complete
    remaining claim set after each ownership change. Returning from the
    callback means persistence succeeded; an exception stops further writes.
    Initial creation may buffer until the owning row is inserted. Report an
    entry that cannot converge without failing unrelated setup entries; core
    retains incomplete evidence and reports the owning repair operation.

    ``secrets`` holds only this integration's own config secrets. ``redactions``
    is every secret the operation resolved or its log registered, and always
    includes ``secrets``: sanitize any guest text an error or warning quotes
    with it (``probe_native`` takes it).
    """

    vm: VMRow
    access: VMAccess
    files: Files
    runner: Transport
    prior: SetupRecord | None
    checkpoint: Callable[[tuple[NativeClaim, ...]], None]
    report_incomplete: Callable[[str], None] = field(default=output.warn, repr=False)
    artifacts: ArtifactInputs = field(default_factory=ArtifactInputs)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)
    redactions: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        # Narrowing ``secrets`` for an integration never narrows what is redacted.
        object.__setattr__(self, "redactions", tuple(dict.fromkeys((*self.redactions, *self.secrets.values()))))

    def request_environment(self, local: Mapping[str, str] | None = None) -> dict[str, str]:
        """The ``env`` for a request: this setup's ``environment``, with ``local`` added over it.

        ``AGENTWORKS_*`` names carry core's identity values, so ``local`` can
        neither override nor add one: each keeps core's value or is absent.
        """
        overrides = {name: value for name, value in (local or {}).items() if not name.startswith("AGENTWORKS_")}
        return {**self.environment, **overrides}


@dataclass(frozen=True, kw_only=True)
class VMSetupInvocation(SetupInvocation):
    """System setup for this VM, without any descendant identities."""


@dataclass(frozen=True, kw_only=True)
class UserSetupInvocation(SetupInvocation):
    """One actual user, equally applicable to an admin or an agent."""

    username: str
    home: str


@dataclass(frozen=True, kw_only=True)
class WorkspaceSetupInvocation(SetupInvocation):
    """Project setup restricted to one workspace's native root."""

    workspace_name: str
    root: str
    linux_group: str


type SetupStatus = Literal["absent", "incomplete", "stale", "unavailable", "current"]


@dataclass(frozen=True)
class SetupEvidence:
    """Read-only current setup evidence for one applicable owning resource."""

    status: SetupStatus
    owner_kind: str
    owner_name: str
    remediation: str
    record: SetupRecord | None = None

    @property
    def current(self) -> bool:
        return self.status == "current"


@dataclass(frozen=True)
class SetupGap:
    """A consuming integration chooses severity and explains its prerequisite."""

    evidence: SetupEvidence
    severity: Literal["required", "recommended"]
    reason: str


@dataclass
class SetupReadiness:
    """Load only requested ancestor facts, cached for this readiness invocation."""

    lookup: Callable[[SetupFacet], SetupEvidence] = field(repr=False)
    _cache: dict[SetupFacet, SetupEvidence] = field(default_factory=dict, init=False, repr=False)

    def _get(self, facet: SetupFacet) -> SetupEvidence:
        if facet not in self._cache:
            self._cache[facet] = self.lookup(facet)
        return self._cache[facet]

    @property
    def vm(self) -> SetupEvidence:
        return self._get("vm")

    @property
    def user(self) -> SetupEvidence:
        return self._get("user")

    @property
    def workspace(self) -> SetupEvidence:
        return self._get("workspace")
