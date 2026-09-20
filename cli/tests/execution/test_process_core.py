"""Independent reuse proof for the stdlib-only owned-process core."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution._process import ProcessResult, StreamResult

CORE_PATH = Path(__file__).parents[2] / "agentworks" / "execution" / "_process.py"
PYTHON_311 = Path("/usr/bin/python3.11")


@pytest.fixture(scope="module", params=[Path(sys.executable), PYTHON_311], ids=["current", "distribution-3.11"])
def interpreter(request: pytest.FixtureRequest) -> Path:
    python: Path = request.param
    if python == PYTHON_311:
        if not python.is_file():
            pytest.skip("This host has no /usr/bin/python3.11 compatibility interpreter")
        version = subprocess.run(
            [str(python), "-I", "-S", "-B", "-c", "import sys; print(*sys.version_info[:2])"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=5,
            text=True,
        )
        if version.stdout.strip() != "3 11":
            pytest.skip(f"Expected Python 3.11 at {python}, found {version.stdout.strip()}")
    return python


def test_process_core_has_only_standard_library_dependencies() -> None:
    tree = ast.parse(CORE_PATH.read_bytes(), filename=str(CORE_PATH))
    imported = {
        alias.name.partition(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported.update(
        node.module.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert imported <= sys.stdlib_module_names


def test_process_result_representation_hides_nested_stream_bytes() -> None:
    secret = b"process-core-repr-canary"
    stream = StreamResult(secret, complete=True)
    result = ProcessResult(True, 0, 0, stream, stream, None)

    assert secret.decode() not in repr(stream)
    assert secret.decode() not in repr(result)


@pytest.mark.windows
def test_standalone_core_reuse_roundtrips_binary_input_and_exit_status(interpreter: Path) -> None:
    bootstrap = r"""
import hashlib
import importlib.util
import json
import sys
import time

core_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("_standalone_process_core", core_path)
assert spec is not None and spec.loader is not None
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
assert "agentworks" not in sys.modules

payload = bytes(range(256)) * 257
child = (
    "import hashlib,sys; "
    "data=sys.stdin.buffer.read(); "
    "sys.stdout.buffer.write(data); "
    "sys.stderr.buffer.write(hashlib.sha256(data).digest()); "
    "sys.exit(37)"
)
result = core.run_owned_process(
    [sys.executable, "-I", "-S", "-B", "-c", child],
    input=core.ProcessInput(data=payload),
    output=core.ProcessOutput(capture_limit=len(payload)),
    deadline=core.Deadline(time.monotonic() + 10),
)
print(json.dumps({
    "agentworks_loaded": "agentworks" in sys.modules,
    "exit": result.exit_status,
    "failure": result.failure,
    "local": result.local_status,
    "stderr": result.stderr.data.hex(),
    "stderr_complete": result.stderr.complete,
    "stdout": result.stdout.data.hex(),
    "stdout_complete": result.stdout.complete,
}))
"""
    completed = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", bootstrap, str(CORE_PATH)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=20,
    )
    observed = json.loads(completed.stdout)
    payload = bytes(range(256)) * 257

    assert completed.stderr == b""
    assert observed == {
        "agentworks_loaded": False,
        "exit": 37,
        "failure": None,
        "local": 37,
        "stderr": hashlib.sha256(payload).digest().hex(),
        "stderr_complete": True,
        "stdout": payload.hex(),
        "stdout_complete": True,
    }
