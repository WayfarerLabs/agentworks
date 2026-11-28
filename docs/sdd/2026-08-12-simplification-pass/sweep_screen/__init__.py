"""The sweep map's tooling: the estate, the row grammar, the screens, the reports.

This package is the implementation behind
[sweep-inventory.md](../sweep-inventory.md), and `sweep-screen.py` beside it is
the entry point. Both die with that inventory when the sweep closes; nothing in
the shipped CLI depends on either, which is why they live beside the artifact
they serve rather than under `cli/` or `scripts/`.

Read [inventory.py](inventory.py) first if the question is what a row means,
and [estate.py](estate.py) first if the question is what a site is.
"""
