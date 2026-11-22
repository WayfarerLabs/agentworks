"""EC2 route calls that do not finish: each ends at its bound, and an authorize cut short is revoked and named.

Opening the route bounds each call on its own; a held removal shares one bound across its revokes.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import wall_bound
from agentworks.plugins.aws import network as aws_network
from agentworks.plugins.aws import platform as aws_platform
from agentworks.plugins.aws.network import EC2Error
from tests._aws_fakes import install_fakes
from tests.test_aws_ec2_ops import _DETECTED_PREFIX, _config, _ec2, _platform, _vm
from tests.test_aws_ec2_ops import _stub_egress_detection as _stub_egress_detection

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tests.conftest import CapturedOutput

# The shrunk bound, and how long a held call waits for release before it goes on anyway, so code that waits
# for the call itself fails the test rather than hanging it.
_BOUND = 0.2
_CAP = 10.0


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Holds the named method of a fake client (for ``seconds``, or until released), with the bounds shrunk.

    Dry runs go through at once. At teardown it releases every hold and joins the threads held.
    """
    monkeypatch.setattr(aws_platform, "_ROUTE_CALL_TIMEOUT_SECONDS", _BOUND)
    monkeypatch.setattr(aws_network, "_REMOVAL_TIMEOUT_SECONDS", _BOUND)
    release = threading.Event()
    senders: list[threading.Thread] = []

    def _hold(client: Any, method: str, seconds: float | None = None) -> None:
        real = getattr(client, method)

        def _held(**kwargs: Any) -> Any:
            if kwargs.get("DryRun"):
                return real(**kwargs)
            senders.append(threading.current_thread())
            release.wait(_CAP if seconds is None else seconds)
            return real(**kwargs)

        monkeypatch.setattr(client, method, _held)

    yield _hold
    release.set()
    for sender in senders:  # so a call given up on does not outlive the test
        if sender is not threading.current_thread():
            sender.join(_CAP)


def test_a_route_revoke_that_outlives_the_bound_warns_at_the_bound(
    monkeypatch: pytest.MonkeyPatch, hold: Callable[..., None], captured_output: CapturedOutput
) -> None:
    install_fakes(monkeypatch)
    platform = _platform()

    with platform._transient_route(_vm(), RunContext(config=_config())):
        hold(_ec2(platform), "revoke_security_group_ingress")
        started = time.monotonic()
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2
    assert any(_DETECTED_PREFIX in warning for warning in captured_output.warnings)


def test_a_poke_whose_authorize_outlives_the_bound_revokes_and_warns_it_may_still_land(
    monkeypatch: pytest.MonkeyPatch, hold: Callable[..., None], captured_output: CapturedOutput
) -> None:
    rec = install_fakes(monkeypatch)
    platform = _platform()
    hold(_ec2(platform), "authorize_security_group_ingress")

    started = time.monotonic()
    with pytest.raises(EC2Error), platform._transient_route(_vm(), RunContext(config=_config())):
        pytest.fail("the body must not run when the poke does not finish")
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2
    assert _DETECTED_PREFIX in rec.revoked_cidrs()  # the revoke ran before the warning
    (warning,) = captured_output.warnings
    assert _DETECTED_PREFIX in warning


def test_a_held_removal_shares_one_bound_across_its_revokes(
    monkeypatch: pytest.MonkeyPatch, hold: Callable[..., None], captured_output: CapturedOutput
) -> None:
    """Each revoke alone is inside the bound; one bound per revoke would hold Ctrl-C for both."""
    install_fakes(monkeypatch)
    platform = _platform()
    monkeypatch.setattr(aws_network, "_REMOVAL_TIMEOUT_SECONDS", 0.5)

    with platform._transient_route(_vm(), RunContext(config=_config(["198.51.100.0/24"]))):
        hold(_ec2(platform), "revoke_security_group_ingress", seconds=0.4)
        started = time.monotonic()
    elapsed = time.monotonic() - started

    assert elapsed < 0.75  # one 0.5s bound, where a bound per revoke would take 0.4 + 0.4
    (warning,) = captured_output.warnings
    assert "198.51.100.0/24" in warning


def test_every_call_on_a_route_runs_as_a_bounded_call(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch)
    platform = _platform()
    ec2 = _ec2(platform)
    record = ec2._record
    threads: list[tuple[str, str]] = []

    def _recorded(method: str, **kwargs: Any) -> None:
        threads.append((method, threading.current_thread().name))
        record(method, **kwargs)

    monkeypatch.setattr(ec2, "_record", _recorded)

    with platform._transient_route(_vm(), RunContext(config=_config())):
        platform._public_address(_vm(), RunContext())
    platform.close_provisioning_access(_vm(), RunContext())

    assert {method for method, _thread in threads} == {
        "revoke_security_group_ingress",
        "authorize_security_group_ingress",
        "describe_instances",
    }
    assert {thread for _method, thread in threads} == {wall_bound.THREAD_NAME}


def test_an_interrupted_authorize_is_revoked_and_named_even_when_a_second_ctrl_c_lands_during_the_revoke(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The authorize may still land after its revoke; the warning goes out before either interrupt propagates."""
    install_fakes(monkeypatch)
    platform = _platform()
    ec2 = _ec2(platform)
    revoke = ec2.revoke_security_group_ingress

    def _interrupted_authorize(**_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    def _revoke_then_ctrl_c(**kwargs: Any) -> Any:
        if not kwargs.get("DryRun"):
            signal.raise_signal(signal.SIGINT)
        return revoke(**kwargs)

    monkeypatch.setattr(ec2, "authorize_security_group_ingress", _interrupted_authorize)
    monkeypatch.setattr(ec2, "revoke_security_group_ingress", _revoke_then_ctrl_c)

    with pytest.raises(KeyboardInterrupt), platform._transient_route(_vm(), RunContext(config=_config())):
        pytest.fail("the body must not run when the poke is interrupted")

    (warning,) = captured_output.warnings
    assert _DETECTED_PREFIX in warning
