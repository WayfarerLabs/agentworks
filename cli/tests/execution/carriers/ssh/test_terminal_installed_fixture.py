"""Installed terminal fixture failures retain evidence without admitting SSH."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import NoReturn

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="The deferred terminal probe requires Linux")
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_fixture_entry_failure_retains_evidence_and_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    from tests.execution.carriers.ssh import posix_terminal_probe as probe

    primary = error_type()
    evidence = tmp_path / "terminal-fixture" / "server" / "failure.bin"

    def refuse_native(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("Fixture entry failure admitted a native resource")

    class FailedServer(AbstractContextManager[None]):
        def __enter__(self) -> NoReturn:
            evidence.write_bytes(b"retained-native-fixture-evidence\x00\xff")
            raise primary

        def __exit__(self, *args: object) -> None:
            pytest.fail("Failed fixture entry cannot acquire an exit lifetime")

    monkeypatch.setattr(probe, "enrollment_server", lambda *args, **kwargs: FailedServer())
    monkeypatch.setattr(subprocess, "Popen", refuse_native)
    monkeypatch.setattr(os, "openpty", refuse_native)

    with pytest.raises(error_type) as caught:
        probe.run_case(tmp_path, monkeypatch, refusal=False)

    assert caught.value is primary
    assert evidence.read_bytes() == b"retained-native-fixture-evidence\x00\xff"
    assert evidence.parent.parent.is_dir()
