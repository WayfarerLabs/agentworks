"""The reports: what the map claims, where its rows now sit, and what a fresh
cut of the mechanical batch looks like.

`attribute`, `resolve` and `carry` are one join at three granularities: an
anchor from some map against a snapshot of some tree. `attribute` reads it
site-first ("which row owns this site"), `resolve` anchor-first ("where does
this row's anchor sit now"), and `carry` row-first against a second, older map
("does this row's evidence still apply"). `generate` and `reanchor` write
instead of reporting: the first emits the mechanical batch a fresh cut owes,
the second rewrites an older map's line anchors into identities.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

from .estate import Site, Snapshot
from .inventory import (
    INVENTORY,
    MECHANICAL_BATCH,
    Row,
    SiteAnchor,
    read_rows,
    split_cells,
)
from .tree import Tree

GROUP_1 = "Group 1"


def _claimants(rows: list[Row], sites: list[Site]) -> dict[Site, list[Row]]:
    return {s: [r for r in rows if r.claims(s)] for s in sites}


def attribute(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """Which row claims each site, and which sites or row anchors do not resolve."""
    rows = read_rows(map_path, snapshot)
    group_one = [r for r in rows if r.group == GROUP_1]
    claimed = _claimants(rows, snapshot.sites)

    match_sites = [s for s in snapshot.sites if s.kind == "match="]
    print(f"estate at {snapshot.tree}: {len(snapshot.sites)} sites ({len(match_sites)} `match=`)")
    print(f"rows: {len(rows)} total, {len(group_one)} in group 1")

    # The property the inventory claims: group 1 owns every `match=` site, once.
    owners = {s: [r.id for r in group_one if r.claims(s)] for s in match_sites}
    unowned = [s for s in match_sites if not owners[s]]
    twice = [s for s in match_sites if len(owners[s]) > 1]
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

    # A group-4 row's anchor legitimately covers a group-1 site: the two
    # address different assertions in the same test. Only same-group overlap is
    # a defect, so cross-group overlap is counted rather than listed.
    cross = sum(1 for hits in claimed.values() if len({r.group for r in hits}) > 1)
    print(f"\nsites addressed by rows in more than one group (expected, not a defect): {cross}")

    print("\ngroup-1 anchors that do not resolve:")
    for row in group_one:
        for anchor in row.anchors:
            outcome = anchor.resolve(snapshot)
            if outcome.state not in ("resolved", "line-anchored"):
                print(f"  {row.id} ({anchor.path}): {outcome.state} {anchor.render()} {outcome.detail}")


def resolve(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """Every anchor in the map, to its place in this tree."""
    rows = read_rows(map_path, snapshot)
    states: Counter[str] = Counter()
    per_group: dict[str, Counter[str]] = defaultdict(Counter)
    unresolved: list[str] = []

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
            if outcome.state not in ("resolved", "line-anchored"):
                unresolved.append(f"{row.id} {anchor.path}::{anchor.render()} {outcome.state}")

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
    group_one_sites: Counter[str] = Counter()

    print("row\tgroup\tanchor\tstate\tat-source\tat-head")
    for row in rows:
        outcomes: list[str] = []
        for anchor in row.anchors:
            before = anchor.resolve(older)
            after = anchor.resolve(snapshot)
            state = after.state
            if state == "resolved" and before.state == "resolved":
                state = "found" if before.where == after.where else "moved"
            elif state == "resolved":
                state = "found"
            outcomes.append(state)
            states[state] += 1
            per_group[row.group][state] += 1
            if row.group == GROUP_1 and isinstance(anchor, SiteAnchor):
                group_one_sites[state] += 1
            print(f"{row.id}\t{row.group}\t{anchor.path}::{anchor.render()}\t{state}\t{before.where}\t{after.where}")
        settled = {o for o in outcomes if o != "line-anchored"}
        if not outcomes:
            verdict = "no-anchor"
        elif settled <= {"found", "moved"} and settled:
            verdict = "carries"
        elif settled & {"found", "moved"}:
            verdict = "partial"
        else:
            verdict = "lost"
        row_states[verdict] += 1
        families[_family(row.id)][verdict] += 1
        if "subtracted" in row.markers:
            families["[subtracted]"][verdict] += 1

    print(f"\n# carry {map_path} (lines read at {at}) onto {snapshot.tree}", file=sys.stderr)
    print(f"# anchors: {' '.join(f'{k}={v}' for k, v in sorted(states.items()))}", file=sys.stderr)
    print(
        f"# group-1 site anchors: {' '.join(f'{k}={v}' for k, v in sorted(group_one_sites.items()))}"
        f" (total {sum(group_one_sites.values())})",
        file=sys.stderr,
    )
    print(f"# rows: {' '.join(f'{k}={v}' for k, v in sorted(row_states.items()))}", file=sys.stderr)
    for name in sorted(families):
        spelled = " ".join(f"{k}={v}" for k, v in sorted(families[name].items()))
        print(f"#   {name}: {spelled} (rows {sum(families[name].values())})", file=sys.stderr)
    for group in sorted(per_group):
        spelled = " ".join(f"{k}={v}" for k, v in sorted(per_group[group].items()))
        print(f"#   {group}: {spelled}", file=sys.stderr)


def generate(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """The group-1 mechanical batch at this tree: the estate minus the claims.

    A claim is any group-1 row outside the mechanical batch, which is exactly
    how the 2026-08-19 batch was built by hand. The batch is one row per file
    and every row lists the sites it owns, so no range can sweep up a kept
    site.
    """
    rows = read_rows(map_path, snapshot)
    claims = [r for r in rows if r.group == GROUP_1 and r.section != MECHANICAL_BATCH]
    estate = [s for s in snapshot.sites if s.kind == "match="]
    claimed = {s for s in estate if any(r.claims(s) for r in claims)}
    remaining = [s for s in estate if s not in claimed]

    by_path: dict[str, list[Site]] = defaultdict(list)
    for site in remaining:
        by_path[site.path].append(site)

    print("| id | file and sites | shape | disposition |")
    print("| --- | --- | --- | --- |")
    for number, path in enumerate(sorted(by_path), start=1):
        sites = sorted(by_path[path], key=lambda s: (s.line, s.col))
        tails = ",".join(s.identity.tail for s in sites)
        kinds = ", ".join(sorted({s.identity.type_name for s in sites}))
        shape = f"{len(sites)} `match=` site(s) over {kinds}"
        print(f"| G1-{number:03d} | `{path}::{tails}` | {shape} | delete |")

    print(f"\n# generated {len(by_path)} rows over {len(remaining)} sites", file=sys.stderr)
    print(f"# claimed by {len(claims)} judgment and keep rows: {len(claimed)} sites", file=sys.stderr)
    print(f"# generated + claimed = {len(remaining) + len(claimed)}; estate = {len(estate)}", file=sys.stderr)
    if len(remaining) + len(claimed) != len(estate):
        raise SystemExit("the generated batch and the claims do not partition the estate")


def reanchor(map_path: str, at: str) -> None:
    """Rewrite a map's line anchors into identity anchors, in place.

    `at` is the commit the map's line numbers were measured against. Reading
    them at any other tree would name whatever function happens to sit at that
    line now, which is the drift this grammar exists to retire, so the ref is
    required rather than defaulted to HEAD.
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
        out.append("| " + " | ".join(cells) + " |")
        for anchor in row.anchors:
            kinds[type(anchor).__name__] += 1
    Path(map_path).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"# rewrote {len(rows)} rows in {map_path} against {at}", file=sys.stderr)
    for name, count in sorted(kinds.items()):
        print(f"#   {name}: {count}", file=sys.stderr)
