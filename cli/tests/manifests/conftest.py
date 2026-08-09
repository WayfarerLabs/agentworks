"""Shared helpers for the resource-manifest test package.

Samples ship commented out so a manifest file is inert until an operator
activates it. Several modules here have to perform that activation to
assert what the activated text becomes, so the rule lives in one place:
a second copy is a second thing to keep in step with the emitter.

The rule is deliberately NOT the one in ``tests/test_sample_config.py``.
That file activates ``sample-config.toml``, where prose is ``# <text>``
and examples are ``#<toml>``, so its rule must leave ``# `` lines alone.
Manifest samples are the opposite: document lines are ``#`` plus their
own YAML indentation (``#  name: my-vm-template``), so leaving ``# ``
lines alone would comment out every nested key. The two conventions are
not variants of one rule and must not be merged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.manifests.emit import MODELINE_PREFIX

if TYPE_CHECKING:
    from pathlib import Path


def _uncommented_lines(text: str) -> list[str]:
    """The rule itself: one leading ``#`` off each line, if present."""
    return [line.removeprefix("#") for line in text.splitlines()]


def uncomment(text: str) -> str:
    """The sample surface's documented activation rule, over sample TEXT.

    Document lines become YAML; ``##`` prose lines become ordinary YAML
    comments. Use this on what ``sample_text`` returns. A sample FILE
    additionally carries a modeline header that must survive activation,
    which is what :func:`activate` is for.
    """
    return "\n".join(_uncommented_lines(text)) + "\n"


def activate(path: Path) -> None:
    """The activation the guide documents, applied in place to a WRITTEN
    file: :func:`uncomment`'s rule over the body, modeline left alone.

    The modeline is a file header rather than a document line, so
    uncommenting it would make it a key the loader rejects.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    head, body = (lines[:1], lines[1:]) if lines and lines[0].startswith(MODELINE_PREFIX) else ([], lines)
    path.write_text("\n".join(head + _uncommented_lines("\n".join(body))) + "\n", encoding="utf-8")
