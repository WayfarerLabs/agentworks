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

import sys
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from .inventory import (
    CITED_ID,
    GROUP_1,
    INVENTORY,
    MECHANICAL_BATCH,
    LineAnchor,
    Row,
    SiteAnchor,
    SpanAnchor,
    read_rows,
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


def check_map(rows: list[Row]) -> list[str]:
    """Structural faults in the map itself, as a list of complaints.

    These are the properties the map's prose used to promise a reader and
    nothing enforced, which is how it came to cite two dozen ids that were not
    rows: part-local ids from files that no longer exist, and rows the ledger
    dropped. A citation that resolves to nothing sends a reader looking for
    evidence that is not there.
    """
    faults: list[str] = []
    ids = [r.id for r in rows]
    known = set(ids)
    for row_id in sorted({i for i in ids if ids.count(i) > 1}):
        faults.append(f"duplicate row id {row_id}")
    for row in rows:
        cited = {c for cell in (row.shape, row.disposition, row.justification) for c in CITED_ID.findall(cell)}
        for name in sorted(cited - known - {row.id}):
            faults.append(f"{row.id} cites {name}, which is not a row in this map")
    return faults


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
    rows = read_rows(map_path, snapshot)
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
    for number, path in enumerate(sorted(by_path), start=1):
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
        row = Row(f"G1-{number:03d}", GROUP_1, MECHANICAL_BATCH, list(anchors), shape, "delete", "", 0)
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


def totals(snapshot: Snapshot, map_path: str = INVENTORY) -> None:
    """The row markup, counted, which is what the Totals section reports.

    Counted here rather than by hand because the map has twice carried a total
    forward that no longer matched its rows.
    """
    rows = read_rows(map_path, snapshot)
    groups = list(dict.fromkeys(r.group for r in rows))
    print("| Group | Live | delete | convert | keep | Deferred | Ledger |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
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
        print(
            f"| {GROUP_TITLES.get(group, group)} | {len(live)} | {counts['delete']} | {counts['convert']} "
            f"| {counts['keep']} | {deferred} | {len(here)} |"
        )
    print(
        f"| **All** | {ledger['live']} | {ledger['delete']} | {ledger['convert']} | {ledger['keep']} "
        f"| {ledger['deferred']} | {ledger['ledger']} |"
    )
    faults = check_map(rows)
    print(f"\n# structural faults: {len(faults)}", file=sys.stderr)
    for fault in faults:
        print(f"#   {fault}", file=sys.stderr)
    print(f"# executable set: {ledger['live']} rows; ledger: {ledger['ledger']} rows", file=sys.stderr)
    if faults:
        raise SystemExit(f"the map has {len(faults)} structural fault(s)")
