"""Make the six legacy execution modules unavailable to every test in this directory.

The package's tests must pass with the legacy stack blocked (R10, HLA D14). The
fresh-interpreter guard in ``test_independence.py`` proves import time; this
blocker proves test time, where the suite's session fixtures have already
imported ``agentworks.ssh`` and a plain ``import`` would find it in
``sys.modules``. Each test evicts the modules, removes the attribute on their
parent package (so ``from agentworks import ssh`` fails too) and puts a finder
that refuses them first on ``sys.meta_path``. ``monkeypatch`` restores all of
it afterwards.
"""

from __future__ import annotations

import importlib.abc
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from importlib.machinery import ModuleSpec
    from types import ModuleType

LEGACY_MODULES = (
    "agentworks.transports",
    "agentworks.ssh",
    "agentworks.remote_exec",
    "agentworks.harness_setup.runner",
    "agentworks.native_files",
    "agentworks.plugins.proxmox.transport",
)


def is_legacy(name: str) -> bool:
    return any(name == legacy or name.startswith(legacy + ".") for legacy in LEGACY_MODULES)


class _BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if is_legacy(fullname):
            raise ImportError(f"legacy execution module is unavailable to this test: {fullname}")
        return None


@pytest.fixture(autouse=True)
def _block_legacy_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [loaded for loaded in sys.modules if is_legacy(loaded)]:
        monkeypatch.delitem(sys.modules, name)
    for legacy in LEGACY_MODULES:
        parent_name, _, attribute = legacy.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and hasattr(parent, attribute):
            monkeypatch.delattr(parent, attribute)
    monkeypatch.setattr(sys, "meta_path", [_BlockLegacy(), *sys.meta_path])
