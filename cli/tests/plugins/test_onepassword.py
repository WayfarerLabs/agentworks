"""Configured-source contract for the bundled 1Password backend."""

from __future__ import annotations

import subprocess
from contextlib import AbstractContextManager
from typing import cast

import pytest
from pydantic import ValidationError

import agentworks.plugins.onepassword.backend as onepassword_backend
from agentworks.capabilities.secret_backend import (
    BackendFailed,
    BackendResolved,
    FailureReason,
    IndeterminateReason,
    OperatorImpact,
    PreviewAvailable,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewIntent,
    ResolutionIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.plugins.onepassword.backend import (
    AppAuthenticationImpact,
    OnePasswordBackend,
    OnePasswordMapping,
    OnePasswordSourceConfig,
    _BoundedRead,
)


def _request(
    reference: str = "op://Work/item/password",
    *,
    name: str = "token",
) -> SecretLookupRequest:
    return SecretLookupRequest(name=name, mapping=OnePasswordMapping.model_validate(reference))


def _client(
    *,
    intent: PreviewIntent | ResolutionIntent,
    account: str | None = None,
    timeout: float = 30.0,
    app_impact: AppAuthenticationImpact = AppAuthenticationImpact.OPERATOR_ACTION,
    tty_access: TtyInteractionAccess = TtyInteractionAccess.DISABLED,
) -> tuple[AbstractContextManager[SecretSourceClient], SecretSourceClient]:
    context = OnePasswordBackend.create_client(
        config=OnePasswordSourceConfig(
            name="onepassword",
            account=account,
            timeout=timeout,
            app_authentication_impact=app_impact,
        ),
        intent=intent,
        tty_access=tty_access,
        interaction_broker=None,
    )
    return context, context.__enter__()


def test_config_and_mapping_validation() -> None:
    config = OnePasswordSourceConfig(name="onepassword", account="work.example.com", timeout=7)
    assert config.timeout == 7.0
    assert config.app_authentication_impact is AppAuthenticationImpact.OPERATOR_ACTION
    for invalid in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            OnePasswordSourceConfig(name="onepassword", timeout=invalid)
    assert OnePasswordMapping.model_validate("op://Work/item/password").root == "op://Work/item/password"
    for invalid_mapping in (
        "https://example.test",
        "op://Work/item",
        {"reference": "op://Work/item/password"},
    ):
        with pytest.raises(ValidationError):
            OnePasswordMapping.model_validate(invalid_mapping)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("operator-action", AppAuthenticationImpact.OPERATOR_ACTION),
        ("none", AppAuthenticationImpact.NONE),
    ],
)
def test_operator_authored_app_authentication_impact_values_parse(
    written: str,
    expected: AppAuthenticationImpact,
) -> None:
    config = OnePasswordSourceConfig.model_validate({"name": "onepassword", "app_authentication_impact": written})

    assert config.app_authentication_impact is expected


@pytest.mark.parametrize("written", [b"none", bytearray(b"none")])
def test_app_authentication_impact_rejects_binary_inputs(written: bytes | bytearray) -> None:
    with pytest.raises(ValidationError):
        OnePasswordSourceConfig.model_validate({"name": "onepassword", "app_authentication_impact": written})


def test_bounded_read_repr_redacts_value() -> None:
    bounded = _BoundedRead(value="sentinel-resolved")
    assert "sentinel-resolved" not in repr(bounded)


def test_factory_and_context_entry_do_no_provider_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("provider work started"))
    context = OnePasswordBackend.create_client(
        config=OnePasswordSourceConfig(name="onepassword"),
        intent=ResolutionIntent(),
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=None,
    )
    client = context.__enter__()
    context.__exit__(None, None, None)
    assert client is not None


def test_resolution_exact_subprocess_boundary_ignores_tty_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    clock = iter((100.0, 102.5))
    monkeypatch.setattr(onepassword_backend, "_MONOTONIC", lambda: next(clock))

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="resolved", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client(
        intent=ResolutionIntent(),
        account="work.example.com",
        timeout=7.0,
        tty_access=TtyInteractionAccess.DISABLED,
    )
    try:
        result = client.resolve((_request(),))["token"]
    finally:
        context.__exit__(None, None, None)
    assert result == BackendResolved("resolved")
    [(argv, kwargs)] = calls
    assert argv == ["op", "read", "--no-newline", "--account", "work.example.com", "op://Work/item/password"]
    assert cast("float", kwargs["timeout"]) == pytest.approx(4.5)
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "check": False,
        "stdin": subprocess.DEVNULL,
        "timeout": kwargs["timeout"],
    }


@pytest.mark.parametrize(
    "intent",
    [PreviewIntent(OperatorImpact.ALLOW), ResolutionIntent()],
    ids=("preview", "resolution"),
)
def test_configured_timeout_shrinks_across_the_whole_source_turn(
    monkeypatch: pytest.MonkeyPatch,
    intent: PreviewIntent | ResolutionIntent,
) -> None:
    clock = iter((100.0, 102.0, 105.0))
    monkeypatch.setattr(onepassword_backend, "_MONOTONIC", lambda: next(clock))
    timeouts: list[float] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(cast("float", kwargs["timeout"]))
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(args, 0, stdout="resolved", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client(intent=intent, timeout=10.0)
    requests = (
        _request("op://Work/first/password", name="first"),
        _request("op://Work/second/password", name="second"),
    )
    try:
        if isinstance(intent, PreviewIntent):
            client.preview(requests)
        else:
            client.resolve(requests)
    finally:
        context.__exit__(None, None, None)
    assert timeouts == [8.0, 5.0]


def test_zero_impact_is_indeterminate_without_provider_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("must not spawn"))
    context, client = _client(intent=PreviewIntent(OperatorImpact.NONE))
    try:
        result = client.preview((_request(),))["token"]
    finally:
        context.__exit__(None, None, None)
    assert result == PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)


@pytest.mark.parametrize("unattended", ["configured", "service-account", "connect"])
def test_zero_impact_reads_when_noninteractive_authentication_is_known(
    monkeypatch: pytest.MonkeyPatch,
    unattended: str,
) -> None:
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)
    app_impact = AppAuthenticationImpact.OPERATOR_ACTION
    if unattended == "configured":
        app_impact = AppAuthenticationImpact.NONE
    elif unattended == "service-account":
        monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "token")
    else:
        monkeypatch.setenv("OP_CONNECT_HOST", "https://connect.example.test")
        monkeypatch.setenv("OP_CONNECT_TOKEN", "token")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="discarded", stderr=""),
    )
    context, client = _client(intent=PreviewIntent(OperatorImpact.NONE), app_impact=app_impact)
    try:
        result = client.preview((_request(),))["token"]
    finally:
        context.__exit__(None, None, None)
    assert result == PreviewAvailable()


def test_allow_impact_is_definitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="discarded", stderr=""),
    )
    context, client = _client(intent=PreviewIntent(OperatorImpact.ALLOW))
    try:
        result = client.preview((_request(),))["token"]
    finally:
        context.__exit__(None, None, None)
    assert result == PreviewAvailable()


@pytest.mark.parametrize(
    ("native", "reason"),
    [
        ("timeout", FailureReason.DEADLINE_EXCEEDED),
        ("oserror", FailureReason.CONNECTIVITY),
        ("not currently signed in", FailureReason.AUTHENTICATION),
        ("no such item", FailureReason.LOOKUP_REJECTED),
        ("network is unreachable", FailureReason.CONNECTIVITY),
        ("dial tcp: lookup api.1password.com: no such host", FailureReason.CONNECTIVITY),
        ("provider exploded", FailureReason.EXTERNAL),
    ],
)
def test_provider_failures_project_to_closed_reasons(
    monkeypatch: pytest.MonkeyPatch,
    native: str,
    reason: FailureReason,
) -> None:
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if native == "timeout":
            raise subprocess.TimeoutExpired(cmd=args, timeout=1, output="secret", stderr="native")
        if native == "oserror":
            raise OSError("native")
        return subprocess.CompletedProcess(args, 1, stdout="secret", stderr=native)

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client(intent=ResolutionIntent())
    try:
        actual = client.resolve((_request(),))["token"]
    finally:
        context.__exit__(None, None, None)
    assert actual == BackendFailed(reason)

    context, client = _client(intent=PreviewIntent(OperatorImpact.ALLOW))
    try:
        preview = client.preview((_request(),))["token"]
    finally:
        context.__exit__(None, None, None)
    assert preview == PreviewFailed(reason)


def test_not_found_is_not_claimed_as_ordinary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr="no such field"),
    )
    context, client = _client(intent=ResolutionIntent())
    try:
        result = client.resolve((_request(),))["token"]
    finally:
        context.__exit__(None, None, None)
    assert result == BackendFailed(FailureReason.LOOKUP_REJECTED)
