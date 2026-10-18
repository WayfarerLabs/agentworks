"""Freeze live request policy without borrowing application endpoints."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._managed_job_request import ManagedLiveIO, decode_request, encode_request
from agentworks.execution._managed_lease_wire import sampled_lease
from agentworks.execution._managed_request_adapter import compose_managed_body, compose_managed_request
from agentworks.execution._managed_runs import (
    ManagedOutputMode,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
)
from agentworks.execution.models import Command, Input, Output, Script, Shell

from .test_managed_request_adapter import RUN, _spec

pytestmark = pytest.mark.windows


class Endpoint:
    def try_read(self, limit: int) -> bytes | None:
        raise AssertionError("body composition borrowed input")

    def try_write(self, data: memoryview) -> int | None:
        raise AssertionError("body composition borrowed output")


def _options(*, live_input: bool, live_output: bool) -> dict[str, Any]:
    endpoint = Endpoint()
    return {
        "input": Input.live(endpoint) if live_input else Input.bytes(b"finite\0\xff"),
        "output": Output.live(endpoint, endpoint) if live_output else Output.capture(12),
        "env": {"PRIVATE_VALUE": "environment-canary"},
        "cwd": "/tmp",
        "sensitive": False,
        "identity": RUN,
        "spec": replace(
            _spec(),
            owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "b" * 32),
            lifetime=ManagedRunLifetime.OPERATION,
        ),
        "live_io": ManagedLiveIO("c" * 32, live_input, live_output),
    }


@pytest.mark.parametrize(("live_input", "live_output"), [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("sensitive", [False, True])
def test_live_directions_and_durable_retention_are_independent(
    live_input: bool, live_output: bool, sensitive: bool
) -> None:
    options = _options(live_input=live_input, live_output=live_output)
    options["sensitive"] = sensitive
    body = compose_managed_body(Command(["/bin/cat"]), **options)
    request = body.request
    assert request.live_io is options["live_io"]
    assert request.stdin == (b"" if live_input else b"finite\0\xff")
    assert request.environment == (("PRIVATE_VALUE", "environment-canary"),)
    assert body.output_policy.mode is (
        ManagedOutputMode.DISCARD
        if live_output
        else ManagedOutputMode.SENSITIVITY_SUPPRESSED
        if sensitive
        else ManagedOutputMode.CAPTURE
    )
    assert request.output_mode == body.output_policy.mode.value
    request = replace(request, operation_lease=sampled_lease(request.launch, 123))
    assert decode_request(encode_request(request)) == request
    assert "environment-canary" not in repr(body)


@pytest.mark.parametrize("output", [Output.discard(), Output.capture(0)])
def test_sensitive_live_input_does_not_authorize_presentation(output: Output) -> None:
    options = _options(live_input=True, live_output=False)
    options.update(input=Input.live(Endpoint(), sensitive=True), output=output)
    body = compose_managed_body(Command(["/bin/cat"]), **options)
    assert body.request.live_io is not None and not body.request.live_io.output
    assert body.output_policy.mode is ManagedOutputMode.SENSITIVITY_SUPPRESSED


@pytest.mark.parametrize("change", ["missing", "input", "output", "independent", "type"])
def test_mismatched_live_admission_refuses_before_endpoint_use(change: str) -> None:
    options = _options(live_input=True, live_output=True)
    if change == "missing":
        options["live_io"] = None
    elif change == "input":
        options["input"] = Input.eof()
    elif change == "output":
        options["output"] = Output.discard()
    elif change == "independent":
        options["spec"] = _spec()
    else:
        options["live_io"] = object()
    with pytest.raises(ValidationError):
        compose_managed_body(Command(["/bin/cat"]), **options)


def test_script_source_and_finite_input_remain_distinct_with_live_output() -> None:
    options = _options(live_input=False, live_output=True)
    options["spec"] = replace(
        options["spec"],
        shell=replace(options["spec"].shell, requested=Shell.SH, resolved_executable="/bin/sh"),
    )
    body = compose_managed_body(Script("cat; printf done", Shell.SH), **options)
    request, policy = compose_managed_request(
        Script("cat; printf done", Shell.SH),
        **options,
        operation_lease=sampled_lease(body.request.launch, 123),
    )
    assert request.source == b"cat; printf done"
    assert request.stdin == b"finite\0\xff"
    assert policy.mode is ManagedOutputMode.DISCARD
    assert decode_request(encode_request(request)) == request
