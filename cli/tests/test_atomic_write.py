"""The in-place fallback of ``atomic_write_text``: the original survives a failed rewrite."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks import atomic_write
from agentworks.atomic_write import atomic_write_text
from agentworks.errors import StateError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX owners and resource limits")

_ORIGINAL = b"Host *\n    ServerAliveInterval 60\n"

# Refuses chown, so the write falls back to rewriting in place, and caps the
# file size below the new content, so that rewrite fails after truncating.
_FAILS_AFTER_TRUNCATION = """
import os, resource, signal, sys
from pathlib import Path
from agentworks.atomic_write import atomic_write_text

def refuse(*args, **kwargs):
    raise PermissionError("refused")

os.chown = refuse
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (64, resource.RLIM_INFINITY))
try:
    atomic_write_text(Path(sys.argv[1]), "x" * 1000, preserve_metadata=True)
except OSError as error:
    print(error.errno)
"""


def test_a_rewrite_that_fails_after_truncating_puts_the_original_back(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_bytes(_ORIGINAL)

    completed = subprocess.run(
        [sys.executable, "-c", _FAILS_AFTER_TRUNCATION, str(path)], capture_output=True, text=True, check=True
    )

    assert completed.stdout.strip() == "27"  # EFBIG: the write itself failed
    assert path.read_bytes() == _ORIGINAL
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["config"]


def test_a_rewrite_that_cannot_be_undone_names_the_copy_of_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config"
    path.write_bytes(_ORIGINAL)

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError("refused")

    def truncate_then_fail(target: Path, data: bytes) -> None:
        target.write_bytes(b"")
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "chown", refuse)
    monkeypatch.setattr(atomic_write, "_write_synced", truncate_then_fail)

    with pytest.raises(StateError) as caught:
        atomic_write_text(path, "replacement\n", preserve_metadata=True)

    (backup,) = [entry for entry in tmp_path.iterdir() if entry.name != "config"]
    assert backup.read_bytes() == _ORIGINAL
    assert backup.name in (caught.value.hint or "")
