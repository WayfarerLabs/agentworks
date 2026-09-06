"""The map's row grammar: how a row says what it addresses, and how that
resolves to a place in a given tree.

This module is the reference implementation the inventory's "Reading this file
mechanically" section points at. Two grammars are read, deliberately:

* **Identity anchors**, the current grammar, key a row to what it addresses.
* **Line anchors**, the grammar every earlier cut used, key a row to where it
  sat. They are still read so `carry` and `reanchor` can take an older map as
  input; nothing writes them any more except a row that says why it must.

Four anchor kinds, in order of precision. A row lists as many as it addresses.

    path::qualname::Type::digest#n   one assertion site      -> its current line
    path::qualname                   one test function       -> its line span
    path::L120-124                   literal lines           -> nothing
    path                             the whole file          -> the file

`::` separates the fields of one anchor and `,` separates anchors, so a cell is
one backticked string a reader can copy whole. Neither separator can occur
inside a path, a qualname, a type name or a hex digest, which is what makes the
split unambiguous. A line anchor's `L` prefix is what tells it from a qualname;
no test function is named `L` followed by digits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .estate import Identity, Site, Snapshot

INVENTORY = "docs/sdd/2026-08-12-simplification-pass/sweep-inventory.md"

#: A row id, as the inventory's own reading instructions describe it: an
#: original-read prefix, the mechanical batch's, one of the pulled-out blocks,
#: or the re-baseline's, optionally with the letter suffix a split row carries.
#: Header cells that look like ids (`Sub-batch`, `File`, `Recipe`) fail this by
#: construction, which is why the parser needs no list of them.
ROW_ID = re.compile(r"(?:[A-F]|L|RB|G1)-[A-Z]?\d{1,3}[a-z]?$")

PATH_RE = re.compile(r"((?:cli|website)/[A-Za-z0-9_./-]+?\.(?:py|mjs))")
LINE_ANCHOR = re.compile(r"L(\d+)(?:-(\d+))?$")
SPAN_RE = re.compile(r"\d+(?:-\d+)?")
MARKER_RE = re.compile(r"\[(dead|deferred|1-raise|unverified|subtracted:[^\]]*|line-anchored:[^\]]*)\]")

#: The one section whose rows `generate` emits. Everything else in group 1 is a
#: claim against the estate that the generated batch must leave alone.
MECHANICAL_BATCH = "The mechanical batch"


@dataclass(frozen=True)
class Resolution:
    """Where one anchor lands in a given tree, and how confidently."""

    state: str
    where: str
    detail: str = ""


class Anchor:
    """One thing a row addresses. Subclasses differ only in precision."""

    path: str

    def render(self) -> str:
        raise NotImplementedError

    def resolve(self, snapshot: Snapshot) -> Resolution:
        raise NotImplementedError

    def claims(self, site: Site) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class SiteAnchor(Anchor):
    """One assertion site, keyed on what it asserts."""

    identity: Identity

    @property
    def path(self) -> str:  # type: ignore[override]
        return self.identity.path

    def render(self) -> str:
        return self.identity.tail

    def resolve(self, snapshot: Snapshot) -> Resolution:
        site = snapshot.by_identity.get(self.identity)
        if site is not None:
            return Resolution("resolved", site.where)
        if not snapshot.tree.exists(self.identity.path):
            return Resolution("file-gone", "")
        # A reworded message leaves the assertion in place matching something
        # else. Saying so beats "gone", which would send a reader looking for a
        # deleted test.
        near = snapshot.near(self.identity)
        if near:
            spelled = ", ".join(f"{s.where} ({s.identity.digest})" for s in near)
            return Resolution("retargeted", near[0].where, f"same test and type, other needle: {spelled}")
        return Resolution("gone", "")

    def claims(self, site: Site) -> bool:
        return site.identity == self.identity


@dataclass(frozen=True)
class SpanAnchor(Anchor):
    """One test function, wherever it now sits."""

    path: str
    qualname: str

    def render(self) -> str:
        return self.qualname

    def resolve(self, snapshot: Snapshot) -> Resolution:
        if not snapshot.tree.exists(self.path):
            return Resolution("file-gone", "")
        function = snapshot.function(self.path, self.qualname)
        if function is None:
            return Resolution("gone", "")
        return Resolution("resolved", f"{self.path}:{function.start}-{function.end}")

    def claims(self, site: Site) -> bool:
        return site.path == self.path and site.identity.qualname == self.qualname


@dataclass(frozen=True)
class LineAnchor(Anchor):
    """Literal lines, kept only where nothing better resolves.

    A line anchor is a declaration that this row will go stale, so every one
    the map carries says on the row why it could not be anchored to a name.
    """

    path: str
    spans: tuple[tuple[int, int], ...]

    def render(self) -> str:
        return ",".join(f"L{lo}" if lo == hi else f"L{lo}-{hi}" for lo, hi in self.spans)

    def resolve(self, snapshot: Snapshot) -> Resolution:
        if not snapshot.tree.exists(self.path):
            return Resolution("file-gone", "")
        return Resolution("line-anchored", f"{self.path}:{self.render().replace('L', '')}")

    def claims(self, site: Site) -> bool:
        return site.path == self.path and any(lo <= site.line <= hi for lo, hi in self.spans)


@dataclass(frozen=True)
class FileAnchor(Anchor):
    """A whole file: rows that split one file by pattern, and rows that name a
    file to say it needs no rows."""

    path: str

    def render(self) -> str:
        return ""

    def resolve(self, snapshot: Snapshot) -> Resolution:
        if not snapshot.tree.exists(self.path):
            return Resolution("file-gone", "")
        return Resolution("resolved", self.path)

    def claims(self, site: Site) -> bool:
        return site.path == self.path


@dataclass
class Row:
    """One row of the map, with everything a report needs off it."""

    id: str
    group: str
    section: str
    anchors: list[Anchor]
    shape: str
    disposition: str
    source_line: int
    cells: list[str] = field(default_factory=list)

    @property
    def path(self) -> str | None:
        return self.anchors[0].path if self.anchors else None

    @property
    def markers(self) -> set[str]:
        return {m.split(":")[0] for m in MARKER_RE.findall(self.shape)}

    @property
    def live(self) -> bool:
        return not (self.markers & {"dead", "subtracted", "deferred"})

    def claims(self, site: Site) -> bool:
        return any(a.claims(site) for a in self.anchors)

    def render_cell(self) -> str:
        if not self.anchors:
            return self.cells[1] if len(self.cells) > 1 else ""
        path = self.anchors[0].path
        tails = [a.render() for a in self.anchors if a.render()]
        return f"`{path}::{','.join(tails)}`" if tails else f"`{path}`"


def parse_anchors(cell: str, snapshot: Snapshot | None = None) -> list[Anchor]:
    """The anchors one column-2 cell addresses, in either grammar.

    `snapshot` is what turns a legacy line anchor into an identity, and it must
    be the tree that map's line numbers were measured at. Without it the legacy
    numbers stay `LineAnchor`s, which is the honest reading of a number nobody
    can date.
    """
    target = cell.replace("`", "").strip()
    match = PATH_RE.search(target)
    if match is None:
        return []
    path = match.group(1)
    tail = target[match.end() :]
    if tail.startswith("::"):
        return _parse_identity_anchors(path, tail[2:])
    if tail.startswith(":"):
        return _lift(path, _parse_legacy_spans(tail[1:]), snapshot)
    return [FileAnchor(path)]


def _parse_identity_anchors(path: str, tail: str) -> list[Anchor]:
    anchors: list[Anchor] = []
    lines: list[tuple[int, int]] = []
    for token in (t.strip() for t in tail.split(",")):
        if not token:
            continue
        span = LINE_ANCHOR.fullmatch(token)
        if span is not None:
            lo = int(span.group(1))
            lines.append((lo, int(span.group(2)) if span.group(2) else lo))
            continue
        parts = token.split("::")
        if len(parts) == 4:
            qualname, type_name, rest = parts[0], parts[2], parts[3]
            digest, _, ordinal = rest.partition("#")
            anchors.append(SiteAnchor(Identity(path, qualname, type_name, digest, int(ordinal or 1))))
        elif len(parts) == 3:
            qualname, type_name, rest = parts
            digest, _, ordinal = rest.partition("#")
            anchors.append(SiteAnchor(Identity(path, qualname, type_name, digest, int(ordinal or 1))))
        else:
            anchors.append(SpanAnchor(path, token))
    if lines:
        anchors.append(LineAnchor(path, tuple(lines)))
    return anchors


def _parse_legacy_spans(tail: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for token in SPAN_RE.findall(tail):
        if "-" in token:
            lo, hi = token.split("-")
            spans.append((int(lo), int(hi)))
        else:
            spans.append((int(token), int(token)))
    return spans


def _lift(path: str, spans: list[tuple[int, int]], snapshot: Snapshot | None) -> list[Anchor]:
    """Turn legacy line spans into the most precise anchor the tree supports.

    One function at a time. Where the row's lines include assertion sites in
    that function, the sites are what the row addresses and they alone are
    emitted: a site anchor is self-verifying, because the tree agrees the line
    holds that assertion, and a span anchor beside it would claim every other
    site in the same test for the same row. Where they include no site, the
    function itself is the anchor. Lines inside no function at all stay
    literal, because inventing a name for them would be a guess.
    """
    if not spans:
        return [FileAnchor(path)]
    if snapshot is None:
        return [LineAnchor(path, tuple(spans))]
    #: qualname -> the sites this row cites in it, empty when it cites none
    cited: dict[str, list[Identity]] = {}
    orphans: list[tuple[int, int]] = []
    for lo, hi in spans:
        landed = False
        for line in range(lo, hi + 1):
            site = snapshot.site_at(path, line)
            if site is not None:
                here = cited.setdefault(site.identity.qualname, [])
                if site.identity not in here:
                    here.append(site.identity)
                landed = True
                continue
            function = snapshot.enclosing(path, line)
            if function is not None:
                cited.setdefault(function.qualname, [])
                landed = True
        if not landed:
            orphans.append((lo, hi))
    anchors: list[Anchor] = []
    for qualname, identities in cited.items():
        if identities:
            anchors.extend(SiteAnchor(i) for i in identities)
        else:
            anchors.append(SpanAnchor(path, qualname))
    if orphans:
        anchors.append(LineAnchor(path, tuple(orphans)))
    return anchors or [FileAnchor(path)]


def split_cells(line: str) -> list[str]:
    r"""One table row's cells, split on unescaped pipes only.

    `\|` inside a code span is content: a naive split turns one row into three.
    """
    cells: list[str] = []
    current: list[str] = []
    index = 0
    body = line.strip()
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body) and body[index + 1] == "|":
            current.append("\\|")
            index += 2
        elif body[index] == "|":
            cells.append("".join(current).strip())
            current = []
            index += 1
        else:
            current.append(body[index])
            index += 1
    cells.append("".join(current).strip())
    # The leading pipe opens the row, so the first split is always empty.
    return cells[1:]


def read_rows(path: str = INVENTORY, snapshot: Snapshot | None = None) -> list[Row]:
    """Every row of a map, with the group and section heading it sits under.

    Only a `##` heading changes the group, since the pulled-out blocks are
    `###` subsections of the group they belong to; the section tracks those,
    which is how `generate` knows the mechanical batch from the claims above
    it.
    """
    rows: list[Row] = []
    group = section = ""
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            heading = line.lstrip("# ").strip()
            if level <= 2:
                group, section = heading.split(":")[0], ""
            else:
                section = heading
            continue
        if not line.startswith("|"):
            continue
        cells = split_cells(line)
        if len(cells) < 3 or not ROW_ID.fullmatch(cells[0]):
            continue
        rows.append(
            Row(
                id=cells[0],
                group=group,
                section=section,
                anchors=parse_anchors(cells[1], snapshot),
                shape=cells[2],
                disposition=cells[3] if len(cells) > 3 else "",
                source_line=number,
                cells=cells,
            )
        )
    return rows
