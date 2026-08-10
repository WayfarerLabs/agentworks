"""Tests for the doctor health check API."""

from __future__ import annotations

from typing import cast

import pytest

from agentworks.doctor import (
    HealthCheck,
    HealthGroup,
    HealthReport,
    Status,
    health_report_data,
)


def _first_projected_check(report: HealthReport) -> dict[str, object]:
    """Return the single check from a one-group, one-check report."""
    data = health_report_data(report)
    groups = cast("list[dict[str, object]]", data["groups"])
    checks = cast("list[dict[str, object]]", groups[0]["checks"])
    return checks[0]


def test_health_group_convenience_methods() -> None:
    g = HealthGroup("test")
    g.ok("check1", "all good")
    g.info("check2", "not applicable")
    g.warn("check3", "might be a problem")
    g.fail("check4", "broken")

    assert len(g.checks) == 4
    assert g.checks[0].status == Status.OK
    assert g.checks[1].status == Status.INFO
    assert g.checks[2].status == Status.WARN
    assert g.checks[3].status == Status.FAIL
    assert [status.value for status in Status] == ["ok", "info", "warn", "fail"]


def test_health_report_counts() -> None:
    report = HealthReport()

    g1 = HealthGroup("group1")
    g1.ok("a")
    g1.ok("b")
    g1.info("c")
    report.groups.append(g1)

    g2 = HealthGroup("group2")
    g2.warn("d")
    g2.fail("e")
    g2.ok("f")
    report.groups.append(g2)

    assert report.ok_count == 3
    assert report.info_count == 1
    assert report.warn_count == 1
    assert report.fail_count == 1
    assert report.has_failures is True


def test_health_report_no_failures() -> None:
    report = HealthReport()
    g = HealthGroup("clean")
    g.ok("all good")
    g.info("fyi")
    report.groups.append(g)

    assert report.has_failures is False
    assert report.fail_count == 0
    assert report.warn_count == 0


def test_health_check_message_optional() -> None:
    check = HealthCheck(name="test", status=Status.OK)
    assert check.message is None

    check_with_msg = HealthCheck(name="test", status=Status.WARN, message="details")
    assert check_with_msg.message == "details"


def test_machine_output_serializes_the_same_health_check_facts() -> None:
    """Human and JSON renderers consume one presentation-neutral check."""
    report = HealthReport()
    group = HealthGroup("Configuration")
    group.fail("Config", "configuration did not load", hint="fix the config")
    report.groups.append(group)

    check = _first_projected_check(report)

    assert check["message"] == "configuration did not load"
    assert check["hint"] == "fix the config"


def test_config_exception_becomes_one_shared_health_fact(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import config, doctor
    from agentworks.errors import ConfigError

    marker = "secret://credentials/production"
    config_path = tmp_path / "config.toml"
    config_path.write_text("[operator]\n")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    def fail_load_config(**_kwargs: object):
        raise ConfigError(f"backend rejected {marker}", hint=f"fix {marker}")

    monkeypatch.setattr(config, "load_config", fail_load_config)

    config_group, _config, _registry = doctor._check_config()
    report = HealthReport(groups=[config_group])
    rendered = health_report_data(report)

    groups = cast("list[dict[str, object]]", rendered["groups"])
    checks = cast("list[dict[str, object]]", groups[0]["checks"])
    config_check = next(check for check in checks if check["name"] == "Config")
    assert config_check["message"] == f"backend rejected {marker}"
    assert config_check["hint"] == f"fix {marker}"


class TestCompletionChecks:
    """Relevance-aware staleness reporting in `_check_completions`."""

    def _stamped(self, path, version: str):
        path.write_text(f"# agentworks-completion-version: {version}\n")
        return path

    def test_unavailable_shell_reports_info_not_warn(self, tmp_path, monkeypatch) -> None:
        """A stale completion file for a shell that isn't on this machine
        (e.g. zsh on Windows) should be an info note, not a warning."""
        from agentworks import doctor

        f = self._stamped(tmp_path / "_agentworks", "v-old")
        monkeypatch.setattr(doctor, "_get_completion_paths", lambda: [("zsh", [f])])
        monkeypatch.setattr(doctor, "_shell_available", lambda name: False)

        g = doctor._check_completions("v-new")

        assert len(g.checks) == 1
        assert g.checks[0].status == Status.INFO
        message = g.checks[0].message
        assert message is not None and "not found on this machine" in message

    def test_stale_available_shell_still_warns(self, tmp_path, monkeypatch) -> None:
        from agentworks import doctor

        f = self._stamped(tmp_path / "agentworks", "v-old")
        monkeypatch.setattr(doctor, "_get_completion_paths", lambda: [("bash", [f])])
        monkeypatch.setattr(doctor, "_shell_available", lambda name: True)

        g = doctor._check_completions("v-new")

        assert g.checks[0].status == Status.WARN
        message = g.checks[0].message
        assert message is not None and "stale" in message

    def test_up_to_date_available_shell_is_ok(self, tmp_path, monkeypatch) -> None:
        from agentworks import doctor

        f = self._stamped(tmp_path / "agentworks", "v-cur")
        monkeypatch.setattr(doctor, "_get_completion_paths", lambda: [("bash", [f])])
        monkeypatch.setattr(doctor, "_shell_available", lambda name: True)

        g = doctor._check_completions("v-cur")

        assert g.checks[0].status == Status.OK

    def test_shell_available_maps_powershell_to_pwsh(self, monkeypatch) -> None:
        """On systems where only `pwsh` exists (not `powershell`), the
        powershell shell should still count as available. Also pins the
        symmetric case: `powershell` on PATH without `pwsh` should also
        count (Windows-native Windows PowerShell)."""
        import shutil

        from agentworks import doctor

        monkeypatch.setattr(shutil, "which", lambda name: "/x/pwsh" if name == "pwsh" else None)
        assert doctor._shell_available("powershell") is True
        assert doctor._shell_available("zsh") is False

        monkeypatch.setattr(shutil, "which", lambda name: "/x/powershell" if name == "powershell" else None)
        assert doctor._shell_available("powershell") is True


@pytest.mark.integration
def test_run_checks_returns_report() -> None:
    """Smoke test: run_checks returns a valid HealthReport with expected groups.

    Marked as integration because it probes the real environment (filesystem,
    subprocesses, database).
    """
    from agentworks.doctor import run_checks

    report = run_checks()

    assert isinstance(report, HealthReport)
    assert len(report.groups) >= 5  # python, tools, platforms, tailscale, config, db, completions

    group_names = [g.name for g in report.groups]
    assert "Python" in group_names
    assert "Required tools" in group_names
    assert "VM platforms" in group_names
    assert "Database" in group_names


@pytest.mark.integration
def test_run_checks_group_order_and_config_failure_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group order is a presentation choice decoupled from which checks
    need config: with config/registry unavailable, the report keeps its shape;
    the registry-dependent groups (VM platforms, VM sites, Secret backends,
    Secrets) each render a skipped pointer (they precede the Configuration
    group that explains the failure, so silent absence would read as "no
    sites"/"no secrets") and every config-free group renders in presentation
    order. VM platforms and Secret backends now read stored readiness off the
    graph (R11), so they too need the registry and skip cleanly in degraded
    mode. Integration for the same reason as the smoke test above: the
    config-free groups probe the real environment.
    """
    from agentworks import doctor

    failed = doctor.HealthGroup("Configuration")
    failed.fail("Config", "did not load")
    monkeypatch.setattr(doctor, "_check_config", lambda: (failed, None, None))

    report = doctor.run_checks()

    assert [g.name for g in report.groups] == [
        "System",
        "Python",
        "Required tools",
        "Tailscale",
        "System plugins",
        "VM platforms",
        "VM sites",
        "Configuration",
        "Secret backends",
        "Secrets",
        "Database",
    ]
    for group_name in ("VM platforms", "System plugins", "VM sites", "Secret backends", "Secrets"):
        placeholder = next(g for g in report.groups if g.name == group_name).checks
        assert len(placeholder) == 1, group_name
        assert placeholder[0].status is doctor.Status.INFO, group_name
        message = placeholder[0].message or ""
        assert "skipped" in message, group_name
        assert "Configuration" in message, group_name


@pytest.mark.integration
def test_run_checks_secrets_group_skips_not_vanishes_when_config_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #253: when config/manifests are unavailable, the
    Secrets group must render an explicit skipped-with-reason line rather
    than vanishing from the report entirely, matching the VM sites group.
    """
    from agentworks import doctor

    failed = doctor.HealthGroup("Configuration")
    failed.fail("Manifest", "kind: not-a-real-kind")
    monkeypatch.setattr(doctor, "_check_config", lambda: (failed, None, None))

    report = doctor.run_checks()

    secrets = next(g for g in report.groups if g.name == "Secrets")
    vm_sites = next(g for g in report.groups if g.name == "VM sites")
    # Secrets follows the VM sites skip pattern exactly.
    assert len(secrets.checks) == 1
    assert secrets.checks[0].status is doctor.Status.INFO
    assert secrets.checks[0].message == vm_sites.checks[0].message
