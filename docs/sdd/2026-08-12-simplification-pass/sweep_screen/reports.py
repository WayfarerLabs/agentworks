"""The reports: what the map claims, where its rows now sit, and what a fresh
cut of the mechanical batch looks like.

`attribute` and `resolve` are one join at two granularities: an anchor of the
map against the tree. `attribute` reads it site-first ("which row owns this
site") and `resolve` anchor-first ("where does this row's anchor sit now").
`totals` counts the row markup, and `generate` writes instead of reporting.

`carry` and `reanchor` were the third and fourth and retired with the fresh
cut: both existed to move a line-anchored map's evidence into an
identity-anchored one, and the cut leaves no line numbers to move.
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from .estate import assertion_digest
from .inventory import (
    ACCOUNTED,
    CITED_FILE,
    CITED_ID,
    CITED_LINE,
    CITED_QUALNAME,
    GROUP_1,
    INVENTORY,
    MECHANICAL_BATCH,
    NO_ROW_HEADING,
    QUALIFIED,
    RETIRED_HEADING,
    RETIRED_ROW,
    URL,
    LineAnchor,
    Row,
    SiteAnchor,
    SpanAnchor,
    read_rows,
    split_cells,
    stamp_spans,
)
from .screens import screen_verdicts

if TYPE_CHECKING:
    from .estate import Site, Snapshot

#: How the map titles its groups, so a `totals` run pastes into it unedited.
GROUP_TITLES = {
    "Group 1": "1. Mechanical `match=` narrowing",
    "Group 3": "3. Report lines and hints (four sub-batches)",
    "Group 4": "4. Schema, manifests, capabilities and platforms",
    "Group 5": "5. Authored-artifact form policing",
    "Group 6": "6. Source guards",
    "Deferred": "Deferred (held for R4)",
}


def claim_rows(rows: list[Row]) -> list[Row]:
    """Group 1's judgment and keep rows: everything it holds outside the batch.

    Every one of them is a claim. A row whose estate is gone is not in the
    ledger any more, so there is no such thing here as a claim that cannot be
    checked.
    """
    return [r for r in rows if r.group == GROUP_1 and r.section != MECHANICAL_BATCH]


def unresolved_claims(rows: list[Row], snapshot: Snapshot) -> list[tuple[str, str, str]]:
    """Claim anchors that reach nothing at this tree.

    The same join `attribute` and `generate` both need: one says the map is not
    true here, the other refuses to cut a batch against it.
    """
    return [
        (row.id, anchor.render(), outcome.state)
        for row in rows
        for anchor in row.anchors
        if (outcome := anchor.resolve(snapshot)).state not in ("resolved", "line-anchored")
    ]


def retired_ids(map_path: str) -> tuple[set[str], set[str]]:
    """The retired table's two namespaces: this cut's ids, and the 2026-08-19 map's.

    They have to be separate. That map numbered its mechanical batch by
    position, so an id it retired was handed to a different row when this cut
    regenerated, and five ids name one row there and another here. Merging them
    would make a citation of `G1-013` resolve to whichever the reader guesses.
    """
    mine: set[str] = set()
    theirs: set[str] = set()
    lines = Path(map_path).read_text(encoding="utf-8").splitlines()
    if RETIRED_HEADING not in lines:
        return mine, theirs
    for line in lines[lines.index(RETIRED_HEADING) + 1 :]:
        if line.startswith("#"):
            break
        if match := RETIRED_ROW.match(line):
            (theirs if match.group(2) else mine).add(match.group(1))
    return mine, theirs


def section_ids(map_path: str, heading: str, pattern: re.Pattern[str]) -> set[str]:
    """The first cell of every table row under `heading`, up to the next one."""
    lines = Path(map_path).read_text(encoding="utf-8").splitlines()
    if heading not in lines:
        return set()
    found = set()
    for line in lines[lines.index(heading) + 1 :]:
        if line.startswith("#"):
            break
        if match := pattern.match(line):
            found.add(match.group(1))
    return found


def check_map(
    rows: list[Row],
    retired: tuple[set[str], set[str]],
    files: Counter[str],
    snapshot: Snapshot,
    map_path: str = INVENTORY,
) -> list[str]:
    """Structural faults in the map itself, as a list of complaints.

    These are the properties the map's prose used to promise a reader and
    nothing enforced, which is how it came to cite two dozen ids that were not
    rows: part-local ids from files that no longer exist, and rows the ledger
    dropped. A citation that resolves to nothing sends a reader looking for
    evidence that is not there.
    """
    faults: list[str] = []
    ids = [r.id for r in rows]
    live = set(ids)
    mine, theirs = retired
    known = live | mine
    row_at = {row.source_line: row for row in rows}
    # An id this cut retired must not also be live: that one really would
    # resolve to two rows. The 2026-08-19 map's ids are a separate namespace
    # and may collide freely, which is what qualifying a citation is for.
    for row_id in sorted(live & mine):
        faults.append(f"{row_id} is both a live row and an id this cut retired, so a citation resolves to two rows")
    for row_id in sorted({i for i in ids if ids.count(i) > 1}):
        faults.append(f"duplicate row id {row_id}")
    for row in rows:
        for anchor in row.anchors:
            if isinstance(anchor, SpanAnchor) and not anchor.digest:
                faults.append(f"{row.id} span anchor {anchor.qualname} carries no assertion digest; run restamp")
    # Every line, not only a row's cells: the map's prose cites ids and files
    # the same way its rows do, and three prose citations dangled while this
    # read rows alone.
    #
    # An id is read OUTSIDE code spans and a path INSIDE them, which is not an
    # inconsistency but the two conventions this file actually uses. A row
    # quoting `L-401` is showing the grammar, not citing a row; a path is
    # written in backticks every time, so blanking spans would read almost none
    # of them.
    checked = 0
    for number, line in enumerate(Path(map_path).read_text(encoding="utf-8").splitlines(), start=1):
        here = row_at.get(number)
        source = f"row {here.id}" if here else f"line {number}"
        # Code spans are NOT blanked for ids. A citation is written in
        # backticks as often as not, and blanking them left a wrong id invisible
        # to the very check that exists to catch it. A grammar example uses a
        # placeholder `ROW_ID` does not match, or is written in prose.
        bare = URL.sub(" ", line)
        # A qualified citation says it means the previous map's numbering, and
        # that list is right here, so it is checked rather than waved through.
        for name in sorted(set(QUALIFIED.findall(bare)) - theirs):
            faults.append(f"{source} cites {name} (2026-08-19 map), which that map's retired list does not hold")
        # A qualified citation is already answered: it says the id belongs to a
        # map that no longer exists, so there is nothing here to resolve it
        # against and pointing a reader at this file would be the error. It is
        # dropped before the rest is read, so the id inside it is not taken for
        # a row of this cut.
        for name in sorted(set(CITED_ID.findall(QUALIFIED.sub("", bare))) - known - {here.id if here else ""}):
            faults.append(f"{source} cites {name}, which is neither a row nor an id this cut retired")
        # A cell says what it means by name. A line number in one is an edit
        # recipe that was right when written and silently wrong afterwards, and
        # eleven of them were pointing at the wrong statement by the time anyone
        # executed one. Running prose may still carry a line number.
        #
        # Keyed on being a TABLE CELL rather than on being a parsed row: the
        # retired list and the recipe-verification table are cells a reader acts
        # on exactly like rows, and neither parses as one, so keying on the row
        # let two citations through with every command green. Code spans are NOT
        # blanked, because a citation lives inside one; URLs are, like every
        # sibling here, since the digits after a colon in a link are a port or a
        # path and reading them as a line number is a fault nobody can fix.
        if line.startswith("| "):
            for spelling in sorted({m.group(0) for m in CITED_LINE.finditer(bare)}):
                faults.append(f"{source} cites {spelling.strip()} by line; name the function instead")
        # A cited function resolves like an anchor, so a citation that names
        # nothing is refused rather than read and believed.
        for cited_path, qualname in sorted(set(CITED_QUALNAME.findall(URL.sub(" ", line)))):
            real = snapshot.tree.resolve_suffix(cited_path)
            if real is None or not snapshot.function(real, qualname):
                faults.append(f"{source} cites {cited_path}::{qualname}, which names no function in this tree")
        for path in sorted(set(CITED_FILE.findall(line))):
            checked += 1
            matches = files.get(path, 0)
            if matches == 0:
                faults.append(f"{source} cites {path}, which is not a file in this tree")
            elif matches > 1:
                faults.append(f"{source} cites {path}, which names {matches} files; add a directory segment")
    print(f"# file citations checked: {checked}", file=sys.stderr)
    return faults


def unrowed(rows: list[Row], snapshot: Snapshot) -> list[str]:
    """Every test file in the sweep's population that no row's anchor names.

    The map has to account for what it left alone as well as what it addresses,
    and that accounting was previously a recipe a second auditor was asked to
    re-execute by hand. An anchor names its file outright, so the set is a
    subtraction: the population, minus every path an anchor carries.
    """
    named = {anchor.path for row in rows for anchor in row.anchors}
    return [path for path in snapshot.tree.test_files() if path not in named]


def check_accounting(map_path: str, missing: list[str]) -> list[str]:
    """The files-with-no-row table names exactly the files no row names.

    Its reasons are authored and reviewed like any other prose here. Its
    membership is not prose: it is a set the tooling already computes, and it
    drifted the moment two of its files gained rows, because nothing compared
    the two. The table is found by its heading and read to the next one.
    """
    listed = section_ids(map_path, NO_ROW_HEADING, ACCOUNTED)
    if not listed:
        return [f"the map has no {NO_ROW_HEADING!r} section, so nothing accounts for the files no row names"]
    return [f"the files-with-no-row table names {p}, which a row addresses" for p in sorted(listed - set(missing))] + [
        f"no row names {p}, and the files-with-no-row table omits it" for p in sorted(set(missing) - listed)
    ]


def _ownership(rows: list[Row], sites: list[Site]) -> tuple[list[Site], list[Site], dict[Site, list[str]]]:
    """Sites owned by no row and by more than one, plus the owner ids."""
    owners = {s: [r.id for r in rows if r.claims(s)] for s in sites}
    return (
        [s for s in sites if not owners[s]],
        [s for s in sites if len(owners[s]) > 1],
        owners,
    )


def attribute(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """Which row claims each site, and which sites or row anchors do not resolve.

    Exits non-zero when the ownership claim the map makes is false, because a
    report that says "25 unowned" and returns success is a check nothing can
    gate on.
    """
    rows = read_rows(map_path)
    group_one = [r for r in rows if r.group == GROUP_1]
    claimed = {s: [r for r in rows if r.claims(s)] for s in snapshot.sites}

    match_sites = [s for s in snapshot.sites if s.kind == "match="]
    print(f"estate at {snapshot.tree}: {len(snapshot.sites)} sites ({len(match_sites)} `match=`)")
    print(f"identities: {len(snapshot.by_identity)}, of which {len(snapshot.ties)} name more than one site")
    print(f"rows: {len(rows)} total, {len(group_one)} in group 1")

    unowned, twice, owners = _ownership(group_one, match_sites)
    print(f"\n`match=` sites owned by exactly one group-1 row: {len(match_sites) - len(unowned) - len(twice)}")
    print(f"  owned by no group-1 row:       {len(unowned)}")
    for site in unowned:
        print(f"      {site.where}  {site.identity.tail}")
    print(f"  owned by several group-1 rows: {len(twice)}")
    for site in twice:
        print(f"      {site.where}  {', '.join(owners[site])}")

    print("\nwebsite regex sites, by the row that claims them:")
    for site in (s for s in snapshot.sites if s.kind != "match="):
        by = ", ".join(f"{r.id} [{r.group}]" for r in claimed[site]) or "NONE"
        print(f"  {site.where}  {site.kind}  {by}")

    # A group-4 row's anchor legitimately covers a group-1 site: the two address
    # different assertions in the same test. Only same-group overlap is a
    # defect, so cross-group overlap is counted rather than listed.
    cross = sum(1 for hits in claimed.values() if len({r.group for r in hits}) > 1)
    print(f"\nsites addressed by rows in more than one group (expected, not a defect): {cross}")

    print("\ngroup-1 anchors that do not resolve:")
    resized = []
    for row in group_one:
        for anchor in row.anchors:
            outcome = anchor.resolve(snapshot)
            if outcome.state in ("grown", "shrunk"):
                resized.append(f"{row.id} {anchor.render()} {outcome.detail}")
            if outcome.state not in ("resolved", "line-anchored"):
                print(f"  {row.id} ({anchor.path}): {outcome.state} {anchor.render()} {outcome.detail}")

    # Two more ownership faults, neither of which the counts above can see. A
    # resized group means the row claims a different number of assertions than
    # it was written against. A claim anchor that reaches nothing means the row
    # shields no site at all, and a group that vanished entirely reads as `gone`
    # rather than `shrunk`, so the resized check alone would let it past.
    stale = unresolved_claims(claim_rows(rows), snapshot)
    print(f"\ngroup-1 claim anchors that reach nothing here: {len(stale)}")
    for row_id, rendered, state in stale:
        print(f"  {row_id} {state} {rendered}")
    if unowned or twice or resized or stale:
        raise SystemExit(
            f"\nownership is not exactly-once: {len(unowned)} unowned, {len(twice)} owned twice,"
            f" {len(resized)} site groups resized, {len(stale)} claim anchors reaching nothing"
        )


def resolve(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """Every anchor in the map, to its place in this tree."""
    rows = read_rows(map_path)
    states: Counter[str] = Counter()
    per_group: dict[str, Counter[str]] = defaultdict(Counter)

    print("row\tgroup\tanchor\tstate\twhere\tdetail")
    for row in rows:
        for anchor in row.anchors:
            outcome = anchor.resolve(snapshot)
            states[outcome.state] += 1
            per_group[row.group][outcome.state] += 1
            print(
                f"{row.id}\t{row.group}\t{anchor.path}::{anchor.render()}"
                f"\t{outcome.state}\t{outcome.where}\t{outcome.detail}"
            )

    print(f"\n# anchors resolved against {snapshot.tree}", file=sys.stderr)
    for group in sorted(per_group):
        spelled = " ".join(f"{k}={v}" for k, v in sorted(per_group[group].items()))
        print(f"#   {group}: {spelled}", file=sys.stderr)
    print(f"# totals: {' '.join(f'{k}={v}' for k, v in sorted(states.items()))}", file=sys.stderr)
    print(f"# anchors: {sum(states.values())} over {len(rows)} rows", file=sys.stderr)


def generate(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """The group-1 mechanical batch at this tree: the estate minus the claims.

    A claim is any group-1 row outside the mechanical batch, which is exactly
    how the 2026-08-19 batch was built by hand.

    Four checks follow, because the generated set is the complement of the
    claims and so adds up by construction whatever the claims are. Comparing
    counts proves nothing; these ask whether the claims are worth complementing.

    * **Ownership.** The map's own property, run over the claims plus what was
      just generated: every `match=` site owned by exactly one row. This is what
      catches two claim rows reaching the same site.
    * **Widened claims.** A group-1 claim that reaches a site through a span or
      line anchor fails, because such a claim covers every assertion the test
      gains after it was written rather than the one it was written about.
    * **A handle the screen found.** A site the callee screen marks
      `multi-handle-discriminates` is `hla.md` case 2's first arm: the code
      already offers a handle that tells the targeted raise apart, so the row
      converts onto it rather than deleting the assertion. Emitting one as a
      mechanical delete would decide case 2 by not noticing it.
    * **Stale claims.** A group-1 claim row with an anchor that does not resolve
      here fails, because its sites fall into the mechanical batch as ordinary
      deletes and the judgment that pulled them out is lost without a word. This
      is the check the count comparison was standing in for. A span claim that
      resolves and covers no site is reported beside it and does not refuse; the
      comment on `barren` says why.

    `claim_rows` says which rows are claims. `[deferred]` rows are among them,
    because their sites are real and must not fall into the batch.
    """
    rows = read_rows(map_path)
    claims = claim_rows(rows)
    screened = screen_verdicts(snapshot.tree)
    estate = [s for s in snapshot.sites if s.kind == "match="]
    claimed = {s for s in estate if any(r.claims(s) for r in claims)}
    remaining = [s for s in estate if s not in claimed]

    by_path: dict[str, list[Site]] = defaultdict(list)
    for site in remaining:
        by_path[site.path].append(site)

    generated: list[Row] = []
    print("<!-- prettier-ignore -->")
    print("| id | file and anchors | shape | disposition |")
    print("| --- | --- | --- | --- |")
    # An id is never reused, so a file that still has sites keeps the id it
    # was given and a new file takes the next one above every id this map has
    # ever used. Numbering positionally, which is what this did, renamed rows
    # under every citation of them whenever a file left the batch.
    held = {a.path: r.id for r in rows if r.section == MECHANICAL_BATCH for a in r.anchors}
    # Every id this cut has used, not every id it still uses: a retired id is
    # retired, so allocating above the live maximum alone would reissue one. The
    # sixteenth new file would have taken G1-155, which the retired table holds.
    ever = {r.id for r in rows} | retired_ids(map_path)[0]
    used = [int(i[3:]) for i in ever if i.startswith("G1-") and i[3:].isdigit()]
    nxt = max(used, default=0) + 1
    for path in sorted(by_path):
        row_id = held.get(path)
        if row_id is None:
            row_id, nxt = f"G1-{nxt:03d}", nxt + 1
        sites = sorted(by_path[path], key=lambda s: (s.line, s.col))
        identities = dict.fromkeys(s.identity for s in sites)
        anchors = [SiteAnchor(i, snapshot.by_identity[i].multiplicity) for i in identities]
        kinds = ", ".join(sorted({s.identity.type_name for s in sites}))
        # The marker is the screen's verdict, so it is derived from the screen
        # rather than carried across by a reader. A row earns it only when every
        # site it claims is single-raise-path, which is what the marker's entry
        # in the map's marker section says it means.
        verified = all(screened.get(s.where, ("", "", ""))[0] == "single-raise-path" for s in sites)
        marker = "**[1-raise]** " if verified else ""
        shape = f"{marker}{len(sites)} `match=` site(s) over {kinds}"
        row = Row(row_id, GROUP_1, MECHANICAL_BATCH, list(anchors), shape, "delete", 0)
        generated.append(row)
        print(f"| {row.id} | {row.render_cell()} | {shape} | delete |")

    print(f"\n# generated {len(by_path)} rows over {len(remaining)} sites", file=sys.stderr)
    print(f"# claimed by {len(claims)} judgment and keep rows: {len(claimed)} sites", file=sys.stderr)

    handled = [
        (site.where, verdict[1])
        for site in remaining
        if (verdict := screened.get(site.where)) is not None and verdict[0] == "multi-handle-discriminates"
    ]
    unowned, twice, owners = _ownership(claims + generated, estate)
    widened = [
        (row.id, anchor.render())
        for row in claims
        for anchor in row.anchors
        if isinstance(anchor, (SpanAnchor, LineAnchor)) and any(anchor.claims(s) for s in estate)
    ]
    stale = unresolved_claims(claims, snapshot)
    # A group-1 span claim that resolves and covers no site shields nothing from
    # the batch. It is reported and does not refuse, because one tree cannot
    # tell a test that LOST its sites from a test that never had any, and a
    # group-1 row was only ever given a span where the function it anchored held
    # no cited site, so every instance here is the second kind. The comparison
    # is against every site rather than this batch's `match=` estate,
    # which is what keeps a group-1 row over `website/tests` out of the list.
    barren = [
        (row.id, anchor.render())
        for row in claims
        for anchor in row.anchors
        if isinstance(anchor, SpanAnchor)
        and anchor.resolve(snapshot).state == "resolved"
        and not any(anchor.claims(s) for s in snapshot.sites)
    ]
    print(f"# ownership check: {len(unowned)} unowned, {len(twice)} owned twice", file=sys.stderr)
    for site in unowned:
        print(f"#   unowned {site.where} {site.identity.tail}", file=sys.stderr)
    for site in twice:
        print(f"#   owned twice {site.where} by {', '.join(owners[site])}", file=sys.stderr)
    print(f"# group-1 claims reaching a site through a span or line anchor: {len(widened)}", file=sys.stderr)
    for row_id, rendered in widened:
        print(f"#   {row_id} {rendered}", file=sys.stderr)
    print(f"# batched sites the callee screen gives a handle: {len(handled)}", file=sys.stderr)
    for where, target in handled:
        print(f"#   {where} discriminates on the raise at {target}", file=sys.stderr)
    print(f"# group-1 claim anchors that do not resolve here: {len(stale)}", file=sys.stderr)
    for row_id, rendered, state in stale:
        print(f"#   {row_id} {state} {rendered}", file=sys.stderr)
    print(f"# group-1 span claims covering no site (reported, not refused): {len(barren)}", file=sys.stderr)
    for row_id, rendered in barren:
        print(f"#   {row_id} {rendered}", file=sys.stderr)
    if unowned or twice or widened or stale or handled:
        raise SystemExit(
            "this batch is not sound here"
            f" ({len(unowned)} unowned, {len(twice)} owned twice, {len(widened)} widened,"
            f" {len(stale)} unresolved claim anchors, {len(handled)} sites with a handle)"
        )


def _refuse_unless_only_digests_moved(map_path: str, before: str, after: str) -> None:
    """Two checks on what `restamp` is about to write; it writes nothing if
    either fails.

    Both exist because stamping went wrong once in a way that looked right.

    The first is that the map differs from the one it read by digests and
    nothing else: strip every digest from both and they must be identical, so a
    moved cell boundary, a mangled escape or a lost line fails here rather than
    in a reader six weeks later.

    The second is that every digest ends an anchor token, and it is the check
    the first cannot make. Stamping by substring rewrote every occurrence of a
    qualname in a cell, and 23 rows carry a span anchor and a site anchor on one
    name, so 55 site anchors became `qualname@span::Type::digest`. Stripping the
    digests hides that exactly, because the corruption strips out too.
    """
    bare = re.compile(r"@[0-9a-f]{6}")
    if bare.sub("", after) != bare.sub("", before):
        raise SystemExit(
            f"{map_path}: restamp would change more than digests, so it wrote nothing."
            " Strip the digests from both to see what moved"
        )
    misplaced = [
        after[: found.start()].count("\n") + 1
        for found in bare.finditer(after)
        if after[found.end() : found.end() + 1] not in {",", "`"}
    ]
    if misplaced:
        where = ", ".join(str(number) for number in misplaced[:5])
        raise SystemExit(
            f"{map_path}: restamp would put {len(misplaced)} digest(s) somewhere other than the end of"
            f" an anchor token (line {where}), so it wrote nothing"
        )


def restamp(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """Bring every span anchor's assertion digest into line with the tree.

    Two consumers, which is why this is a command and not a one-off script. An
    interpreter upgrade changes how `ast.unparse` spells a test and invalidates
    every digest at once, which the entry point refuses until they are stamped
    again. And an executing PR that edits an anchored test changes its
    assertions ON PURPOSE, so it brings the map with it in the same PR rather
    than leaving the next reader a `changed` verdict that means "we did that".

    This is the one command that WRITES the map. Everything else prints and
    leaves the file alone, so the divergence is deliberate: the whole span-anchor
    population is not a paste, and the diff under review is the artifact either
    way.

    A span anchor whose function is gone keeps whatever it had and is reported,
    because inventing a digest for a function nobody can find would answer a
    question the row's owner has to. What it is about to write is checked before
    it writes it, and a failure leaves the map alone.
    """
    rows = read_rows(map_path)
    digests: dict[tuple[str, str], str] = {}
    absent: list[tuple[str, str]] = []
    moved: list[tuple[str, str, str]] = []
    for row in rows:
        for anchor in row.anchors:
            if not isinstance(anchor, SpanAnchor):
                continue
            ranges = snapshot.function(anchor.path, anchor.qualname)
            if not ranges:
                absent.append((row.id, f"{anchor.path}::{anchor.qualname}"))
                continue
            now = assertion_digest([text for f in ranges for text in f.assertions])
            digests[(anchor.path, anchor.qualname)] = now
            if now != anchor.digest:
                moved.append((row.id, anchor.render(), f"{anchor.qualname}@{now}"))

    by_line = {row.source_line: row for row in rows}
    before = Path(map_path).read_text(encoding="utf-8")
    lines = before.splitlines()
    for number in by_line:
        cells = split_cells(lines[number - 1])
        if len(cells) < 2:
            continue
        stamped = stamp_spans(cells[1], digests)
        if stamped != cells[1]:
            lines[number - 1] = lines[number - 1].replace(cells[1], stamped, 1)
    after = "\n".join(lines) + "\n"
    _refuse_unless_only_digests_moved(map_path, before, after)
    Path(map_path).write_text(after, encoding="utf-8")

    print(f"# functions digested: {len(digests)}", file=sys.stderr)
    print(f"# span anchor digests that moved: {len(moved)}", file=sys.stderr)
    for row_id, was, now in moved:
        print(f"#   {row_id} {was} -> {now}", file=sys.stderr)
    print(f"# span anchors whose function is gone, left alone: {len(absent)}", file=sys.stderr)
    for row_id, where in absent:
        print(f"#   {row_id} {where}", file=sys.stderr)


def totals(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """The row markup, counted, which is what the Totals section reports.

    Counted here rather than by hand because the map has twice carried a total
    forward that no longer matched its rows.
    """
    rows = read_rows(map_path)
    groups = list(dict.fromkeys(r.group for r in rows))
    emitted = [
        "| Group | Live | delete | convert | keep | Deferred | Ledger |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    ledger: Counter[str] = Counter()
    for group in groups:
        here = [r for r in rows if r.group == group]
        live = [r for r in here if r.live]
        counts = Counter(r.disposition for r in live)
        deferred = sum(1 for r in here if "deferred" in r.markers)
        ledger["live"] += len(live)
        for name in ("delete", "convert", "keep"):
            ledger[name] += counts[name]
        ledger["deferred"] += deferred
        ledger["ledger"] += len(here)
        emitted.append(
            f"| {GROUP_TITLES.get(group, group)} | {len(live)} | {counts['delete']} | {counts['convert']} "
            f"| {counts['keep']} | {deferred} | {len(here)} |"
        )
    emitted.append(
        f"| **All** | {ledger['live']} | {ledger['delete']} | {ledger['convert']} | {ledger['keep']} "
        f"| {ledger['deferred']} | {ledger['ledger']} |"
    )
    for line in emitted:
        print(line)
    # The marker means the screen verified every site the row claims. It is
    # derived for the batch, but a judgment row can carry one by hand, and a
    # hand-carried verdict goes stale the moment the code under it moves.
    screened = screen_verdicts(snapshot.tree)
    stale = [
        row.id
        for row in rows
        if "1-raise" in row.markers
        and not all(
            screened.get(site.where, ("", "", ""))[0] == "single-raise-path"
            for site in snapshot.sites
            if any(a.claims(site) for a in row.anchors)
        )
    ]
    # An anchor that does not resolve is a row addressing something that is not
    # there, which is the whole point of anchoring by identity. Nothing failed on
    # one until now: a digest moved by an edit to an anchored test reported
    # `changed` into a listing nobody gated on, and a wrong digest written by
    # hand survived a round that way. `line-anchored` is the one state that
    # passes, because a line anchor declares up front that it resolves to
    # nothing; every other state is a fault, and `restamp` is how a deliberate
    # change is absorbed.
    adrift = [
        f"{row.id} anchor {anchor.render()} is {outcome.state}" + (f" ({outcome.detail})" if outcome.detail else "")
        for row in rows
        for anchor in row.anchors
        if (outcome := anchor.resolve(snapshot)).state not in ("resolved", "line-anchored")
    ]
    # The Totals section is a pasted copy of what this prints, and a copy is a
    # thing that drifts: it has been a round behind twice. Comparing the two is
    # what makes pasting it safe, and it is the only gate on a figure the map
    # states rather than derives.
    # The whole block, in order, header and alignment row included: comparing
    # only the data rows let a reordered or re-headed table pass.
    printed = [line.rstrip() for line in emitted]
    pasted = [line.rstrip() for line in Path(map_path).read_text(encoding="utf-8").splitlines()]
    absent: list[str] = []
    if not any(pasted[i : i + len(printed)] == printed for i in range(len(pasted))):
        absent = ["the Totals section is not this block, in this order"]
    missing = unrowed(rows, snapshot)
    faults = check_map(rows, retired_ids(map_path), snapshot.tree.path_suffixes(), snapshot, map_path)
    faults += absent
    faults += adrift
    faults += [f"{row_id} carries [1-raise] and the screen does not verify every site it claims" for row_id in stale]
    faults += check_accounting(map_path, missing)
    print(f"\n# structural faults: {len(faults)}", file=sys.stderr)
    for fault in faults:
        print(f"#   {fault}", file=sys.stderr)
    print(f"# executable set: {ledger['live']} rows; ledger: {ledger['ledger']} rows", file=sys.stderr)
    print(
        f"# test files: {len(snapshot.tree.test_files())}, of which no row names: {len(missing)}",
        file=sys.stderr,
    )
    for path in missing:
        print(f"#   {path}", file=sys.stderr)
    if faults:
        raise SystemExit(f"the map has {len(faults)} structural fault(s)")
