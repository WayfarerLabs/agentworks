"""Package fixed first-party helper modules without installing guest files."""

from __future__ import annotations

import base64
import zlib
from importlib.resources import files


def build_helper_modules(package_name: str, module_names: tuple[str, ...]) -> str:
    """Build a loader for core-selected sibling modules in dependency order.

    Only trusted packaged source belongs here. Request values travel separately
    through stdin and must never select modules or become part of this source.
    The caller appends its fixed entry point after the modules are registered.
    """
    package = files(__package__)
    sources = tuple(
        (name, base64.b64encode(zlib.compress(package.joinpath(f"{name}.py").read_bytes())).decode("ascii"))
        for name in module_names
    )
    return (
        "import base64,sys,types,zlib\n"
        f"p=types.ModuleType({package_name!r});p.__path__=[];p.__package__={package_name!r};"
        f"sys.modules[{package_name!r}]=p\n"
        f"for n,s in {sources!r}:\n"
        f" q={package_name!r}+'.'+n;m=types.ModuleType(q);m.__file__='<'+q+'>';"
        f"m.__package__={package_name!r};sys.modules[q]=m;"
        "exec(compile(zlib.decompress(base64.b64decode(s)),m.__file__,'exec'),m.__dict__)\n"
    )
