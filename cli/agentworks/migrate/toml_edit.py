"""The mandatory ``config.toml`` edit: comment out or delete migrated sections.

tomlkit round-trips the file, so comments and formatting of every
surviving section are preserved byte-for-byte. Structure notes (probed
against tomlkit 0.15):

- The document body holds ONE entry per contiguous header run: a file
  with ``[secrets.a]`` ... ``[operator]`` ... ``[secrets.b]`` has two
  ``secrets`` body entries. Editing per-entry therefore lands each
  change exactly where that piece of the resource sits in the file --
  which is how non-contiguous multi-section units are handled "as one
  unit" without moving any text.
- Rendering a subtree through a scratch document reproduces its full
  ``[section.name]`` header, inline comments, and attached leading
  comments.
- ``Key.__str__`` includes raw whitespace (``'npm-token '`` for
  ``npm-token = {...}``); ``Key.key`` is the clean name.
- A super table stops being one the moment a Comment item lands in its
  body (tomlkit then renders a stray bare ``[section]`` header), so
  comment blocks are NEVER inserted inside a super table: a partially
  migrated contiguous run is split into kept-run super tables and
  top-level comment blocks, preserving every child's position.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tomlkit
from tomlkit import items as toml_items
from tomlkit.items import Comment, Trivia, Whitespace

if TYPE_CHECKING:
    from collections.abc import Iterable

BodyEntry = tuple["toml_items.Key | None", "toml_items.Item"]


def key_name(key: object) -> str:
    """The clean key text (``Key.__str__`` includes raw whitespace)."""
    return getattr(key, "key", str(key))


def apply_toml_edits(
    text: str,
    *,
    units: set[tuple[str, str]],
    singleton_sections: set[str],
    mode: str,
    markers: dict[tuple[str, str], str],
    drop_sections: set[str],
) -> str:
    """Return the rewritten TOML text.

    ``units`` are (section, name) pairs to comment/delete; for sections
    in ``singleton_sections`` the whole section is the unit regardless
    of name. ``drop_sections`` (the ``[secret_backends.*]`` residue) are
    always deleted outright -- they are semantically empty no-ops with
    no manifest successor, so commenting them out would preserve pure
    clutter.
    """
    doc = tomlkit.parse(text)
    body = doc.body

    sections_with_units = {s for s, _ in units} | singleton_sections

    index = 0
    while index < len(body):
        key, item = body[index]
        section = key_name(key) if key is not None else None
        if section in drop_sections:
            del body[index]
            continue
        if section is None or section not in sections_with_units or not isinstance(
            item, toml_items.Table
        ):
            index += 1
            continue

        if section in singleton_sections:
            index = _replace_entry(
                body, index, section, item, mode, markers.get((section, "default"), "")
            )
            continue

        selected = {n for s, n in units if s == section}
        children = [
            key_name(k)
            for k, _child in item.value.body
            if k is not None
        ]
        hit = [n for n in children if n in selected]
        if not hit:
            index += 1
            continue
        if set(hit) == set(children):
            # Every child of this contiguous run migrates: replace the
            # whole entry (avoids the stray empty [section] header).
            index = _replace_entry(
                body, index, section, item, mode, markers.get((section, hit[0]), "")
            )
            continue
        if mode == "delete":
            _delete_children(item, selected)
            index += 1
            continue
        entries = _split_occurrence(section, item, selected, markers)
        del body[index]
        for offset, entry in enumerate(entries):
            body.insert(index + offset, entry)
        index += len(entries)

    return tomlkit.dumps(doc)


def _replace_entry(
    body: list,  # type: ignore[type-arg]
    index: int,
    section: str,
    item: toml_items.Table,
    mode: str,
    marker: str,
) -> int:
    """Delete or comment out one whole top-level body entry.

    Returns the index at which scanning should continue.
    """
    del body[index]
    if mode == "delete":
        return index
    rendered = _render_entry(section, item)
    entries = _comment_block(rendered, marker)
    for offset, entry in enumerate(entries):
        body.insert(index + offset, entry)
    return index + len(entries)


def _delete_children(occurrence: toml_items.Table, selected: set[str]) -> None:
    """Delete-mode partial edit: drop selected children in place.

    Only sub-tables remain afterwards, so the run stays a super table
    and no stray header appears.
    """
    body = occurrence.value.body
    index = 0
    while index < len(body):
        k, _child = body[index]
        if k is not None and key_name(k) in selected:
            del body[index]
            continue
        index += 1


def _split_occurrence(
    section: str,
    occurrence: toml_items.Table,
    selected: set[str],
    markers: dict[tuple[str, str], str],
) -> list[BodyEntry]:
    """Comment-mode partial edit: split one contiguous run into
    kept-child super tables and top-level comment blocks, in the
    children's original order."""
    entries: list[BodyEntry] = []
    current: toml_items.Table | None = None

    def flush() -> None:
        nonlocal current
        current = None

    for k, child in occurrence.value.body:
        name = key_name(k) if k is not None else None
        if name is not None and name in selected and isinstance(child, toml_items.Table):
            flush()
            rendered = _render_child(section, name, child)
            entries.extend(_comment_block(rendered, markers.get((section, name), "")))
            continue
        if k is None and current is None:
            # Loose trivia between runs (whitespace/comments) lands at
            # the top level, keeping its position.
            entries.append((None, child))
            continue
        if current is None:
            current = tomlkit.table(is_super_table=True)
            entries.append((_raw_key(section), current))
        if k is None:
            current.value.body.append((None, child))
        else:
            current.append(k, child)
    return entries


def _raw_key(section: str) -> toml_items.Key:
    return toml_items.SingleKey(section)


def _render_entry(section: str, item: toml_items.Table) -> str:
    """Render a whole body entry (headers, comments, nested sections)."""
    scratch = tomlkit.document()
    scratch.append(section, item)
    return tomlkit.dumps(scratch)


def _render_child(section: str, name: str, child: toml_items.Table) -> str:
    """Render one ``[section.name]`` subtree with its full header path."""
    scratch = tomlkit.document()
    super_table = tomlkit.table(is_super_table=True)
    super_table.append(name, child)
    scratch.append(section, super_table)
    return tomlkit.dumps(scratch)


def _comment_block(rendered: str, marker: str) -> list[BodyEntry]:
    """The marker + ``# ``-prefixed section text, as body entries."""
    lines = [f"# migrated to {marker}"] if marker else []
    for line in rendered.rstrip("\n").splitlines():
        lines.append(f"# {line}" if line.strip() else "#")
    entries: list[BodyEntry] = [
        (None, Comment(Trivia(indent="", comment_ws="", comment=line, trail="\n")))
        for line in lines
    ]
    entries.append((None, Whitespace("\n")))
    return entries


def iter_child_names(item: toml_items.Table) -> Iterable[str]:
    """Clean names of an occurrence's keyed children (planning helper)."""
    for k, _child in item.value.body:
        if k is not None:
            yield key_name(k)
