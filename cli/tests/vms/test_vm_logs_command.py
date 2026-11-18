"""``vm logs`` shows the named VM's operation logs and no other VM's."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentworks.cli import app


def test_vm_logs_never_shows_a_log_of_a_vm_whose_name_extends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.execution.oplog.LOG_DIR", tmp_path)
    (tmp_path / "dev-20261101T090000Z-vm-create.log").write_text("created dev\n")
    (tmp_path / "dev-2-20261102T090000Z-vm-create.log").write_text("created dev-2\n")

    result = CliRunner().invoke(app, ["vm", "logs", "dev", "--all"])

    assert result.exit_code == 0, result.output
    assert "created dev\n" in result.output
    assert "created dev-2" not in result.output
