#!/usr/bin/env python3
"""Derive the sweep's `match=` estate, screen it, and keep its map anchored.

This is the tooling behind [sweep-inventory.md](sweep-inventory.md). It dies
with that inventory when the sweep closes; nothing in the shipped CLI depends
on it, which is why it lives beside the artifact it serves rather than under
`cli/` or `scripts/`. The implementation is the `sweep_screen` package next to
this file; this file is the entry point.

Six questions an executor needs and a reader should not have to take on trust:

* **Estate.** Every `pytest.raises(..., match=)` site under `cli/tests` and
  every `assertRaisesRegex`-family site under `website/tests`, with the
  identity each is keyed by.
* **Attribution.** Which inventory row claims each site, and which sites or row
  anchors do not resolve.
* **Screen.** For each site, whether the operation under test can raise the
  asserted type from more than one path, and whether a structural handle tells
  the targeted raise apart. `hla.md`'s case 1 holds only where it cannot.
* **Injected markers.** Which sites match a string their own test wrote, where
  the assertion pins nothing this repository authors.
* **Resolution and carry.** Where every anchor in the map sits now, and which
  rows of an older map survive at HEAD by identity rather than by line.
* **Generation.** The group-1 mechanical batch as the estate minus the map's
  judgment claims, in the row grammar, ready to paste.

Run from the repository root with Python 3.12 or newer, which this enforces
rather than documents: 3.11 cannot parse the PEP 701 f-strings some estate
files use, and a file that fails to parse contributes no sites, so it can never
be reported unowned and the one guarantee here degrades into a smaller estate
that still looks complete. Any unparsed file is fatal for the same reason.

    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py estate
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py attribute
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py injected
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py screen
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py resolve
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py carry OLD.md --at REF
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py generate
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py reanchor MAP.md --at REF

Every command reads HEAD's working tree unless `--at` names a commit, and every
one writes its table to stdout and its totals to stderr, so a run can be piped
without losing the count.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sweep_screen import reports  # noqa: E402
from sweep_screen.estate import Snapshot  # noqa: E402
from sweep_screen.inventory import INVENTORY  # noqa: E402
from sweep_screen.screens import injected, screen  # noqa: E402
from sweep_screen.tree import Tree  # noqa: E402

#: Below this, `ast.parse` rejects the PEP 701 f-strings some estate files use.
#: Those files would then be skipped rather than counted, and a short estate
#: reports the same "every site is claimed" as a complete one, so this is a
#: refusal rather than a warning.
MIN_PYTHON = (3, 12)

#: The commit the ported 2026-08-19 map's line numbers were measured against.
#: A line number means nothing without the tree it was read from, so `carry`
#: and `reanchor` need one; this is the only map either has been pointed at so
#: far, so it is the default rather than a required argument.
CARRY_BASIS = "426cccae"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("estate", "attribute", "injected", "screen", "resolve", "generate"):
        sub.add_parser(name)
    for name in ("carry", "reanchor"):
        one = sub.add_parser(name)
        one.add_argument("map", nargs="?", default=INVENTORY)
        one.add_argument("--at", default=CARRY_BASIS, help="the commit that map's line numbers were read at")
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


if __name__ == "__main__":
    main()
