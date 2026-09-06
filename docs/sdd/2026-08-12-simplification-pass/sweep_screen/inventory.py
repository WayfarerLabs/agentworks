"""The map's row grammar: how a row says what it addresses, and how that
resolves to a place in a given tree.

The map owns the grammar; its "Reading this file mechanically" section is the
prose, and this module is the implementation it points at. What the code needs
is only this. A cell is one or more backticked anchor groups, each `path` or
`path::tail,tail`; a tail is `qualname::Type::digest` for a site group, with
`*n` where the group holds more than one site, a bare `qualname` for a test
function, or `L120-124` for literal lines. One grammar is read. The
line-anchored cell every earlier cut used is refused rather than guessed at,
and nothing writes a line anchor except a row with no name to reach for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .estate import Identity, Site, Snapshot, assertion_digest

INVENTORY = "docs/sdd/2026-08-12-simplification-pass/sweep-inventory.md"

#: A row id, as the inventory's own reading instructions describe it: an
#: original-read prefix, the mechanical batch's, one of the pulled-out blocks,
#: or the re-baseline's, optionally with the letter suffix a split row carries.
#: Header cells that look like ids (`Sub-batch`, `File`, `Recipe`) fail this by
#: construction, which is why the parser needs no list of them.
ROW_ID = re.compile(r"(?:[A-F]|L|RB|G1)-[A-Z]?\d{1,3}[a-z]?$")

#: A row id as a justification cites one. It admits the `P2-`, `P3-` and
#: `P4-` shapes the part cuts used, which are exactly the citations that stop
#: resolving when those files are folded in and deleted.
#: A file cited with a line or a line range, in any cell. The map cites paths
#: at whatever depth reads clearly, from a bare basename to a full repository
#: path, so the check behind this resolves a citation as a path SUFFIX rather
#: than demanding one spelling. What it answers is only whether the file is
#: still there; a line number inside it is not checked, because line numbers in
#: a justification are a reading aid and the anchors are what resolve. A match
#: opening on a quote is NOT a citation: it is a string literal the row is
#: quoting from the test, like the rendered `"a.yaml:2"` location a CLI prints,
#: which names no file in this tree and never did.
CITED_FILE = re.compile(r"([A-Za-z0-9_./+-]*[A-Za-z0-9_+-]\.(?:py|mjs)):\d+(?:-\d+)?")

#: A row id, always three digits. The looser form matched prose: `L-1` in the
#: middle of a sentence about a visa is not a citation, and a check that faults
#: on it teaches its reader to stop believing it.
CITED_ID = re.compile(r"\b(?:[A-F]|L|RB|G1)-[A-Z]?\d{3}[a-z]?\b")

PATH_RE = re.compile(r"((?:cli|website)/[A-Za-z0-9_./-]+?\.(?:py|mjs))")
LINE_ANCHOR = re.compile(r"L(\d+)(?:-(\d+))?$")
MARKER_RE = re.compile(r"\[(deferred|1-raise|unverified|line-anchored:[^\]]*)\]")
ANY_MARKER = re.compile(r"\[(?:deferred|1-raise|unverified|line-anchored)\b[^\]]*\]")

#: Markers this map no longer has: `[dead]` because a row whose estate is gone
#: is not in the ledger, and `[subtracted]` because the subtraction is reversed.
#: One written by hand would read as row state and carry none, so it is refused
#: rather than ignored.
RETIRED_MARKER = re.compile(r"\[(?:dead|subtracted)\b[^\]]*\]")
CAUSE_RE = re.compile(r"\[line-anchored:\s*([^\]]*)\]")

#: Every cause a line anchor may record. A row that has to fall back to
#: literal lines carries its cause by hand, so one outside the set is a hand
#: edit that has drifted.
CAUSES = frozenset({"not Python", "between functions", "module level"})
#: Six lowercase hex, the shape both digests in this grammar take.
DIGEST = re.compile(r"[0-9a-f]{6}")
BACKTICKED = re.compile(r"`([^`]+)`")
FENCE = re.compile(r"^\s*(```|~~~)")

#: The section that accounts for what the map leaves alone, and the shape of
#: one of its rows: a backticked path in the first cell. It is checked against
#: the estate rather than read, so both are needed here.
NO_ROW_HEADING = "## Files with no row, and why"

#: The section recording rows that left the map. An id is never reused, so a
#: retired one still resolves a citation even though it addresses nothing.
RETIRED_HEADING = "## Rows this cut retired"
#: A retired id in the retired table's first cell. Three digits and an
#: optional split-row suffix, which is what `D-169a` is; the separator line's
#: dashes are not an id and this is what tells them apart. The optional
#: qualification is what separates the two namespaces this table holds.
RETIRED_ROW = re.compile(r"^\| ((?:[A-F]|L|RB|G1)-[A-Z]?\d{3}[a-z]?)( \(2026-08-19 map\))? *\|")

#: A citation that says which map's numbering it means. The 2026-08-19 map
#: numbered its mechanical batch positionally, so ids it used were handed to
#: different rows when this cut regenerated: `G1-013` names one row there and
#: another here. A citation of the old one says so, and resolves only against
#: the ids that map retired.
QUALIFIED = re.compile(r"\b((?:[A-F]|L|RB|G1)-[A-Z]?\d{3}[a-z]?) \(2026-08-19 map\)")
ACCOUNTED = re.compile(r"^\| `([^`]+)` *\|")

#: The one section whose rows `generate` emits. Everything else in group 1 is a
#: claim against the estate that the generated batch must leave alone.
MECHANICAL_BATCH = "The mechanical batch"

GROUP_1 = "Group 1"


def outside_code_spans(text: str) -> str:
    r"""`text` with every code span blanked, so a quoted marker is not one.

    A row that writes `` `[dead]` `` is quoting the vocabulary, not using it.
    Quoted in the SHAPE cell it was read as row state, which took a keep row out
    of the executable set and handed its site to the mechanical batch as a
    delete while every command reported success; quoted anywhere else it was a
    RowError, which is loud but still wrong. `\`` is an escaped backtick and
    opens nothing, the same exemption `split_cells` makes for `\|`.
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] == "`":
            out.append("  ")
            index += 2
            continue
        if text[index] == "`":
            run = len(text[index:]) - len(text[index:].lstrip("`"))
            closing = text.find("`" * run, index + run)
            if closing != -1:
                out.append(" " * (closing + run - index))
                index = closing + run
                continue
        out.append(text[index])
        index += 1
    return "".join(out)


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
    """One test function, wherever it now sits, and what it asserted there.

    A span resolves on the function surviving, which says nothing about the
    assertions inside it. Three rows retired in this cut because an assertion
    went while the span went on resolving, and one of the three was a
    substitution rather than a deletion, so counting was not enough: the digest
    is over the assertions themselves, and a swap of equal size moves it.

    It cannot see an assertion a test hands to a helper. Those functions digest
    as `e3b0c4`, over nothing, and the grammar section names them.
    """

    path: str
    qualname: str
    #: What the function asserted when the row was cut.
    digest: str = ""

    def render(self) -> str:
        return f"{self.qualname}@{self.digest}" if self.digest else self.qualname

    def resolve(self, snapshot: Snapshot) -> Resolution:
        if not snapshot.tree.exists(self.path):
            return Resolution("file-gone", "")
        ranges = snapshot.function(self.path, self.qualname)
        if not ranges:
            return Resolution("gone", "")
        where = ",".join(f"{f.start}-{f.end}" for f in ranges)
        # One name may hold several ranges, the platform-conditional idiom of
        # defining a test in sibling branches. Digesting their assertions in
        # range order makes that one answer rather than a merge of answers.
        now = assertion_digest([text for f in ranges for text in f.assertions])
        if not self.digest:
            return Resolution("unstamped", f"{self.path}:{where}", f"this function asserts @{now} here")
        if now != self.digest:
            return Resolution(
                "changed",
                f"{self.path}:{where}",
                f"cut against @{self.digest}, and this function asserts @{now} here",
            )
        detail = "defined in more than one branch" if len(ranges) > 1 else ""
        return Resolution("resolved", f"{self.path}:{where}", detail)

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
    justification: str
    source_line: int

    @property
    def path(self) -> str | None:
        return self.anchors[0].path if self.anchors else None

    @property
    def markers(self) -> set[str]:
        return {m.split(":")[0] for m in MARKER_RE.findall(outside_code_spans(self.shape))}

    @property
    def live(self) -> bool:
        """In the executable set. `[deferred]` is the only marker that takes a
        row out of it: a row whose estate is gone is no longer in the ledger at
        all, so there is nothing left for a marker to say."""
        return "deferred" not in self.markers

    def claims(self, site: Site) -> bool:
        return any(a.claims(site) for a in self.anchors)

    def render_cell(self) -> str:
        grouped: dict[str, list[str]] = {}
        for anchor in self.anchors:
            grouped.setdefault(anchor.path, []).append(anchor.render())
        parts = []
        for path, tails in grouped.items():
            written = [t for t in tails if t]
            parts.append(f"`{path}::{','.join(written)}`" if written else f"`{path}`")
        return ", ".join(parts)


def parse_anchors(cell: str, where: str = "cell") -> list[Anchor]:
    """The anchors one column-2 cell addresses.

    Reading a cell is a question about the map and not about any tree, so this
    takes none: what an anchor MEANS at a tree is `Anchor.resolve`'s answer.
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
        elif tail:
            raise RowError(where, f"{group!r} is neither a bare path nor `path::anchors`")
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
            qualname, at, digest = token.partition("@")
            # No digest at all is UNSTAMPED, which parses: `restamp` has to read
            # the map to stamp it, and a row written by hand should get its
            # digest from the tool rather than from someone guessing six hex.
            # `totals` is what refuses an unstamped anchor, beside the other
            # things a finished map has to be. A digest that is present and
            # malformed is a typo, not a state, and is refused here.
            if at and not DIGEST.fullmatch(digest):
                raise RowError(where, f"span anchor {token!r} has a malformed assertion digest {digest!r}")
            anchors.append(SpanAnchor(path, qualname, digest))
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


def stamp_spans(cell: str, digests: dict[tuple[str, str], str]) -> str:
    """Column 2 with each span anchor's assertion digest brought up to date.

    Token-wise, and never a substring replace over the cell. A row may carry a
    span anchor and a site anchor on ONE qualname, and 23 rows in this map do:
    replacing the bare name would stamp the site anchor's copy too, turning
    `qualname::Type::digest` into `qualname@span::Type::digest`. A token holding
    `::` is a site anchor and is left exactly as written, as is anything outside
    a backticked group, so what this rewrites is only what it means to.
    """

    def group(match: re.Match[str]) -> str:
        body = match.group(1)
        found = PATH_RE.search(body)
        if found is None or not body[found.end() :].startswith("::"):
            return match.group(0)
        path = found.group(1)
        out: list[str] = []
        for token in body[found.end() + 2 :].split(","):
            bare = token.strip()
            digest = digests.get((path, bare.partition("@")[0]))
            if not bare or "::" in bare or digest is None:
                out.append(token)
                continue
            out.append(token.replace(bare, f"{bare.partition('@')[0]}@{digest}"))
        return "`" + body[: found.end()] + "::" + ",".join(out) + "`"

    return BACKTICKED.sub(group, cell)


def split_cells(line: str) -> list[str]:
    r"""One table row's cells, split on pipes that are neither escaped nor
    inside a code span.

    Both exemptions are content a naive split turns into extra columns: `\|` is
    a literal pipe, and a `|` inside backticks is part of a regex the row is
    quoting. Getting either wrong shifts every later cell, which silently moves
    the disposition out of column 4. The escape is undone here, so a cell reads
    as what it says.

    Code spans follow CommonMark: a run of n backticks opens a span that only a
    run of exactly n closes, so ``a | b`` is one span rather than two. An
    escaped backtick opens nothing, so this and `outside_code_spans`, the two
    parsers that read one cell, agree on where its spans are. It is kept
    verbatim rather than unescaped, unlike `\|`, because a writer escapes
    only the pipe and a bare backtick written back would open a span.
    """
    cells: list[str] = []
    current: list[str] = []
    index = 0
    fence = 0
    body = line.strip()
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body) and body[index + 1] == "|":
            current.append("|")
            index += 2
            continue
        if char == "\\" and index + 1 < len(body) and body[index + 1] == "`":
            current.append(body[index : index + 2])
            index += 2
            continue
        if char == "`":
            run = len(body[index:]) - len(body[index:].lstrip("`"))
            if fence == 0:
                fence = run
            elif fence == run:
                fence = 0
            current.append(body[index : index + run])
            index += run
            continue
        if char == "|" and fence == 0:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    if fence:
        raise ValueError(f"unclosed code span: a run of {fence} backticks never closes")
    cells.append("".join(current).strip())
    # The leading pipe opens the row and the trailing one closes it, so the
    # first split is always empty and the last is too when the row is closed.
    body_cells = cells[1:]
    if body_cells and body_cells[-1] == "":
        body_cells.pop()
    return body_cells


def read_rows(path: str) -> list[Row]:
    """Every row of a map, with the group and section heading it sits under.

    Only a `##` heading changes the group, since the pulled-out blocks are `###`
    subsections of the group they sit in; the section tracks those, which is how
    `generate` knows the mechanical batch from the claims above it. Fenced
    blocks are skipped, because an example row inside one is documentation
    rather than a claim.

    Everything this cannot read is refused by name rather than skipped. A
    silently skipped row is a row whose sites nobody owns, reported later as
    unowned with no hint that a parse gave up.
    """
    rows: list[Row] = []
    group = section = ""
    retired = False
    fence = ""
    fence_line = 0
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        opener = FENCE.match(line)
        if fence:
            if opener is not None and opener.group(1) == fence:
                fence = ""
            continue
        if opener is not None:
            fence, fence_line = opener.group(1), number
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            heading = line.lstrip("# ").strip()
            if level <= 2:
                group, section = heading.split(":")[0], ""
            else:
                section = heading
            # The retired list is keyed by row id and shaped like a table, so it
            # would parse as a run of two-cell rows. It records ids that are no
            # longer rows, which is the opposite of what this loop collects.
            retired = line == RETIRED_HEADING
            continue
        if retired or not line.startswith("|"):
            continue
        where = f"{path}:{number}"
        try:
            cells = split_cells(line)
        except ValueError as exc:
            # A prose line with an unclosed span is not this parser's business;
            # only a row's cells have to be split correctly. Whether it is a row
            # is decided on the naive split, since the careful one just failed.
            first = line.strip().strip("|").split("|")[0].strip()
            if not ROW_ID.fullmatch(first):
                continue
            raise RowError(f"{where} row {first}", str(exc)) from exc
        if not cells or not ROW_ID.fullmatch(cells[0]):
            continue
        where = f"{where} row {cells[0]}"
        if not group:
            raise RowError(where, "a row above the first `##` heading belongs to no group")
        if len(cells) < 3:
            raise RowError(where, f"{len(cells)} cells; a row has at least an id, a target and a shape")
        _check_markers(cells, where)
        rows.append(
            Row(
                id=cells[0],
                group=group,
                section=section,
                anchors=parse_anchors(cells[1], where=where),
                shape=cells[2],
                disposition=cells[3] if len(cells) > 3 else "",
                justification=cells[4] if len(cells) > 4 else "",
                source_line=number,
            )
        )
    if fence:
        raise RowError(f"{path}:{fence_line}", f"a `{fence}` fence never closes, so every row after it goes unread")
    return rows


def _check_markers(cells: list[str], where: str) -> None:
    """Markers live in the shape cell, once each, with a known cause.

    Every marker follows the map's own vocabulary, so one that is duplicated,
    misplaced, unrecognised or retired is a hand edit that has drifted, and a
    row state read off a drifted marker is worse than no row at all.
    """
    for index, cell in enumerate(cells):
        if index == 2:
            continue
        stray = ANY_MARKER.search(outside_code_spans(cell))
        if stray is not None:
            raise RowError(where, f"marker {stray.group(0)!r} is in column {index + 1}; markers live in the shape cell")
    shape = outside_code_spans(cells[2])
    retired = RETIRED_MARKER.search(shape)
    if retired is not None:
        raise RowError(where, f"marker {retired.group(0)!r} retired on 2026-09-06 and no longer means anything")
    found = MARKER_RE.findall(shape)
    for marker in found:
        if found.count(marker) > 1:
            raise RowError(where, f"marker [{marker}] appears more than once")
    # A marker the vocabulary recognises but the reader cannot parse is worse
    # than an unknown one: `[subtracted]` without an owner and `[line-anchored]`
    # without a cause both look like row state and carry none.
    for hit in ANY_MARKER.findall(shape):
        if hit not in (f"[{m}]" for m in found):
            raise RowError(where, f"marker {hit!r} is incomplete; it needs the value after its colon")
    causes = CAUSE_RE.findall(shape)
    if len(causes) > 1:
        raise RowError(where, "more than one line-anchored marker")
    for cause in causes:
        unknown = [c for c in (part.strip() for part in cause.split(",")) if c not in CAUSES]
        if unknown:
            raise RowError(where, f"line-anchored cause {', '.join(unknown)!r} is not one of {sorted(CAUSES)}")
