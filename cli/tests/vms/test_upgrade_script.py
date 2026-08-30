from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentworks.vms.upgrade.journal import UpgradePair
from agentworks.vms.upgrade.remote import REMOTE_ROOT
from agentworks.vms.upgrade.scripts import render_upgrade_script


@pytest.mark.parametrize(
    "suites",
    [
        ("trixie",),
        ("trixie", "trixie-updates", "trixie-security"),
    ],
)
def test_rendered_package_service_script_is_valid_bash(suites: tuple[str, ...]) -> None:
    script = render_upgrade_script(
        UpgradePair("bookworm", "trixie"),
        target_suites=suites,
    )

    result = subprocess.run(
        ["bash", "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_retried_source_switch_never_overwrites_original_source_archive(tmp_path: Path) -> None:
    pair = UpgradePair("bookworm", "trixie")
    apt_root = tmp_path / "etc" / "apt"
    source_directory = apt_root / "sources.list.d"
    source_directory.mkdir(parents=True)
    (apt_root / "sources.list").write_text("deb https://deb.debian.org/debian bookworm main\n")
    (source_directory / "vendor.list").write_text("deb https://packages.example.test bookworm main\n")
    journal_directory = tmp_path / "state" / pair.dirname
    journal_directory.mkdir(parents=True)
    (journal_directory / "lock").touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    apt_get = fake_bin / "apt-get"
    apt_get.write_text("#!/bin/sh\nexit 0\n")
    apt_get.chmod(0o700)
    script = render_upgrade_script(pair, target_suites=("trixie",))
    script = script.replace(REMOTE_ROOT, str(tmp_path / "state")).replace("/etc/apt", str(apt_root))
    script_path = tmp_path / "upgrade.sh"
    script_path.write_text(script)
    script_path.chmod(0o700)
    environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    first = subprocess.run(
        [str(script_path), "switch-sources"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert first.returncode == 0, first.stderr
    archived = journal_directory / "sources-before" / "sources.list"
    original = archived.read_text()
    second = subprocess.run(
        [str(script_path), "switch-sources"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert second.returncode == 0, second.stderr
    assert archived.read_text() == original
    assert (journal_directory / "sources-before" / ".archive-complete").is_file()
