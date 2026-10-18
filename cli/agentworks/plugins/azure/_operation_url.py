"""Bounded fixed-origin ARM monitor identities for streamed mutation receipts."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from agentworks.plugins.azure._identity import resource_name_equal

MAX_OPERATION_URL_BYTES = 8192


def valid_operation_url(value: object, subscription: str) -> bool:
    """Validate an external monitor URL without rewriting its retained bytes.

    ARM subscription identity ignores ASCII casing; the later request and
    response must still use this exact retained URL. Never follow redirects.
    """
    if (
        type(value) is not str
        or not 0 < len(value) <= MAX_OPERATION_URL_BYTES
        or not all(32 < ord(char) < 127 for char in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        decoded_path = unquote(parsed.path, errors="strict")
        prefix = f"/subscriptions/{quote(subscription, safe='')}/"
        return (
            parsed.scheme == "https"
            and parsed.netloc == "management.azure.com"
            and not parsed.fragment
            and "#" not in value
            and "," not in value
            and re.search(r"%(?![0-9a-fA-F]{2})", value) is None
            and all(component not in {".", ".."} for component in decoded_path.split("/"))
            and "\\" not in decoded_path
            and all(ord(char) >= 32 and ord(char) != 127 for char in decoded_path)
            and resource_name_equal(parsed.path[: len(prefix)], prefix)
            and (not parsed.query or (len(query) == 1 and query[0][0] == "api-version" and bool(query[0][1])))
        )
    except (ValueError, UnicodeError):
        return False
