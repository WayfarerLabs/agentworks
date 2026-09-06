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
    generate   the group-1 mechanical batch as the estate minus the claims
    totals     the row markup, counted, which the Totals section reports

Run from the repository root with Python 3.12 or newer, which this enforces
rather than documents: 3.11 cannot parse the PEP 701 f-strings some estate
files use, and a file that fails to parse contributes no sites, so it can never
be reported unowned and the one guarantee here degrades into a smaller estate
that still looks complete. Any unparsed file is fatal for the same reason, as
is an assertion whose arguments are splatted.

    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py estate
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py generate

Every command reads the working tree, and every one writes its table to stdout
and its totals to stderr, so a run can be piped without losing the count.
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

#: The interpreter minor this map's span-anchor digests were stamped with, and
#: the only one that can read them. Exact, not a floor, and for two reasons.
#: Below it, `ast.parse` rejects the PEP 701 f-strings some estate files use, so
#: those files would be skipped rather than counted and a short estate reports
#: the same "every site is claimed" as a complete one. Above it, `ast.unparse`
#: is free to spell the same tree differently, and every span anchor in the map
#: would then report `changed` at once: 1,516 spurious verdicts saying nothing
#: about the tests, which is the silence this digest exists to end, wearing a
#: different costume. An upgrade is a re-stamp, and a re-stamp is a reviewed
#: change to the map, so it fails loudly here rather than in a reader's head.
STAMPED_PYTHON = (3, 12)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("estate", "attribute", "injected", "screen", "resolve", "generate", "restamp", "totals"):
        sub.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> None:
    if sys.version_info[:2] != STAMPED_PYTHON:
        running = ".".join(str(n) for n in sys.version_info[:3])
        want = ".".join(str(n) for n in STAMPED_PYTHON)
        if sys.version_info[:2] < STAMPED_PYTHON:
            raise SystemExit(f"needs Python {want} to parse the whole estate; this is {running}")
        raise SystemExit(
            f"this map's span-anchor digests were stamped with Python {want} and this is {running}."
            " `ast.unparse` may spell the same test differently here, which would report every span"
            " anchor as `changed` and tell you nothing. Run it on"
            f" {want}, or re-stamp the digests on {running} and move STAMPED_PYTHON in the same commit,"
            " reviewing the diff: a digest that moves because the assertions moved is a finding, and one"
            " that moves because the interpreter did is not."
        )
    if not Path(INVENTORY).exists():
        raise SystemExit("run this from the repository root")
    args = build_parser().parse_args(argv)
    here = Snapshot(Tree())
    if args.command == "estate":
        for site in here.sites:
            print(f"{site.identity}\t{site.where}\t{site.kind}\t{site.needle}")
        ties = here.ties
        print(
            f"\n# {len(here.sites)} sites over {len(here.by_identity)} identities;"
            f" {len(ties)} identities name more than one site,"
            f" covering {sum(g.multiplicity for g in ties)}",
            file=sys.stderr,
        )
        for group in ties:
            print(f"#   x{group.multiplicity} {group.identity} at {group.where}", file=sys.stderr)
        excluded = here.excluded
        print(f"# sites the per-root kind filter excludes: {sum(excluded.values())}", file=sys.stderr)
        for name in sorted(excluded):
            print(f"#   {name}: {excluded[name]}", file=sys.stderr)
    elif args.command == "attribute":
        reports.attribute(here)
    elif args.command == "injected":
        injected(here.tree)
    elif args.command == "screen":
        screen(here.tree)
    elif args.command == "resolve":
        reports.resolve(here)
    elif args.command == "generate":
        reports.generate(here)
    elif args.command == "restamp":
        reports.restamp(here)
    elif args.command == "totals":
        reports.totals(here)


if __name__ == "__main__":
    main()
