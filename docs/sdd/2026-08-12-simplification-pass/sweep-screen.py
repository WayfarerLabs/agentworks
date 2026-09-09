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
    restamp    every span anchor's assertion digest, brought up to date IN PLACE
    totals     the row markup, counted, which the Totals section reports

Run from the repository root with Python 3.12, which this enforces rather than
documents. The floor is PEP 695: `type` statements and the new generic
parameter syntax, which 3.11 cannot parse at all and which 30 of the 32 files
3.11 chokes on use. A file that fails to parse contributes no sites, so it can
never be reported unowned and the one guarantee here degrades into a smaller
estate that still looks complete. Any unparsed file is fatal for the same
reason, as is an assertion whose arguments are splatted. The ceiling is
separate and belongs to the digests: `resolve` and `restamp` refuse a newer
interpreter, because `ast.unparse` is free to spell the same tree differently
there and every span anchor would report `changed` at once.

    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py estate
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py generate

Every command reads the working tree, and every one writes its table to stdout
and its totals to stderr, so a run can be piped without losing the count.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from sweep_screen import reports
from sweep_screen.estate import DIGEST_LENGTH, Snapshot, full_digest
from sweep_screen.inventory import INVENTORY
from sweep_screen.screens import injected, screen
from sweep_screen.tree import Tree

#: The interpreter minor this map's span-anchor digests were stamped with.
#: Below it nothing can read the estate at all, which is why this file refuses
#: outright; above it the estate reads fine and only the digests are suspect, so
#: `resolve` and `restamp` are what refuse there. Both halves are the same pin
#: because the digests were stamped on the floor.
STAMPED_PYTHON = (3, 12)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("estate", "attribute", "injected", "screen", "resolve", "generate", "restamp", "totals"):
        sub.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> None:
    if sys.version_info[:2] < STAMPED_PYTHON:
        running = ".".join(str(n) for n in sys.version_info[:3])
        want = ".".join(str(n) for n in STAMPED_PYTHON)
        raise SystemExit(f"needs Python {want} to parse the whole estate; this is {running}")
    if not Path(INVENTORY).exists():
        raise SystemExit("run this from the repository root")
    args = build_parser().parse_args(argv)
    if args.command == "totals":
        # `totals` is the one command that reads the COMMIT GRAPH: every commit
        # the map cites has to be an ancestor of HEAD, which a rebase silently
        # breaks. Without git it cannot ask, and in a shallow clone the answer is
        # "no" for every commit older than the fetch depth, which would report
        # 177 faults that are all the clone's. Both refuse rather than guess.
        if shutil.which("git") is None:
            raise SystemExit("`totals` checks cited commits against the commit graph and git is not on PATH")
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
            check=False,
        )
        if shallow.returncode != 0:
            raise SystemExit("`totals` checks cited commits against the commit graph and this is not a git tree")
        if shallow.stdout.strip() == "true":
            raise SystemExit(
                "this is a shallow clone, where a commit older than the fetch depth is unreachable"
                " whether or not the map is wrong, so every cited commit would report as an orphan."
                " Run `git fetch --unshallow` first."
            )
    if sys.version_info[:2] > STAMPED_PYTHON and args.command in ("resolve", "restamp", "totals"):
        running = ".".join(str(n) for n in sys.version_info[:3])
        want = ".".join(str(n) for n in STAMPED_PYTHON)
        raise SystemExit(
            f"this map's span-anchor digests were stamped with Python {want} and this is {running}."
            " `ast.unparse` may spell the same test differently here, which would report every span"
            f" anchor as `changed` and tell you nothing. Run it on {want}, or re-stamp on {running} and"
            " move STAMPED_PYTHON in the same commit, reviewing the diff through restamp's own"
            " self-check plus a spot read: a digest that moves because the assertions moved is a"
            " finding, and one that moves because the interpreter did is not."
        )
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
        needles = {s.needle for s in here.sites}
        shortest = next(
            (n for n in range(1, 65) if len({full_digest(x)[:n] for x in needles}) == len(needles)),
            64,
        )
        print(
            f"# {len(needles)} distinct needles; shortest separating prefix {shortest} (this map uses {DIGEST_LENGTH})",
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
