"""Test-only composition for fixed stdin-delivered file-helper families."""

from __future__ import annotations

import textwrap
from importlib.resources import files

from agentworks.execution._helper_bundle import FixedFileHelperBundle, _build_file_helper_bundle


def fixture_file_bundle(
    package_name: str,
    module_names: tuple[str, ...],
    guest_module: str,
    guest_patch: str = "",
) -> FixedFileHelperBundle:
    """Package a fixed test entrypoint after the production module closure."""
    package = files("agentworks.execution")
    sources = tuple((name, package.joinpath(f"{name}.py").read_text(encoding="utf-8")) for name in module_names)
    entrypoint = "_fixture_entry"
    entrypoint_source = f"""
import sys
guest=sys.modules[{(package_name + "." + guest_module)!r}]
{textwrap.dedent(guest_patch)}
def main(nonce):
 return guest.main(nonce)
"""
    return _build_file_helper_bundle(
        package_name,
        (*sources, (entrypoint, textwrap.dedent(entrypoint_source))),
        entrypoint,
    )
