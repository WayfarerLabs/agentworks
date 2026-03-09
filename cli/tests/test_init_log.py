"""Tests for VM init logging."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agentworks.vms.init_log import InitLogger, delete_init_logs, find_init_logs, log_path_for_vm


def test_log_path_contains_vm_name() -> None:
    path = log_path_for_vm("test-vm")
    assert "vm-init-test-vm-" in path.name
    assert path.suffix == ".log"


def test_init_logger_writes_log(tmp_path: Path) -> None:
    with patch("agentworks.vms.init_log.LOG_DIR", tmp_path):
        logger = InitLogger("my-vm")
        logger.step("Step 1")
        logger.output("some output here")
        logger.step("Step 2")
        logger.warning("something went wrong")
        logger.close()

        assert logger.path.exists()
        content = logger.path.read_text()
        assert "# VM Init Log: my-vm" in content
        assert "Step 1" in content
        assert "some output here" in content
        assert "Step 2" in content
        assert "WARNING: something went wrong" in content
        assert "# Warnings: 1" in content


def test_init_logger_no_warnings(tmp_path: Path) -> None:
    with patch("agentworks.vms.init_log.LOG_DIR", tmp_path):
        logger = InitLogger("clean-vm")
        logger.step("Only step")
        logger.close()

        assert not logger.has_warnings
        assert logger.warnings == []
        content = logger.path.read_text()
        assert "WARNING" not in content


def test_init_logger_tracks_warnings(tmp_path: Path) -> None:
    with patch("agentworks.vms.init_log.LOG_DIR", tmp_path):
        logger = InitLogger("warn-vm")
        logger.warning("first")
        logger.warning("second")
        logger.close()

        assert logger.has_warnings
        assert logger.warnings == ["first", "second"]


def test_find_init_logs(tmp_path: Path) -> None:
    with patch("agentworks.vms.init_log.LOG_DIR", tmp_path):
        # Create some fake log files
        (tmp_path / "vm-init-foo-20260301T120000Z.log").write_text("log1")
        (tmp_path / "vm-init-foo-20260302T120000Z.log").write_text("log2")
        (tmp_path / "vm-init-bar-20260301T120000Z.log").write_text("other vm")

        logs = find_init_logs("foo")
        assert len(logs) == 2
        # Newest first
        assert "20260302" in logs[0].name

        logs_bar = find_init_logs("bar")
        assert len(logs_bar) == 1

        logs_none = find_init_logs("nonexistent")
        assert len(logs_none) == 0


def test_delete_init_logs(tmp_path: Path) -> None:
    with patch("agentworks.vms.init_log.LOG_DIR", tmp_path):
        (tmp_path / "vm-init-foo-20260301T120000Z.log").write_text("log1")
        (tmp_path / "vm-init-foo-20260302T120000Z.log").write_text("log2")
        (tmp_path / "vm-init-bar-20260301T120000Z.log").write_text("keep this")

        count = delete_init_logs("foo")
        assert count == 2
        assert not list(tmp_path.glob("vm-init-foo-*.log"))
        # bar's log should still exist
        assert (tmp_path / "vm-init-bar-20260301T120000Z.log").exists()
