"""Shared ordering contract for list-service projections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping, Sequence

SortValue = tuple[str, ...]


def nullable_sort_value(value: str | None) -> SortValue:
    """Return a lexical key that places missing values before present ones."""
    return ("0", "") if value is None else ("1", value)


def normalize_sort_keys(
    sort_keys: Sequence[str] | None,
    *,
    allowed: Collection[str],
    entity_kind: str,
) -> tuple[str, ...]:
    """Validate public list-service input and append the stable alpha key."""
    if sort_keys is None:
        return ("alpha",)
    if not sort_keys:
        raise ValidationError(
            "sort_keys must contain at least one key (or pass None for alpha order)",
            entity_kind=entity_kind,
        )

    seen: set[str] = set()
    for key in sort_keys:
        if not key:
            raise ValidationError("sort keys must not be empty", entity_kind=entity_kind)
        if key in seen:
            raise ValidationError(f"duplicate sort key {key!r}", entity_kind=entity_kind)
        if key not in allowed:
            raise ValidationError(
                f"unknown sort key {key!r}",
                entity_kind=entity_kind,
                hint=f"known keys: {', '.join(sorted(allowed))}",
            )
        seen.add(key)

    normalized = [key for key in sort_keys if key != "alpha"]
    normalized.append("alpha")
    return tuple(normalized)


def sort_rows[T](
    rows: Iterable[T],
    *,
    sort_keys: Sequence[str] | None,
    key_functions: Mapping[str, Callable[[T], SortValue]],
    entity_kind: str,
) -> tuple[T, ...]:
    """Validate sort keys and order rows with alpha as the final key."""
    normalized = normalize_sort_keys(
        sort_keys,
        allowed=key_functions,
        entity_kind=entity_kind,
    )
    return tuple(
        sorted(
            rows,
            key=lambda row: tuple(key_functions[key](row) for key in normalized),
        )
    )
