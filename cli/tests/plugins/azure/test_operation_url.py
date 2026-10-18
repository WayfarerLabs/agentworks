"""Literal monitor scope comparison and unchanged retained evidence bytes."""

from __future__ import annotations

import pytest

from agentworks.plugins.azure._activation import (
    ActivationPayload,
    StartAcknowledgment,
    decode_activation_payload,
    encode_activation_payload,
)
from agentworks.plugins.azure._operation_url import valid_operation_url

SUBSCRIPTION = "aabbccdd-1111-2222-3333-444455556666"
RESOURCE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
URL = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Network/operations/op"


@pytest.mark.parametrize("suffix", ["", "?api-version=2025-09-01"])
@pytest.mark.parametrize("case", ["same", "subscription", "label", "both"])
def test_subscription_casing_preserves_monitor_and_persisted_bytes(suffix, case):
    selected = URL + suffix
    if case in {"subscription", "both"}:
        selected = selected.replace(SUBSCRIPTION, SUBSCRIPTION.upper())
    if case in {"label", "both"}:
        selected = selected.replace("/subscriptions/", "/SUBSCRIPTIONS/")
    assert valid_operation_url(selected, SUBSCRIPTION)
    assert valid_operation_url(selected, SUBSCRIPTION.upper())
    payload = ActivationPayload(RESOURCE, StartAcknowledgment(202, location=selected))
    encoded = encode_activation_payload(payload)
    decoded = decode_activation_payload(encoded)
    assert decoded == payload
    assert decoded.acknowledgment is not None
    assert decoded.acknowledgment.location == selected
    assert encode_activation_payload(decoded) == encoded


@pytest.mark.parametrize(
    "selected",
    [
        None,
        b"https://management.azure.com",
        "",
        URL.replace(SUBSCRIPTION, SUBSCRIPTION + "a"),
        URL.replace(SUBSCRIPTION, "another-subscription"),
        URL.replace("https://", "http://"),
        URL.replace("management.azure.com", "management.azure.com.invalid"),
        URL.replace("management.azure.com", "user@management.azure.com"),
        URL.replace("management.azure.com", "management.azure.com:443"),
        URL + "#fragment",
        URL + ",another-value",
        URL + "/..",
        URL + "/%2e%2e",
        URL + "/%2E",
        URL + "/%5c",
        URL + "/%00",
        URL + "/%7f",
        URL + "/%ff",
        URL + "/%xy",
        URL + "\n",
        URL + "\x7f",
        URL + "\ud800",
        URL + "?api-version=",
        URL + "?api-version=v&api-version=v",
        URL + "?api-version=v&other=value",
        URL + "?not-an-api-version=v",
        URL + "x" * 8192,
    ],
)
def test_invalid_or_foreign_monitor_never_enters_receipt(selected):
    assert not valid_operation_url(selected, SUBSCRIPTION)
