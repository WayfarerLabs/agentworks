"""Real creation composition with scripted delivery, not native acceptance."""

from pathlib import Path

import pytest

from tests.execution.test_independence import _run_fresh_process


@pytest.mark.windows
@pytest.mark.parametrize("first_import", ["agentworks.plugins", "agentworks.capabilities.vm_platform.registry"])
@pytest.mark.parametrize(
    "scenario", ["success", "body-failure", "prebootstrap", "unknown-rollback", "canonical-failure"]
)
def test_proxmox_creation_without_retired_modules(first_import: str, scenario: str, tmp_path: Path) -> None:
    script = r"""
import importlib
import sys
from pathlib import Path

def forbid_delivery(event, args):
    if event in ("subprocess.Popen", "os.system", "socket.connect", "socket.getaddrinfo"):
        raise AssertionError("unscripted external delivery: " + event)

sys.addaudithook(forbid_delivery)
importlib.import_module(sys.argv[2])
from tests.vms._proxmox_creation_composition import exercise_creation

exercise_creation(Path(sys.argv[3]), sys.argv[4])
assert_first_party_origins(require_tests=True)
assert not any(
    loaded == root or loaded.startswith(root + ".")
    for loaded in sys.modules for root in retired
)
"""
    _run_fresh_process(Path(__file__).resolve().parents[2], script, first_import, str(tmp_path), scenario)
