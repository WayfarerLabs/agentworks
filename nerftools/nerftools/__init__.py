"""nerftools -- build and manage nerf tools."""

from pathlib import Path

BUILTIN_MANIFESTS_DIR = Path(__file__).parent.parent / "manifests"

_NERFCTL_DIR = Path(__file__).parent / "nerfctl"

NERFCTL_FRAMEWORKS: dict[str, dict[str, Path]] = {
    "claude": {
        "grant": _NERFCTL_DIR / "claude" / "grant.sh",
        "deny": _NERFCTL_DIR / "claude" / "deny.sh",
        "reset": _NERFCTL_DIR / "claude" / "reset.sh",
        "list": _NERFCTL_DIR / "claude" / "list.sh",
    },
}


def install_nerfctl(framework: str, output: Path) -> list[Path]:
    """Copy nerfctl scripts for *framework* into *output*. Returns paths written."""
    scripts = NERFCTL_FRAMEWORKS.get(framework)
    if scripts is None:
        known = ", ".join(NERFCTL_FRAMEWORKS)
        msg = f"unknown nerfctl framework '{framework}'. Known: {known}"
        raise ValueError(msg)

    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for action, src in scripts.items():
        if not src.exists():
            msg = f"nerfctl script not found: {src}"
            raise FileNotFoundError(msg)
        dest = output / f"nerfctl-{framework}-{action}"
        # Read as text to normalize CRLF -> LF (Windows checkout), then
        # write as raw UTF-8 bytes to guarantee Unix line endings.
        dest.write_bytes(src.read_text(encoding="utf-8").encode("utf-8"))
        dest.chmod(0o755)
        written.append(dest)
    return written
