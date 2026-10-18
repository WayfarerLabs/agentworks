"""New package imports must work when legacy execution is unavailable."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_FRESH_PROCESS_BOOTSTRAP = r"""
import sys

assert not any(name == "agentworks" or name.startswith("agentworks.") for name in sys.modules)

import importlib.abc
import os

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
source_root = os.path.normcase(os.path.realpath(sys.argv[1]))
package_roots = {
    package: os.path.normcase(os.path.realpath(os.path.join(source_root, package)))
    for package in ("agentworks", "tests")
}
sys.path.insert(0, sys.argv[1])

def within_package(package, path):
    try:
        candidate = os.path.normcase(os.path.realpath(path))
        package_root = package_roots[package]
        return os.path.commonpath((package_root, candidate)) == package_root
    except ValueError:
        return False

def assert_first_party_origins(require_tests=False):
    found = {"agentworks": False, "tests": False}
    for name, module in tuple(sys.modules.items()):
        for package in found:
            if name == package or name.startswith(package + "."):
                found[package] = True
                spec = module.__spec__
                assert spec is not None, name
                if spec.origin is not None:
                    assert within_package(package, spec.origin), (name, spec.origin)
                if spec.submodule_search_locations is not None:
                    locations = tuple(spec.submodule_search_locations)
                    assert locations and all(within_package(package, path) for path in locations), (name, locations)
    assert found["agentworks"] and (not require_tests or found["tests"]), found
"""


def _run_fresh_process(cli_root: Path, script: str, *arguments: str, timeout: int = 20) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _FRESH_PROCESS_BOOTSTRAP + script, str(cli_root), *arguments],
        capture_output=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.windows
def test_new_execution_imports_in_a_fresh_process_without_legacy() -> None:
    script = r"""
import importlib
import pkgutil
import sys

import agentworks.execution

# Discover the supplied stack, including SSH when the combined tree contains it.
# Import errors propagate: a present carrier cannot silently evade this guard.
for module in pkgutil.walk_packages(
    agentworks.execution.__path__, agentworks.execution.__name__ + "."
):
    importlib.import_module(module.name)

from agentworks.execution.carrier import CarrierIO, Deadline, PreparedInvocation
from agentworks.execution.models import Command
from agentworks.execution.preparation import prepare
prepared = prepare(Command(("/bin/true",)))
assert isinstance(prepared.invocation, PreparedInvocation)
assert isinstance(prepared.io, CarrierIO)
assert Deadline.after(None).remaining() is None
assert not any(
    loaded == name or loaded.startswith(name + ".")
    for loaded in sys.modules for name in retired
)
assert_first_party_origins()
"""
    cli_root = Path(__file__).resolve().parents[2]
    _run_fresh_process(cli_root, script)


@pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux workflow fixture")
def test_native_context_workflow_in_a_fresh_process(tmp_path: Path) -> None:
    script = r"""
import grp
import json
import os
import pwd
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath

import pytest
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import Create, JsonStrategy, NewMetadata
from agentworks.execution.models import Command, Input, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ExitCode
from agentworks.execution.target import ExecutionRoute
from agentworks.vms._native_operation import vm_execution_context
from tests.vms.test_native_operation import _install_route
from tests.vms.test_target_preparation import _MARKER

assert_first_party_origins(require_tests=True)
work_root = Path(sys.argv[2])
os.chmod(work_root, 0o700)
database_path = work_root / "state.db"
trusted_root_path = work_root / "files"
trusted_root_path.mkdir(mode=0o700)
scratch = work_root / "scratch"
scratch.mkdir(mode=0o700)
scratch.chmod(0o1777)
trusted_root = PurePosixPath(str(trusted_root_path))
metadata = NewMetadata(
    pwd.getpwuid(os.geteuid()).pw_name,
    grp.getgrgid(os.getegid()).gr_name,
    0o600,
)
platform = WSL2Platform("wsl2", {})
deadline = Deadline.after(30)
command = Command((
    "/usr/bin/python3", "-I", "-S", "-B", "-c",
    "import sys; assert sys.argv[1:] == ['arg with spaces', 'quote with \"double\" and spaces', 'back\\\\slash']; "
    "assert sys.stdin.buffer.read() == bytes.fromhex('00ff800d0a'); "
    "sys.stdout.buffer.write(bytes.fromhex('6f757400ff0d0a')); "
    "sys.stderr.buffer.write(bytes.fromhex('65727200800d0a')); sys.exit(23)",
    "arg with spaces", "quote with \"double\" and spaces", "back\\slash",
))
scope = OperationScope(OperationResourceKind.VM, "box")
retained = None
route = None

try:
    with closing(Database(database_path)) as database:
        database.insert_vm("box", "wsl2", "box", admin_username="admin", instance_marker=_MARKER)
        database.update_vm_platform_metadata("box", {"distro_name": "Ubuntu"})
        with pytest.MonkeyPatch.context() as monkeypatch:
            route, native, observer = _install_route(database, platform, monkeypatch)
            route.local.scratch = scratch
            with vm_execution_context(
                database,
                "box",
                platform,
                RunContext(),
                route=ExecutionRoute.NATIVE,
                deadline=deadline,
                trusted_root=trusted_root,
            ) as context:
                retained = context.admin_execution_target()
                assert retained is not None
                execution = retained.execution()
                files = retained.files()
                assert execution is not None and files is not None
                result = execution.run(
                    command,
                    profile=Protection.DIRECT,
                    stdin=Input.bytes(bytes.fromhex("00ff800d0a")),
                    output=Output.capture(max_bytes=64),
                    deadline=deadline,
                )
                assert result.status == ExitCode(23)
                assert result.stdout.data == bytes.fromhex("6f757400ff0d0a")
                assert result.stderr.data == bytes.fromhex("65727200800d0a")

                file_path = trusted_root / "workflow.txt"
                written = files.write_file(
                    file_path,
                    b"workflow-file",
                    condition=Create(),
                    create_metadata=metadata,
                )
                assert written.revision is not None
                read = files.read_file(file_path, max_bytes=64)
                assert read is not None and read.data == b"workflow-file"

                json_path = trusted_root / "settings.json"
                seeded = files.write_file(
                    json_path,
                    b'{"kept":1,"replace":"old"}',
                    condition=Create(),
                    create_metadata=metadata,
                )
                assert seeded.revision is not None
                updated = files.update_json(
                    json_path,
                    {"replace": "new", "added": True},
                    strategy=JsonStrategy.MERGE_OVERWRITE,
                    create=False,
                    create_metadata=metadata,
                )
                assert updated.revision is not None
                merged = files.read_file(json_path, max_bytes=64)
                assert merged is not None
                assert json.loads(merged.data) == {"kept": 1, "replace": "new", "added": True}

            assert database.operations.inspect(scope) is None
            assert native.events.count("dispatch") == 1
            assert observer.events == ["observe"]
            effects_after_close = (len(route.routes), route.local.calls, tuple(native.events))
            with pytest.raises(StateError):
                retained.execution().run(command, profile=Protection.DIRECT)
            with pytest.raises(StateError):
                retained.files().write_file(
                    trusted_root / "after-close",
                    b"refused",
                    condition=Create(),
                    create_metadata=metadata,
                )
            assert (len(route.routes), route.local.calls, tuple(native.events)) == effects_after_close
            assert not (trusted_root_path / "after-close").exists()
            assert not list(scratch.iterdir())
finally:
    os.chmod(scratch, 0o700)

assert_first_party_origins(require_tests=True)
assert not any(name == root or name.startswith(root + ".") for name in sys.modules for root in retired)
"""
    cli_root = Path(__file__).resolve().parents[2]
    _run_fresh_process(cli_root, script, str(tmp_path), timeout=60)
