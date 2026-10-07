"""Positive resource closure from an already validated exact-run observation."""

from __future__ import annotations

from ._managed_job_store import FactName
from ._managed_observation_exchange import ManagedObservationCandidate, ManagedObservationState
from ._managed_observation_protocol import ControllerState
from .carrier import Dispatch, ExitStatus


def terminal_observation_proved(candidate: ManagedObservationCandidate | None, expected_launch: bytes) -> bool:
    """Require settled exact launch, closed streams, empty workload and controller.

    The observation decoder has already validated fact and controller binding.
    Callers separately require acknowledged launch authority. Application WAIT
    precision is independent of these positive resource-closure facts.
    """
    observation = None if candidate is None else candidate.observation
    facts = {} if observation is None else dict(observation.facts)
    required = {FactName.LAUNCH, FactName.STDOUT_END, FactName.STDERR_END, FactName.BOUNDARY_EMPTY}
    return (
        candidate is not None
        and candidate.dispatch is Dispatch.SENT
        and candidate.carrier_completion == ExitStatus(0)
        and candidate.carrier_failure is None
        and observation is not None
        and observation.state is ManagedObservationState.OBSERVED
        and facts.get(FactName.LAUNCH) == expected_launch
        and required.issubset(facts)
        and observation.controller is not None
        and observation.controller.state in {ControllerState.EXITED, ControllerState.ABSENT}
    )
