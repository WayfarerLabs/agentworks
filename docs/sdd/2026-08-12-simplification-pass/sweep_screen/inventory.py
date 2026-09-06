"""The map's row grammar: how a row says what it addresses, and how that
resolves to a place in a given tree.

The map owns the grammar; its "Reading this file mechanically" section is the
prose, and this module is the implementation it points at. What the code needs
is only this. A cell is one or more backticked anchor groups, each `path` or
`path::tail,tail`; a tail is `qualname::Type::digest` for a site group, with
`*n` where the group holds more than one site, a bare `qualname` for a test
function, or `L120-124` for literal lines. Two grammars are read: the current
one, and the line-anchored one every earlier cut used, so `carry` and
`reanchor` can take an older map as input. Nothing writes line anchors any more
except a row that has no name to reach for.
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
BACKTICKED = re.compile(r"`([^`]+)`")
FENCE = re.compile(r"^\s*(```|~~~)")

#: The one section whose rows `generate` emits. Everything else in group 1 is a
#: claim against the estate that the generated batch must leave alone.
MECHANICAL_BATCH = "The mechanical batch"

GROUP_1 = "Group 1"


class RowError(SystemExit):
    """A row this parser cannot read, named so it can be found and fixed."""

    def __init__(self, where: str, message: str) -> None:
        super().__init__(f"{where}: {message}")


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
    """One group of indistinguishable assertion sites, keyed on what it asserts.

    `multiplicity` is what the group held when the row was cut. A group that has
    since grown or shrunk resolves loudly rather than quietly covering a
    different number of assertions than the row was written against.
    """

    identity: Identity
    multiplicity: int = 1

    @property
    def path(self) -> str:  # type: ignore[override]
        return self.identity.path

    def render(self) -> str:
        return self.identity.tail if self.multiplicity == 1 else f"{self.identity.tail}*{self.multiplicity}"

    def resolve(self, snapshot: Snapshot) -> Resolution:
        group = snapshot.by_identity.get(self.identity)
        if group is not None:
            if group.multiplicity == self.multiplicity:
                return Resolution("resolved", group.where)
            state = "grown" if group.multiplicity > self.multiplicity else "shrunk"
            return Resolution(state, group.where, f"{self.multiplicity} site(s) when cut, {group.multiplicity} now")
        if not snapshot.tree.exists(self.identity.path):
            return Resolution("file-gone", "")
        # A reworded message leaves the assertion in place matching something
        # else. Saying so beats "gone", which would send a reader looking for a
        # deleted test.
        near = snapshot.near(self.identity)
        if near:
            spelled = ", ".join(f"{g.where} ({g.identity.digest})" for g in near)
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
    """Literal lines, kept only where nothing could be named.

    A line anchor is a declaration that this row will go stale, so every one the
    map carries says on the row why it could not be anchored to a name.
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
        grouped: dict[str, list[str]] = {}
        for anchor in self.anchors:
            grouped.setdefault(anchor.path, []).append(anchor.render())
        parts = []
        for path, tails in grouped.items():
            written = [t for t in tails if t]
            parts.append(f"`{path}::{','.join(written)}`" if written else f"`{path}`")
        return ", ".join(parts)


def parse_anchors(cell: str, snapshot: Snapshot, *, sites_only: bool, where: str = "cell") -> list[Anchor]:
    """The anchors one column-2 cell addresses, in either grammar.

    `snapshot` must be the tree the cell's line numbers were measured at; it is
    what turns a legacy line anchor into an identity, and what an identity
    anchor is checked against. `sites_only` is the group-1 rule described on
    `_lift`.
    """
    groups = BACKTICKED.findall(cell) or [cell]
    anchors: list[Anchor] = []
    for group in groups:
        target = group.strip().strip(",").strip()
        match = PATH_RE.search(target)
        if match is None:
            continue
        path = match.group(1)
        tail = target[match.end() :]
        if PATH_RE.search(tail):
            raise RowError(
                where,
                f"{group!r} names more than one file; write one backticked `path::anchors` group per file,"
                " because everything after the first path is read as belonging to it",
            )
        if tail.startswith("::"):
            anchors.extend(_parse_identity_anchors(path, tail[2:], where))
        elif tail.startswith(":"):
            anchors.extend(_lift(path, _parse_legacy_spans(tail[1:]), snapshot, sites_only=sites_only))
        else:
            anchors.append(FileAnchor(path))
    return anchors


def _parse_identity_anchors(path: str, tail: str, where: str) -> list[Anchor]:
    anchors: list[Anchor] = []
    lines: list[tuple[int, int]] = []
    for token in (t.strip() for t in tail.split(",")):
        if not token:
            continue
        if PATH_RE.search(token):
            raise RowError(
                where,
                f"anchor {token!r} carries a second path; write one backticked `path::anchors` group per file",
            )
        span = LINE_ANCHOR.fullmatch(token)
        if span is not None:
            lo = int(span.group(1))
            lines.append((lo, int(span.group(2)) if span.group(2) else lo))
            continue
        parts = token.split("::")
        if len(parts) == 1:
            anchors.append(SpanAnchor(path, token))
            continue
        if len(parts) != 3:
            raise RowError(where, f"anchor {token!r} has {len(parts)} `::` fields; a site anchor has three")
        qualname, type_name, rest = parts
        digest, _, count = rest.partition("*")
        if count and not count.isdigit():
            raise RowError(where, f"anchor {token!r} has a non-numeric multiplicity {count!r}")
        anchors.append(SiteAnchor(Identity(path, qualname, type_name, digest), int(count or 1)))
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


def _lift(path: str, spans: list[tuple[int, int]], snapshot: Snapshot, *, sites_only: bool) -> list[Anchor]:
    """Turn legacy line spans into the most precise anchor the tree supports.

    One function at a time, and the rule differs by what the row is about.
    Group 1's rows address `match=` sites, so where a function holds sites the
    row cited, those sites are the whole claim and no span anchor joins them:
    a span anchor there would claim every other site in the same test for the
    same row and break the exactly-once ownership the group rests on.

    Every other group's rows address assertions the estate scan cannot see, so
    the enclosing function is always emitted, with any cited sites alongside it.
    Dropping the span there narrowed 36 rows onto `match=` sites they were never
    about.

    Lines inside no function stay literal, because inventing a name for them
    would be a guess.
    """
    if not spans:
        return [FileAnchor(path)]
    #: qualname -> the site identities this row cites in it, empty when it cites none
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
        if not identities or not sites_only:
            anchors.append(SpanAnchor(path, qualname))
        for identity in identities:
            group = snapshot.by_identity[identity]
            anchors.append(SiteAnchor(identity, group.multiplicity))
    if orphans:
        anchors.append(LineAnchor(path, tuple(orphans)))
    return anchors or [FileAnchor(path)]


def split_cells(line: str) -> list[str]:
    r"""One table row's cells, split on pipes that are neither escaped nor
    inside a code span.

    Both exemptions are content a naive split turns into extra columns: `\|` is
    a literal pipe, and a `|` inside backticks is part of a regex the row is
    quoting. Getting either wrong shifts every later cell, which silently moves
    the disposition out of column 4. The escape is undone here, so a cell reads
    as what it says.
    """
    cells: list[str] = []
    current: list[str] = []
    index = 0
    in_code = False
    body = line.strip()
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body) and body[index + 1] == "|":
            current.append("|")
            index += 2
            continue
        if char == "`":
            in_code = not in_code
        if char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current).strip())
    # The leading pipe opens the row, so the first split is always empty.
    return cells[1:]


def join_cells(cells: list[str]) -> str:
    """One table row from its cell texts, with every literal pipe escaped.

    Reading is lenient and writing is strict, deliberately. `split_cells` treats
    a pipe inside a code span as content because rows in the wild carry regexes
    written that way, but markdownlint and prettier are not code-span aware and
    count such a pipe as a column break, so anything written back escapes every
    pipe. `\\|` renders as a literal pipe in both places.
    """
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def read_rows(path: str, snapshot: Snapshot) -> list[Row]:
    """Every row of a map, with the group and section heading it sits under.

    Only a `##` heading changes the group, since the pulled-out blocks are `###`
    subsections of the group they sit in; the section tracks those, which is how
    `generate` knows the mechanical batch from the claims above it. Fenced
    blocks are skipped, because an example row inside one is documentation
    rather than a claim.
    """
    rows: list[Row] = []
    group = section = ""
    fence = ""
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        opener = FENCE.match(line)
        if fence:
            if opener is not None and opener.group(1) == fence:
                fence = ""
            continue
        if opener is not None:
            fence = opener.group(1)
            continue
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
                anchors=parse_anchors(
                    cells[1], snapshot, sites_only=group == GROUP_1, where=f"{path}:{number} row {cells[0]}"
                ),
                shape=cells[2],
                disposition=cells[3] if len(cells) > 3 else "",
                source_line=number,
                cells=cells,
            )
        )
    return rows
