"""Every package's ``__all__`` names something that is actually there.

Ruff's F822 (undefined name in ``__all__``) is selected, and ruff
deliberately does NOT apply it inside ``__init__.py``, which is the only
place the repo writes ``__all__``. So the one rule that covers this is
switched off exactly where it would fire, and the gate cannot see a
package whose exports outlived the symbols they name.

It happened: `agentworks.config` kept twelve entries (`validate_name`,
`NAME_RE`, the length caps) after they moved to `agentworks.naming`, so
`from agentworks.config import *` raised AttributeError while every gate
stayed green. This is the check that would have caught it, over every
package rather than the one that broke.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import agentworks


def _package_names() -> list[str]:
    """Every importable package under ``agentworks``, including itself."""
    found = [agentworks.__name__]
    for info in pkgutil.walk_packages(agentworks.__path__, prefix=f"{agentworks.__name__}."):
        if info.ispkg:
            found.append(info.name)
    return sorted(found)


@pytest.mark.parametrize("package", _package_names())
def test_every_exported_name_resolves(package: str) -> None:
    module = importlib.import_module(package)
    missing = sorted(name for name in getattr(module, "__all__", ()) or () if not hasattr(module, name))

    assert not missing, f"{package}.__all__ names symbols that are not there: {missing}"
