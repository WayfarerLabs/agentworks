"""New package imports must work when legacy execution is unavailable."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.windows
def test_new_execution_imports_in_a_fresh_process_without_legacy() -> None:
    script = r"""
import importlib
import importlib.abc
import pkgutil
import sys

retired = (
    "agentworks.transports", "agentworks.ssh", "agentworks.remote_exec",
    "agentworks.harness_setup.runner", "agentworks.native_files",
    "agentworks.plugins.proxmox.transport",
)
class BlockRetired(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in retired):
            raise ImportError("Retired execution module is unavailable: " + fullname)

sys.meta_path.insert(0, BlockRetired())
import agentworks.execution

# Discover the supplied stack, including SSH when the combined tree contains it.
# Import errors propagate: a present carrier cannot silently evade this guard.
for module in pkgutil.walk_packages(
    agentworks.execution.__path__, agentworks.execution.__name__ + "."
):
    importlib.import_module(module.name)

from agentworks.execution.carrier import CarrierIO, Deadline, PreparedInvocation
from agentworks.execution.preparation import Command, prepare
prepared = prepare(Command(("/bin/true",)))
assert isinstance(prepared.invocation, PreparedInvocation)
assert isinstance(prepared.io, CarrierIO)
assert Deadline.after(None).remaining() is None
assert not any(
    loaded == name or loaded.startswith(name + ".")
    for loaded in sys.modules for name in retired
)
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
