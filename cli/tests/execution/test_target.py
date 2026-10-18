"""Passive channel facts remain distinct from supplied interfaces."""

from dataclasses import FrozenInstanceError

import pytest

from agentworks.execution.carrier import ChannelFeatures
from agentworks.execution.target import ExecutionIdentity, ExecutionRoute, ExecutionTarget


@pytest.mark.parametrize("live_stdio", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_absent_interfaces_preserve_selected_features(live_stdio: bool, terminal: bool) -> None:
    features = ChannelFeatures(live_stdio=live_stdio, terminal=terminal)
    identity = ExecutionIdentity("worker", 2401, 2402, (2402, 2403))
    target = ExecutionTarget(None, None, features, identity, ExecutionRoute.CANONICAL)
    assert target.execution() is None
    assert target.files() is None
    assert target.features is features
    assert target.identity is identity
    assert target.route is ExecutionRoute.CANONICAL
    with pytest.raises(FrozenInstanceError):
        target.features = ChannelFeatures()  # type: ignore[misc]


@pytest.mark.parametrize("route", list(ExecutionRoute))
def test_identity_and_route_are_frozen_passive_metadata(route: ExecutionRoute) -> None:
    identity = ExecutionIdentity("worker", 2401, 2402, (2402, 2403))
    target = ExecutionTarget(None, None, ChannelFeatures(), identity, route)
    for _ in range(2):
        assert target.identity == ExecutionIdentity("worker", 2401, 2402, (2402, 2403))
        assert target.route is route
    with pytest.raises(FrozenInstanceError):
        target.route = ExecutionRoute.NATIVE  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        target.identity = identity  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        identity.uid = 0  # type: ignore[misc]
