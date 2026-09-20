"""Build the fixed no-install source bundle for the private inline helper."""

from __future__ import annotations

import base64
from importlib.resources import files

_PACKAGE = "_agw_inline"
_MODULE_NAMES = ("_process", "_evidence_wire", "_inline_request", "_inline_control", "_inline_guest")


def _packaged_sources() -> tuple[tuple[str, str], ...]:
    package = files(__package__)
    return tuple(
        (name, base64.b64encode(package.joinpath(f"{name}.py").read_bytes()).decode("ascii")) for name in _MODULE_NAMES
    )


def _build_fixed_source() -> str:
    sources = repr(_packaged_sources())
    return (
        "import base64,sys,types\n"
        f"p=types.ModuleType({_PACKAGE!r});p.__path__=[];p.__package__={_PACKAGE!r};sys.modules[{_PACKAGE!r}]=p\n"
        f"for n,s in {sources}:\n"
        f" q={_PACKAGE!r}+'.'+n;m=types.ModuleType(q);m.__file__='<'+q+'>';m.__package__={_PACKAGE!r};"
        "sys.modules[q]=m;exec(compile(base64.b64decode(s),m.__file__,'exec'),m.__dict__)\n"
        f"raise SystemExit(sys.modules[{(_PACKAGE + '._inline_guest')!r}].main(sys.argv[1]))\n"
    )


FIXED_SOURCE = _build_fixed_source()
