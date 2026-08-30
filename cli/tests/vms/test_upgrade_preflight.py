from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.errors import StateError
from agentworks.vms.upgrade.preflight import PreflightIssue, UpgradePreflight
from agentworks.vms.upgrade.probe import (
    _is_third_party,
    _lines,
    _mentions_other_debian_suite,
    target_source_hygiene_issues,
)


def _safe() -> UpgradePreflight:
    return UpgradePreflight(
        database_release="bookworm",
        live_release="bookworm",
        architecture="amd64",
        kernel="6.1.0",
        kernel_metapackage="linux-image-amd64",
        boot_filesystem="boot",
        boot_free_bytes=1024,
        boot_required_bytes=100,
        root_free_bytes=1024,
        root_required_bytes=100,
        cache_free_bytes=1024,
        cache_required_bytes=100,
    )


def _issues(plan: UpgradePreflight) -> tuple[PreflightIssue, ...]:
    return plan.issues(expected_source="bookworm", supported_architectures=frozenset({"amd64", "arm64"}))


def test_safe_preflight_has_no_blockers() -> None:
    assert _issues(_safe()) == ()


def test_each_persisted_safety_class_blocks_before_mutation() -> None:
    cases = (
        (replace(_safe(), live_release="trixie"), PreflightIssue.RELEASE_MISMATCH),
        (replace(_safe(), architecture="i386"), PreflightIssue.UNSUPPORTED_ARCHITECTURE),
        (replace(_safe(), non_quiescent_sessions=("dev:ok",)), PreflightIssue.SESSION_NOT_QUIESCENT),
        (replace(_safe(), dpkg_audit=("half-configured",)), PreflightIssue.PACKAGE_DATABASE_BROKEN),
        (replace(_safe(), package_manager_owner="42"), PreflightIssue.PACKAGE_MANAGER_BUSY),
        (
            replace(_safe(), apt_timer_states={"apt-daily.timer": ("unknown", "inactive")}),
            PreflightIssue.AUTOMATIC_APT_TIMER_STATE,
        ),
        (replace(_safe(), held_packages=("linux-image",)), PreflightIssue.HELD_PACKAGES),
        (replace(_safe(), kernel_metapackage=None), PreflightIssue.KERNEL_METAPACKAGE_MISSING),
        (replace(_safe(), openssh_minimum_satisfied=False), PreflightIssue.OPENSSH_TOO_OLD),
        (replace(_safe(), modified_conffiles=("/etc/ssh/sshd_config",)), PreflightIssue.MODIFIED_CONFFILES),
        (replace(_safe(), apt_pins=("/etc/apt/preferences",)), PreflightIssue.APT_PINNING),
        (replace(_safe(), mixed_suites=("/etc/apt/sources.list",)), PreflightIssue.MIXED_SUITES),
        (replace(_safe(), release_blockers=("rabbitmq",)), PreflightIssue.RELEASE_BLOCKER),
        (replace(_safe(), boot_free_bytes=99), PreflightIssue.BOOT_SPACE_LOW),
        (replace(_safe(), root_free_bytes=99), PreflightIssue.ROOT_SPACE_LOW),
        (replace(_safe(), var_free_bytes=99, var_required_bytes=100), PreflightIssue.VAR_SPACE_LOW),
        (replace(_safe(), cache_free_bytes=99), PreflightIssue.CACHE_SPACE_LOW),
    )
    for plan, expected in cases:
        assert expected in _issues(plan)


def test_provider_managed_kernel_does_not_require_a_guest_metapackage() -> None:
    plan = replace(_safe(), kernel_metapackage=None, guest_kernel_required=False)

    assert PreflightIssue.KERNEL_METAPACKAGE_MISSING not in _issues(plan)


def test_shared_filesystem_space_reports_one_aggregate_blocker() -> None:
    plan = replace(
        _safe(),
        root_filesystem="shared",
        root_free_bytes=99,
        root_required_bytes=100,
        var_filesystem="shared",
        var_free_bytes=99,
        var_required_bytes=100,
        cache_filesystem="shared",
        cache_free_bytes=99,
        cache_required_bytes=100,
    )

    assert _issues(plan) == (PreflightIssue.ROOT_SPACE_LOW,)


def test_material_plan_fingerprint_changes_on_second_pass_drift() -> None:
    preliminary = _safe()
    final = replace(preliminary, removals=("obsolete-package",))

    assert preliminary.material_plan() != final.material_plan()


def test_plan_serialization_keeps_timer_restore_state() -> None:
    plan = replace(
        _safe(),
        apt_timer_states={
            "apt-daily.timer": ("enabled", "active"),
            "apt-daily-upgrade.timer": ("disabled", "inactive"),
        },
    )

    assert plan.to_plan()["apt_timer_states"] == {
        "apt-daily.timer": ["enabled", "active"],
        "apt-daily-upgrade.timer": ["disabled", "inactive"],
    }


def test_mixed_file_still_identifies_its_third_party_repository() -> None:
    content = """\
deb https://deb.debian.org/debian bookworm main
deb https://packages.example.test/debian bookworm main
"""

    assert _is_third_party(content) is True


def test_suite_detection_uses_policy_suites_instead_of_known_codename_list() -> None:
    content = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: forky forky-updates
Components: main
"""

    assert _mentions_other_debian_suite(content, ("forky", "forky-updates")) is False
    assert _mentions_other_debian_suite(content, ("trixie", "trixie-updates")) is True


def test_target_source_hygiene_requires_each_suite_from_an_enabled_binary_stanza() -> None:
    content = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main

Types: deb
URIs: https://security.debian.org/debian-security
Suites: trixie-security
Components: main
Enabled: no
"""

    class _Target:
        def run(self, command: str, **kwargs: object) -> object:
            del command, kwargs
            return SimpleNamespace(
                ok=True,
                stdout="AGW-SOURCE:/etc/apt/sources.list.d/debian.sources\n" + content,
            )

    issues = target_source_hygiene_issues(
        _Target(),  # type: ignore[arg-type]
        ("trixie", "trixie-updates", "trixie-security"),
    )

    assert issues == ("<missing-target-suite:trixie-security>",)


def test_target_source_hygiene_accepts_complete_enabled_target_sources() -> None:
    content = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main

Types: deb
URIs: https://security.debian.org/debian-security
Suites: trixie-security
Components: main
"""

    class _Target:
        def run(self, command: str, **kwargs: object) -> object:
            del command, kwargs
            return SimpleNamespace(
                ok=True,
                stdout="AGW-SOURCE:/etc/apt/sources.list.d/debian.sources\n" + content,
            )

    assert (
        target_source_hygiene_issues(
            _Target(),  # type: ignore[arg-type]
            ("trixie", "trixie-updates", "trixie-security"),
        )
        == ()
    )


@pytest.mark.parametrize(
    ("path", "content"),
    (
        (
            "/etc/apt/sources.list",
            "deb https://deb.debian.org.evil.example/debian trixie main\n",
        ),
        (
            "/etc/apt/sources.list.d/debian.sources",
            "Types: deb\nURIs: https://deb.debian.org.evil.example/debian\nSuites: trixie\n",
        ),
    ),
)
def test_spoofed_debian_hostname_is_third_party_and_cannot_cover_target_suite(
    path: str,
    content: str,
) -> None:
    class _Target:
        def run(self, command: str, **kwargs: object) -> object:
            del command, kwargs
            return SimpleNamespace(ok=True, stdout=f"AGW-SOURCE:{path}\n{content}")

    assert _is_third_party(content) is True
    assert _mentions_other_debian_suite(content, ("bookworm",)) is False
    issues = target_source_hygiene_issues(
        _Target(),  # type: ignore[arg-type]
        ("trixie",),
    )
    assert path in issues
    assert "<missing-target-suite:trixie>" in issues


@pytest.mark.parametrize(
    "content",
    (
        "deb-src https://evil.example/debian trixie main\n",
        "Types: deb-src\nURIs: https://evil.example/debian\nSuites: trixie\n",
    ),
)
def test_source_only_stanzas_participate_in_third_party_classification(content: str) -> None:
    assert _is_third_party(content) is True


@pytest.mark.parametrize(
    "content",
    (
        "deb-src https://deb.debian.org/debian bookworm main\n",
        "Types: deb-src\nURIs: https://deb.debian.org/debian\nSuites: bookworm\n",
    ),
)
def test_source_only_stanzas_participate_in_foreign_suite_classification(content: str) -> None:
    assert _mentions_other_debian_suite(content, ("trixie",)) is True


def test_failed_empty_safety_probe_cannot_become_a_safe_empty_fact() -> None:
    class _Target:
        def run(self, command: str, **kwargs: object) -> object:
            del command, kwargs
            return SimpleNamespace(ok=False, stdout="")

    with pytest.raises(StateError):
        _lines(_Target(), "dpkg --audit")  # type: ignore[arg-type]
