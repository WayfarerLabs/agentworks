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

import pytest

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "agentworks" / "sample-config.toml"


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


def _install_sample_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA...")
    (ssh_dir / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")


def test_sample_config_parses_and_loads_as_shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The active sample is valid, settings-only input to the production loader."""
    from agentworks.config import EXPECTED_TOP_LEVEL_KEYS, load_config

    src = SAMPLE_PATH.read_text()
    parsed = tomllib.loads(src)
    assert set(parsed) <= EXPECTED_TOP_LEVEL_KEYS

    _install_sample_keys(tmp_path, monkeypatch)
    config = load_config(SAMPLE_PATH, warn_issues=False, warn_deprecations=False, raise_errors=True)
    assert config.source_path == SAMPLE_PATH


def test_sample_config_examples_uncomment_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    from agentworks.config import EXPECTED_TOP_LEVEL_KEYS, load_config

    # Fully uncommented, the sample contains exactly the live settings roots:
    # no retired resource root can sneak in and no settings section can drift
    # out of the discovery surface.
    assert set(parsed) == EXPECTED_TOP_LEVEL_KEYS

    _install_sample_keys(tmp_path, monkeypatch)
    candidate_path = tmp_path / "config.toml"
    candidate_path.write_text(candidate)
    config = load_config(candidate_path, warn_issues=False, warn_deprecations=False, raise_errors=True)
    assert config.source_path == candidate_path
