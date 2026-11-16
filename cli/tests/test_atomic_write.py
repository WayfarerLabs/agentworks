"""The in-place fallback of ``atomic_write_text``: the original survives a failed rewrite."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.atomic_write import atomic_write_text

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


# Refuses chown and makes every raw write fail, so both the in-place rewrite
# and the restore that follows it fail after truncating.
_REWRITE_AND_RESTORE_FAIL = """
import os, sys
from pathlib import Path
from agentworks.atomic_write import atomic_write_text
from agentworks.errors import StateError

def refuse(*args, **kwargs):
    raise PermissionError("refused")

def fail(*args, **kwargs):
    raise OSError(5, "Input/output error")

os.chown = refuse
os.write = fail
try:
    atomic_write_text(Path(sys.argv[1]), "replacement\\n", preserve_metadata=True)
except StateError as error:
    print(error.hint)
"""


def test_a_rewrite_that_cannot_be_undone_names_the_copy_of_the_original(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_bytes(_ORIGINAL)

    completed = subprocess.run(
        [sys.executable, "-c", _REWRITE_AND_RESTORE_FAIL, str(path)], capture_output=True, text=True, check=True
    )

    (backup,) = [entry for entry in tmp_path.iterdir() if entry.name != "config"]
    assert backup.read_bytes() == _ORIGINAL
    assert backup.name in completed.stdout


@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root may write a read-only file")
def test_a_file_that_refuses_writing_is_left_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only config owned by someone else is the usual way into the in-place fallback."""
    path = tmp_path / "config"
    path.write_bytes(_ORIGINAL)
    path.chmod(0o444)

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError("refused")

    monkeypatch.setattr(os, "chown", refuse)

    with pytest.raises(PermissionError):
        atomic_write_text(path, "replacement\n", preserve_metadata=True)

    assert path.read_bytes() == _ORIGINAL
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["config"]
