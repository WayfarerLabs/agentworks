"""Shell completion script generation for agentworks."""

from __future__ import annotations

from agentworks.completions.spec import build_spec, completion_version


def generate(shell: str) -> str:
    """Generate a completion script for the given shell."""
    from agentworks.cli import app
    from agentworks.completions.powershell import generate_powershell
    from agentworks.completions.zsh import generate_zsh

    spec = build_spec(app)
    version = completion_version(spec)

    generators = {
        "zsh": generate_zsh,
        "powershell": generate_powershell,
    }

    generator = generators.get(shell)
    if generator is None:
        supported = ", ".join(sorted(generators))
        msg = f"Unsupported shell: {shell}. Supported: {supported}"
        raise ValueError(msg)

    return generator(spec, version)


SUPPORTED_SHELLS = ("zsh", "powershell")
