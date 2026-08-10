"""azure-identity logging suppression (issue #309).

The azure platform quiets azure-identity's own credential-failure WARNING so a
failed credential surfaces only as agentworks' typed ``AzureError``, never as
raw SDK chatter ahead of it. These tests pin that behavior without touching
Azure: they exercise the module-level suppression helper directly and assert
the ``azure.identity`` logger's threshold, including that --debug / AGW_DEBUG=1
opts back into the SDK detail. No network, no credentials, and no azure SDK
import is needed: the helper only adjusts a logger addressed by name.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from agentworks.plugins.azure.auth import _AZURE_IDENTITY_LOGGER, _quiet_azure_identity_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _restore_azure_identity_level() -> Iterator[None]:
    """Save and restore the ``azure.identity`` logger level so a test's
    suppression (a process-global logging mutation) does not leak into the
    rest of the suite."""
    logger = logging.getLogger(_AZURE_IDENTITY_LOGGER)
    saved = logger.level
    try:
        yield
    finally:
        logger.setLevel(saved)


def test_quiets_azure_identity_warning_when_debug_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With debug off, the helper raises the azure.identity threshold above
    WARNING (dropping the credential-failure chatter) while leaving genuine
    ERROR records to pass."""
    monkeypatch.delenv("AGW_DEBUG", raising=False)
    logger = logging.getLogger(_AZURE_IDENTITY_LOGGER)
    logger.setLevel(logging.NOTSET)

    _quiet_azure_identity_logging()

    assert logger.level == logging.ERROR
    # isEnabledFor is exactly what azure-identity's WARNING log call is gated
    # on, so this is the real behavioral guarantee: the WARNING record is
    # never created, and a genuine ERROR still is.
    assert logger.isEnabledFor(logging.WARNING) is False
    assert logger.isEnabledFor(logging.ERROR) is True


def test_does_not_quiet_azure_identity_under_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under AGW_DEBUG=1 (the signal --debug also sets) the operator wants the
    SDK detail, so the helper is a no-op and the WARNING still passes."""
    monkeypatch.setenv("AGW_DEBUG", "1")
    logger = logging.getLogger(_AZURE_IDENTITY_LOGGER)
    logger.setLevel(logging.NOTSET)

    _quiet_azure_identity_logging()

    # Untouched: still deferring to the root threshold (default WARNING).
    assert logger.level == logging.NOTSET
    assert logger.isEnabledFor(logging.WARNING) is True
