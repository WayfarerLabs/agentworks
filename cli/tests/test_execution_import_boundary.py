"""Files migrated onto ``agentworks.execution`` must not import the legacy transport stack.

Most of the tree imports the legacy modules transitively, so an import
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
    # The package guard imports the vm_platform modules too, but runs no
    # function body and skips TYPE_CHECKING blocks; this check reads both.
    "capabilities/vm_platform/__init__.py",
    "capabilities/vm_platform/base.py",
    "capabilities/vm_platform/bootstrap_script.py",
    "capabilities/vm_platform/cloud_init.py",
    "capabilities/vm_platform/debian_release.py",
    "capabilities/vm_platform/kinds.py",
    "capabilities/vm_platform/lima.py",
    "capabilities/vm_platform/lima_host.py",
    "capabilities/vm_platform/public_ssh.py",
    "capabilities/vm_platform/skel.py",
    "capabilities/vm_platform/ssh_exposure.py",
    "capabilities/vm_platform/tailscale_join.py",
    "capabilities/vm_platform/wsl2.py",
    # The package guard imports every plugin too, likewise running no
    # function body; this check reads the plugins migrated so far whole.
    "plugins/aws/platform.py",
    "plugins/azure/platform.py",
    "plugins/gcp/platform.py",
    "plugins/proxmox/platform.py",
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
    "command_checks.py",
    "capabilities/harness_integration/base.py",
    "capabilities/harness_integration/shell.py",
    "capabilities/git_credential/github.py",
    "plugins/azure/azdo.py",
    "artifacts/native/probe.py",
    "plugins/claude/artifacts.py",
    "capabilities/base.py",
    "plugins/claude/harness_integration.py",
    "plugins/codex/harness_integration.py",
    "plugins/grok/harness_integration.py",
    "artifacts/publication.py",
    "artifacts/session.py",
    "artifacts/native/common.py",
    "harness_setup/dispatch.py",
    "harness_setup/readiness.py",
    "plugins/codex/artifacts.py",
    "plugins/_harness_native/native.py",
    "plugins/_harness_native/native_cli.py",
    "capabilities/harness_integration/setup.py",
    "vms/initializer/steps.py",
    "vms/initializer/packages.py",
    "vms/hardening.py",
    "vms/tailscale_dns.py",
    "vms/initializer/workspaces_dir.py",
    "workspaces/acls.py",
    "sources.py",
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
        ("from agentworks.transports import base", ["agentworks.transports.base"]),
        ("def f():\n    from agentworks.remote_exec import x", ["agentworks.remote_exec.x"]),
        ("if TYPE_CHECKING:\n    import agentworks.remote_exec", ["agentworks.remote_exec"]),
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
