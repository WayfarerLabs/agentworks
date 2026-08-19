"""Final configured-source contract for the bundled 1Password backend."""

from __future__ import annotations

import subprocess
from contextlib import AbstractContextManager
from typing import cast

import pytest
from pydantic import ValidationError

from agentworks.capabilities.secret_backend.client import (
    SecretClientFailure,
    SecretClientFailureKind,
    SecretClientTimeout,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.plugins.onepassword.backend import (
    OnePasswordBackend,
    OnePasswordMapping,
    OnePasswordSourceConfig,
    _BoundedRead,
)


def _request(reference: str = "op://Work/item/password") -> SecretLookupRequest:
    return SecretLookupRequest(name="token", mapping=OnePasswordMapping.model_validate(reference))


def _client(
    *,
    account: str | None = None,
    timeout: float = 30.0,
    remaining: float = 12.0,
) -> tuple[AbstractContextManager[SecretSourceClient], SecretSourceClient]:
    context = OnePasswordBackend.create_client(
        source_name="work",
        config=OnePasswordSourceConfig(name="onepassword", account=account, timeout=timeout),
        interaction_broker=None,
        remaining_time=lambda: remaining,
    )
    return context, context.__enter__()


def test_source_config_owns_account_and_positive_finite_timeout() -> None:
    config = OnePasswordSourceConfig(name="onepassword", account="work.example.com", timeout=7)
    assert config.account == "work.example.com"
    assert config.timeout == 7.0
    assert OnePasswordBackend.external_operation_timeout(config) == 7.0
    for invalid in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            OnePasswordSourceConfig(name="onepassword", timeout=invalid)


def test_mapping_is_only_a_scalar_op_reference() -> None:
    assert OnePasswordMapping.model_validate("op://Work/item/password").root == "op://Work/item/password"
    with pytest.raises(ValidationError):
        OnePasswordMapping.model_validate({"account": "work.example.com", "reference": "op://Work/item/password"})


def test_bounded_read_repr_never_contains_a_resolved_value() -> None:
    bounded = _BoundedRead(value="sentinel-resolved")
    assert repr(bounded) == "_BoundedRead(value=<redacted>)"
    assert "sentinel-resolved" not in repr(bounded)


def test_exact_argv_and_remaining_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], float]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, cast("float", kwargs["timeout"])))
        return subprocess.CompletedProcess(args, 0, stdout="resolved", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client(account="work.example.com", remaining=4.5)
    try:
        values = client.resolve((_request(),), remaining_time=lambda: 4.5)
    finally:
        context.__exit__(None, None, None)

    assert values == {"token": "resolved"}
    assert calls == [
        (
            [
                "op",
                "read",
                "--no-newline",
                "--account",
                "work.example.com",
                "op://Work/item/password",
            ],
            4.5,
        )
    ]


def test_zero_remaining_time_never_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("must not spawn"))
    context, client = _client(remaining=0.0)
    try:
        with pytest.raises(SecretClientTimeout) as caught:
            client.resolve((_request(),), remaining_time=lambda: 0.0)
    finally:
        context.__exit__(None, None, None)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    # The backend attaches its fixed timeout guidance on every path that
    # raises SecretClientTimeout, not just the subprocess-timeout one.
    assert caught.value.guidance is not None


def test_subprocess_timeout_translates_without_native_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["op"], timeout=1, output="sentinel-output", stderr="sentinel-error")

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client()
    try:
        with pytest.raises(SecretClientTimeout) as caught:
            client.resolve((_request(),), remaining_time=lambda: 1.0)
    finally:
        context.__exit__(None, None, None)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "sentinel" not in repr(caught.value)
    # The no-leak assertion above must not pass vacuously: confirm the
    # exception actually carries the (fixed, non-native) guidance text.
    assert caught.value.guidance is not None


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("not currently signed in sentinel", SecretClientFailureKind.AUTHENTICATION),
        ("no such item sentinel", SecretClientFailureKind.HARD_MAPPING),
        ("provider exploded sentinel", SecretClientFailureKind.EXTERNAL),
    ],
)
def test_nonzero_results_translate_to_fixed_failure_kinds(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    kind: SecretClientFailureKind,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["op"], 1, stdout="sentinel-stdout", stderr=stderr),
    )
    context, client = _client()
    try:
        with pytest.raises(SecretClientFailure) as caught:
            client.resolve((_request(),), remaining_time=lambda: 2.0)
    finally:
        context.__exit__(None, None, None)
    assert caught.value.kind is kind
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "sentinel" not in repr(caught.value)


def test_missing_binary_is_connectivity(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("sentinel-path")

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client()
    try:
        with pytest.raises(SecretClientFailure) as caught:
            client.resolve((_request(),), remaining_time=lambda: 2.0)
    finally:
        context.__exit__(None, None, None)
    assert caught.value.kind is SecretClientFailureKind.CONNECTIVITY


def test_later_failure_does_not_expose_earlier_value(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(args, 0, stdout="sentinel-resolved", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="sentinel-stdout", stderr="no such item")

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client()
    requests = (
        SecretLookupRequest(name="first", mapping=OnePasswordMapping.model_validate("op://W/a/p")),
        SecretLookupRequest(name="second", mapping=OnePasswordMapping.model_validate("op://W/b/p")),
    )
    try:
        with pytest.raises(SecretClientFailure) as caught:
            client.resolve(requests, remaining_time=lambda: 2.0)
    finally:
        context.__exit__(None, None, None)
    assert "sentinel-resolved" not in repr(caught.value)


@pytest.mark.parametrize("native", ["timeout", "oserror", "result"])
def test_native_failure_projects_to_safe_context_free_exception(
    monkeypatch: pytest.MonkeyPatch,
    native: str,
) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if native == "timeout":
            raise subprocess.TimeoutExpired(
                cmd=["op", "sentinel-native-reference"],
                timeout=1,
                output="sentinel-native-output",
                stderr="sentinel-native-error",
            )
        if native == "oserror":
            raise OSError("sentinel-native-path")
        return subprocess.CompletedProcess(
            ["op", "sentinel-native-reference"],
            1,
            stdout="sentinel-native-output",
            stderr="sentinel-native-error",
        )

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client()
    try:
        with pytest.raises((SecretClientFailure, SecretClientTimeout)) as caught:
            client.resolve((_request(),), remaining_time=lambda: 1.0)
    finally:
        context.__exit__(None, None, None)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = repr((str(caught.value), repr(caught.value), caught.value.args))
    assert "sentinel-native" not in rendered


def test_factory_entry_and_prepare_are_subprocess_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("must remain lazy"))
    context = OnePasswordBackend.create_client(
        source_name="work",
        config=OnePasswordSourceConfig(name="onepassword"),
        interaction_broker=None,
        remaining_time=lambda: 30.0,
    )
    client = context.__enter__()
    try:
        client.prepare((_request(),), remaining_time=lambda: 30.0)
    finally:
        context.__exit__(None, None, None)


def test_source_timeout_caps_a_larger_operation_remainder(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(cast("float", kwargs["timeout"]))
        return subprocess.CompletedProcess(args, 0, stdout="resolved", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client(timeout=3.0, remaining=20.0)
    try:
        assert client.resolve((_request(),), remaining_time=lambda: 20.0) == {"token": "resolved"}
    finally:
        context.__exit__(None, None, None)
    assert timeouts == [3.0]
