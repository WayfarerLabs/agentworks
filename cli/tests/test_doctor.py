"""Tests for the doctor health check API."""

from __future__ import annotations

import pytest

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
        assert "not found on this machine" in g.checks[0].message

    def test_stale_available_shell_still_warns(self, tmp_path, monkeypatch) -> None:
        from agentworks import doctor

        f = self._stamped(tmp_path / "agentworks", "v-old")
        monkeypatch.setattr(doctor, "_get_completion_paths", lambda: [("bash", [f])])
        monkeypatch.setattr(doctor, "_shell_available", lambda name: True)

        g = doctor._check_completions("v-new")

        assert g.checks[0].status == Status.WARN
        assert "stale" in g.checks[0].message

    def test_up_to_date_available_shell_is_ok(self, tmp_path, monkeypatch) -> None:
        from agentworks import doctor

        f = self._stamped(tmp_path / "agentworks", "v-cur")
        monkeypatch.setattr(doctor, "_get_completion_paths", lambda: [("bash", [f])])
        monkeypatch.setattr(doctor, "_shell_available", lambda name: True)

        g = doctor._check_completions("v-cur")

        assert g.checks[0].status == Status.OK

    def test_shell_available_maps_powershell_to_pwsh(self, monkeypatch) -> None:
        """On systems where only `pwsh` exists (not `powershell`), the
        powershell shell should still count as available."""
        from agentworks import doctor

        monkeypatch.setattr(doctor.shutil, "which", lambda name: "/x/pwsh" if name == "pwsh" else None)
        assert doctor._shell_available("powershell") is True
        assert doctor._shell_available("zsh") is False


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
