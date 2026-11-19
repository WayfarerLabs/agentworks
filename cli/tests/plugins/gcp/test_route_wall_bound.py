"""A GCE route's provider calls end at their bound on the wall clock, its held removal included."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from agentworks.plugins.gcp import platform as gcp_platform
from tests._gcp_platform_fakes import gce_ctx, gce_guest, gce_platform, gce_request
from tests.plugins.gcp.test_platform import _vm

if TYPE_CHECKING:
    import pytest

    from tests.conftest import CapturedOutput

# The shrunk bound, and how long the held delete waits for release before it goes on anyway, so a removal
# that waits for the request itself fails the test rather than hanging it.
_BOUND = 0.2
_CAP = 10.0


def test_a_route_removal_whose_delete_outlives_the_bound_warns_at_the_bound(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    vm = _vm(platform.create(gce_request(), gce_ctx()).platform_metadata)
    monkeypatch.setattr(gcp_platform, "_OPERATION_TIMEOUT_SECONDS", _BOUND)
    release = threading.Event()
    senders: list[threading.Thread] = []
    delete = cache.firewalls.delete

    def _held_delete(**kwargs: object) -> object:
        senders.append(threading.current_thread())
        release.wait(_CAP)
        return delete(**kwargs)  # type: ignore[arg-type]

    try:
        with platform._transient_route(vm, gce_ctx()):
            (route,) = set(cache.firewalls.resources) - {
                vm.platform_metadata["deny_rule"],
                vm.platform_metadata["allow_rule"],
            }
            monkeypatch.setattr(cache.firewalls, "delete", _held_delete)
            started = time.monotonic()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        for sender in senders:  # so the delete given up on does not outlive the test
            if sender is not threading.current_thread():
                sender.join(_CAP)

    assert elapsed < _CAP / 2
    assert any(route in warning for warning in captured_output.warnings)
