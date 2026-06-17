"""Tests for ``cli/agentworks/sample-config.toml``.

The sample config is the operator's discovery surface for what's
configurable. Two contracts:

1. It parses as TOML as-shipped (active lines + bare-`#` paragraph breaks).
2. The commented-out examples use a `#<toml>` (no space) convention so they
   can be uncommented in-place into valid TOML. Prose comments use `# <text>`
   (with space) or bare `#` so they can be distinguished from examples.

Together those let an operator strip the `#` prefix from any example line
they want to enable, without re-deriving the right indentation or comment
shape.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

SAMPLE_PATH = (
    Path(__file__).resolve().parent.parent / "agentworks" / "sample-config.toml"
)


def _uncomment_examples(src: str) -> str:
    """Strip a single leading `#` from `#<toml>` lines; leave `# <prose>` and
    bare `#` lines as-is."""
    out: list[str] = []
    for line in src.splitlines():
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("# ") or stripped == "#":
            # Prose comment or paragraph break: keep as-is.
            out.append(line)
        elif stripped.startswith("#"):
            # Commented-out example: strip one `#`.
            out.append(indent + stripped[1:])
        else:
            out.append(line)
    return "\n".join(out)


def test_sample_config_parses_as_shipped() -> None:
    """The file as shipped (active sections + commented examples) is valid TOML."""
    src = SAMPLE_PATH.read_text()
    tomllib.loads(src)  # raises on any parse error


def test_sample_config_examples_uncomment_cleanly() -> None:
    """Stripping a single `#` from every `#<toml>` line produces valid TOML.

    Pins the `#<toml>` convention for commented-out examples. A contributor
    who writes `# key = value` (with the extra space) breaks the
    uncomment-in-place ergonomic and trips this test.
    """
    src = SAMPLE_PATH.read_text()
    candidate = _uncomment_examples(src)
    try:
        parsed = tomllib.loads(candidate)
    except tomllib.TOMLDecodeError as e:
        # Surface the offending line in the error for fast diagnosis.
        lines = candidate.splitlines()
        n = getattr(e, "lineno", None)
        ctx = ""
        if n and 1 <= n <= len(lines):
            ctx = f"\n  line {n}: {lines[n - 1]!r}"
        raise AssertionError(
            "uncommented sample-config does not parse. A `# key = value` "
            f"line (extra space) is the usual culprit.\n  {e}{ctx}"
        ) from e

    # Spot-check the major sections all exist after uncommenting.
    expected_top = {
        "operator", "paths", "defaults", "named_console",
        "git_credentials", "secrets", "secret_backends", "secret_config",
        "azure", "proxmox",
        "vm_templates", "admin", "agent_templates",
        "workspace_templates", "session_templates", "session",
    }
    missing = expected_top - set(parsed.keys())
    assert not missing, f"missing top-level sections after uncomment: {missing}"


def test_sample_config_env_tables_live_with_their_resources() -> None:
    """Pin the layout choice: each ``[<resource>.env]`` example is colocated
    with its parent resource (rather than gathered into a standalone env
    section). The uncommented form has each env subtable as a child of
    its resource definition."""
    src = SAMPLE_PATH.read_text()
    parsed = tomllib.loads(_uncomment_examples(src))

    assert "env" in parsed["vm_templates"]["default"]
    assert "env" in parsed["admin"]
    assert "env" in parsed["agent_templates"]["default"]
    assert "env" in parsed["workspace_templates"]["default"]
    assert "env" in parsed["session_templates"]["default"]
