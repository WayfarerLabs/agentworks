"""The reports: what the map claims, where its rows now sit, and what a fresh
cut of the mechanical batch looks like.

`attribute`, `resolve` and `carry` are one join at three granularities: an
anchor from some map against a snapshot of some tree. `attribute` reads it
site-first ("which row owns this site"), `resolve` anchor-first ("where does
this row's anchor sit now"), and `carry` row-first against a second, older map
("does this row's evidence still apply"). `totals` counts the row markup.
`generate` and `reanchor` write instead of reporting.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

from .estate import Site, Snapshot
from .inventory import (
    GROUP_1,
    INVENTORY,
    MECHANICAL_BATCH,
    LineAnchor,
    Row,
    SiteAnchor,
    SpanAnchor,
    join_cells,
    read_rows,
    split_cells,
)
from .tree import Tree

#: Anchor states that mean the row still reaches what it was cut against.
INTACT = frozenset({"resolved", "found", "moved"})


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
    rows = read_rows(map_path, snapshot)
    group_one = [r for r in rows if r.group == GROUP_1]
    claimed = {s: [r for r in rows if r.claims(s)] for s in snapshot.sites}

    match_sites = [s for s in snapshot.sites if s.kind == "match="]
    print(f"estate at {snapshot.tree}: {len(snapshot.sites)} sites ({len(match_sites)} `match=`)")
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
    for row in group_one:
        for anchor in row.anchors:
            outcome = anchor.resolve(snapshot)
            if outcome.state not in ("resolved", "line-anchored"):
                print(f"  {row.id} ({anchor.path}): {outcome.state} {anchor.render()} {outcome.detail}")

    if unowned or twice:
        raise SystemExit(f"\nownership is not exactly-once: {len(unowned)} unowned, {len(twice)} owned twice")


def resolve(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """Every anchor in the map, to its place in this tree."""
    rows = read_rows(map_path, snapshot)
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


def _family(row_id: str) -> str:
    for prefix in ("G1-C", "G1-M", "G1-I", "G1-K", "RB-"):
        if row_id.startswith(prefix):
            return prefix
    return row_id.split("-")[0]


def _verdict(outcomes: list[str]) -> str:
    """One row's carry verdict from its anchors' states.

    `carries` means every anchor that can settle reaches what it was cut
    against. `partial` means some do and some do not. `lost` means none.
    `no-anchor` means the row addresses nothing this parser can read.
    """
    settled = {o for o in outcomes if o != "line-anchored"}
    if not outcomes:
        return "no-anchor"
    if settled and settled <= INTACT:
        return "carries"
    if settled & INTACT:
        return "partial"
    return "lost"


def carry(snapshot: Snapshot, map_path: str, at: str) -> None:
    """An older map's rows onto the current estate, by identity.

    `at` is the commit that map's line numbers were measured against, which is
    what turns them into identities in the first place. Rows already written in
    the identity grammar ignore it.
    """
    older = Snapshot(Tree(at))
    rows = read_rows(map_path, older)
    states: Counter[str] = Counter()
    row_states: Counter[str] = Counter()
    per_group: dict[str, Counter[str]] = defaultdict(Counter)
    families: dict[str, Counter[str]] = defaultdict(Counter)
    #: Group 1's own estate is `match=` under `cli/tests`; its website sites are
    #: the same rows' `assertRaisesRegex` claims and are counted apart.
    group_one_match: Counter[str] = Counter()
    group_one_web: Counter[str] = Counter()

    print("row\tgroup\tanchor\tstate\tat-source\tat-head")
    for row in rows:
        outcomes: list[str] = []
        for anchor in row.anchors:
            before = anchor.resolve(older)
            after = anchor.resolve(snapshot)
            state = after.state
            if state == "resolved":
                state = "found" if before.where == after.where else "moved"
            outcomes.append(state)
            states[state] += 1
            per_group[row.group][state] += 1
            if row.group == GROUP_1 and isinstance(anchor, SiteAnchor):
                bucket = group_one_match if anchor.path.startswith("cli/tests/") else group_one_web
                bucket[state] += 1
            print(f"{row.id}\t{row.group}\t{anchor.path}::{anchor.render()}\t{state}\t{before.where}\t{after.where}")
        verdict = _verdict(outcomes)
        row_states[verdict] += 1
        families[_family(row.id)][verdict] += 1
        if "subtracted" in row.markers:
            families["[subtracted]"][verdict] += 1

    def spell(counter: Counter[str]) -> str:
        return " ".join(f"{k}={v}" for k, v in sorted(counter.items()))

    print(f"\n# carry {map_path} (lines read at {at}) onto {snapshot.tree}", file=sys.stderr)
    print(f"# anchors: {spell(states)}", file=sys.stderr)
    print(
        f"# group-1 site anchors, `match=` under cli/tests: {spell(group_one_match)}"
        f" (total {sum(group_one_match.values())})",
        file=sys.stderr,
    )
    print(
        f"# group-1 site anchors, regex family under website/tests: {spell(group_one_web)}"
        f" (total {sum(group_one_web.values())})",
        file=sys.stderr,
    )
    print(f"# rows: {spell(row_states)}", file=sys.stderr)
    for name in sorted(families):
        print(f"#   {name}: {spell(families[name])} (rows {sum(families[name].values())})", file=sys.stderr)
    for group in sorted(per_group):
        print(f"#   {group}: {spell(per_group[group])}", file=sys.stderr)


def generate(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """The group-1 mechanical batch at this tree: the estate minus the claims.

    A claim is any group-1 row outside the mechanical batch, which is exactly
    how the 2026-08-19 batch was built by hand.

    Three checks follow, because the generated set is the complement of the
    claims and so adds up by construction whatever the claims are. Comparing
    counts proves nothing; these ask whether the claims are worth complementing.

    * **Ownership.** The map's own property, run over the claims plus what was
      just generated: every `match=` site owned by exactly one row. This is what
      catches two claim rows reaching the same site.
    * **Widened claims.** A group-1 claim that reaches a site through a span or
      line anchor fails, because such a claim covers every assertion the test
      gains after it was written rather than the one it was written about.
    * **Stale claims.** A group-1 claim row with an anchor that does not resolve
      here fails, because its sites fall into the mechanical batch as ordinary
      deletes and the judgment that pulled them out is lost without a word. This
      is the check the count comparison was standing in for.
    """
    rows = read_rows(map_path, snapshot)
    claims = [r for r in rows if r.group == GROUP_1 and r.section != MECHANICAL_BATCH]
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
    for number, path in enumerate(sorted(by_path), start=1):
        sites = sorted(by_path[path], key=lambda s: (s.line, s.col))
        anchors = [SiteAnchor(i) for i in dict.fromkeys(s.identity for s in sites)]
        kinds = ", ".join(sorted({s.identity.type_name for s in sites}))
        shape = f"{len(sites)} `match=` site(s) over {kinds}"
        row = Row(f"G1-{number:03d}", GROUP_1, MECHANICAL_BATCH, list(anchors), shape, "delete", 0)
        generated.append(row)
        print(f"| {row.id} | {row.render_cell()} | {shape} | delete |")

    print(f"\n# generated {len(by_path)} rows over {len(remaining)} sites", file=sys.stderr)
    print(f"# claimed by {len(claims)} judgment and keep rows: {len(claimed)} sites", file=sys.stderr)

    unowned, twice, owners = _ownership(claims + generated, estate)
    widened = [
        (row.id, anchor.render())
        for row in claims
        for anchor in row.anchors
        if isinstance(anchor, (SpanAnchor, LineAnchor)) and any(anchor.claims(s) for s in estate)
    ]
    stale = [
        (row.id, anchor.render(), outcome.state)
        for row in claims
        for anchor in row.anchors
        if (outcome := anchor.resolve(snapshot)).state not in ("resolved", "line-anchored")
    ]
    print(f"# ownership check: {len(unowned)} unowned, {len(twice)} owned twice", file=sys.stderr)
    for site in unowned:
        print(f"#   unowned {site.where} {site.identity.tail}", file=sys.stderr)
    for site in twice:
        print(f"#   owned twice {site.where} by {', '.join(owners[site])}", file=sys.stderr)
    print(f"# group-1 claims reaching a site through a span or line anchor: {len(widened)}", file=sys.stderr)
    for row_id, rendered in widened:
        print(f"#   {row_id} {rendered}", file=sys.stderr)
    print(f"# group-1 claim anchors that do not resolve here: {len(stale)}", file=sys.stderr)
    for row_id, rendered, state in stale:
        print(f"#   {row_id} {state} {rendered}", file=sys.stderr)
    if unowned or twice or widened or stale:
        raise SystemExit(
            "the claims this batch was generated against are not sound here"
            f" ({len(unowned)} unowned, {len(twice)} owned twice, {len(widened)} widened,"
            f" {len(stale)} unresolved claim anchors)"
        )


def totals(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """The row markup, counted, which is what the Totals section reports.

    Counted here rather than by hand because the map has twice carried a total
    forward that no longer matched its rows.
    """
    rows = read_rows(map_path, snapshot)
    groups = list(dict.fromkeys(r.group for r in rows))
    print("| group | live | delete | convert | keep | dead | subtracted | deferred | ledger |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    ledger: Counter[str] = Counter()
    for group in groups:
        here = [r for r in rows if r.group == group]
        live = [r for r in here if r.live]
        counts = Counter(r.disposition for r in live)
        marks = Counter(m for r in here for m in r.markers if m in ("dead", "subtracted", "deferred"))
        ledger["live"] += len(live)
        for name in ("delete", "convert", "keep"):
            ledger[name] += counts[name]
        for name in ("dead", "subtracted", "deferred"):
            ledger[name] += marks[name]
        ledger["ledger"] += len(here)
        print(
            f"| {group} | {len(live)} | {counts['delete']} | {counts['convert']} | {counts['keep']} "
            f"| {marks['dead']} | {marks['subtracted']} | {marks['deferred']} | {len(here)} |"
        )
    print(
        f"| **all** | {ledger['live']} | {ledger['delete']} | {ledger['convert']} | {ledger['keep']} "
        f"| {ledger['dead']} | {ledger['subtracted']} | {ledger['deferred']} | {ledger['ledger']} |"
    )
    print(f"\n# executable set: {ledger['live']} rows; ledger: {ledger['ledger']} rows", file=sys.stderr)


def bases(map_path: str, first: str, second: str) -> None:
    """Which rows the two candidate bases disagree about.

    A line number means nothing without the tree it was read from, and this map
    was cut against two. Where both trees put a row's lines in the same test the
    anchor is safe whichever tree it came from; where they differ, the anchor
    written into the map may name the test next to the one the row meant.
    """
    trees = [Snapshot(Tree(first)), Snapshot(Tree(second))]
    lifted = [{r.id: r for r in read_rows(map_path, s)} for s in trees]
    verdicts: Counter[str] = Counter()
    print("row\tgroup\tfile\tat-first\tat-second")
    for row_id, row in lifted[0].items():
        other = lifted[1][row_id]
        if row.path is None or not row.path.endswith(".py"):
            verdicts["not applicable"] += 1
            continue
        # A cell's line numbers were read at one of these trees and nobody
        # recorded which, so lift it at both and compare what each names.
        landings = [[a.render() for a in r.anchors] for r in (row, other)]
        absent = [not s.tree.exists(row.path) for s in trees]
        if any(absent):
            verdicts["not applicable"] += 1
        elif landings[0] == landings[1]:
            verdicts["agree"] += 1
        elif not landings[0] or not landings[1]:
            verdicts["one places it nowhere"] += 1
        else:
            verdicts["disagree"] += 1
        if landings[0] != landings[1] and not any(absent):
            print(f"{row_id}\t{row.group}\t{row.path}\t{landings[0]}\t{landings[1]}")
    print(f"\n# {first} versus {second}", file=sys.stderr)
    for name, count in sorted(verdicts.items()):
        print(f"#   {name}: {count}", file=sys.stderr)


def reanchor(map_path: str, at: str) -> None:
    """Rewrite a map's line anchors into identity anchors, in place.

    One job: it reproduces the rewrite commit `c5b94404` made, so that rewrite
    is reproducible rather than taken on trust, and it retires with the fresh
    cut, when no line anchors remain to lift. `at` is the commit the map's line
    numbers were measured against and is required, because reading them at any
    other tree would name whatever function happens to sit at that line now,
    which is the drift this grammar exists to retire.
    """
    older = Snapshot(Tree(at))
    rows = {r.source_line: r for r in read_rows(map_path, older)}
    kinds: Counter[str] = Counter()
    out: list[str] = []
    for number, line in enumerate(Path(map_path).read_text(encoding="utf-8").splitlines(), start=1):
        row = rows.get(number)
        if row is None:
            out.append(line)
            continue
        cells = split_cells(line)
        if cells and cells[-1] == "":
            cells = cells[:-1]
        cells[1] = row.render_cell()
        out.append(join_cells(cells))
        for anchor in row.anchors:
            kinds[type(anchor).__name__] += 1
    Path(map_path).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"# rewrote {len(rows)} rows in {map_path} against {at}", file=sys.stderr)
    for name, count in sorted(kinds.items()):
        print(f"#   {name}: {count}", file=sys.stderr)
