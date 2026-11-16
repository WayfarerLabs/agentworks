"""``probe_presence``'s contract beyond what its call sites exercise."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentworks.remote_presence import TEST_FALSE_EXIT, probe_presence


def test_a_sudo_probe_cannot_take_exit_1_as_absent() -> None:
    """``sudo -n`` exits 1 when it refuses, so a sudo probe answering absent
    with 1 would be a guess; the helper refuses the pairing before running."""
    target = MagicMock()
    with pytest.raises(ValueError):
        probe_presence(target, "test -d /x", absent_exit=TEST_FALSE_EXIT, sudo=True)
    target.run.assert_not_called()
