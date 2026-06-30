"""Framework strategies for the catalog kinds: ``apt_package``,
``system_install_command``, ``user_install_command``.

All three use the **error miss policy**: a typo in
``[vm_templates.*].apt_packages = ["..."]`` etc. surfaces as a
framework miss-policy error at ``build_registry`` time, citing the
reference's source. There is no auto-declare path -- catalog
entries are operator-declared in code (the built-in catalog ships with
the package) or operator-declared in the operator's TOML, and
references must resolve to a known name.

``apt_source`` is deliberately NOT a framework kind: it's an internal
cross-reference inside the catalog (validated by
``catalog._validate_references``), not directly referenced by any
operator-facing config field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference


def _synthesize_no_default(kind: str, requirements: Sequence[ResourceReference]) -> Any:
    """Shared synthesize body for the catalog kinds. Unreachable under
    the ``error`` miss policy (Registry.finalize raises ConfigError
    before dispatching to synthesize for error-policy kinds). Honors
    the Phase 2a empty-requirements contract by raising the typed
    framework error so a hypothetical future change that gives a
    catalog kind a reserved default has an obvious landing pad.
    """
    if not requirements:
        raise NoUnreferencedDefaultError(
            f"the {kind} kind has no reserved default name; "
            f"synthesize is never invoked under the error miss policy"
        )
    raise NoUnreferencedDefaultError(
        f"the {kind} kind has miss_policy='error'; synthesize should "
        f"never be invoked (the framework raises ConfigError first)"
    )


@dataclass(frozen=True)
class _AptPackageKind:
    """Implementation of ``ResourceKind`` for ``"apt_package"``."""

    kind: str = "apt_package"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None

    def synthesize(self, requirements: Sequence[ResourceReference]) -> Any:
        return _synthesize_no_default(self.kind, requirements)


@dataclass(frozen=True)
class _SystemInstallCommandKind:
    """Implementation of ``ResourceKind`` for ``"system_install_command"``."""

    kind: str = "system_install_command"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None

    def synthesize(self, requirements: Sequence[ResourceReference]) -> Any:
        return _synthesize_no_default(self.kind, requirements)


@dataclass(frozen=True)
class _UserInstallCommandKind:
    """Implementation of ``ResourceKind`` for ``"user_install_command"``."""

    kind: str = "user_install_command"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None

    def synthesize(self, requirements: Sequence[ResourceReference]) -> Any:
        return _synthesize_no_default(self.kind, requirements)


KIND_REGISTRY["apt_package"] = _AptPackageKind()
KIND_REGISTRY["system_install_command"] = _SystemInstallCommandKind()
KIND_REGISTRY["user_install_command"] = _UserInstallCommandKind()
