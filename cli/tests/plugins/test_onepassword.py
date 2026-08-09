"""Final configured-source contract for the bundled 1Password backend."""

from __future__ import annotations

import inspect
import subprocess
import sys
from contextlib import AbstractContextManager, contextmanager
from typing import Any, cast

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
    _bounded_read,
    _BoundedRead,
)


@contextmanager
def _interrupt_exact_line(function: Any, source_line: str) -> Any:
    lines, first_line = inspect.getsourcelines(function)
    matches = [first_line + index for index, line in enumerate(lines) if line.rstrip("\n") == source_line]
    assert len(matches) == 1
    target_line = matches[0]
    target_code = function.__code__

    def trace(frame: Any, event: str, argument: object) -> Any:
        del argument
        if frame.f_code is target_code and event == "line" and frame.f_lineno == target_line:
            sys.settrace(None)
            raise KeyboardInterrupt
        return trace

    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(None)


@contextmanager
def _interrupt_call_entry(function: Any) -> Any:
    target_code = function.__code__

    def trace(frame: Any, event: str, argument: object) -> Any:
        del argument
        if frame.f_code is target_code and event == "call":
            sys.settrace(None)
            raise KeyboardInterrupt
        return trace

    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(None)


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


def test_later_failure_discards_earlier_value(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _traceback_local_text(exc: BaseException) -> str:
    values: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            if traceback.tb_frame.f_globals.get("__name__", "").startswith("agentworks."):
                values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
            traceback = traceback.tb_next
        current = current.__cause__ or current.__context__
    return "\n".join(values)


@pytest.mark.parametrize("native", ["timeout", "oserror", "result"])
def test_native_failure_graph_and_traceback_locals_are_value_free(
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
    assert "sentinel-native" not in _traceback_local_text(caught.value)


def test_actual_bounded_read_constructor_entry_never_receives_or_retains_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["op", "sentinel-native-reference"],
            0,
            stdout="sentinel-extracted-value",
            stderr="sentinel-native-stderr",
        ),
    )

    argv = ["read", "sentinel-constructor-argv", "op://Work/item/password"]
    with pytest.raises(KeyboardInterrupt) as caught, _interrupt_call_entry(_BoundedRead.__init__):
        _bounded_read(argv, timeout=1.0)

    retained = _traceback_local_text(caught.value)
    assert "sentinel-native" not in retained
    assert "sentinel-extracted-value" not in retained
    assert "sentinel-constructor-argv" not in retained
    assert argv == []


@pytest.mark.parametrize(
    "source_line",
    ["        if completed is not None:", "        return bounded"],
    ids=["first-post-run-classification", "successful-return"],
)
def test_exact_bounded_read_boundaries_clear_native_result_value_and_argv(
    monkeypatch: pytest.MonkeyPatch,
    source_line: str,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["op", "sentinel-native-argv"],
            0,
            stdout="sentinel-bounded-return-value",
            stderr="sentinel-native-stderr",
        ),
    )
    argv = ["read", "sentinel-caller-argv", "op://Work/item/password"]
    with pytest.raises(KeyboardInterrupt) as caught, _interrupt_exact_line(_bounded_read, source_line):
        _bounded_read(argv, timeout=1.0)

    retained = _traceback_local_text(caught.value)
    assert "sentinel-native" not in retained
    assert "sentinel-caller-argv" not in retained
    assert "sentinel-bounded-return-value" not in retained
    assert argv == []


def test_exact_native_stderr_classification_interrupt_has_no_helper_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["op", "sentinel-classification-argv"],
            1,
            stdout="sentinel-classification-stdout",
            stderr="sentinel-classification-stderr no such item",
        ),
    )
    argv = ["read", "sentinel-classification-caller-argv", "op://Work/item/password"]
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        _interrupt_exact_line(_bounded_read, "                    if signed_out_marker in stderr:"),
    ):
        _bounded_read(argv, timeout=1.0)

    retained = _traceback_local_text(caught.value)
    assert "sentinel-classification" not in retained
    assert "<genexpr>" not in retained
    assert argv == []


def test_keyboard_interrupt_clears_prior_onepassword_values_from_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(args, 0, stdout="sentinel-prior-value", stderr="")
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", run)
    context, client = _client()
    requests = (
        SecretLookupRequest(name="first", mapping=OnePasswordMapping.model_validate("op://W/a/p")),
        SecretLookupRequest(name="second", mapping=OnePasswordMapping.model_validate("op://W/b/p")),
    )
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            client.resolve(requests, remaining_time=lambda: 2.0)
    finally:
        context.__exit__(None, None, None)

    assert "sentinel-prior-value" not in _traceback_local_text(caught.value)


def test_exact_client_success_return_interrupt_clears_onepassword_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout="sentinel-onepassword-return-value",
            stderr="",
        ),
    )
    context, client = _client()
    try:
        with (
            pytest.raises(KeyboardInterrupt) as caught,
            _interrupt_exact_line(type(client).resolve, "            return resolved"),
        ):
            client.resolve((_request(),), remaining_time=lambda: 2.0)
    finally:
        context.__exit__(None, None, None)

    assert "sentinel-onepassword-return-value" not in _traceback_local_text(caught.value)


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
