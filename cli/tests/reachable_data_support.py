"""What an integration can read as data from a value core hands it, for redaction-scope tests."""

from collections.abc import Iterator, Mapping


def reachable_data(value: object, seen: set[int] | None = None, depth: int = 0) -> Iterator[str]:
    """Every string an integration can read from ``value`` as data: items and public attributes, not calls."""
    seen = set() if seen is None else seen
    if id(value) in seen or depth > 6 or callable(value):
        return
    seen.add(id(value))
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from reachable_data(key, seen, depth + 1)
            yield from reachable_data(item, seen, depth + 1)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            yield from reachable_data(item, seen, depth + 1)
    elif not isinstance(value, (int, float, bool, bytes, type(None))):
        # Any other object's public attributes, properties among them.
        for name in dir(value):
            if not name.startswith("_"):
                yield from reachable_data(getattr(value, name, None), seen, depth + 1)
