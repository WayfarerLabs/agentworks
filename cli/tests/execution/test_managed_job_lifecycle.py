"""Connected private independent-job controls over one persisted run."""

from __future__ import annotations

from pathlib import Path

from agentworks.execution._managed_disposal_access import dispose_bound_managed_run
from agentworks.execution._managed_disposal_exchange import DisposalState
from agentworks.execution._managed_job_store import FactName, Stream
from agentworks.execution._managed_observation_protocol import ManagedResultControl
from agentworks.execution._managed_observe_access import (
    observe_and_reconcile_bound_managed_run,
    read_bound_managed_output,
)
from agentworks.execution._managed_result import collect_bound_managed_result
from agentworks.execution._managed_runs import ManagedLaunchState
from agentworks.execution._managed_stop_access import stop_bound_managed_run
from agentworks.execution._managed_stop_exchange import ManagedStopState

from .test_managed_disposal import ExchangeCarrier, _disposed
from .test_managed_disposal_access import _not_ready
from .test_managed_observation import ScriptedCarrier, _records
from .test_managed_observe_access import RUN, _options, _output_reply, _reserved
from .test_managed_result import _reply as _result_reply
from .test_managed_stop import Carrier as StopCarrier
from .test_managed_stop_access import _response as _stop_response


def test_possible_dispatch_receipt_reconnects_without_relaunch(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path)
    try:
        reserved = repository.inspect(RUN)
        assert reserved is not None
        possible = repository.mark_possible_dispatch(reserved)
        assert possible.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH

        observe_carrier = ScriptedCarrier(
            lambda request: _records(
                request.nonce,
                ManagedResultControl((FactName.LAUNCH,)),
                (request.expected_launch,),
            )
        )
        observed = observe_and_reconcile_bound_managed_run(
            repository,
            RUN,
            **_options(owner, observe_carrier),  # type: ignore[arg-type]
        )
        assert observed.candidate is not None
        assert not observed.requires_owner_retention
        confirmed = repository.inspect(RUN)
        assert confirmed is not None
        assert confirmed.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
        assert confirmed.launch_reconciled_at is not None

        repeated = observe_and_reconcile_bound_managed_run(
            repository,
            RUN,
            **_options(owner, observe_carrier),  # type: ignore[arg-type]
        )
        assert repeated.candidate is not None
        assert repository.inspect(RUN) == confirmed
        assert observe_carrier.calls == 2

        result_carrier = ScriptedCarrier(_result_reply)
        collected = collect_bound_managed_result(
            repository,
            RUN,
            **_options(owner, result_carrier),  # type: ignore[arg-type]
        )
        assert collected.result.ok
        assert not collected.requires_owner_retention
        assert result_carrier.calls == 3
        assert repository.inspect(RUN) == confirmed

        output_carrier = ScriptedCarrier(
            lambda request: _output_reply(request, disposition="complete-capture", content=b"ok")
        )
        output = read_bound_managed_output(
            repository,
            RUN,
            **_options(owner, output_carrier, stream=Stream.STDOUT),  # type: ignore[arg-type]
        )
        assert output.accepted
        assert output.output == b"ok"
        stderr = read_bound_managed_output(
            repository,
            RUN,
            **_options(owner, output_carrier, stream=Stream.STDERR),  # type: ignore[arg-type]
        )
        assert stderr.accepted
        assert stderr.output == b"ok"
        assert output_carrier.calls == 2

        stop_carrier = StopCarrier(lambda request: _stop_response(request, terminated=False))
        accepted = stop_bound_managed_run(
            repository,
            RUN,
            **_options(owner, stop_carrier, obligation_id="1" * 32),  # type: ignore[arg-type]
        )
        assert accepted.state is ManagedStopState.ACCEPTED
        assert not accepted.requires_owner_retention

        disposal_carrier = ExchangeCarrier(_not_ready)
        not_ready = dispose_bound_managed_run(
            repository,
            RUN,
            **_options(owner, disposal_carrier, obligation_id="2" * 32),  # type: ignore[arg-type]
        )
        assert not_ready.state is DisposalState.NOT_READY
        assert not not_ready.requires_owner_retention

        terminated_carrier = StopCarrier(lambda request: _stop_response(request, terminated=True))
        terminated = stop_bound_managed_run(
            repository,
            RUN,
            **_options(owner, terminated_carrier, obligation_id="3" * 32),  # type: ignore[arg-type]
        )
        assert terminated.state is ManagedStopState.TERMINATED

        disposed_carrier = ExchangeCarrier(_disposed)
        disposed = dispose_bound_managed_run(
            repository,
            RUN,
            **_options(owner, disposed_carrier, obligation_id="4" * 32),  # type: ignore[arg-type]
        )
        assert disposed.state is DisposalState.DISPOSED
        assert repository.inspect(RUN) == confirmed
        assert (observe_carrier.calls, stop_carrier.calls, terminated_carrier.calls, disposal_carrier.calls) == (
            2,
            1,
            1,
            1,
        )
    finally:
        database.close()
