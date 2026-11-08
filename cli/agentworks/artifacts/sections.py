"""Replace only the explicitly delimited Agentworks part of a native instruction file."""

from __future__ import annotations

import re

BEGIN = b"<!-- BEGIN AGENTWORKS GENERATED -->"
END = b"<!-- END AGENTWORKS GENERATED -->"


class MalformedSectionError(ValueError):
    """Incomplete, repeated, or non-line delimiters cannot identify an owned section."""


def replace_section(existing: bytes, body: bytes | None) -> bytes:
    """Preserve external bytes, replacing any valid section regardless of its contents.

    A missing section is appended for application and left alone for retirement.
    Delimiters are whole lines; ambiguity never authorizes a best-effort rewrite.
    """
    if body is not None and (BEGIN in body or END in body):
        raise MalformedSectionError("generated content contains Agentworks section delimiters")
    starts = list(re.finditer(rb"(?m)^" + re.escape(BEGIN) + rb"(?=\r?$)", existing))
    ends = list(re.finditer(rb"(?m)^" + re.escape(END) + rb"(?=\r?$)", existing))
    if not starts and not ends and BEGIN not in existing and END not in existing:
        if body is None:
            return existing
        separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
        return existing + separator + BEGIN + b"\n" + body.rstrip(b"\n") + b"\n" + END + b"\n"
    if (
        len(starts) != 1
        or len(ends) != 1
        or existing.count(BEGIN) != 1
        or existing.count(END) != 1
        or starts[0].start() >= ends[0].start()
    ):
        raise MalformedSectionError("Agentworks section delimiters are incomplete, repeated, or out of order")
    replacement = b"" if body is None else BEGIN + b"\n" + body.rstrip(b"\n") + b"\n" + END
    return existing[: starts[0].start()] + replacement + existing[ends[0].end() :]
