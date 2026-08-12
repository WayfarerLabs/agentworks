"""Shared fixed diagnostics for line-oriented secret consumers."""

from __future__ import annotations

import pytest

from agentworks.errors import ValidationError
from agentworks.secrets.line_safety import (
    LineOrientedSecretUse,
    require_line_safe_secret,
)

_VALUE_SENTINEL = "line-secret-value-sentinel"


@pytest.mark.parametrize(
    "value",
    [
        f"{_VALUE_SENTINEL}\nafter",
        f"{_VALUE_SENTINEL}\rafter",
        f"{_VALUE_SENTINEL}\0after",
    ],
)
@pytest.mark.parametrize("use", list(LineOrientedSecretUse))
def test_line_safety_failure_is_fixed_detached_and_value_free(
    value: str,
    use: LineOrientedSecretUse,
) -> None:
    with pytest.raises(ValidationError) as caught:
        require_line_safe_secret(value, use=use, secret_name="named-secret")

    failure = caught.value
    rendered_graph = repr((failure.args, vars(failure), failure.__cause__, failure.__context__))
    assert _VALUE_SENTINEL not in rendered_graph
    assert failure.__cause__ is None
    assert failure.__context__ is None
    assert failure.entity_kind == "secret"
    assert failure.entity_name == "named-secret"


def test_line_safety_preserves_other_opaque_string_content() -> None:
    value = " leading\tvalue "
    assert require_line_safe_secret(value, use=LineOrientedSecretUse.ENVIRONMENT) is value
