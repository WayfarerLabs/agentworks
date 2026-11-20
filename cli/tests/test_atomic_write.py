"""``atomic_write_text``, the replacement agentworks uses for the files it owns."""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest

from agentworks.atomic_write import atomic_write_text


def test_the_temporary_file_is_one_an_include_glob_does_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh's ``Include config.d/*`` would read a temporary file a crash left there, unless the glob skips it."""
    config_d = tmp_path / "config.d"
    config_d.mkdir()
    seen: list[list[str]] = []
    replace = os.replace

    def look_then_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        assert Path(source).exists()
        seen.append(glob.glob(str(config_d / "*")))
        replace(source, target)

    monkeypatch.setattr(os, "replace", look_then_replace)

    atomic_write_text(config_d / "agentworks.conf", "Host awvm--web\n")

    assert seen == [[]]
