"""Process contention and nested ownership for native setup guards."""

import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.errors import StateError
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard

pytestmark = pytest.mark.windows


def test_nested_guard_and_independent_families(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with native_mutation_guard(database, "first") as guard:
        with native_mutation_guard(database, "first", held=guard) as nested:
            assert nested is guard
        with native_mutation_guard(database, "second"):
            pass
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(database, "first"):
            pass
        with pytest.raises(StateError), native_mutation_guard(database, "second", held=guard):
            pass
    with pytest.raises(StateError), native_mutation_guard(database, "first", held=guard):
        pass
    with native_mutation_guard(database, "first"):
        pass


def test_exception_releases_guard_without_unlinking(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with pytest.raises(KeyboardInterrupt), native_mutation_guard(database, "vm"):
        raise KeyboardInterrupt
    lock_files = tuple(tmp_path.rglob("*"))
    with native_mutation_guard(database, "vm"):
        assert tuple(tmp_path.rglob("*")) == lock_files


def test_process_exit_releases_guard(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    script = """
import sys
from pathlib import Path
from agentworks.harness_setup.locking import native_mutation_guard
with native_mutation_guard(Path(sys.argv[1]), 'vm'):
    print('held', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(database, "vm"):
            pass
    finally:
        process.kill()
        process.communicate(timeout=15)
    with native_mutation_guard(database, "vm"):
        pass
