#!/usr/bin/env python3
"""Build the local Agentworks static 404 artifact."""

from __future__ import annotations

import argparse
import re
import shutil
import tempfile
from pathlib import Path

SITE_BASE_TOKEN = "{{SITE_BASE}}"
EXPECTED_FILES = {
    Path("404.html"),
    Path("assets/agw-rocket.svg"),
    Path("static/lander.css"),
    Path("static/lander-model.js"),
    Path("static/lander-game.js"),
}
REQUIRED_TEMPLATE_REFERENCES = {
    f'href="{SITE_BASE_TOKEN}"',
    f'href="{SITE_BASE_TOKEN}static/lander.css"',
    f'src="{SITE_BASE_TOKEN}static/lander-game.js"',
    f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-mark"',
    f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-engine-left"',
    f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-engine-right"',
}
SITE_BASE_PATTERN = re.compile(r"/(?:[A-Za-z0-9][A-Za-z0-9._~-]*/)*\Z", re.ASCII)


def validate_site_base(value: str) -> str:
    """Validate an ASCII same-origin path made from safe URL segment characters."""
    if SITE_BASE_PATTERN.fullmatch(value) is None:
        raise ValueError("site base must be an ASCII URL path with safe slash-bounded segments")
    return value


def render_template(template: str, site_base: str) -> str:
    """Render the closed 404 template vocabulary."""
    tokens = set(re.findall(r"{{[^{}]+}}", template))
    if tokens != {SITE_BASE_TOKEN}:
        raise ValueError(f"template token vocabulary must be exactly {SITE_BASE_TOKEN}")
    missing = sorted(reference for reference in REQUIRED_TEMPLATE_REFERENCES if reference not in template)
    if missing:
        raise ValueError(f"template is missing required site-base references: {missing}")
    rendered = template.replace(SITE_BASE_TOKEN, site_base)
    if re.search(r"{{[^{}]+}}", rendered):
        raise ValueError("rendered template contains an unexpanded token")
    return rendered


def validate_output_location(repo_root: Path, output: Path) -> None:
    """Reject build output within the source repository before any write occurs."""
    if output.resolve().is_relative_to(repo_root.resolve()):
        raise ValueError("output cannot be the repository or any of its descendants")


def _replace_output(staging: Path, output: Path) -> None:
    if output.exists() or output.is_symlink():
        if output.is_symlink() or not output.is_dir():
            raise ValueError("existing output must be a real directory")
        existing = {
            path.relative_to(output)
            for path in output.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        directories = {path.relative_to(output) for path in output.rglob("*") if path.is_dir()}
        if (
            any(path.is_symlink() for path in output.rglob("*"))
            or not existing.issubset(EXPECTED_FILES)
            or not directories.issubset({Path("assets"), Path("static")})
        ):
            raise ValueError("existing output contains files not owned by the 404 builder")
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(output)


def build_404(repo_root: Path, output: Path, site_base: str) -> None:
    """Render 404.html and copy its complete local dependency set."""
    base = validate_site_base(site_base)
    source = repo_root.resolve() / "website"
    validate_output_location(repo_root, output)
    template_path = source / "templates" / "404.html"
    rendered = render_template(template_path.read_text(encoding="utf-8"), base)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="agentworks-404-", dir=output.parent.resolve()))
    try:
        (staging / "assets").mkdir()
        (staging / "static").mkdir()
        (staging / "404.html").write_text(rendered, encoding="utf-8")
        shutil.copy2(source / "assets" / "agw-rocket.svg", staging / "assets" / "agw-rocket.svg")
        for name in ("lander.css", "lander-model.js", "lander-game.js"):
            shutil.copy2(source / "static" / name, staging / "static" / name)
        produced = {path.relative_to(staging) for path in staging.rglob("*") if path.is_file()}
        if produced != EXPECTED_FILES:
            raise RuntimeError(f"unexpected 404 output set: {sorted(map(str, produced))}")
        _replace_output(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("404",), required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site-base", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        build_404(args.repo_root, args.output, args.site_base)
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(f"error: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
