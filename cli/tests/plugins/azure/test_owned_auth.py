"""Selected authentication and same-original retirement, without live token work."""

from __future__ import annotations

import traceback
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import ClientAuthenticationError

from agentworks.errors import LimitExceededError, StateError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.azure._owned_auth import _OwnedAzureCredential
from agentworks.plugins.azure.config import AzureAmbientAuth, AzureServicePrincipalAuth
from agentworks.plugins.azure.network import AzureError


@pytest.fixture
def owned(monkeypatch):
    now = [100.0]
    calls: list[Any] = []
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    monkeypatch.setattr("agentworks.plugins.azure._owned_auth.output.info", lambda _: calls.append("info"))
    result = SimpleNamespace(
        now=now, calls=calls, probe_error=None, close_error=None, constructor_error=None, late=None
    )

    def get_token(*scopes, **kwargs):
        calls.append(("probe", scopes))
        assert result.helper._credential is result.default
        if result.late == "probe":
            now[0] = 111.0
        if result.probe_error is not None:
            raise result.probe_error
        return object()

    def close_default():
        calls.append("default-close")
        assert result.helper._credential is result.default
        if result.late == "close":
            now[0] = 111.0
        if result.close_error is not None:
            raise result.close_error

    def construct_default(**kwargs):
        calls.append(("default", kwargs))
        if result.constructor_error is not None:
            raise result.constructor_error
        if result.late == "default":
            now[0] = 111.0
        return result.default

    def construct_browser(**kwargs):
        calls.append(("browser", kwargs))
        assert result.helper._credential is None
        if result.late == "browser":
            now[0] = 111.0
        return result.browser

    result.default = SimpleNamespace(get_token=get_token, close=close_default)
    result.browser = SimpleNamespace(
        get_token=lambda *a, **k: pytest.fail("browser token interaction must stay lazy"),
        close=lambda: calls.append("browser-close"),
    )
    result.ctx = SimpleNamespace(secret=lambda _: pytest.fail("ambient must not resolve a configured secret"))
    result.helper = _OwnedAzureCredential(AzureAmbientAuth(mode="ambient"), result.ctx, "site", "vm")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", construct_default)
    monkeypatch.setattr("azure.identity.InteractiveBrowserCredential", construct_browser)
    return result


def test_passive_selection_retains_before_probe_and_reuses_same_original(owned):
    assert owned.calls == [] and not owned.helper.has_credential and not owned.helper.failed
    assert owned.helper.credential(Deadline.after(10)) is owned.default
    assert owned.helper.credential(Deadline.after(10)) is owned.default
    assert [call[0] for call in owned.calls] == ["default", "probe"]
    assert not owned.helper.cleanup_incomplete
    assert owned.helper.close()
    with pytest.raises(StateError):
        owned.helper.credential(Deadline.after(10))


def test_fallback_retires_default_before_lazy_browser(owned):
    owned.probe_error = ClientAuthenticationError("offline authentication failure")
    assert owned.helper.credential(Deadline.after(10)) is owned.browser
    assert [call if isinstance(call, str) else call[0] for call in owned.calls] == [
        "default",
        "probe",
        "default-close",
        "info",
        "browser",
    ]
    assert owned.helper.close() and owned.calls[-1] == "browser-close"


@pytest.mark.parametrize("exception_type", [ValueError, KeyboardInterrupt, SystemExit])
def test_unrelated_probe_or_constructor_failure_never_falls_back(owned, exception_type):
    sentinel = "offline-auth-diagnostic-sentinel"
    primary = exception_type(sentinel)
    owned.probe_error = primary
    expected = AzureError if exception_type is ValueError else exception_type
    with pytest.raises(expected) as caught:
        owned.helper.credential(Deadline.after(10))
    if exception_type is ValueError:
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert sentinel not in "".join(traceback.format_exception(caught.value))
    else:
        assert caught.value is primary
    assert owned.helper._credential is owned.default and owned.helper.failed
    assert all(not isinstance(call, tuple) or call[0] != "browser" for call in owned.calls)
    assert owned.helper.close()
    # A constructor that never returned has no original to retire.
    owned.helper = _OwnedAzureCredential(AzureAmbientAuth(mode="ambient"), owned.ctx, "site", "vm")
    owned.constructor_error = primary
    before = len(owned.calls)
    with pytest.raises(expected) as caught:
        owned.helper.credential(Deadline.after(10))
    if exception_type is ValueError:
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert sentinel not in "".join(traceback.format_exception(caught.value))
    else:
        assert caught.value is primary
    assert owned.helper.failed and not owned.helper.has_credential
    assert len(owned.calls) == before + 1


@pytest.mark.parametrize("exception_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_failed_default_close_has_terminal_retry_only(owned, exception_type):
    canary = "offline-fallback-secret-canary"
    owned.probe_error = ClientAuthenticationError(canary)
    owned.close_error = exception_type("offline close failure")
    expected = StateError if exception_type is RuntimeError else exception_type
    with pytest.raises(expected) as caught:
        owned.helper.credential(Deadline.after(10))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert canary not in "".join(traceback.format_exception(caught.value))
    if exception_type is not RuntimeError:
        assert caught.value is owned.close_error
    assert owned.helper._credential is owned.default and owned.helper.cleanup_incomplete
    before = list(owned.calls)
    with pytest.raises(StateError):
        owned.helper.credential(Deadline.after(10))
    assert owned.calls == before
    owned.close_error = None
    assert owned.helper.close()
    assert owned.calls == before + ["default-close"]
    with pytest.raises(StateError):
        owned.helper.credential(Deadline.after(10))


@pytest.mark.parametrize("stage", ["default", "probe", "close", "browser"])
def test_original_deadline_refuses_late_selection(owned, stage):
    owned.late = stage
    canary = "offline-late-fallback-secret-canary"
    if stage in {"close", "browser"}:
        owned.probe_error = ClientAuthenticationError(canary)
    with pytest.raises(LimitExceededError) as caught:
        owned.helper.credential(Deadline.after(10))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert canary not in "".join(traceback.format_exception(caught.value))
    assert owned.helper.failed
    if stage == "close":
        assert not owned.helper.has_credential
        assert all(not isinstance(call, tuple) or call[0] != "browser" for call in owned.calls)
    else:
        assert owned.helper.has_credential
    assert owned.helper.close()


@pytest.mark.parametrize("stage", ["constructor", "probe"])
def test_principal_failure_uses_only_selected_secret_and_retains_returned_original(owned, monkeypatch, stage):
    canary = "offline-principal-secret-canary"
    auth = AzureServicePrincipalAuth(
        mode="service-principal", tenant_id="tenant", client_id="client", secret="selected"
    )

    def secret(name):
        assert name == "selected"
        owned.calls.append("secret")
        return canary

    def probe(*scopes, **kwargs):
        assert owned.helper._credential is principal
        raise ClientAuthenticationError(canary)

    principal = SimpleNamespace(get_token=probe, close=lambda: owned.calls.append("principal-close"))

    def construct(tenant, client, value, **kwargs):
        assert (tenant, client, value) == ("tenant", "client", canary)
        if stage == "constructor":
            raise ValueError(canary)
        return principal

    monkeypatch.setattr("azure.identity.ClientSecretCredential", construct)
    owned.helper = _OwnedAzureCredential(auth, SimpleNamespace(secret=secret), "site", "vm")
    with pytest.raises(AzureError) as caught:
        owned.helper.credential(Deadline.after(10))
    assert canary not in str(caught.value) and canary not in caught.value.detail
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert canary not in "".join(traceback.format_exception(caught.value))
    assert owned.helper.has_credential == (stage == "probe")
    assert owned.calls == ["secret"]
    assert owned.helper.close()
    assert owned.calls == (["secret", "principal-close"] if stage == "probe" else ["secret"])


def test_terminal_close_retries_same_ready_credential_without_authentication(owned):
    assert owned.helper.credential(Deadline.after(10)) is owned.default
    owned.close_error = RuntimeError("offline close failure")
    assert not owned.helper.close()
    assert owned.helper.cleanup_incomplete and owned.helper._credential is owned.default
    owned.close_error = None
    assert owned.helper.close()
    assert [call[0] for call in owned.calls if isinstance(call, tuple)] == ["default", "probe"]


@pytest.mark.parametrize("budget,seconds", [(0.9, None), (1.9, 1), (20.9, 10), (9.9, 9)])
def test_public_integer_timeout_never_increases_budget(owned, budget, seconds):
    if seconds is None:
        with pytest.raises(LimitExceededError):
            owned.helper.credential(Deadline.after(budget))
        assert owned.calls == []
        return
    owned.probe_error = ClientAuthenticationError("offline authentication failure")
    assert owned.helper.credential(Deadline.after(budget)) is owned.browser
    default_options = owned.calls[0][1]
    browser_options = owned.calls[-1][1]
    assert default_options["process_timeout"] == seconds
    assert 0 < browser_options["timeout"] <= budget
    assert browser_options["timeout"] == int(budget)
    assert owned.helper.close()
