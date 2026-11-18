"""Execute the admin-grant writer without touching the workstation's sudoers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform import bootstrap_script, sudoers
from agentworks.ssh import SSHError
from agentworks.vms.initializer.shell_env import _write_sudoers_admin
from tests.conftest import requires_posix_shell

pytestmark = requires_posix_shell


@pytest.fixture
def grant_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    directory = tmp_path / "sudoers with 'quotes'"
    directory.mkdir()
    monkeypatch.setattr(sudoers, "_SUDOERS_DIRECTORY", str(directory))
    return directory


@pytest.fixture
def validator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, grant_directory: Path) -> Path:
    """Observe the file at the validation boundary and allow rejection injection."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "validation.json"
    program = bindir / "visudo"
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1] == '-cf'\n"
        "staged = Path(sys.argv[2])\n"
        "grant = staged.parent / 'test-admin'\n"
        "Path(os.environ['VALIDATION_LOG']).write_text(json.dumps({\n"
        "    'mode': staged.stat().st_mode & 0o777,\n"
        "    'body': staged.read_text(),\n"
        "    'prior': grant.read_text() if grant.exists() else None,\n"
        "}))\n"
        "raise SystemExit(int(os.environ.get('VALIDATION_STATUS', '0')))\n"
    )
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("VALIDATION_LOG", str(log))
    return log


@pytest.mark.parametrize("prior", [False, True])
def test_grant_is_validated_before_promotion_and_converges_mode(
    grant_directory: Path, validator: Path, prior: bool
) -> None:
    grant = grant_directory / "test-admin"
    if prior:
        grant.write_text("previous grant\n")
        grant.chmod(0o644)
    script = sudoers.admin_sudoers_script("test-admin")

    subprocess.run(["bash", "-c", script], check=True)

    observed = json.loads(validator.read_text())
    assert observed["mode"] == 0o440
    assert observed["prior"] == ("previous grant\n" if prior else None)
    assert grant.read_text() == observed["body"]
    assert grant.stat().st_mode & 0o777 == 0o440
    assert list(grant_directory.iterdir()) == [grant]

    grant.chmod(0o644)
    subprocess.run(["bash", "-c", script], check=True)
    assert grant.stat().st_mode & 0o777 == 0o440
    assert list(grant_directory.iterdir()) == [grant]


@pytest.mark.parametrize("prior", [False, True])
def test_rejected_grant_preserves_prior_state_and_cleans_staging(
    monkeypatch: pytest.MonkeyPatch, grant_directory: Path, validator: Path, prior: bool
) -> None:
    grant = grant_directory / "test-admin"
    if prior:
        grant.write_text("previous grant\n")
        grant.chmod(0o644)
    monkeypatch.setenv("VALIDATION_STATUS", "1")

    result = subprocess.run(["bash", "-c", sudoers.admin_sudoers_script("test-admin")], check=False)

    assert result.returncode != 0
    assert json.loads(validator.read_text())["mode"] == 0o440
    if prior:
        assert grant.read_text() == "previous grant\n"
        assert grant.stat().st_mode & 0o777 == 0o644
    assert list(grant_directory.iterdir()) == ([grant] if prior else [])


@pytest.mark.parametrize("reject", [False, True])
def test_reinit_executes_the_grant_writer_and_warns_on_rejection(
    monkeypatch: pytest.MonkeyPatch, grant_directory: Path, validator: Path, reject: bool
) -> None:
    grant = grant_directory / "test-admin"
    grant.write_text("previous grant\n")
    grant.chmod(0o644)
    if reject:
        monkeypatch.setenv("VALIDATION_STATUS", "1")

    def run(command: str, *, sudo: bool) -> None:
        assert sudo
        result = subprocess.run(["bash", "-c", command], check=False)
        if result.returncode:
            raise SSHError("validation rejected")

    target = MagicMock(run=run)
    logger = MagicMock()
    _write_sudoers_admin(target, logger, "test-admin")

    assert json.loads(validator.read_text())["mode"] == 0o440
    assert grant.stat().st_mode & 0o777 == (0o644 if reject else 0o440)
    assert logger.warning.call_count == int(reject)
    assert list(grant_directory.iterdir()) == [grant]


def test_bootstrap_uses_the_admin_grant_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = MagicMock(return_value=":\n")
    monkeypatch.setattr(bootstrap_script, "admin_sudoers_script", writer)

    bootstrap_script.generate_bootstrap_script(
        admin_username="test-admin",
        ssh_public_key="",
        provisioning_packages=[],
        hostname="test-vm",
        swap=0,
    )

    writer.assert_called_once_with("test-admin")


def test_grant_passes_real_visudo(monkeypatch: pytest.MonkeyPatch, grant_directory: Path) -> None:
    executable = shutil.which("visudo", path=os.defpath + os.pathsep + "/usr/sbin")
    if executable is None:
        pytest.skip("visudo is not installed")
    monkeypatch.setenv("PATH", str(Path(executable).parent) + os.pathsep + os.environ["PATH"])

    subprocess.run(["bash", "-c", sudoers.admin_sudoers_script("test-admin")], check=True)

    grant = grant_directory / "test-admin"
    subprocess.run([executable, "-cf", str(grant)], check=True)
    assert grant.stat().st_mode & 0o777 == 0o440
