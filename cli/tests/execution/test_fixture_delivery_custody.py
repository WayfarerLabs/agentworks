"""Actual local workers cannot escape a standalone helper fixture."""

import sys

import pytest

from agentworks.execution import _process
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CapturedOutput, CarrierIO, Deadline, PreparedInvocation
from agentworks.execution.carriers._subprocess import ProcessResult
from tests.execution import _bound_carrier_support, test_account_resolution, test_execution_access, test_target_identity
from tests.execution.files import (
    _file_read_support,
    _file_snapshot_support,
    _file_stage_support,
    test_file_inventory_helper,
    test_file_metadata_helper,
    test_file_object_helper,
    test_file_snapshot_exchange,
)
from tests.execution.files.test_file_snapshot_exchange import plan as plan

pytestmark = pytest.mark.windows

_CARRIERS = (
    test_account_resolution.LocalCarrier,
    test_execution_access.LocalCarrier,
    test_target_identity.LocalCarrier,
    _file_read_support.LocalCarrier,
    _file_snapshot_support.LocalCarrier,
    _file_stage_support.LocalCarrier,
    test_file_inventory_helper.LocalCarrier,
    test_file_metadata_helper.LocalCarrier,
    test_file_object_helper.LocalCarrier,
)


def test_later_cleanup_does_not_make_an_initially_pending_report_usable(monkeypatch):
    custody = LocalDeliveryCustody()

    def pending(argv, **kwargs):
        assert kwargs["custody"] is custody
        custody.begin_process()
        return ProcessResult(False, None, None, CapturedOutput(), CapturedOutput(), None)

    monkeypatch.setattr(_bound_carrier_support, "run_process", pending)
    try:
        with pytest.raises(AssertionError):
            _bound_carrier_support.run_fixture_process(
                [sys.executable, "-c", "pass"],
                io=CarrierIO(),
                deadline=Deadline.after(3),
                custody=None,
                standalone_custody=custody,
            )
        assert custody.settled
    finally:
        assert custody.close(Deadline.after(3))


@pytest.mark.parametrize("factory", _CARRIERS, ids=lambda factory: factory.__module__)
def test_standalone_fixture_refuses_to_discard_pending_worker(factory, monkeypatch):
    carrier = factory()
    try:
        with monkeypatch.context() as fault:
            fault.setattr(_process, "_cleanup", lambda status: False)
            with pytest.raises(AssertionError):
                carrier.execute(
                    PreparedInvocation((sys.executable, "-c", "pass")),
                    io=CarrierIO(),
                    deadline=Deadline.after(3),
                )
            assert not carrier.local_delivery.settled
    finally:
        assert carrier.local_delivery.close(Deadline.after(3))


@pytest.mark.parametrize("factory", _CARRIERS, ids=lambda factory: factory.__module__)
def test_external_owner_retains_pending_worker_until_its_teardown(factory, monkeypatch):
    carrier = factory()
    custody = LocalDeliveryCustody()
    try:
        with monkeypatch.context() as fault:
            fault.setattr(_process, "_cleanup", lambda status: False)
            fault.setattr(custody, "close", lambda deadline: pytest.fail("fixture closed its external owner's custody"))
            carrier.execute(
                PreparedInvocation((sys.executable, "-c", "pass")),
                io=CarrierIO(),
                deadline=Deadline.after(3),
                custody=custody,
            )
            assert not custody.settled
            assert carrier.local_delivery.settled
    finally:
        assert custody.close(Deadline.after(3))


@pytest.mark.skipif(sys.platform != "linux", reason="the snapshot helper requires Linux")
def test_missing_runtime_proof_cannot_discard_pending_standalone_worker(tmp_path, monkeypatch, plan):
    carrier = _file_snapshot_support.LocalCarrier()
    monkeypatch.setattr(test_file_snapshot_exchange, "LocalCarrier", lambda: carrier)
    try:
        with monkeypatch.context() as fault:
            fault.setattr(_process, "_cleanup", lambda status: False)
            with pytest.raises(AssertionError):
                test_file_snapshot_exchange.test_missing_runtime_yields_no_snapshot_observation(tmp_path, plan)
            assert not carrier.local_delivery.settled
    finally:
        assert carrier.local_delivery.close(Deadline.after(3))
