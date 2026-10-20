"""Pure frozen managed bodies and complete private request composition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from itertools import islice
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._managed_job_protocol import ManagedJobFactError, encode_managed_job_fact
from ._managed_job_request import (
    MAX_CAPTURE_PREFIX_BYTES,
    MAX_CONTROL_BYTES,
    MAX_ENVIRONMENT_ENTRIES,
    MAX_SOURCE_BYTES,
    OPERATION_LEASE_CONTROL_HEADROOM,
    ManagedJobRequest,
    ManagedLiveIO,
    RequestError,
    _encode_request_assets,
    decode_request_launch,
    encode_request,
)
from ._managed_runs import (
    ManagedOutputMode,
    ManagedOutputPolicy,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunReceipt,
    ManagedRunSpec,
)
from .models import Command, Input, Output, Script

if TYPE_CHECKING:
    from ._managed_lease_wire import OperationLease


@dataclass(frozen=True, slots=True, repr=False)
class _ManagedBody:
    """Validated immutable caller body, deliberately lacking lease authority."""

    request: ManagedJobRequest
    output_policy: ManagedOutputPolicy

    def __post_init__(self) -> None:
        size = len(_encode_request_assets(self.request, body_only=True)["request-control"])
        if (
            decode_request_launch(self.request.launch)["lifetime"] == "operation"
            and size + OPERATION_LEASE_CONTROL_HEADROOM > MAX_CONTROL_BYTES
        ):
            raise RequestError("operation control exceeds bound with lease headroom")


def compose_managed_body(
    invocation: Command | Script,
    *,
    input: Input,
    output: Output,
    env: Mapping[str, str] | None,
    cwd: str | None,
    sensitive: bool,
    identity: ManagedRunIdentity,
    spec: ManagedRunSpec,
    live_io: ManagedLiveIO | None = None,
) -> _ManagedBody:
    """Snapshot and validate caller input before reservation or clock effects."""
    if (
        type(input) is not Input
        or type(output) is not Output
        or type(sensitive) is not bool
        or type(identity) is not ManagedRunIdentity
        or type(spec) is not ManagedRunSpec
    ):
        raise ValidationError("Invalid managed body")
    if not input.allow_staging:
        raise ValidationError("Managed execution requires input staging")
    live_input, live_output = input.source is not None, output.stdout is not None
    if live_io is None:
        if live_input or live_output:
            raise ValidationError("Managed live pipes require explicit foreground composition")
    elif (
        type(live_io) is not ManagedLiveIO
        or spec.lifetime is not ManagedRunLifetime.OPERATION
        or live_io.input != live_input
        or live_io.output != live_output
    ):
        raise ValidationError("Managed live directions do not match the operation request")

    if isinstance(invocation, Command):
        kind, argv, source = "command", invocation.argv, b""
    elif isinstance(invocation, Script):
        shell = spec.shell
        if (
            shell.requested is not invocation.shell
            or shell.login != invocation.login
            or shell.interactive != invocation.interactive
        ):
            raise ValidationError("Script shell identity does not match invocation")
        if len(invocation.source) > MAX_SOURCE_BYTES:
            raise ValidationError("Managed script source exceeds byte bound")
        try:
            source = invocation.source.encode("utf-8")
        except UnicodeError:
            raise ValidationError("Invalid managed script source") from None
        if len(source) > MAX_SOURCE_BYTES:
            raise ValidationError("Managed script source exceeds byte bound")
        kind, argv = "script", ()
    else:
        raise ValidationError("Managed execution requires a command or script")

    if env is None:
        environment: tuple[tuple[str, str], ...] = ()
    else:
        if not isinstance(env, Mapping):
            raise ValidationError("Managed environment must be a string mapping")
        try:
            environment = tuple(sorted(islice(env.items(), MAX_ENVIRONMENT_ENTRIES + 1)))
        except (TypeError, ValueError):
            raise ValidationError("Managed environment must be a string mapping") from None
        if len(environment) > MAX_ENVIRONMENT_ENTRIES:
            raise ValidationError("Managed environment exceeds entry bound")

    if output.max_bytes is not None and output.max_bytes > MAX_CAPTURE_PREFIX_BYTES:
        raise ValidationError("Managed capture exceeds byte bound")
    if live_output:
        # Durable retention and this call's explicit presentation are distinct.
        policy = ManagedOutputPolicy(ManagedOutputMode.DISCARD)
    elif sensitive or input.is_sensitive:
        policy = ManagedOutputPolicy(ManagedOutputMode.SENSITIVITY_SUPPRESSED)
    elif output.max_bytes is None:
        policy = ManagedOutputPolicy(ManagedOutputMode.DISCARD)
    else:
        policy = ManagedOutputPolicy(ManagedOutputMode.CAPTURE, output.max_bytes)
    try:
        launch = encode_managed_job_fact(ManagedRunReceipt(identity, identity.unit_name, spec))
        request = ManagedJobRequest(
            launch,
            kind,
            argv,
            cwd,
            policy.mode.value,
            policy.capture_prefix_bytes,
            environment,
            source,
            input.data if input.data is not None else b"",
            live_io=live_io,
        )
        return _ManagedBody(request, policy)
    except (ManagedJobFactError, RequestError):
        raise ValidationError("Invalid managed request") from None


def compose_managed_request(
    invocation: Command | Script,
    *,
    input: Input,
    output: Output,
    env: Mapping[str, str] | None,
    cwd: str | None,
    sensitive: bool,
    identity: ManagedRunIdentity,
    spec: ManagedRunSpec,
    operation_lease: OperationLease | None = None,
    live_io: ManagedLiveIO | None = None,
) -> tuple[ManagedJobRequest, ManagedOutputPolicy]:
    """Validate one complete request before its caller reserves a durable run."""
    body = compose_managed_body(
        invocation,
        input=input,
        output=output,
        env=env,
        cwd=cwd,
        sensitive=sensitive,
        identity=identity,
        spec=spec,
        live_io=live_io,
    )
    request = replace(body.request, operation_lease=operation_lease)
    try:
        encode_request(request)
    except RequestError:
        raise ValidationError("Invalid managed request") from None
    return request, body.output_policy
