"""``atomic_write_text``: what a replacement keeps, and what it refuses to replace."""

from __future__ import annotations

import glob
import os
import shlex
import sys
from pathlib import Path

import pytest

from agentworks.atomic_write import atomic_write_text
from agentworks.errors import AuthorizationError

_ORIGINAL = b"Host *\n    ServerAliveInterval 60\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owners")
def test_a_file_whose_owner_cannot_be_kept_is_left_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename would hand the file to this user, so the write is refused and says how to make it the user's own."""
    path = tmp_path / "config"
    path.write_bytes(_ORIGINAL)
    before = path.stat()

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError("not permitted")

    monkeypatch.setattr(os, "chown", refuse)

    with pytest.raises(AuthorizationError) as caught:
        atomic_write_text(path, "replacement\n", preserve_metadata=True)

    assert path.stat().st_ino == before.st_ino
    assert path.read_bytes() == _ORIGINAL
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["config"]
    assert caught.value.hint is not None
    assert shlex.quote(str(path)) in caught.value.hint


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
