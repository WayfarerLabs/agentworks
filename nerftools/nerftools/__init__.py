"""nerftools: build and manage nerf tools."""

from importlib.resources import files
from pathlib import Path

# Default manifests ship as a subpackage, accessible via importlib.resources
# for both editable installs and published wheels.
BUILTIN_MANIFESTS_DIR = Path(str(files("nerftools.default_manifests")))

_NERFCTL_DIR = Path(__file__).parent / "nerfctl" / "claude"

NERFCTL_SCRIPTS: dict[str, Path] = {
    "nerfctl-grant-allow": _NERFCTL_DIR / "grant-allow.sh",
    "nerfctl-grant-deny": _NERFCTL_DIR / "grant-deny.sh",
    "nerfctl-grant-reset": _NERFCTL_DIR / "grant-reset.sh",
    "nerfctl-grant-by-threat": _NERFCTL_DIR / "grant-by-threat.sh",
    "nerfctl-grant-list": _NERFCTL_DIR / "grant-list.sh",
    "nerfctl-install-plugin": _NERFCTL_DIR / "install-plugin.sh",
}


def install_nerfctl(output: Path) -> list[Path]:
    """Copy nerfctl scripts into *output*. Returns paths written."""
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, src in NERFCTL_SCRIPTS.items():
        if not src.exists():
            msg = f"nerfctl script not found: {src}"
            raise FileNotFoundError(msg)
        dest = output / name
        # Read as text to normalize CRLF -> LF (Windows checkout), then
        # write as raw UTF-8 bytes to guarantee Unix line endings.
        dest.write_bytes(src.read_text(encoding="utf-8").encode("utf-8"))
        dest.chmod(0o755)
        written.append(dest)
    return written
