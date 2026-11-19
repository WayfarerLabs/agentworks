"""Execute the admin-grant writer without touching the workstation's sudoers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform import bootstrap_script, sudoers
from agentworks.execution.requests import Script, Shell
from agentworks.execution.results import Completed, Result
from agentworks.vms.initializer.shell_env import _repair_admin_sudoers
from tests.phase_b_support import StepGuest

if TYPE_CHECKING:
    from tests.vm_access_support import Call

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Debian guest scripts require GNU coreutils")


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
        "    'prior': grant.read_text() if grant.is_file() else None,\n"
        "}))\n"
        "raise SystemExit(int(os.environ.get('VALIDATION_STATUS', '0')))\n"
    )
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("VALIDATION_LOG", str(log))
    return log


def scripts_run_here() -> StepGuest:
    """A step guest whose elevated sh scripts run on this machine, in this process's environment."""
    guest = StepGuest(local_files=False)

    def answer(call: Call) -> Result | None:
        request = call.request
        assert isinstance(request, Script) and request.shell is Shell.SH and request.elevated
        completed = subprocess.run(["sh", "-c", request.source], check=False, timeout=30)
        return Result(Completed(completed.returncode))

    guest.access.answer = answer
    return guest


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
    prior = "test-admin ALL=(root) /usr/bin/true\n"
    grant.write_text(prior)
    grant.chmod(0o644)
    inode = grant.stat().st_ino
    if reject:
        monkeypatch.setenv("VALIDATION_STATUS", "1")

    guest = scripts_run_here()
    _repair_admin_sudoers(guest.files, guest.log, "test-admin")

    assert json.loads(validator.read_text())["mode"] == 0o644
    assert grant.stat().st_ino == inode
    assert grant.read_text() == prior
    assert grant.stat().st_mode & 0o777 == (0o644 if reject else 0o440)
    assert len(guest.log.warned) == int(reject)
    assert list(grant_directory.iterdir()) == [grant]


def test_bootstrap_projects_the_admin_grant_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    fragment = "printf '%s\\n' 'arbitrary writer result'\n"
    writer = MagicMock(return_value=fragment)
    monkeypatch.setattr(bootstrap_script, "admin_sudoers_script", writer)

    script = bootstrap_script.generate_bootstrap_script(
        admin_username="test-admin",
        ssh_public_key="",
        provisioning_packages=[],
        hostname="test-vm",
        swap=0,
    )

    writer.assert_called_once_with("test-admin")
    assert fragment in script


def test_reinit_does_not_create_a_missing_grant(grant_directory: Path, validator: Path) -> None:
    guest = scripts_run_here()
    _repair_admin_sudoers(guest.files, guest.log, "test-admin")

    assert len(guest.log.warned) == 1
    assert not validator.exists()
    assert not list(grant_directory.iterdir())


def test_grant_passes_real_visudo(monkeypatch: pytest.MonkeyPatch, grant_directory: Path) -> None:
    executable = shutil.which("visudo", path=os.defpath + os.pathsep + "/usr/sbin")
    if executable is None:
        pytest.skip("visudo is not installed")
    monkeypatch.setenv("PATH", str(Path(executable).parent) + os.pathsep + os.environ["PATH"])

    subprocess.run(["bash", "-c", sudoers.admin_sudoers_script("test-admin")], check=True)

    grant = grant_directory / "test-admin"
    subprocess.run([executable, "-cf", str(grant)], check=True)
    assert grant.stat().st_mode & 0o777 == 0o440

    prior = "test-admin ALL=(root) /usr/bin/true\n"
    grant.chmod(0o644)
    grant.write_text(prior)
    subprocess.run(["bash", "-c", sudoers.repair_admin_sudoers_script("test-admin")], check=True)
    subprocess.run([executable, "-cf", str(grant)], check=True)
    assert grant.read_text() == prior
    assert grant.stat().st_mode & 0o777 == 0o440


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_reinit_refuses_nonregular_grants_without_reading_them(
    grant_directory: Path, validator: Path, kind: str
) -> None:
    grant = grant_directory / "test-admin"
    if kind == "directory":
        grant.mkdir()
    else:
        os.mkfifo(grant)
    before = grant.stat()

    result = subprocess.run(["bash", "-c", sudoers.repair_admin_sudoers_script("test-admin")], check=False, timeout=5)

    assert result.returncode != 0
    assert not validator.exists()
    assert grant.stat() == before


def test_reinit_preserves_a_symlink_and_the_existing_grant_inode(grant_directory: Path, validator: Path) -> None:
    existing = grant_directory / "existing"
    existing.write_text("test-admin ALL=(root) /usr/bin/true\n")
    existing.chmod(0o644)
    inode = existing.stat().st_ino
    grant = grant_directory / "test-admin"
    grant.symlink_to(existing)

    subprocess.run(["bash", "-c", sudoers.repair_admin_sudoers_script("test-admin")], check=True)

    assert grant.is_symlink()
    assert existing.stat().st_ino == inode
    assert existing.stat().st_mode & 0o777 == 0o440
    assert existing.read_text() == json.loads(validator.read_text())["body"]


def test_install_refuses_a_directory_destination_and_cleans_staging(grant_directory: Path, validator: Path) -> None:
    grant = grant_directory / "test-admin"
    grant.mkdir()

    result = subprocess.run(["bash", "-c", sudoers.admin_sudoers_script("test-admin")], check=False)

    assert result.returncode != 0
    assert grant.is_dir() and not list(grant.iterdir())
    assert list(grant_directory.iterdir()) == [grant]
