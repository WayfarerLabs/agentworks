"""FR19 (issue #214): contributed manifests validate through the ONE regime.

Issue #214 asked whether a plugin-contributed sample should warn or error
on an unknown key. FR12's closed-world direction answers it: hard error,
and the same one an operator gets.

There is no second validation path to delete, and that is the claim these
tests defend rather than establish: a plugin's bundle already goes through
``manifests/package.publish_manifest_package``, which calls the operator
loader. What was missing was the PIN, so that a future "just warn for
contributed content" shortcut breaks a test that says why.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests.loader import load_manifests
from agentworks.manifests.package import publish_manifest_package
from agentworks.origin import Origin
from agentworks.plugins.publish import PLUGIN_MANIFEST_KINDS
from agentworks.resources.registry import Registry

#: The fixture plugin bundle: one apt-source with `key_dearmour` where the
#: kind declares `key_dearmor`.
_UNKNOWN_KEY_ANCHOR = "tests.plugins._manifest_unknown_key_fixture"

#: The same document, as an operator would have written it. Spelled here
#: rather than read from the fixture package so the two paths are
#: demonstrably given the same input.
_OPERATOR_DOCUMENT = """\
apiVersion: agentworks/v1
kind: apt-source
metadata:
  name: fixture-unknown-key
  description: a contributed source with a misspelled field
spec:
  key_url: https://example.invalid/keys/fixture.gpg
  key_path: /etc/apt/keyrings/fixture.gpg
  source: "deb [arch={arch} signed-by=/etc/apt/keyrings/fixture.gpg] https://example.invalid/apt stable main"
  source_file: fixture.list
  key_dearmour: true
"""


def _contributed_error() -> ConfigError:
    with pytest.raises(ConfigError) as excinfo:
        publish_manifest_package(
            Registry.empty(),
            anchor=_UNKNOWN_KEY_ANCHOR,
            subdir="manifests",
            origin_for=lambda file_name: Origin.built_in(source=f"fixture/{file_name}"),
            allowed_kinds=PLUGIN_MANIFEST_KINDS,
        )
    return excinfo.value


def _operator_error(tmp_path: Path) -> ConfigError:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "unknown-key.yaml").write_text(_OPERATOR_DOCUMENT)
    with pytest.raises(ConfigError) as excinfo:
        load_manifests(resources)
    return excinfo.value


def test_a_contributed_unknown_key_is_a_hard_error() -> None:
    """Not a warning, and not a silently ignored key: the same closed-world
    posture every modeled surface has."""
    message = str(_contributed_error())

    assert "key_dearmour" in message
    assert "unknown" in message.lower()


def test_a_contributed_unknown_key_fails_exactly_as_a_first_party_one_does(tmp_path: Path) -> None:
    """The point of FR19. The two paths differ in where the file came from
    and in nothing else, so the diagnostic an operator reads is the same
    diagnostic either way, down to the remediation.

    Compared with the file's own location stripped, because that is the one
    thing that legitimately differs: a bundle names the package it shipped
    from, an operator's manifest names their path.
    """
    contributed = _contributed_error()
    operator = _operator_error(tmp_path)

    assert _without_location(str(contributed)) == _without_location(str(operator))
    assert contributed.hint == operator.hint


def test_the_shared_diagnostic_is_the_good_one(tmp_path: Path) -> None:
    """What makes sharing it worth pinning. A contributed bundle inherits
    the field list and the sample pointer rather than a terser second
    message, so a plugin author debugging their own bundle gets what an
    operator gets."""
    error = _operator_error(tmp_path)

    assert "key_dearmor" in str(error), "the field they meant is in the list"
    assert error.hint is not None
    assert "agw resource sample apt-source" in error.hint


def _without_location(message: str) -> str:
    """``<path>:<line>: <the diagnostic>`` without the path and line."""
    return message.split(": ", 1)[1] if ": " in message else message
