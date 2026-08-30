"""Structured Debian upgrade preflight and fail-closed safety checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PreflightIssue(StrEnum):
    """Machine-checkable reasons an upgrade may not begin."""

    RELEASE_MISMATCH = "release-mismatch"
    UNSUPPORTED_ARCHITECTURE = "unsupported-architecture"
    SESSION_NOT_QUIESCENT = "session-not-quiescent"
    PACKAGE_DATABASE_BROKEN = "package-database-broken"
    APT_SIMULATION_FAILED = "apt-simulation-failed"
    PACKAGE_MANAGER_BUSY = "package-manager-busy"
    AUTOMATIC_APT_TIMER_STATE = "automatic-apt-timer-state"
    HELD_PACKAGES = "held-packages"
    KERNEL_METAPACKAGE_MISSING = "kernel-metapackage-missing"
    OPENSSH_TOO_OLD = "openssh-too-old"
    MODIFIED_CONFFILES = "modified-conffiles"
    APT_PINNING = "apt-pinning"
    MIXED_SUITES = "mixed-suites"
    RELEASE_BLOCKER = "release-blocker"
    BOOT_SPACE_LOW = "boot-space-low"
    ROOT_SPACE_LOW = "root-space-low"
    CACHE_SPACE_LOW = "cache-space-low"


@dataclass(frozen=True, slots=True)
class UpgradePreflight:
    """Read-only facts used for both preliminary and final planning."""

    database_release: str
    live_release: str
    architecture: str
    kernel: str
    dpkg_audit: tuple[str, ...] = ()
    held_packages: tuple[str, ...] = ()
    kernel_metapackage: str | None = None
    openssh_minimum_satisfied: bool = True
    package_manager_owner: str | None = None
    non_quiescent_sessions: tuple[str, ...] = ()
    modified_conffiles: tuple[str, ...] = ()
    release_blockers: tuple[str, ...] = ()
    apt_pins: tuple[str, ...] = ()
    mixed_suites: tuple[str, ...] = ()
    third_party_sources: tuple[str, ...] = ()
    non_debian_packages: tuple[str, ...] = ()
    obsolete_packages: tuple[str, ...] = ()
    removals: tuple[str, ...] = ()
    boot_free_bytes: int | None = None
    boot_required_bytes: int = 0
    root_free_bytes: int = 0
    root_required_bytes: int = 0
    cache_free_bytes: int = 0
    cache_required_bytes: int = 0
    apt_timer_states: dict[str, tuple[str, str]] = field(default_factory=dict)
    extra_issues: tuple[PreflightIssue, ...] = field(default_factory=tuple)

    def issues(self, *, expected_source: str, supported_architectures: frozenset[str]) -> tuple[PreflightIssue, ...]:
        """Return deterministic blockers without mutating the guest."""
        found = list(self.extra_issues)
        if self.database_release != self.live_release or self.live_release != expected_source:
            found.append(PreflightIssue.RELEASE_MISMATCH)
        if self.architecture not in supported_architectures:
            found.append(PreflightIssue.UNSUPPORTED_ARCHITECTURE)
        if self.non_quiescent_sessions:
            found.append(PreflightIssue.SESSION_NOT_QUIESCENT)
        if self.dpkg_audit:
            found.append(PreflightIssue.PACKAGE_DATABASE_BROKEN)
        if self.package_manager_owner is not None:
            found.append(PreflightIssue.PACKAGE_MANAGER_BUSY)
        timer_enable_states = {
            "enabled",
            "enabled-runtime",
            "disabled",
            "static",
            "indirect",
            "generated",
            "alias",
            "linked",
            "linked-runtime",
            "masked",
            "masked-runtime",
        }
        if any(
            enabled not in timer_enable_states or active not in {"active", "inactive"}
            for enabled, active in self.apt_timer_states.values()
        ) or any(
            enabled in {"masked", "masked-runtime"} and active == "active"
            for enabled, active in self.apt_timer_states.values()
        ):
            found.append(PreflightIssue.AUTOMATIC_APT_TIMER_STATE)
        if self.held_packages:
            found.append(PreflightIssue.HELD_PACKAGES)
        if self.kernel_metapackage is None:
            found.append(PreflightIssue.KERNEL_METAPACKAGE_MISSING)
        if not self.openssh_minimum_satisfied:
            found.append(PreflightIssue.OPENSSH_TOO_OLD)
        if self.modified_conffiles:
            found.append(PreflightIssue.MODIFIED_CONFFILES)
        if self.apt_pins:
            found.append(PreflightIssue.APT_PINNING)
        if self.mixed_suites:
            found.append(PreflightIssue.MIXED_SUITES)
        if self.release_blockers:
            found.append(PreflightIssue.RELEASE_BLOCKER)
        if self.boot_free_bytes is not None and self.boot_free_bytes < self.boot_required_bytes:
            found.append(PreflightIssue.BOOT_SPACE_LOW)
        if self.root_free_bytes < self.root_required_bytes:
            found.append(PreflightIssue.ROOT_SPACE_LOW)
        if self.cache_free_bytes < self.cache_required_bytes:
            found.append(PreflightIssue.CACHE_SPACE_LOW)
        return tuple(dict.fromkeys(found))

    def material_fingerprint(self) -> tuple[object, ...]:
        """The safety facts for which preliminary consent cannot be reused."""
        return (
            self.dpkg_audit,
            self.held_packages,
            self.modified_conffiles,
            self.release_blockers,
            self.apt_pins,
            self.mixed_suites,
            self.third_party_sources,
            self.non_debian_packages,
            self.obsolete_packages,
            self.removals,
            self.boot_free_bytes,
            self.root_free_bytes,
            self.cache_free_bytes,
            self.cache_required_bytes,
        )

    def to_plan(self) -> dict[str, object]:
        return {
            "database_release": self.database_release,
            "live_release": self.live_release,
            "architecture": self.architecture,
            "kernel": self.kernel,
            "dpkg_audit": list(self.dpkg_audit),
            "held_packages": list(self.held_packages),
            "kernel_metapackage": self.kernel_metapackage,
            "apt_pins": list(self.apt_pins),
            "mixed_suites": list(self.mixed_suites),
            "third_party_sources": list(self.third_party_sources),
            "non_debian_packages": list(self.non_debian_packages),
            "obsolete_packages": list(self.obsolete_packages),
            "modified_conffiles": list(self.modified_conffiles),
            "release_blockers": list(self.release_blockers),
            "removals": list(self.removals),
            "boot_free_bytes": self.boot_free_bytes,
            "root_free_bytes": self.root_free_bytes,
            "cache_free_bytes": self.cache_free_bytes,
            "cache_required_bytes": self.cache_required_bytes,
            "apt_timer_states": {name: list(state) for name, state in self.apt_timer_states.items()},
        }
