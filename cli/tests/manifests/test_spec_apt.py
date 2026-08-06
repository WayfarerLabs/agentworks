"""The apt-source / apt-package spec models.

The two smallest kinds, and the ones that bed in the pattern: every field
round-trips off the document, an unknown key is a hard error naming what
IS valid, and the one semantic rule (``source_file`` is a simple filename)
still fires with a message an operator can act on.
"""

from __future__ import annotations

from agentworks.apt import AptPackageEntry, AptSourceEntry

from ._specs import WHERE, decode, rejection

_SOURCE = {
    "key_url": "https://example.test/key.asc",
    "key_path": "/etc/apt/keyrings/example.gpg",
    "source": "deb [signed-by=/etc/apt/keyrings/example.gpg] https://example.test/ stable main",
    "source_file": "example.list",
    "key_dearmor": True,
}


def test_an_apt_source_round_trips_every_field() -> None:
    row = decode("apt-source", "example", dict(_SOURCE), description="the example repo")

    assert row == AptSourceEntry(name="example", description="the example repo", declared_at=WHERE, **_SOURCE)


def test_an_apt_source_defaults_key_dearmor_and_description() -> None:
    row = decode("apt-source", "example", {key: value for key, value in _SOURCE.items() if key != "key_dearmor"})

    assert (row.key_dearmor, row.description) == (False, None)


def test_an_apt_package_round_trips_every_field() -> None:
    row = decode("apt-package", "tools", {"apt": ["jq", "ripgrep"], "apt_sources": ["example"]})

    assert row == AptPackageEntry(name="tools", apt=["jq", "ripgrep"], apt_sources=["example"], declared_at=WHERE)


def test_an_apt_package_defaults_its_sources_to_none() -> None:
    assert decode("apt-package", "tools", {"apt": ["jq"]}).apt_sources == []


# -- What an operator reads when it is wrong ----------------------------------


def test_an_unknown_key_names_the_fields_that_are_valid() -> None:
    assert rejection("apt-package", "tools", {"apt": ["jq"], "packages": ["jq"]}) == (
        "res.yaml:7: apt-package/tools.packages: unknown field; expected one of: apt, apt_sources"
    )


def test_a_missing_required_field_says_it_is_required() -> None:
    assert rejection("apt-package", "tools", {}) == "res.yaml:7: apt-package/tools.apt: is required"


def test_a_bare_string_where_a_list_belongs_is_refused() -> None:
    """Strict mode's whole point: a single package written without the
    list is the mistake, not a value to wrap."""
    assert rejection("apt-package", "tools", {"apt": "jq"}) == "res.yaml:7: apt-package/tools.apt: must be a list"


def test_a_source_file_with_a_path_separator_is_refused() -> None:
    """It is interpolated into a shell command on the VM, so it has to be
    a bare filename."""
    assert "source_file: String should match pattern" in rejection(
        "apt-source", "example", {**_SOURCE, "source_file": "../etc/passwd"}
    )


def test_a_description_written_in_spec_is_sent_to_metadata() -> None:
    assert rejection("apt-package", "tools", {"apt": ["jq"], "description": "here"}) == (
        "res.yaml:7: description belong(s) in metadata, not in spec"
    )


def test_every_rejection_points_at_the_sample_surface() -> None:
    """FR16's pointer discipline at the one place an operator is already
    looking at a shape they got wrong."""
    import pytest

    from agentworks.errors import ConfigError

    with pytest.raises(ConfigError) as caught:
        decode("apt-package", "tools", {})

    assert caught.value.hint == "see `agw resource sample <kind>` for this kind's fields"
