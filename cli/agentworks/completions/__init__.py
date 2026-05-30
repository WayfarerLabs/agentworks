"""Shell completion script generation for agentworks."""

from __future__ import annotations

import os
from pathlib import Path

from agentworks.completions.spec import build_spec, completion_version


def detect_shell() -> str | None:
    """Best-effort detection of the user's shell. Conservative: returns None
    when the answer isn't unambiguous so the caller can ask for --shell.

    Looks at the basename of $SHELL (the operator's login shell). Recognizes
    bash and zsh; everything else is treated as unknown.
    """
    raw = os.environ.get("SHELL")
    if not raw:
        return None
    name = Path(raw).name.lower()
    if name == "bash":
        return "bash"
    if name == "zsh":
        return "zsh"
    return None


def generate(shell: str) -> str:
    """Generate a completion script for the given shell."""
    from agentworks.cli import app
    from agentworks.completions.bash import generate_bash
    from agentworks.completions.powershell import generate_powershell
    from agentworks.completions.zsh import generate_zsh

    spec = build_spec(app)
    version = completion_version(spec)

    generators = {
        "bash": generate_bash,
        "zsh": generate_zsh,
        "powershell": generate_powershell,
    }

    generator = generators.get(shell)
    if generator is None:
        supported = ", ".join(sorted(generators))
        msg = f"Unsupported shell: {shell}. Supported: {supported}"
        raise ValueError(msg)

    return generator(spec, version)


SUPPORTED_SHELLS = ("bash", "zsh", "powershell")
