"""Exact retained EC2 observations and retryable original-client cleanup."""

from __future__ import annotations

import gc
import inspect
import sys
import weakref
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import VMStatus
from agentworks.errors import (
    ConfigError,
    LimitExceededError,
    NotFoundError,
    StateError,
    ValidationError,
)
from agentworks.execution.carrier import Deadline
from agentworks.plugins.aws import _native_access
from agentworks.plugins.aws._native_access import EC2OwnedAccess
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.aws.platform import EC2Platform

INSTANCE = "i-0123456789abcdef0"
ACCOUNT = "123456789012"
REGION = "us-east-1"
ENDPOINT = "192.0.2.7"


def response() -> dict[str, Any]:
    return {
        "Reservations": [
            {
                "OwnerId": ACCOUNT,
                "Instances": [
                    {
                        "InstanceId": INSTANCE,
                        "State": {"Name": "running"},
                        "PublicIpAddress": ENDPOINT,
                    }
                ],
            }
        ]
    }


@pytest.fixture
def owned_read(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    events: list[str] = []
    vm = SimpleNamespace(
        name="vm-one",
        platform_metadata={
            "instance_id": INSTANCE,
            "region": REGION,
            "account_id": ACCOUNT,
        },
    )
    platform = EC2Platform("aws-site", {"region": "eu-west-1", "auth": {"mode": "ambient"}})
    access = EC2OwnedAccess(vm, platform, RunContext())
    probe = SimpleNamespace(
        access=access,
        vm=vm,
        platform=platform,
        clock=clock,
        events=events,
        clients=[],
        configs=[],
        result=response(),
        construct_error=None,
        read_error=None,
        close_error=None,
        construct_hook=lambda: None,
        read_hook=lambda: None,
        close_hook=lambda: None,
    )

    class Client:
        meta = SimpleNamespace(events=SimpleNamespace(register=lambda *args: None))

        def describe_instances(self, **kwargs):
            assert access._read_client is self
            assert kwargs == {"InstanceIds": [INSTANCE]}
            events.append("describe")
            probe.read_hook()
            if probe.read_error is not None:
                raise probe.read_error
            return probe.result

        def close(self):
            assert access._read_client is self
            events.append("close")
            probe.close_hook()
            if probe.close_error is not None:
                raise probe.close_error

    def client(service, **kwargs):
        assert service == "ec2" and kwargs["region_name"] == REGION
        config = kwargs["config"]
        assert 0 < config.connect_timeout <= 5 and config.connect_timeout == config.read_timeout
        assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
        probe.configs.append(config)
        events.append("client")
        probe.construct_hook()
        if probe.construct_error is not None:
            raise probe.construct_error
        original = Client()
        probe.clients.append(original)
        return original

    session = SimpleNamespace(client=client)

    def ambient(region):
        assert region == "eu-west-1"
        events.append("session")
        return session

    def deny(*args, **kwargs):
        pytest.fail("unexpected legacy or alternate credential path")

    monkeypatch.setattr(_native_access, "_build_ambient_session", ambient)
    monkeypatch.setattr(_native_access, "_build_access_key_session", deny)
    for name in ("_get_session", "_client", "_read_exact_instance"):
        monkeypatch.setattr(platform, name, deny)
    probe.session = session
    return probe


def test_constructor_is_passive(owned_read, monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("constructor reached selected credentials")

    monkeypatch.setattr(_native_access, "_build_ambient_session", deny)
    monkeypatch.setattr(_native_access, "_build_access_key_session", deny)
    access = EC2OwnedAccess(owned_read.vm, owned_read.platform, RunContext())
    assert not access.cleanup_incomplete and owned_read.events == []
    assert access.close(Deadline.after(5))
    assert owned_read.events == []


def test_selected_credentials_never_use_legacy_client_cache(owned_read):
    probe = owned_read
    for _ in range(2):
        assert probe.access.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    assert probe.events == [
        "session",
        "client",
        "describe",
        "close",
        "client",
        "describe",
        "close",
    ]
    assert probe.clients[0] is not probe.clients[1]
    assert probe.platform._session_cached is None and probe.platform._clients == {}


@pytest.mark.parametrize("role", [None, "arn:aws:iam::123456789012:role/offline"])
def test_explicit_credential_mode_delivers_selected_secret_without_fallback(owned_read, monkeypatch, role):
    class Secrets:
        def get(self, name):
            assert name == "selected-secret"
            return "synthetic-secret"

    platform = EC2Platform(
        "selected-site",
        {
            "region": REGION,
            "auth": {
                "mode": "access-key",
                "access_key_id": "OFFLINEKEY",
                "access_key_secret": "selected-secret",
                "assume_role_arn": role,
            },
        },
    )
    calls = []

    def explicit(auth, secret, site, region, **kwargs):
        calls.append((auth.assume_role_arn, secret, site, region))
        return owned_read.session

    def deny(*args, **kwargs):
        pytest.fail("unexpected ambient fallback")

    monkeypatch.setattr(_native_access, "_build_access_key_session", explicit)
    monkeypatch.setattr(_native_access, "_build_ambient_session", deny)
    access = EC2OwnedAccess(owned_read.vm, platform, RunContext(secrets=Secrets()))

    def client(*args, **kwargs):
        def read(**kwargs):
            assert access.cleanup_incomplete
            return response()

        return SimpleNamespace(
            describe_instances=read,
            close=lambda: None,
            meta=SimpleNamespace(events=SimpleNamespace(register=lambda *args: None)),
        )

    monkeypatch.setattr(owned_read.session, "client", client)
    assert access.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    assert calls == [(role, "synthetic-secret", "selected-site", REGION)]


@pytest.mark.parametrize(
    "failure",
    [ConfigError("unresolved synthetic secret"), EC2Error("empty secret", "offline")],
)
def test_configured_credential_failure_has_no_ambient_fallback(owned_read, monkeypatch, failure):
    class Secrets:
        def get(self, name):
            return ""

    platform = EC2Platform(
        "selected-site",
        {
            "region": REGION,
            "auth": {
                "mode": "access-key",
                "access_key_id": "OFFLINEKEY",
                "access_key_secret": "secret",
            },
        },
    )
    calls = []

    def explicit(*args, **kwargs):
        calls.append("explicit")
        raise failure

    def deny(*args):
        pytest.fail("unexpected ambient fallback")

    monkeypatch.setattr(_native_access, "_build_access_key_session", explicit)
    monkeypatch.setattr(_native_access, "_build_ambient_session", deny)
    access = EC2OwnedAccess(owned_read.vm, platform, RunContext(secrets=Secrets()))
    with pytest.raises(type(failure)) as caught:
        access.observe_locator(Deadline.after(5))
    assert caught.value is failure and calls == ["explicit"]
    assert not access.cleanup_incomplete and owned_read.events == []


def test_missing_context_secret_refuses_before_session_construction(owned_read, monkeypatch):
    platform = EC2Platform(
        "selected-site",
        {
            "region": REGION,
            "auth": {
                "mode": "access-key",
                "access_key_id": "OFFLINEKEY",
                "access_key_secret": "secret",
            },
        },
    )

    def deny(*args):
        pytest.fail("unresolved secret reached a credential builder")

    monkeypatch.setattr(_native_access, "_build_access_key_session", deny)
    monkeypatch.setattr(_native_access, "_build_ambient_session", deny)
    access = EC2OwnedAccess(owned_read.vm, platform, RunContext())
    with pytest.raises(ConfigError):
        access.observe_power(Deadline.after(5))
    assert owned_read.events == [] and not access.cleanup_incomplete


def test_observations_share_exact_read_identity_and_deadline(owned_read, monkeypatch):
    access = owned_read.access
    deadline = Deadline.after(5)
    observed = []
    original = access._read_exact_instance

    def read(selected):
        observed.append(selected)
        return original(selected)

    monkeypatch.setattr(access, "_read_exact_instance", read)
    callback = access.observe_locator
    assert callback.__self__ is access
    assert access.observe_power(deadline) is VMStatus.RUNNING
    assert callback(deadline) == ProviderLocator(f"aws-ec2:{ACCOUNT}:{REGION}:{INSTANCE}")
    assert access.observe_public_endpoint(deadline) == ENDPOINT
    assert len(observed) == 3 and all(selected is deadline for selected in observed)
    assert owned_read.events.count("describe") == 3


@pytest.mark.parametrize("deadline", [Deadline(None), Deadline(0)])
def test_invalid_or_expired_deadline_is_passive(owned_read, deadline):
    with pytest.raises((ValidationError, LimitExceededError)):
        owned_read.access.observe_power(deadline)
    assert owned_read.events == []


@pytest.mark.parametrize(
    "key,value",
    [("instance_id", "i-wrong"), ("region", "INVALID"), ("account_id", "wrong")],
)
def test_invalid_persisted_identity_refuses_credentials(owned_read, key, value):
    owned_read.vm.platform_metadata[key] = value
    with pytest.raises(StateError):
        owned_read.access.observe_locator(Deadline.after(5))
    assert owned_read.events == []


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_constructor_failure_has_no_returned_original(owned_read, failure_type):
    probe = owned_read
    failure = failure_type("synthetic constructor failure")
    probe.construct_error = failure
    with pytest.raises(EC2Error if failure_type is OSError else failure_type) as caught:
        probe.access.observe_power(Deadline.after(5))
    assert (caught.value.__cause__ if failure_type is OSError else caught.value) is failure
    assert probe.events == ["session", "client"] and not probe.access.cleanup_incomplete


@pytest.mark.parametrize("boundary", ["construct", "read", "close"])
def test_late_result_refuses_and_closes_original(owned_read, boundary):
    probe = owned_read
    setattr(probe, f"{boundary}_hook", lambda: probe.clock.__setitem__(0, 106.0))
    with pytest.raises(LimitExceededError):
        probe.access.observe_power(Deadline.after(5))
    assert probe.events == [
        "session",
        "client",
        *([] if boundary == "construct" else ["describe"]),
        "close",
    ]
    assert not probe.access.cleanup_incomplete


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"Reservations": []},
        {"Reservations": [{"OwnerId": ACCOUNT, "Instances": []}]},
    ],
)
def test_exact_identity_rejects_malformed_provider_response(owned_read, bad):
    owned_read.result = bad
    with pytest.raises((EC2Error, NotFoundError)):
        owned_read.access.observe_locator(Deadline.after(5))
    assert owned_read.events[-1] == "close" and not owned_read.access.cleanup_incomplete


@pytest.mark.parametrize("field,value", [("OwnerId", "999999999999"), ("InstanceId", "i-99999999999999999")])
def test_exact_identity_rejects_changed_provider_response(owned_read, field, value):
    reservation = owned_read.result["Reservations"][0]
    (reservation if field == "OwnerId" else reservation["Instances"][0])[field] = value
    with pytest.raises(StateError if field == "OwnerId" else EC2Error):
        owned_read.access.observe_locator(Deadline.after(5))
    assert owned_read.events[-1] == "close"


@pytest.mark.parametrize(
    "state,expected",
    [
        ("stopped", VMStatus.STOPPED),
        ("pending", VMStatus.UNKNOWN),
        (None, VMStatus.UNKNOWN),
        (5, VMStatus.UNKNOWN),
    ],
)
def test_power_does_not_authorize_unknown_state(owned_read, state, expected):
    owned_read.result["Reservations"][0]["Instances"][0]["State"] = {"Name": state}
    assert owned_read.access.observe_power(Deadline.after(5)) is expected


@pytest.mark.parametrize("primary_type", [None, OSError, KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("close_type", [None, OSError, KeyboardInterrupt, SystemExit])
def test_outcomes_preserve_primary_and_cleanup_custody(owned_read, primary_type, close_type):
    probe = owned_read
    primary = primary_type("primary") if primary_type else None
    cleanup = close_type("cleanup") if close_type else None
    probe.read_error, probe.close_error = primary, cleanup
    expected = (
        primary
        if primary is not None and not isinstance(primary, Exception)
        else (cleanup if cleanup is not None and not isinstance(cleanup, Exception) else primary)
    )
    if expected is None:
        assert probe.access.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    else:
        with pytest.raises(EC2Error if isinstance(expected, OSError) else type(expected)) as caught:
            probe.access.observe_power(Deadline.after(5))
        assert (caught.value.__cause__ if isinstance(expected, OSError) else caught.value) is expected
        del caught
    for error in (primary, cleanup):
        if error is not None:
            error.__traceback__ = None
    original = weakref.ref(probe.clients.pop())
    gc.collect()
    assert probe.access.cleanup_incomplete is (cleanup is not None)
    assert (probe.access._read_client is original() and original() is not None) if cleanup else original() is None
    assert probe.events == ["session", "client", "describe", "close"]


@pytest.mark.parametrize("boundary", ["entry", "bookkeeping"])
@pytest.mark.parametrize("primary_type", [None, KeyboardInterrupt, SystemExit])
def test_cleanup_python_boundary_keeps_original_control_and_custody(owned_read, boundary, primary_type):
    probe = owned_read
    primary = primary_type("primary") if primary_type else None
    injected = SystemExit("cleanup boundary")
    probe.read_error = primary
    method = probe.access._close_read_client.__func__
    _, offset = inspect.getsourcelines(method)
    fired: list[bool] = []

    def interrupt(frame, event, arg):
        if event == "line" and frame.f_code is method.__code__ and not fired:
            ready = frame.f_lineno == offset + 1 if boundary == "entry" else probe.events[-1:] == ["close"]
            if ready:
                fired.append(True)
                raise injected
        return interrupt

    prior = sys.gettrace()
    sys.settrace(interrupt)
    try:
        with pytest.raises(type(primary or injected)) as caught:
            probe.access.observe_power(Deadline.after(5))
        assert caught.value is (primary or injected)
        del caught
    finally:
        sys.settrace(prior)
    for error in (primary, injected):
        if error is not None:
            error.__traceback__ = None
    original = weakref.ref(probe.clients.pop())
    assert fired == [True] and original() is not None and probe.access._read_client is original()
    assert probe.access.cleanup_incomplete
    assert probe.events.count("close") == int(boundary == "bookkeeping")


def test_constructor_reentry_cannot_create_another_client(owned_read):
    probe = owned_read

    def reenter():
        with pytest.raises(StateError):
            probe.access.observe_locator(Deadline.after(0.001))

    probe.construct_hook = reenter
    assert probe.access.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    assert probe.events == ["session", "client", "describe", "close"]


def test_occupied_slot_blocks_new_observations(owned_read):
    probe = owned_read
    probe.close_error = OSError("uncertain close")
    assert probe.access.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    before = list(probe.events)
    for observe in (
        probe.access.observe_power,
        probe.access.observe_locator,
        probe.access.observe_public_endpoint,
    ):
        with pytest.raises(StateError):
            observe(Deadline.after(5))
    assert probe.events == before


def test_close_retries_original_without_request_replay(owned_read):
    probe = owned_read
    probe.close_error = OSError("uncertain close")
    assert probe.access.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    original = probe.access._read_client
    assert not probe.access.close(Deadline.after(5)) and probe.access._read_client is original
    probe.close_error = None
    assert probe.access.close(Deadline.after(5)) and not probe.access.cleanup_incomplete
    assert probe.events == ["session", "client", "describe", "close", "close", "close"]


def test_closed_access_refuses_all_observations(owned_read):
    probe = owned_read
    assert probe.access.close(Deadline.after(5))
    for observe in (
        probe.access.observe_power,
        probe.access.observe_locator,
        probe.access.observe_public_endpoint,
    ):
        with pytest.raises(StateError):
            observe(Deadline.after(5))
    assert probe.access.close(Deadline.after(5)) and probe.events == []


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_close_interruption_retains_original_and_stops_admission(owned_read, control_type):
    probe = owned_read
    probe.close_error = OSError("initial uncertain close")
    probe.access.observe_power(Deadline.after(5))
    original = probe.access._read_client
    control = control_type("cleanup interrupted")
    probe.close_error = control
    with pytest.raises(control_type) as caught:
        probe.access.close(Deadline.after(5))
    assert caught.value is control and probe.access._read_client is original
    with pytest.raises(StateError):
        probe.access.observe_locator(Deadline.after(5))
    probe.close_error = None
    assert probe.access.close(Deadline.after(5))
    assert probe.events.count("describe") == 1 and len(probe.clients) == 1
