"""One decode call, for the per-kind spec suites.

Every kind's tests ask the same two questions of a manifest ``spec``: what
row does it produce, and what does an operator read when it is wrong. Both
go through the real ``decode_document``, so what these suites pin is the
shipped path (envelope guard, model validation, the error bridge's
framing) rather than a model validated in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests.decode import decode_document
from agentworks.manifests.envelope import Document
from agentworks.source_location import SourceLocation

#: The location every document in these suites reports, so an asserted
#: error line can name a file and a line the way a real one does.
WHERE = SourceLocation(file=Path("res.yaml"), line=7)


def document(
    kind: str,
    name: str,
    spec: dict[str, object],
    *,
    description: str | None = None,
    expires: object | None = None,
) -> Document:
    return Document(kind=kind, name=name, description=description, expires=expires, spec=spec, location=WHERE)


def decode(
    kind: str,
    name: str,
    spec: dict[str, object],
    *,
    description: str | None = None,
    expires: object | None = None,
    issues: list[str] | None = None,
) -> Any:
    """The row ``spec`` decodes to, appending any advisory lines to
    ``issues``."""
    return decode_document(
        document(kind, name, spec, description=description, expires=expires),
        issues if issues is not None else [],
    )


def decode_issues(kind: str, name: str, spec: dict[str, object]) -> list[str]:
    """The advisory lines ``spec`` earns."""
    issues: list[str] = []
    decode(kind, name, spec, issues=issues)
    return issues


def rejection(
    kind: str,
    name: str,
    spec: dict[str, object],
    *,
    description: str | None = None,
    expires: object | None = None,
) -> str:
    """What an operator reads when ``spec`` is wrong: the raised message,
    verbatim and framed."""
    with pytest.raises(ConfigError) as caught:
        decode(kind, name, spec, description=description, expires=expires)
    return str(caught.value)
