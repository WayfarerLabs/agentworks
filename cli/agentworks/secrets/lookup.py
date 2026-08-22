"""Exact, text-free lookup-description boundary shared by core callers."""

from __future__ import annotations

import concurrent.futures
from typing import TYPE_CHECKING

from agentworks.capabilities.secret_backend import LookupDescription
from agentworks.errors import UserAbort

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.capabilities.secret_backend.base import SecretBackend


class LookupDescriptionProtocolError(Exception):
    """A backend lookup description violated the exact host contract."""

    __slots__ = ()


def describe_lookup_exact(
    backend: type[SecretBackend],
    secret_name: str,
    mapping: BaseModel | None,
) -> LookupDescription:
    """Invoke and snapshot one exact backend lookup description."""
    try:
        description = backend.describe_lookup(secret_name, mapping)
        if type(description) is not LookupDescription:
            raise LookupDescriptionProtocolError
        return LookupDescription(description.disposition, description.identifier)
    except (UserAbort, concurrent.futures.CancelledError):
        raise
    except LookupDescriptionProtocolError:
        raise
    except Exception:
        raise LookupDescriptionProtocolError from None
