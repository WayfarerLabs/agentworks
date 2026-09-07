"""Facet-specific native setup invocations, independent of session construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import VMRow
    from agentworks.harness_setup.model import NativeClaim, SetupRecord
    from agentworks.transports import Transport


@dataclass(frozen=True, kw_only=True)
class SetupInvocation:
    """Core supplies execution and acknowledges each confirmed native mutation.

    Checkpoint the complete remaining claim set after each mutation. Returning
    from the callback means persistence succeeded; an exception stops further
    writes. Initial creation may buffer until the owning row is inserted.
    """

    vm: VMRow
    runner: Transport
    prior: SetupRecord | None
    checkpoint: Callable[[tuple[NativeClaim, ...]], None]


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
