"""Passive channel facts remain distinct from supplied interfaces."""

from dataclasses import FrozenInstanceError

import pytest

from agentworks.execution.carrier import ChannelFeatures
from agentworks.execution.target import ExecutionTarget


@pytest.mark.parametrize("live_stdio", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_absent_interfaces_preserve_selected_features(live_stdio: bool, terminal: bool) -> None:
    features = ChannelFeatures(live_stdio=live_stdio, terminal=terminal)
    target = ExecutionTarget(None, None, features)
    assert target.execution() is None
    assert target.files() is None
    assert target.features is features
    with pytest.raises(FrozenInstanceError):
        target.features = ChannelFeatures()  # type: ignore[misc]
