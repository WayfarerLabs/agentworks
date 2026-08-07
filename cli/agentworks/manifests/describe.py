"""``agw resource describe-kind``: the field reference, for the terminal.

The second presenter over :mod:`agentworks.manifests.reference`, beside the
generated sample. Same facts, different question: the sample answers "give
me something to edit", this one answers "what may I write here, and what
does it mean", which used to be answered by reading the source.

Rendering only. Every fact comes from the collected record, so this module
has no opinion about any kind and no knowledge of any field.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.manifests.field_tree import worth_showing
from agentworks.manifests.reference import plain_text
from agentworks.manifests.yaml_value import render_value
from agentworks.schema import MAPPING_KEY, SEQUENCE_ELEMENT

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.manifests.field_tree import FieldEntry
    from agentworks.manifests.reference import SchemaReference

#: Terminal width to wrap descriptions at. Fixed rather than detected: the
#: output is as often piped or pasted as read live, and a reference whose
#: line breaks move with the window is one nobody can diff.
_WIDTH = 88

#: How far a field's description sits from the left margin, per level.
_INDENT = "  "


def render_reference(reference: SchemaReference) -> None:
    """Print ``reference`` as operator-facing sections."""
    for line in reference_lines(reference):
        output.info(line)


def reference_lines(reference: SchemaReference) -> Iterator[str]:
    """The rendered reference, line by line.

    Separate from the printing so a test can assert on the text and a
    future non-terminal presenter has something to reuse.
    """
    yield from _heading(reference)
    if reference.metadata:
        yield ""
        yield "metadata:"
        yield from _fields(reference.metadata, depth=1)
    if reference.spec:
        yield ""
        yield "spec:" if reference.category == "declarable" else "config:"
        yield from _fields(reference.spec, depth=1)
    if reference.root_value is not None:
        yield ""
        yield "config: (a value, not a table)"
        yield from _wrapped(_facts_of(reference.root_value), depth=1)
        yield from _table_form(reference.root_value)
    if reference.alternatives:
        yield ""
        yield "implementations:"
        for alternative in reference.alternatives:
            yield f"{_INDENT}{alternative.name}"
            if alternative.summary:
                yield from _wrapped(alternative.summary, depth=2)
        yield ""
        yield f"Run `agw resource describe-kind {reference.kind}/<name>` for one implementation's config."
    if reference.category == "declarable":
        yield ""
        yield f"Run `agw resource sample {reference.kind}` for a document to edit."


def _heading(reference: SchemaReference) -> Iterator[str]:
    """What this is: the title, the address an operator types, and the
    authored prose."""
    if reference.implementation is not None:
        what = f"{reference.kind} implementation"
    else:
        what = "resource kind" if reference.category == "declarable" else "capability kind"
    # The address is the heading itself when nothing authored a title, so
    # a thing with no prose does not announce itself twice.
    yield f"{reference.title} ({reference.target}, {what})" if reference.title else f"{reference.target} ({what})"
    if reference.summary:
        yield from _wrapped(reference.summary, depth=0)
    if reference.overview:
        for paragraph in reference.overview.split("\n\n"):
            yield ""
            yield from _wrapped(paragraph, depth=0)


def _table_form(entry: FieldEntry) -> Iterator[str]:
    """The fields of the block a config that is a VALUE may be written as.

    A value that is a bare string or a table has no field name of its own
    to hang the table form under, so the heading above it can only say
    what may go there. Left at that, the arm reads as the bare word
    "table" while the emitted schema beside it spells out both of the
    properties an operator has to write, which is the disagreement this
    surface exists to not have.
    """
    if not entry.children:
        return
    yield f"{_INDENT}as a table:"
    yield from _fields(entry.children, depth=2)


def _fields(entries: tuple[FieldEntry, ...], *, depth: int) -> Iterator[str]:
    for entry in entries:
        yield from _field(entry, depth=depth)


def _field(entry: FieldEntry, *, depth: int) -> Iterator[str]:
    yield f"{_INDENT * depth}{_key(entry)}  {_facts_of(entry)}"
    if entry.doc.description:
        yield from _wrapped(entry.doc.description, depth=depth + 1)
    for alternative in entry.alternatives:
        shown = " (shown below)" if alternative.name == entry.rendered else ""
        summary = f": {alternative.summary}" if alternative.summary else ""
        yield f"{_INDENT * (depth + 1)}- {alternative.name}{shown}{summary}"
    yield from _other_arms_pointer(entry, depth=depth + 1)
    yield from _fields(entry.children, depth=depth + 1)


def _other_arms_pointer(entry: FieldEntry, *, depth: int) -> Iterator[str]:
    """Where to read about the alternatives this field did not expand.

    Listing four platforms by name and expanding one leaves an operator
    with no address for the other three, which is exactly the question the
    list provokes. The generated sample answers it; so should this.
    """
    other = next((alt for alt in entry.alternatives if alt.name != entry.rendered and alt.target), None)
    if other is None:
        return
    yield f"{_INDENT * depth}`agw resource describe-kind {other.target}` for another one's fields."


def _key(entry: FieldEntry) -> str:
    """The key an operator writes, or what stands in for one."""
    if entry.name == MAPPING_KEY:
        return "<key>"
    if entry.name == SEQUENCE_ELEMENT:
        return "- (each element)"
    return entry.name


def _facts_of(entry: FieldEntry) -> str:
    """The parenthetical after a field name: what it takes and whether it
    has to be there.

    Values render as YAML, not as Python ``repr``. The reader is about to
    write these into a document, so a boolean default has to read ``true``
    and a string example has to arrive without Python's quotes around it.
    """
    facts = [entry.type_label, "required" if entry.writable else "optional"]
    if entry.doc.default_template is not None:
        facts.append(f"defaults to `{entry.doc.default_template.replace('{owner_name}', '<name>')}`")
    elif worth_showing(entry.doc.default) and not entry.children:
        facts.append(f"default {render_value(entry.doc.default)}")
    if entry.doc.ref is not None:
        facts.append(f"names a {entry.doc.ref.kind}")
    for key, value in entry.doc.constraints.items():
        facts.append(f"{key.replace('_', ' ')} {value}")
    if entry.doc.examples:
        facts.append(f"e.g. {render_value(entry.doc.examples[0])}")
    return f"({', '.join(facts)})"


def _wrapped(text: str, *, depth: int) -> Iterator[str]:
    prefix = _INDENT * depth
    yield from textwrap.wrap(
        plain_text(" ".join(text.split())),
        width=_WIDTH,
        initial_indent=prefix,
        subsequent_indent=prefix,
    )
