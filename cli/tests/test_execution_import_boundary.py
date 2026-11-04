"""Files migrated onto ``agentworks.execution`` must not import the legacy transport stack.

Most of the tree imports the six legacy modules transitively, so an import
guard cannot prove independence until they are deleted. Each slice instead adds
its migrated files here, and this check rejects any import of a legacy module
anywhere in them: at the top level, inside a function, or under
``TYPE_CHECKING``. The ``agentworks.execution`` package itself is covered by the
fresh-interpreter guard in ``tests/execution/test_independence.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.execution.legacy_modules import is_legacy

PACKAGE = Path(__file__).resolve().parents[1] / "agentworks"


# Later slices append the files they migrate.
MIGRATED_FILES = (
    "vms/manager/exec.py",
    "vms/manager/verification.py",
    "vms/manager/port_forward.py",
    "agents/manager/access.py",
    "capabilities/vm_platform/lima_host.py",
    "capabilities/vm_platform/public_ssh.py",
    "capabilities/vm_platform/bootstrap_script.py",
    "capabilities/vm_platform/tailscale_join.py",
    "plugins/proxmox/guest_agent.py",
    "vms/contract.py",
    "vms/manager/release.py",
    "vms/initializer/phase_a.py",
    "vms/recovery.py",
    "vms/manager/recover.py",
    "vms/manager/tailscale.py",
    "vms/nodes.py",
    "orchestration/activation.py",
    "vms/initializer/tailscale_preflight.py",
)


def legacy_imports(source: str, *, module: str, is_package: bool = False, filename: str = "<unknown>") -> list[str]:
    """Every legacy module the source imports, as written, in any form.

    ``module`` is the dotted name of the file itself, which resolves relative
    imports.
    """
    tree = ast.parse(source, filename=filename)
    package = module if is_package else module.rpartition(".")[0]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if is_legacy(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, base]) if base else ".".join(anchor)
            # `from agentworks import ssh` names the module in the alias.
            found += [
                f"{base}.{alias.name}" for alias in node.names if is_legacy(base) or is_legacy(f"{base}.{alias.name}")
            ]
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import agentworks.ssh", ["agentworks.ssh"]),
        ("import agentworks.ssh as x", ["agentworks.ssh"]),
        ("from agentworks import ssh", ["agentworks.ssh"]),
        ("from agentworks import transports as t", ["agentworks.transports"]),
        ("from agentworks.transports import SSHTransport", ["agentworks.transports.SSHTransport"]),
        ("from agentworks.transports.base import Transport", ["agentworks.transports.base.Transport"]),
        ("from ... import ssh", ["agentworks.ssh"]),
        ("from ...ssh import SSHError", ["agentworks.ssh.SSHError"]),
        ("from agentworks.harness_setup import runner", ["agentworks.harness_setup.runner"]),
        ("def f():\n    from agentworks.native_files import x", ["agentworks.native_files.x"]),
        ("if TYPE_CHECKING:\n    import agentworks.remote_exec", ["agentworks.remote_exec"]),
        ("from agentworks.plugins.proxmox import transport", ["agentworks.plugins.proxmox.transport"]),
    ],
)
def test_every_import_form_of_a_legacy_module_is_found(source: str, expected: list[str]) -> None:
    assert legacy_imports(source, module="agentworks.vms.manager.exec") == expected


@pytest.mark.parametrize(
    "source",
    [
        "import agentworks.sshconfig",
        "from agentworks import ssh_config",
        "from agentworks.execution import trust",
        "from agentworks.vms.manager import exec_vm",
        "import agentworks.transport_helpers",
    ],
)
def test_lookalike_names_are_not_legacy_imports(source: str) -> None:
    assert legacy_imports(source, module="agentworks.vms.manager.exec") == []


@pytest.mark.parametrize("relative", MIGRATED_FILES)
def test_migrated_file_imports_nothing_retired(relative: str) -> None:
    path = PACKAGE / relative
    module = "agentworks." + relative.removesuffix(".py").replace("/", ".")
    assert legacy_imports(path.read_text(encoding="utf-8"), module=module, filename=str(path)) == []
