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

import re
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

#: `carry`'s states for an anchor that still reaches what it was cut against.
INTACT = frozenset({"found", "moved"})

#: How the map titles its groups, so a `totals` run pastes into it unedited.
GROUP_TITLES = {
    "Group 1": "1. Mechanical `match=` narrowing",
    "Group 2": "2. Guide and migration topics",
    "Group 3": "3. Report lines and hints (four sub-batches)",
    "Group 4": "4. Schema, manifests, capabilities and platforms",
    "Group 5": "5. Authored-artifact form policing",
    "Group 6": "6. Source guards",
    "Deferred": "Deferred (held for R4)",
}
#: States that mean it reaches something, but not the same number of sites.
SHIFTED = frozenset({"grown", "shrunk"})


def claim_rows(rows: list[Row]) -> list[Row]:
    """Group 1's judgment and keep rows: everything it holds outside the batch.

    A `[dead]` row is not a claim. Its file is gone, so it can neither own a
    site nor lose one, and gating on it would refuse forever over a row the map
    already records as dead.
    """
    return [r for r in rows if r.group == GROUP_1 and r.section != MECHANICAL_BATCH and "dead" not in r.markers]


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


def _family(row_id: str) -> str:
    for prefix in ("G1-C", "G1-M", "G1-I", "G1-K", "RB-"):
        if row_id.startswith(prefix):
            return prefix
    return row_id.split("-")[0]


def _verdict(outcomes: list[str]) -> str:
    """One row's carry verdict from its anchors' states.

    `carries` means every anchor that can settle reaches what it was cut
    against. `partial` means some do and some do not, which includes a group
    that grew or shrank, since the row's evidence was written against a
    different number of assertions. `lost` means none.

    `unchecked` is the honest answer where nothing could settle: a row with no
    anchor at all, and a row whose only anchors are literal lines in a file that
    still exists, which no tree can confirm or deny. Calling those `lost` said
    the evidence had gone when all that had happened was that nobody looked.
    """
    settled = {o for o in outcomes if o != "line-anchored"}
    if not outcomes or not settled:
        return "unchecked"
    if settled <= INTACT:
        return "carries"
    if settled & (INTACT | SHIFTED):
        return "partial"
    return "lost"


def carry(snapshot: Snapshot, map_path: str, at: str) -> None:
    """An older map's rows onto the current estate, by identity.

    `at` is the commit that map's line numbers were measured against, which is
    what turns them into identities in the first place. A row already written
    in the identity grammar needs it only for the before-state that tells
    `found` from `moved`.
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
                if before.state != "resolved":
                    # A map lifted at `at` resolves at `at` by construction, so
                    # this says the ref is not the tree those lines were read
                    # from, and every "moved" in the run would be a guess.
                    raise SystemExit(
                        f"{row.id}: {anchor.path}::{anchor.render()} does not resolve at {at}"
                        f" but does at {snapshot.tree}; {at} is not the tree this map's lines were read from"
                    )
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
      is the check the count comparison was standing in for. A span claim that
      resolves and covers no site is reported beside it and does not refuse; the
      comment on `barren` says why.

    `claim_rows` says which rows are claims and why. `[subtracted]` and
    `[deferred]` rows are among them, because their sites are real and must not
    fall into the batch.
    """
    rows = read_rows(map_path, snapshot)
    claims = [r for r in rows if r.group == GROUP_1 and r.section != MECHANICAL_BATCH and "dead" not in r.markers]
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
    stale = unresolved_claims(claims, snapshot)
    # A group-1 span claim that resolves and covers no site shields nothing from
    # the batch. It is reported and does not refuse, because one tree cannot
    # tell a test that LOST its sites from a test that never had any, and
    # `_lift` gives a group-1 row a span only where the function it anchored
    # held no cited site, so every instance here is the second kind. The
    # comparison is against every site rather than this batch's `match=` estate,
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
    print(f"# group-1 claim anchors that do not resolve here: {len(stale)}", file=sys.stderr)
    for row_id, rendered, state in stale:
        print(f"#   {row_id} {state} {rendered}", file=sys.stderr)
    print(f"# group-1 span claims covering no site (reported, not refused): {len(barren)}", file=sys.stderr)
    for row_id, rendered in barren:
        print(f"#   {row_id} {rendered}", file=sys.stderr)
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
    print("| Group | Live | delete | convert | keep | Dead | Subtracted | Deferred | Ledger |")
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
            f"| {GROUP_TITLES.get(group, group)} | {len(live)} | {counts['delete']} | {counts['convert']} "
            f"| {counts['keep']} | {marks['dead']} | {marks['subtracted']} | {marks['deferred']} | {len(here)} |"
        )
    print(
        f"| **All** | {ledger['live']} | {ledger['delete']} | {ledger['convert']} | {ledger['keep']} "
        f"| {ledger['dead']} | {ledger['subtracted']} | {ledger['deferred']} | {ledger['ledger']} |"
    )
    print(f"\n# executable set: {ledger['live']} rows; ledger: {ledger['ledger']} rows", file=sys.stderr)


def _with_cause(row: Row, shape: str, causes: Counter[str]) -> str:
    """The shape cell, carrying `[line-anchored: <cause>]` when the row keeps
    literal lines and carrying no such marker when it does not.

    The cause comes off the anchor either way, since both grammars fill it from
    the tree, so this rewrites rather than preserves and no marker in the file
    is ever the authority for what it says.
    """
    stripped = re.sub(r"\*\*\[line-anchored:[^\]]*\]\*\*\s*", "", shape).strip()
    lined = [a for a in row.anchors if isinstance(a, LineAnchor)]
    if not lined:
        return stripped
    cause = ", ".join(sorted({a.cause for a in lined if a.cause}))
    causes[cause] += 1
    return f"**[line-anchored: {cause}]** {stripped}".strip()


def reanchor(map_path: str, at: str) -> None:
    """Rewrite a map's line anchors into identity anchors, in place.

    One job: it reproduces the map's anchor cells from the legacy line-anchored
    map, so every rewrite of them is reproducible rather than taken on trust,
    and it retires with the fresh cut, when no line anchors remain to lift. `at` is the commit the map's line
    numbers were measured against and is required, because reading them at any
    other tree would name whatever function happens to sit at that line now,
    which is the drift this grammar exists to retire.
    """
    older = Snapshot(Tree(at))
    rows = {r.source_line: r for r in read_rows(map_path, older)}
    kinds: Counter[str] = Counter()
    causes: Counter[str] = Counter()
    out: list[str] = []
    for number, line in enumerate(Path(map_path).read_text(encoding="utf-8").splitlines(), start=1):
        row = rows.get(number)
        if row is None:
            out.append(line)
            continue
        cells = split_cells(line)
        cells[1] = row.render_cell()
        cells[2] = _with_cause(row, cells[2], causes)
        out.append(join_cells(cells))
        for anchor in row.anchors:
            kinds[type(anchor).__name__] += 1
    Path(map_path).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"# rewrote {len(rows)} rows in {map_path} against {at}", file=sys.stderr)
    for name, count in sorted(kinds.items()):
        print(f"#   {name}: {count}", file=sys.stderr)
    print(f"# line-anchored rows by cause: {sum(causes.values())}", file=sys.stderr)
    for name in sorted(causes):
        print(f"#   {name}: {causes[name]}", file=sys.stderr)
