"""The schema package is a LEAF, and stays one.

Capability modules declare their config models at class-definition time,
so they import this package at MODULE level. Anything under
``agentworks.resources`` runs that package's ``__init__``, which loads
every kind module, which loads every capability package: if the schema
package reached into it, importing a capability module on its own would
be a circular import.

Both directions are asserted, because each catches a different mistake:
a source scan catches an import nobody exercises, and a subprocess import
catches the cycle itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[2] / "agentworks" / "schema"

#: What the schema package may reach for. Each is a top-level leaf that
#: imports nothing of ours, so none can start a cycle back here.
_PERMITTED = ("agentworks.errors", "agentworks.source_location", "agentworks.schema")


def _agentworks_imports(source: str) -> set[str]:
    return {
        line.split()[1] for line in source.splitlines() if line.startswith(("import agentworks", "from agentworks"))
    }


@pytest.mark.parametrize("module", sorted(_PACKAGE.glob("*.py")), ids=lambda path: path.name)
def test_the_schema_package_imports_only_top_level_leaves(module: Path) -> None:
    reached = _agentworks_imports(module.read_text())
    offenders = sorted(name for name in reached if not name.startswith(_PERMITTED))

    assert not offenders, (
        f"{module.name} imports {offenders}, which puts the schema package above something it must "
        f"stay below. Anything under agentworks.resources loads every kind and every capability "
        f"package, and capability modules import THIS package at class-definition time."
    )


def test_the_import_scan_sees_real_imports() -> None:
    """Non-vacuity for the scan above: a reader that silently matched
    nothing would pass on every module. Package-wide rather than
    per-module, because ``reference.py`` genuinely imports nothing of
    ours, which is the whole reason it can hold the model layer's
    reference records."""
    reached = {name for module in _PACKAGE.glob("*.py") for name in _agentworks_imports(module.read_text())}

    assert "agentworks.errors" in reached
    assert any(name.startswith("agentworks.schema") for name in reached)


def test_the_schema_package_is_importable_on_its_own() -> None:
    """The property the source scan is a proxy for. A subprocess, because
    the test session has already imported half the codebase."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import agentworks.schema"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "agentworks.capabilities.base",
        "agentworks.capabilities.git_credential.base",
        "agentworks.capabilities.harness_integration.base",
        "agentworks.capabilities.vm_platform.base",
        "agentworks.secrets.backends",
    ],
)
def test_a_capability_module_is_importable_on_its_own(module: str) -> None:
    """The consequence that made the boundary necessary: each of these
    declares or hosts config models, so each imports the schema package
    at module level."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
