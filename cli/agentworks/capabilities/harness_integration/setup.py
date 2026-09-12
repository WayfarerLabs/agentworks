"""Facet-specific native setup invocations, independent of session construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.db import VMRow
    from agentworks.harness_setup.model import NativeClaim, SetupFacet, SetupRecord
    from agentworks.transports import Transport


@dataclass(frozen=True, kw_only=True)
class SetupInvocation:
    """Core supplies execution and acknowledges confirmed ownership changes.

    Checkpoint the complete remaining claim set after each ownership change. Returning
    from the callback means persistence succeeded; an exception stops further
    writes. Initial creation may buffer until the owning row is inserted.
    """

    vm: VMRow
    runner: Transport
    prior: SetupRecord | None
    checkpoint: Callable[[tuple[NativeClaim, ...]], None]
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)


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
    runner: Transport
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
