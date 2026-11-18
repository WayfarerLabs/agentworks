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


@pytest.mark.windows
def test_vm_logs_reads_a_log_as_the_utf8_it_was_written_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workstation's locale encoding never decodes a log: on Windows it is a code page such as cp1252.

    Only a non-UTF-8 locale tells the two readings apart: the Windows lane
    has one, and a POSIX run has one under ``LC_ALL=C PYTHONUTF8=0``, where a
    locale read raises.
    """
    from agentworks.execution import oplog

    monkeypatch.setattr("agentworks.execution.oplog.LOG_DIR", tmp_path)
    log = oplog.OperationLogFile("dev", "vm-create")
    # U+0101 is C4 81 in UTF-8, and 0x81 has no character in cp1252.
    log.output("café ā 日本")
    log.close()

    result = CliRunner().invoke(app, ["vm", "logs", "dev"])

    assert result.exit_code == 0, result.output
    assert "café ā 日本" in result.output
