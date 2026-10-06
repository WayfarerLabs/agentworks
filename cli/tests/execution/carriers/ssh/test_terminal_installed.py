"""Deferred Linux installed-SSH composition; requires a native tester charter."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from tests.execution.carriers.ssh.fixture_worker import FixtureWorker
from tests.execution.carriers.ssh.posix_terminal_probe import run_case

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("ssh-posix-terminal-native")]


@pytest.mark.parametrize("refusal", [False, True], ids=["handoff-resize", "runtime-refusal"])
def test_installed_terminal_composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: bool) -> None:
    # Prerequisite skips precede worker admission and every native fixture effect.
    if sys.platform != "linux":
        pytest.skip("Owned installed terminal fixture requires Linux")
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).is_file() or any(shutil.which(tool) is None for tool in ("ssh", "ssh-keygen")):
        pytest.skip("Installed OpenSSH client, key generator and sshd required")
    FixtureWorker(lambda _stop: run_case(tmp_path, monkeypatch, refusal=refusal)).run()
