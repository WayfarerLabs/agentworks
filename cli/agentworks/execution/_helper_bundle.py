"""Package fixed first-party helper modules without installing guest files."""

from __future__ import annotations

import base64
import bz2
import json
from importlib.resources import files


def build_helper_modules(package_name: str, module_names: tuple[str, ...]) -> str:
    """Build a loader for core-selected sibling modules in dependency order.

    Only trusted packaged source belongs here. Request values travel separately
    through stdin and must never select modules or become part of this source.
    The caller appends its fixed entry point after the modules are registered.
    """
    package = files(__package__)
    sources = tuple((name, package.joinpath(f"{name}.py").read_text(encoding="utf-8")) for name in module_names)
    payload = base64.b64encode(
        bz2.compress(json.dumps(sources, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    ).decode("ascii")
    return (
        "import base64,bz2,json,sys,types\n"
        f"p=types.ModuleType({package_name!r});p.__path__=[];p.__package__={package_name!r};"
        f"sys.modules[{package_name!r}]=p\n"
        f"for n,s in json.loads(bz2.decompress(base64.b64decode({payload!r}))):\n"
        f" q={package_name!r}+'.'+n;m=types.ModuleType(q);m.__file__='<'+q+'>';"
        f"m.__package__={package_name!r};sys.modules[q]=m;"
        "exec(compile(s,m.__file__,'exec'),m.__dict__)\n"
    )
