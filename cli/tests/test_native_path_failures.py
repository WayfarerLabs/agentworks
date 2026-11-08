"""Guest path statuses become distinct, path-naming errors on the workstation.

The guest program classifies why a guarded path is unusable; this is the host
half that turns those statuses into what an operator reads. The wording is a
review concern, so these assert the classification and the path the operator
needs, not the sentences carrying them.
"""

from __future__ import annotations

import pytest

from agentworks.errors import StateError
from agentworks.native_files import _DENIED_PATH, _UNSUITABLE_PATH, _path_failure

_DESTINATION = "/home/agentworks/.claude/settings.json"


@pytest.mark.windows
def test_every_status_names_the_destination() -> None:
    """The path is the fact the old message omitted, so each status carries it."""
    for returncode in (_UNSUITABLE_PATH, _DENIED_PATH, 1):
        error = _path_failure("native settings file", _DESTINATION, returncode)
        assert isinstance(error, StateError)
        assert _DESTINATION in str(error)


@pytest.mark.windows
def test_a_link_a_denial_and_an_unknown_failure_stay_distinguishable() -> None:
    """These are three different operator problems and must not collapse."""
    unsuitable = _path_failure("native settings file", _DESTINATION, _UNSUITABLE_PATH)
    denied = _path_failure("native settings file", _DESTINATION, _DENIED_PATH)
    unknown = _path_failure("native settings file", _DESTINATION, 1)

    assert len({str(unsuitable), str(denied), str(unknown)}) == 3


@pytest.mark.windows
def test_an_unsuitable_path_carries_remediation() -> None:
    """An unsuitable path is operator-fixable, so it must offer a hint at all."""
    assert _path_failure("native settings file", _DESTINATION, _UNSUITABLE_PATH).hint
    assert _path_failure("native settings file", _DESTINATION, _DENIED_PATH).hint


@pytest.mark.windows
def test_the_subject_distinguishes_the_surface_that_failed() -> None:
    """One destination fails for several roles; the caller's subject says which."""
    settings = _path_failure("native settings file", _DESTINATION, _UNSUITABLE_PATH)
    artifact = _path_failure("artifact destination", _DESTINATION, _UNSUITABLE_PATH)

    assert str(settings) != str(artifact)
