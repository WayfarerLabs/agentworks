"""The commented-YAML skeleton: a kind's document, rendered.

Input is a :class:`~agentworks.manifests.reference.SchemaReference`; output
is text an operator can uncomment and edit. Nothing here knows any kind.

**Every line starts with ``#``.** A written sample is therefore inert: the
loader ignores it, so ``--write`` can never create a duplicate or a live
resource, and running it twice just appends more comments. Two line shapes,
and the difference is what the operator's one edit turns them into:

- a DOCUMENT line is ``#`` plus the YAML, so deleting one leading ``#``
  makes it real;
- a PROSE line is ``##`` plus text, so deleting one leading ``#`` leaves an
  ordinary YAML comment.

**Only the fields an operator MUST write are document lines.** Every
optional field renders as a commented suggestion at its own indent, with
its description above it. That is what makes an uncommented skeleton a
document that LOADS: it carries exactly the required fields, so there are
no placeholder values to trip a validator and no reference to a resource
the operator has not declared. The old hand-written samples could not
promise that; the `admin-template` one shipped with a warning that
uncommenting it broke the load unless you declared a git-credential too.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from agentworks.manifests.envelope import API_VERSION
from agentworks.manifests.field_tree import worth_showing
from agentworks.manifests.reference import plain_text
from agentworks.manifests.yaml_value import render_value
from agentworks.schema import MAPPING_KEY, SEQUENCE_ELEMENT, UNSET

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.manifests.field_tree import FieldEntry
    from agentworks.manifests.reference import SchemaReference

#: Where prose wraps. Narrower than the code line limit because every line
#: carries a `## ` prefix and lands in an editor beside a manifest.
_WIDTH = 84

#: The one-line instruction every skeleton ends its prose with. Rendered
#: rather than authored per kind, so it cannot go stale in twelve places.
_HOW_TO_USE = "Uncomment the document lines (delete one leading `#`) and edit."


def skeleton_text(reference: SchemaReference) -> str:
    """One kind's commented sample document, prose included."""
    return "\n".join(_document_lines(reference)) + "\n"


def _document_lines(reference: SchemaReference) -> Iterator[str]:
    yield from _prose_lines(reference)
    yield f"#apiVersion: {API_VERSION}"
    yield f"#kind: {reference.kind}"
    yield "#metadata:"
    yield from _block(reference.metadata, target=reference.target, depth=1, commented=False)
    yield "#spec:"
    yield from _block(reference.spec, target=reference.target, depth=1, commented=False)


def _prose_lines(reference: SchemaReference) -> Iterator[str]:
    """The authored header: what this kind is, then how to use the
    skeleton below it."""
    # The kind identifier, not the authored title: this header sits above
    # a document whose `kind:` line says the same word, and an operator
    # reading a `--all` dump is looking for the identifier. The title is
    # the field reference's heading.
    yield f"## kind: {reference.kind}"
    yield "##"
    if reference.summary:
        yield from _wrapped(reference.summary)
        yield "##"
    if reference.overview:
        for paragraph in reference.overview.split("\n\n"):
            yield from _wrapped(paragraph)
            yield "##"
    yield from _wrapped(_HOW_TO_USE)
    yield "##"


def _block(entries: tuple[FieldEntry, ...], *, target: str, depth: int, commented: bool) -> Iterator[str]:
    for entry in entries:
        yield from _field_lines(entry, target=target, depth=depth, commented=commented)


def _field_lines(entry: FieldEntry, *, target: str, depth: int, commented: bool) -> Iterator[str]:
    """One field: its description, its alternatives if it has any, and the
    line (or block) that writes it.

    ``commented`` is inherited: everything under an optional field is a
    suggestion too, because a key written inside a block the document does
    not open is not a key at all.

    ``target`` is the address of the reference being rendered, threaded
    down so a union can point at the surface that shows the arms a
    document cannot: see :func:`_alternatives_of`.
    """
    commented = commented or not entry.writable
    if entry.name in (SEQUENCE_ELEMENT, MAPPING_KEY):
        # The element of a collection of blocks. A model says a list holds
        # tables without saying how many or under what names, so the
        # element is shown ONCE and it has no description of its own to
        # print: it is not a field anyone declared.
        noun = "entry" if entry.name == MAPPING_KEY else "element"
        yield from _comment([f"one {noun}, as an example:"], depth=depth)
    else:
        yield from _comment(_annotation_of(entry), depth=depth)
    # Outside the branch: an ELEMENT may be a tagged union too (a list of
    # blocks each naming which kind of block it is), and an operator
    # writing one needs the same list of what else may go there.
    yield from _comment(_alternatives_of(entry, target), depth=depth)
    yield from _value_lines(entry, depth=depth, commented=commented)
    yield from _block(entry.contents, target=target, depth=depth + 1, commented=commented)


def _value_lines(entry: FieldEntry, *, depth: int, commented: bool) -> Iterator[str]:
    """The field's own line, live or commented: what opens the value, and
    the value itself when it fits on one line."""
    opener = _opener(entry)
    value = entry.sample_value
    if value is UNSET or entry.contents:
        yield _line(opener, depth=depth, commented=commented)
        return
    yield _line(f"{opener} {render_value(value)}", depth=depth, commented=commented)


def _opener(entry: FieldEntry) -> str:
    """What the field's line starts with: the key an operator writes, the
    placeholder standing for a key they choose, or a sequence dash.

    A sequence element has no key at all. Writing one produced ``-:``, a
    mapping under a key literally named ``-``, so an uncommented skeleton
    carrying a required list of tables was a document the loader rejected:
    exactly the promise this module leads with. A bare ``-`` opens the
    element, and its fields follow at the next indent.
    """
    if entry.name == MAPPING_KEY:
        return "<key>:"
    if entry.name == SEQUENCE_ELEMENT:
        return "-"
    return f"{entry.name}:"


def _annotation_of(entry: FieldEntry) -> list[str]:
    """The description an operator reads above the field, and the
    parenthetical that says what may go in it."""
    facts = [entry.type_label, "required" if entry.writable else "optional"]
    if entry.doc.default_template is not None:
        facts.append(f"defaults to the resource named `{_template(entry.doc.default_template)}`")
    elif not entry.writable and not entry.contents and worth_showing(entry.doc.default):
        facts.append(f"default: {render_value(entry.doc.default)}")
    if entry.doc.ref is not None:
        facts.append(f"names a {entry.doc.ref.kind}")
    lines = [] if entry.doc.description is None else [entry.doc.description]
    lines.append(f"({', '.join(facts)})")
    return lines


def _alternatives_of(entry: FieldEntry, target: str) -> list[str]:
    """The line naming the arms this field could hold, and where to read
    about the ones this document cannot show.

    A sample writes ONE arm, so every other arm is a name the operator
    still has to look up, and the pointer is what keeps the list from
    being a list of words. Which pointer depends on the arms: a
    capability's arm has an address of its own, and an arm of any other
    union has none and never will, so the address that answers it is this
    reference's own, where ``describe-kind`` expands every such arm in
    place.
    """
    if not entry.alternatives:
        return []
    names = ", ".join(alt.name for alt in entry.alternatives)
    # No arm is expanded when the one that would be is already open above
    # this point (a group whose members are groups). Naming what is shown
    # would then print the word "None" at an operator, so the line stops
    # at what may go here, which is the half that is still true.
    shown = "" if entry.rendered is None else f" Shown here: {entry.rendered}."
    lines = [f"One of: {names}.{shown}"]
    unshown = [alt for alt in entry.alternatives if alt.name != entry.rendered and not alt.recurring]
    addressed = next((alt for alt in unshown if alt.target), None)
    if addressed is not None:
        lines.append(f"`agw resource describe-kind {addressed.target}` prints another one's fields.")
    elif unshown:
        lines.append(f"`agw resource describe-kind {target}` prints every arm's fields.")
    return lines


def _template(template: str) -> str:
    """An owner template as an operator can read it: the placeholder names
    a fact about THEIR resource, not a variable they type."""
    return template.replace("{owner_name}", "<this resource's name>")


def _comment(lines: list[str], *, depth: int) -> Iterator[str]:
    """Wrapped explanatory text INSIDE the document, at ``depth``.

    A YAML comment rather than prose (``##``), because it belongs to the
    field below it: uncommenting the document leaves these attached to the
    lines they explain.
    """
    for line in lines:
        for wrapped in _wrap(line, _WIDTH - 2 * depth):
            yield _line(f"# {wrapped}", depth=depth, commented=False)


def _line(text: str, *, depth: int, commented: bool) -> str:
    """One document line: the leading ``#`` that makes the file inert, the
    indent, and (for a suggestion) the ``#`` that keeps it a comment after
    the operator's edit."""
    return f"#{'  ' * depth}{'# ' if commented else ''}{text}"


def _wrapped(text: str) -> Iterator[str]:
    """Prose lines (``##``), which stay comments after the operator's
    edit."""
    for line in _wrap(text, _WIDTH):
        yield f"## {line}"


def _wrap(text: str, width: int) -> list[str]:
    """Collapse and wrap one paragraph.

    Markdown list items and fenced content are left alone: an overview may
    carry them, and rewrapping a list into a paragraph would destroy it.
    """
    collapsed = plain_text(" ".join(text.split()))
    if not collapsed:
        return [""]
    if text.lstrip().startswith(("- ", "* ", "1. ", "```")):
        return text.splitlines()
    return textwrap.wrap(collapsed, width=width)
