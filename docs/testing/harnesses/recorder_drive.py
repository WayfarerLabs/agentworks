"""Maintained harness: REAL-CODE driver.

Pattern: import shipped code directly (skip the CLI and the filesystem) and
drive it against a battery of representative payloads, recording what it
actually returns or raises. This is the right tool when a live-test charter
wants to check a CODE-level contract (e.g. "this walker never raises, for
any input") rather than an end-to-end CLI flow: it is faster than a CLI
drive and it observes the real shipped function, not a re-implementation or
a mock of it.

This harness drives `agentworks.schema.extract_references`, which the
module docstring documents as total: it must never raise regardless of
input shape. The corpus below is representative (a handful of "nice" and
"adversarial" blobs), not exhaustive; the project's own property test suite
(tests/schema/test_extract_totality.py) is the maintained, exhaustive
version of this idea. Use this pattern when you want a quick, readable,
one-off check of a similar contract elsewhere in the codebase.

See docs/testing/harnesses/README.md for the maintenance contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from agentworks.schema import extract_references


class _LeafModel(BaseModel):
    """A minimal model with no reference-marked fields, used as the
    "nothing to extract" baseline."""

    name: str = ""


@dataclass(frozen=True)
class Case:
    """One payload to drive through `extract_references`, and what we
    expect to observe (never an exception, by contract)."""

    label: str
    model: type[BaseModel]
    blob: object


# A small, deliberately varied corpus: well-formed, empty, and
# adversarially-shaped blobs against the same model. A real drive for a
# different function would swap in payloads representative of what THAT
# function's callers actually hand it.
CASES: list[Case] = [
    Case("well-formed", _LeafModel, {"name": "example"}),
    Case("empty-mapping", _LeafModel, {}),
    Case("not-a-mapping", _LeafModel, "not-a-dict"),
    Case("none", _LeafModel, None),
    Case("wrong-type-value", _LeafModel, {"name": 12345}),
    Case("nested-junk", _LeafModel, {"name": {"nested": ["junk"]}}),
]


def run(cases: list[Case]) -> int:
    """Drive every case through the real function and record the observed
    outcome. Returns the number of cases that violated the contract
    (raised instead of returning), so callers can treat a nonzero return
    as a failure without needing pytest."""

    violations = 0
    for case in cases:
        try:
            refs = extract_references(case.model, case.blob)
        except Exception as exc:  # noqa: BLE001 - recording, not handling
            violations += 1
            print(f"[VIOLATION] {case.label}: raised {type(exc).__name__}: {exc}")
            continue
        print(f"[ok] {case.label}: {len(refs)} reference(s) extracted")
    return violations


def main() -> None:
    violations = run(CASES)
    total = len(CASES)
    print(f"\n{total - violations}/{total} cases upheld the never-raises contract.")
    if violations:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
