#!/usr/bin/env python3
"""Build the deterministic Agentworks static website artifact."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Final

WEBSITE_DIR = Path(__file__).resolve().parent
if str(WEBSITE_DIR) not in sys.path:
    sys.path.insert(0, str(WEBSITE_DIR))

from site_asset_validation import validate_favicon_asset  # noqa: E402
from site_content import (  # noqa: E402
    CLI_SECRETS_URL,
    CONTRACTS,
    DOCUMENT_CONTRACTS,
    MANIFESTO_CONTRACT,
    PYPI_URL,
    REPORTING_URL,
    REPOSITORY_URL,
    SECURITY_CONTRACT,
    SOURCE_RELATIVE_URLS,
    ContractError,
    _read_utf8,
    _render_inline,
    extract_content,
)
from site_validation import (  # noqa: E402
    GAME_DESCRIPTIONS,
    MAIN_ATTRIBUTES,
    REQUIRED_404_REFERENCES,
    SERVICE_ICON_PATHS,
    TEMPLATE_DESTINATIONS,
    TEMPLATE_METADATA,
    TEMPLATE_TOKENS,
    TOKEN_PATTERN,
    _validate_local_references,
    _validate_runtime_asset,
    _validate_template,
    render_named_template,
    validate_site_base,
)

__all__ = (
    "CLI_SECRETS_URL",
    "CONTRACTS",
    "ContractError",
    "DOCUMENT_CONTRACTS",
    "FULL_MANIFEST",
    "GAME_DESCRIPTIONS",
    "MAIN_ATTRIBUTES",
    "MANIFESTO_CONTRACT",
    "PYPI_URL",
    "REPORTING_URL",
    "REPOSITORY_URL",
    "SECURITY_CONTRACT",
    "REQUIRED_404_REFERENCES",
    "SERVICE_ICON_PATHS",
    "SOURCE_RELATIVE_URLS",
    "TEMPLATE_METADATA",
    "TEMPLATE_TOKENS",
    "TOKEN_PATTERN",
    "_render_artifact",
    "_render_inline",
    "_validate_local_references",
    "_validate_template",
    "build_site",
    "extract_content",
    "render_named_template",
    "validate_site_base",
)

FULL_MANIFEST: Final = frozenset(
    {
        Path("404.html"),
        Path("index.html"),
        Path("manifesto/index.html"),
        Path("lander/index.html"),
        Path("assets/agw-favicon.svg"),
        Path("assets/agw-rocket.svg"),
        Path("security/index.html"),
        Path("static/lander-game.js"),
        Path("static/lander-model.js"),
        Path("static/lander.css"),
        Path("static/site.css"),
    }
)


def _render_artifact(repo_root: Path, site_base: str) -> tuple[dict[Path, bytes], frozenset[Path]]:
    website = repo_root / "website"
    manifest = FULL_MANIFEST
    substitutions = extract_content(repo_root)
    fragment_source = _read_utf8(website / "templates" / "lander-game.html")
    lander_game = render_named_template("lander-game.html", fragment_source, site_base, {})
    shell_substitutions = {**substitutions, "LANDER_GAME": lander_game}
    template_names = (
        "404.html",
        "index.html",
        "lander.html",
        "manifesto.html",
        "security.html",
    )
    rendered: dict[Path, bytes] = {}
    for name in template_names:
        template = _read_utf8(website / "templates" / name)
        destination = TEMPLATE_DESTINATIONS[name]
        rendered[destination] = render_named_template(name, template, site_base, shell_substitutions).encode()
    copies = {
        Path("assets/agw-favicon.svg"): website / "assets/agw-favicon.svg",
        Path("assets/agw-rocket.svg"): website / "assets/agw-rocket.svg",
        Path("static/lander-game.js"): website / "static/lander-game.js",
        Path("static/lander-model.js"): website / "static/lander-model.js",
        Path("static/lander.css"): website / "static/lander.css",
        Path("static/site.css"): website / "static/site.css",
    }
    copy_content = {destination: _read_utf8(source) for destination, source in copies.items()}
    validate_favicon_asset(
        copy_content[Path("assets/agw-favicon.svg")],
        copy_content[Path("assets/agw-rocket.svg")],
    )
    for destination, content in copy_content.items():
        _validate_runtime_asset(destination, content)
        rendered[destination] = content.encode()
    if set(rendered) != manifest:
        raise RuntimeError("rendering invariant failure: artifact does not match complete manifest")
    _validate_local_references(rendered, manifest, site_base)
    return rendered, manifest


def validate_output_location(repo_root: Path, output: Path) -> Path:
    """Return a safe destination without dereferencing its requested final component."""
    if ".." in output.parts or output.name in {"", ".", ".."}:
        raise ValueError("output must name a directory without dot traversal")
    destination = output.parent.resolve() / output.name
    if destination.is_relative_to(repo_root.resolve()):
        raise ValueError("output cannot be the repository or any of its descendants")
    return destination


def _manifest_directories(manifest: frozenset[Path]) -> set[Path]:
    return {parent for path in manifest for parent in path.parents if parent != Path(".")}


def _scan_tree(root: Path) -> tuple[set[Path], set[Path]]:
    files: set[Path] = set()
    directories: set[Path] = set()
    for current, names, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*names, *filenames):
            path = current_path / name
            relative = path.relative_to(root)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"output contains a symlink or special entry: {relative}")
            (directories if stat.S_ISDIR(mode) else files).add(relative)
    return files, directories


def _validate_existing_output(output: Path, manifest: frozenset[Path]) -> None:
    if not output.exists() and not output.is_symlink():
        return
    if output.is_symlink() or not output.is_dir():
        raise ValueError("existing output must be a real directory")
    files, directories = _scan_tree(output)
    if not files.issubset(manifest) or not directories.issubset(_manifest_directories(manifest)):
        raise ValueError("existing output contains entries not owned by the selected builder manifest")


def _verify_manifest(root: Path, manifest: frozenset[Path]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("manifest verification failed: output is not a real directory")
    files, directories = _scan_tree(root)
    if files != manifest or directories != _manifest_directories(manifest):
        raise RuntimeError("manifest verification failed: exact output tree differs")


def _remove_owned_tree(path: Path) -> None:
    if path.exists() and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _install_staging(staging: Path, output: Path, manifest: frozenset[Path]) -> Path | None:
    backup: Path | None = None
    had_output = output.exists()
    if had_output:
        backup = Path(tempfile.mkdtemp(prefix=f".{output.name}.backup-", dir=output.parent))
        backup.rmdir()
        output.replace(backup)
    try:
        staging.replace(output)
        _verify_manifest(output, manifest)
    except BaseException:
        if output.exists() or output.is_symlink():
            _remove_owned_tree(output)
        if backup is not None and backup.exists():
            backup.replace(output)
        raise
    return backup


def build_site(repo_root: Path, output: Path, site_base: str) -> None:
    """Build and atomically install the complete linked site."""
    root = repo_root.resolve()
    base = validate_site_base(site_base)
    destination = validate_output_location(root, output)
    rendered, manifest = _render_artifact(root, base)
    _validate_existing_output(destination, manifest)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
    backup: Path | None = None
    try:
        for relative in sorted(rendered, key=lambda path: path.as_posix()):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(rendered[relative])
        _verify_manifest(staging, manifest)
        backup = _install_staging(staging, destination, manifest)
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except OSError:
                print(
                    f"warning: installed output is valid; retained backup at {backup}",
                    file=sys.stderr,
                )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site-base", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        build_site(args.repo_root, args.output, args.site_base)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
