"""Tests for the doctor health check API."""

from __future__ import annotations

from agentworks.doctor import HealthCheck, HealthGroup, HealthReport, Status


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


def test_run_checks_returns_report() -> None:
    """Smoke test: run_checks returns a valid HealthReport with expected groups."""
    from agentworks.doctor import run_checks

    report = run_checks()

    assert isinstance(report, HealthReport)
    assert len(report.groups) >= 5  # python, tools, platforms, tailscale, config, db, completions

    group_names = [g.name for g in report.groups]
    assert "Python" in group_names
    assert "Required tools" in group_names
    assert "VM platforms" in group_names
    assert "Database" in group_names
