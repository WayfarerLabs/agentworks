"""Package fixed first-party helper modules without installing guest files."""

from __future__ import annotations

import base64
import bz2
import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True, slots=True, repr=False)
class FixedFileHelperBundle:
    """Core-owned bootstrap and exact armored module prefix for one family."""

    bootstrap: str
    prefix: bytes


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


def build_file_helper_bundle(
    package_name: str,
    module_names: tuple[str, ...],
    entrypoint_module: str,
) -> FixedFileHelperBundle:
    """Build one fixed stdin-delivered file-helper family from packaged source."""
    package = files(__package__)
    sources = tuple((name, package.joinpath(f"{name}.py").read_text(encoding="utf-8")) for name in module_names)
    return _build_file_helper_bundle(package_name, sources, entrypoint_module)


def _build_file_helper_bundle(
    package_name: str,
    sources: tuple[tuple[str, str], ...],
    entrypoint_module: str,
) -> FixedFileHelperBundle:
    """Build fixed delivery from already-selected trusted source modules."""
    prefix = base64.b64encode(
        bz2.compress(json.dumps(sources, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    )
    digest = hashlib.sha256(prefix).hexdigest()
    prefix_length = len(prefix)
    entrypoint = f"{package_name}.{entrypoint_module}"
    bootstrap = (
        "import os,base64,bz2,hashlib,json,sys,types\n"
        "b=bytearray()\n"
        f"while len(b)<{prefix_length}:\n"
        f" c=os.read(0,{prefix_length}-len(b))\n"
        " if not c:raise SystemExit(125)\n"
        " b.extend(c)\n"
        f"if hashlib.sha256(b).hexdigest()!={digest!r}:raise SystemExit(125)\n"
        f"p=types.ModuleType({package_name!r});p.__path__=[];p.__package__={package_name!r};"
        f"sys.modules[{package_name!r}]=p\n"
        "for n,s in json.loads(bz2.decompress(base64.b64decode(b,validate=True))):\n"
        f" q={package_name!r}+'.'+n;m=types.ModuleType(q);m.__file__='<'+q+'>';"
        f"m.__package__={package_name!r};sys.modules[q]=m;"
        "exec(compile(s,m.__file__,'exec'),m.__dict__)\n"
        f"raise SystemExit(sys.modules[{entrypoint!r}].main(sys.argv[1]))\n"
    )
    return FixedFileHelperBundle(bootstrap, prefix)
