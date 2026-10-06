"""Package fixed first-party helper modules without installing guest files."""

from __future__ import annotations

import ast
import base64
import bz2
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files


@dataclass(frozen=True, slots=True, repr=False)
class FixedFileHelperBundle:
    """Core-owned bootstrap and exact armored module prefix for one family."""

    bootstrap: str
    prefix: bytes


class RootGuestDelivery(StrEnum):
    INLINE = "inline"
    FIXED_PREFIX = "fixed_prefix"


@dataclass(frozen=True, slots=True, repr=False)
class RootGuestProgram:
    """One trusted two-phase loader and its explicit stdin delivery mode."""

    loader_source: str
    delivery: RootGuestDelivery
    prefix: bytes


_GUEST_MODULES = ("_vm_guest_identity_protocol", "_vm_guest_identity_guest")


def build_root_guest_program(
    package_name: str,
    module_names: tuple[str, ...],
    entrypoint_module: str,
    *,
    delivery: RootGuestDelivery,
) -> RootGuestProgram:
    """Package fixed first-party modules with a mandatory early guest checkpoint."""
    package = files(__package__)
    sources = tuple((name, package.joinpath(f"{name}.py").read_text(encoding="utf-8")) for name in module_names)
    return _build_root_guest_program(package_name, sources, entrypoint_module, delivery=delivery)


def _build_root_guest_program(
    package_name: str,
    sources: tuple[tuple[str, str], ...],
    entrypoint_module: str,
    *,
    delivery: RootGuestDelivery,
) -> RootGuestProgram:
    """Place the canonical identity pair before all remaining trusted sources."""
    names = tuple(name for name, _source in sources)
    present = tuple(name in names for name in _GUEST_MODULES)
    if (
        type(delivery) is not RootGuestDelivery
        or not package_name
        or not names
        or len(set(names)) != len(names)
        or entrypoint_module not in names
        or present == (True, False)
        or present == (False, True)
    ):
        raise ValueError("invalid root guest program")
    package = files(__package__)
    identity = tuple((name, package.joinpath(f"{name}.py").read_text(encoding="utf-8")) for name in _GUEST_MODULES)
    for name, source in identity:
        dependencies = {
            node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom) and node.level > 0
        }
        allowed = {"_vm_guest_identity_protocol"} if name == "_vm_guest_identity_guest" else set()
        if dependencies - allowed:
            raise ValueError("guest checkpoint has an unknown local dependency")
    ordered = identity + tuple((name, source) for name, source in sources if name not in _GUEST_MODULES)
    compact = tuple((name, _compact_fixed_source(source)) for name, source in ordered)
    prefix = base64.b64encode(bz2.compress(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode()))
    if delivery is RootGuestDelivery.INLINE:
        input_source = f"b={prefix!r}\n"
        delivered = b""
    else:
        input_source = (
            "b=bytearray()\n"
            f"while len(b)<{len(prefix)}:\n"
            f" c=os.read(0,{len(prefix)}-len(b))\n"
            " if not c:raise ValueError('missing fixed prefix')\n"
            " b.extend(c)\n"
            f"if hashlib.sha256(b).hexdigest()!={hashlib.sha256(prefix).hexdigest()!r}:"
            "raise ValueError('invalid fixed prefix')\n"
        )
        delivered = prefix
    entrypoint = f"{package_name}.{entrypoint_module}"
    loader = (
        "import os,base64,bz2,hashlib,json,sys,types\n"
        f"_agw_guest_module={package_name + '._vm_guest_identity_guest'!r}\n"
        f"{input_source}"
        "_agw_sources=json.loads(bz2.decompress(base64.b64decode(b,validate=True)))\n"
        f"p=types.ModuleType({package_name!r});p.__path__=[];p.__package__={package_name!r};"
        f"sys.modules[{package_name!r}]=p\n"
        "def _agw_install(items):\n"
        " for n,s in items:\n"
        f"  q={package_name!r}+'.'+n;m=types.ModuleType(q);m.__file__='<'+q+'>';"
        f"m.__package__={package_name!r};sys.modules[q]=m;"
        "exec(compile(s,m.__file__,'exec'),m.__dict__)\n"
        "def _agw_load_identity():_agw_install(_agw_sources[:2])\n"
        "def _agw_load_remaining():_agw_install(_agw_sources[2:])\n"
        f"def _agw_enter_body():return sys.modules[{entrypoint!r}].main(sys.argv[1])\n"
    )
    return RootGuestProgram(loader, delivery, delivered)


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
    compact_sources = tuple((name, _compact_fixed_source(source)) for name, source in sources)
    prefix = base64.b64encode(
        bz2.compress(json.dumps(compact_sources, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
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


def _compact_fixed_source(source: str) -> str:
    """Remove docstrings from trusted fixed helpers before their single delivery."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            node.body.pop(0)
            if not node.body and not isinstance(node, ast.Module):
                node.body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))
