"""Actual-pipeline strict JSON and inert HTTP framing regressions."""

from __future__ import annotations

import io
from http.client import HTTPResponse

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import Deadline
from tests.plugins.gcp.test_activation_sdk import (
    assert_receipt,
    body,
)
from tests.plugins.gcp.test_activation_sdk import (
    isolated_config as isolated_config,
)
from tests.plugins.gcp.test_activation_sdk import (
    sdk as sdk,
)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_constant_in_discarded_field_cannot_publish_ack(sdk, constant):
    sdk.data = body(sdk.adapter)[:-1] + f', "discarded": {constant}}}'.encode()
    with pytest.raises(ValidationError):
        sdk.adapter.start(Deadline.after(5))
    assert sdk.adapter.payload.operation_name is None
    assert_receipt(sdk, False)
    assert sdk.closes == ["response", "service", "credential"] and not sdk.adapter.cleanup_incomplete


def test_ordinary_discarded_fields_remain_accepted(sdk):
    sdk.data = body(sdk.adapter)[:-1] + b', "discarded": [null, true, -1, 1.5, "NaN", {"nested": "Infinity"}]}'
    assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    assert_receipt(sdk, True)


class InertSocket:
    """Only the public makefile input required by stdlib HTTPResponse, no socket."""

    def __init__(self, wire: bytes) -> None:
        self.wire = wire

    def makefile(self, mode: str) -> io.BytesIO:
        assert mode == "rb"
        return io.BytesIO(self.wire)


@pytest.mark.parametrize("late", [False, True])
def test_nonempty_final_framed_read_retains_matching_late_ack(sdk, monkeypatch, late):
    selected = body(sdk.adapter)
    wire = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(selected)).encode() + b"\r\n\r\n" + selected
    response = HTTPResponse(InertSocket(wire))
    response.begin()
    assert not response.isclosed()
    sdk.data = response
    deadline = Deadline.after(5)
    expired: list[bool] = []
    original = Deadline.remaining
    received: list[bytes] = []

    def read(data):
        assert data == selected and response.isclosed()
        received.append(data)
        if late:
            expired.append(True)

    def remaining(self):
        return 0 if self is deadline and expired else original(self)

    monkeypatch.setattr(Deadline, "remaining", remaining)
    sdk.read_callback = read
    if late:
        with pytest.raises(TimeoutError):
            sdk.adapter.start(deadline)
    else:
        assert sdk.adapter.start(deadline) == "Operation_1"
    assert received == [selected] and sdk.responses[0].raw.reads == [8192]
    assert sdk.responses[0].raw.isclosed() and sdk.adapter.payload.operation_name == "Operation_1"
    assert_receipt(sdk, True)
    assert sdk.closes == ["response", "service", "credential"] and not sdk.adapter.cleanup_incomplete
