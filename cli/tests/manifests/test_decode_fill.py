"""Decode is a fill boundary: an owner-templated default resolves there.

No shipped declarable kind carries an owner-templated marker today (the
two inheriting kinds refuse one at import, and the rest happen not to
declare any), so without this fixture kind the fill at decode would be
behavior nothing could miss. The pin is the boundary contract itself:
the validated row and the advisory extraction both read the one filled
payload, exactly as the capability config core does for its blobs.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from agentworks.declared_resource import DeclaredResource
from agentworks.schema import SecretRef

from ._specs import decode

#: Deliberately non-conforming when rendered (uppercase), so the advisory
#: secret-name warning has to SEE the rendered name to fire: an advisory
#: reading the unfilled spec would find no name at all and stay silent.
_TEMPLATE = "Fixture-Token-{owner_name}"


class _TemplatedRow(DeclaredResource):
    """A declarable row whose one spec field defaults from its owner."""

    token: Annotated[str, SecretRef(usage="the fixture token", default_template=_TEMPLATE)]


class _StubKind:
    model = _TemplatedRow


@pytest.fixture
def fixture_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.resources.kind import KIND_REGISTRY

    monkeypatch.setitem(KIND_REGISTRY, "fixture-kind", _StubKind())


def test_decode_fills_an_omitted_templated_field_into_the_row(fixture_kind: None) -> None:
    issues: list[str] = []
    row = decode("fixture-kind", "prod", {}, issues=issues)

    assert row.token == "Fixture-Token-prod"
    # The advisory extraction read the same filled payload: the rendered
    # name is non-conforming by construction, so the warning names it.
    assert any("Fixture-Token-prod" in issue for issue in issues)


def test_a_written_value_survives_decode_unfilled(fixture_kind: None) -> None:
    row = decode("fixture-kind", "prod", {"token": "my-token"}, issues=[])

    assert row.token == "my-token"
