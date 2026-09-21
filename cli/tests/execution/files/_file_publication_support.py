"""Test-only support for private publication exchanges."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution import _file_publication_exchange
from agentworks.execution._file_publication_bundle import _MODULE_NAMES, _PACKAGE
from agentworks.execution._helper_bundle import FixedFileHelperBundle
from tests.execution.files._file_stage_support import LocalCarrier as LocalCarrier
from tests.execution.files._fixed_bundle_support import fixture_file_bundle

if TYPE_CHECKING:
    import pytest


def install_fixture_bundle(
    monkeypatch: pytest.MonkeyPatch,
    guest_patch: str = "",
) -> FixedFileHelperBundle:
    bundle = fixture_file_bundle(_PACKAGE, _MODULE_NAMES, "_file_publication_guest", guest_patch)
    monkeypatch.setattr(_file_publication_exchange, "FIXED_BUNDLE", bundle)
    return bundle
