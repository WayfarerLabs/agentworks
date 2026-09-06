#!/usr/bin/env python3
"""Derive the sweep's `match=` estate, screen it, and keep its map anchored.

This is the tooling behind [sweep-inventory.md](sweep-inventory.md), whose
"Reading this file mechanically" section owns the row grammar these commands
read and write. It dies with that inventory when the sweep closes; nothing in
the shipped CLI depends on it, which is why it lives beside the artifact it
serves rather than under `cli/` or `scripts/`. The implementation is the
`sweep_screen` package next to this file; this file is the entry point.

    estate     every site at HEAD, with the identity it is keyed by
    attribute  which row claims each site; non-zero unless each is claimed once
    injected   sites whose needle is a marker their own test wrote
    screen     whether the asserted type discriminates, callee side
    resolve    every anchor in the map, at its current line
    carry      an older map's rows onto the current estate, by identity
    generate   the group-1 mechanical batch as the estate minus the claims
    totals     the row markup, counted, which the Totals section reports
    bases      which rows two candidate bases disagree about
    reanchor   rewrite a map's line anchors into identities, in place

Run from the repository root with Python 3.12 or newer, which this enforces
rather than documents: 3.11 cannot parse the PEP 701 f-strings some estate
files use, and a file that fails to parse contributes no sites, so it can never
be reported unowned and the one guarantee here degrades into a smaller estate
that still looks complete. Any unparsed file is fatal for the same reason, as
is an assertion whose arguments are splatted.

    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py estate
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py generate
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py carry OLD.md --at REF
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py reanchor MAP.md --at REF

Every command reads the working tree unless it names a commit, and every one
writes its table to stdout and its totals to stderr, so a run can be piped
without losing the count.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sweep_screen import reports
from sweep_screen.estate import Snapshot
from sweep_screen.inventory import INVENTORY
from sweep_screen.screens import injected, screen
from sweep_screen.tree import Tree

#: Below this, `ast.parse` rejects the PEP 701 f-strings some estate files use.
#: Those files would then be skipped rather than counted, and a short estate
#: reports the same "every site is claimed" as a complete one, so this is a
#: refusal rather than a warning.
MIN_PYTHON = (3, 12)

#: The commit the ported 2026-08-19 map's line numbers were measured against.
#: `carry` defaults to it because that map is the only one it has been pointed
#: at; `reanchor` requires it, because a rewrite against the wrong tree writes
#: wrong names into every row.
CARRY_BASIS = "426cccae"

#: The map's other candidate basis. Rows the re-baseline did not re-derive were
#: read here, which is what `bases` compares.
EARLIER_BASIS = "c686cd6d"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("estate", "attribute", "injected", "screen", "resolve", "generate", "totals"):
        sub.add_parser(name)
    one = sub.add_parser("carry")
    one.add_argument("map", nargs="?", default=INVENTORY)
    one.add_argument("--at", default=CARRY_BASIS, help="the commit that map's line numbers were read at")
    two = sub.add_parser("reanchor")
    two.add_argument("map", help="the map to rewrite in place")
    two.add_argument("--at", required=True, help="the commit that map's line numbers were read at")
    three = sub.add_parser("bases")
    three.add_argument("map", nargs="?", default=INVENTORY)
    three.add_argument("--first", default=EARLIER_BASIS)
    three.add_argument("--second", default=CARRY_BASIS)
    return parser


def main(argv: list[str] | None = None) -> None:
    if sys.version_info < MIN_PYTHON:
        running = ".".join(str(n) for n in sys.version_info[:3])
        want = ".".join(str(n) for n in MIN_PYTHON)
        raise SystemExit(f"needs Python {want} or newer to parse the whole estate; this is {running}")
    if not Path(INVENTORY).exists():
        raise SystemExit("run this from the repository root")
    args = build_parser().parse_args(argv)

    if args.command == "reanchor":
        reports.reanchor(args.map, args.at)
        return
    if args.command == "bases":
        reports.bases(args.map, args.first, args.second)
        return

    here = Snapshot(Tree())
    if args.command == "estate":
        for site in here.sites:
            print(f"{site.identity}\t{site.where}\t{site.kind}\t{site.needle}")
        print(f"\n# {len(here.sites)} sites, each with a distinct identity", file=sys.stderr)
    elif args.command == "attribute":
        reports.attribute(here)
    elif args.command == "injected":
        injected(here.tree)
    elif args.command == "screen":
        screen(here.tree)
    elif args.command == "resolve":
        reports.resolve(here)
    elif args.command == "carry":
        reports.carry(here, args.map, args.at)
    elif args.command == "generate":
        reports.generate(here)
    elif args.command == "totals":
        reports.totals(here)


if __name__ == "__main__":
    main()
