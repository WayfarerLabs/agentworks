"""Exact collision-safe GCE retained-name formulas."""

from __future__ import annotations

import re
from uuid import UUID

import pytest

from agentworks.plugins.gcp.names import GceNames, derive_names, transient_route_name


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        (
            "valid-name",
            GceNames(
                backend_name="valid-name",
                network_tag="valid-name-tag-152196b1b5",
                deny_rule="valid-name-deny-24e45249b5",
                allow_rule="valid-name-allow-d587ee2315",
            ),
        ),
        (
            "1_leading",
            GceNames(
                backend_name="agw-1-leading-910d38eb84",
                network_tag="agw-1-leading-tag-e590138f31",
                deny_rule="agw-1-leading-deny-1310606127",
                allow_rule="agw-1-leading-allow-d367ea59b6",
            ),
        ),
        (
            "Mixed_CASE",
            GceNames(
                backend_name="mixed-case-9c5ccf1d00",
                network_tag="mixed-case-tag-0aea07f054",
                deny_rule="mixed-case-deny-4a772c697b",
                allow_rule="mixed-case-allow-5f5e90c235",
            ),
        ),
        (
            "!!!",
            GceNames(
                backend_name="agw-88454dd3d0",
                network_tag="agw-tag-8fe2c3f0af",
                deny_rule="agw-deny-6923a4f9e2",
                allow_rule="agw-allow-55a420c84d",
            ),
        ),
        (
            "a" * 63,
            GceNames(
                backend_name="a" * 63,
                network_tag="a" * 48 + "-tag-a6eb2f6da1",
                deny_rule="a" * 47 + "-deny-a6f53a7d32",
                allow_rule="a" * 46 + "-allow-08bea755ca",
            ),
        ),
        (
            "a" * 64,
            GceNames(
                backend_name="a" * 52 + "-e76627ca91",
                network_tag="a" * 48 + "-tag-14c70601f7",
                deny_rule="a" * 47 + "-deny-4b7958a374",
                allow_rule="a" * 46 + "-allow-368bcaf486",
            ),
        ),
    ],
)
def test_exact_name_vectors(hostname: str, expected: GceNames) -> None:
    assert derive_names(hostname) == expected


def test_normalization_collisions_keep_all_retained_identities_distinct() -> None:
    underscore = derive_names("a_b")
    hyphen = derive_names("a-b")
    assert underscore.backend_name == "a-b-abfeac1e17"
    assert hyphen.backend_name == "a-b"
    assert len(set(vars(underscore).values()) & set(vars(hyphen).values())) == 0


@pytest.mark.parametrize("hostname", ["valid-name", "1_leading", "Mixed_CASE", "!!!", "a" * 64])
def test_every_stable_name_is_gce_valid_and_bounded(hostname: str) -> None:
    names = derive_names(hostname)
    for name in vars(names).values():
        assert len(name) <= 63
        assert re.fullmatch(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?", name)


def test_transient_route_reserves_suffix_and_exact_nonce() -> None:
    name = transient_route_name("Mixed_CASE" * 10, UUID("12345678-1234-5678-1234-567812345678"))
    assert name == "mixed-casemixed-casemixed-casemixed-route-12345678123456781234"
    assert len(name) == 62
    assert re.fullmatch(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?", name)
