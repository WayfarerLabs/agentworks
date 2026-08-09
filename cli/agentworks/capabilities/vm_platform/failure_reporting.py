"""Output helpers for VM platform cleanup under a primary failure."""

from __future__ import annotations

from agentworks import output


def warn_without_masking_primary(message: str, primary_failure: BaseException | None) -> None:
    try:
        output.warn(message)
    except BaseException:
        if primary_failure is None:
            raise


def detail_without_masking_primary(message: str, primary_failure: BaseException | None) -> None:
    try:
        output.detail(message)
    except BaseException:
        if primary_failure is None:
            raise
