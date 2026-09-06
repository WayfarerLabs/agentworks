"""The sweep's estate, and the identity that survives a line number moving.

The map owns the grammar; its "Reading this file mechanically" section is where
a reader goes for what a row means. What the code needs is only this: an
identity is four fields, `path::qualname::Type::digest`, none of them a
position, and it names a GROUP of sites rather than one, because assertions in
one test that tie on all four are indistinguishable in evidence and a row's
disposition applies to all of them alike. The group's size is its multiplicity,
and a change in it is reported rather than silently reattached.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from .tree import TEST_ROOT, WEB_ROOT, Tree, call_name, exc_name, template

#: Assertion helpers that take the asserted type first and the regex second.
TYPED_REGEX_METHODS = frozenset({"assertRaisesRegex", "assertRaisesRegexp", "assertWarnsRegex"})

#: Assertion helpers that take a subject first and the regex second, so the
#: method name is the closest thing to a type the identity can carry.
UNTYPED_REGEX_METHODS = frozenset({"assertRegex", "assertNotRegex"})

REGEX_METHODS = TYPED_REGEX_METHODS | UNTYPED_REGEX_METHODS

#: Six hex is the shortest prefix that separates every distinct needle in this
#: estate: five collides once, over two needles, and four collides twice, over
#: four. The margin above six is for a tree that keeps moving.
DIGEST_LENGTH = 6


def digest_of(needle: str) -> str:
    return hashlib.sha256(needle.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


@dataclass(frozen=True, order=True)
class Identity:
    """What a group of indistinguishable sites is, independent of where it sits.

    `qualname` is the enclosing function dotted through any class or nesting.
    That is a dotted path, not pytest's `::` node-id spelling, which the map's
    grammar section says out loud so nobody pastes one into the other.
    """

    path: str
    qualname: str
    type_name: str
    digest: str

    @property
    def tail(self) -> str:
        """The identity without its path, as a row cell writes it."""
        return f"{self.qualname}::{self.type_name}::{self.digest}"

    def __str__(self) -> str:
        return f"{self.path}::{self.tail}"


@dataclass(frozen=True, order=True)
class Site:
    """One `match=` or regex-family assertion, at one point in history."""

    identity: Identity
    line: int
    col: int
    kind: str
    needle: str

    @property
    def path(self) -> str:
        return self.identity.path

    @property
    def where(self) -> str:
        return f"{self.identity.path}:{self.line}"


@dataclass(frozen=True)
class SiteGroup:
    """Every site of one identity, in source order."""

    identity: Identity
    sites: tuple[Site, ...]

    @property
    def multiplicity(self) -> int:
        return len(self.sites)

    @property
    def where(self) -> str:
        return f"{self.identity.path}:{','.join(str(s.line) for s in self.sites)}"


@dataclass(frozen=True, order=True)
class Function:
    """One function definition, from its first decorator to its last line."""

    qualname: str
    start: int
    end: int

    def holds(self, line: int) -> bool:
        return self.start <= line <= self.end


def functions_in(tree: Tree, path: str) -> list[Function]:
    """Every function in one module, innermost last within a nest."""
    found: list[Function] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                found.append(Function(f"{prefix}{child.name}", start, child.end_lineno or child.lineno))
                walk(child, f"{prefix}{child.name}.")

    walk(tree.parse(path), "")
    return sorted(found)


def _asserted(node: ast.Call, path: str) -> tuple[str, str] | None:
    """The (type, needle) a regex-family assertion carries, or None.

    A call that splats its arguments is refused rather than skipped, for the
    same reason an unparsed file is: a site nobody can see is a site that can
    never be reported unowned, and the estate would look complete while being
    short by it.
    """
    name = call_name(node)
    if name != "raises" and name not in REGEX_METHODS:
        return None
    if any(k.arg is None for k in node.keywords) or any(isinstance(a, ast.Starred) for a in node.args):
        raise SystemExit(
            f"{path}:{node.lineno}: `{name}` splats its arguments, so its match text cannot be read;"
            " the estate would be short by this site"
        )
    if name == "raises":
        keyword = next((k for k in node.keywords if k.arg == "match"), None)
        if keyword is None:
            return None
        return (exc_name(node.args[0]) or "<expr>") if node.args else "<expr>", template(keyword.value) or "<expr>"
    if len(node.args) < 2:
        return None
    if name in UNTYPED_REGEX_METHODS:
        return name, template(node.args[1]) or "<expr>"
    return exc_name(node.args[0]) or "<expr>", template(node.args[1]) or "<expr>"


def sites_in(tree: Tree, path: str) -> list[Site]:
    """`match=` and regex-family sites in one test module, in source order."""
    raw: list[tuple[int, int, str, str, str, str]] = []

    def walk(node: ast.AST, qualname: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{qualname}{child.name}.")
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, f"{qualname}{child.name}.")
                continue
            if isinstance(child, ast.Call):
                found = _asserted(child, path)
                if found is not None:
                    type_name, needle = found
                    kind = "match=" if call_name(child) == "raises" else call_name(child)
                    raw.append((child.lineno, child.col_offset, qualname.rstrip("."), type_name, needle, kind))
            walk(child, qualname)

    walk(tree.parse(path), "")
    return [
        Site(Identity(path, qualname, type_name, digest_of(needle)), line, col, kind, needle)
        for line, col, qualname, type_name, needle, kind in sorted(raw)
    ]


class Snapshot:
    """The estate and the function index of one tree, both derived once.

    Identity resolution needs both: a site anchor resolves against the estate,
    and a span anchor against the function index.
    """

    def __init__(self, tree: Tree) -> None:
        self.tree = tree
        self.sites: list[Site] = []
        for path in tree.files(TEST_ROOT):
            self.sites.extend(s for s in sites_in(tree, path) if s.kind == "match=")
        for path in tree.files(WEB_ROOT):
            self.sites.extend(s for s in sites_in(tree, path) if s.kind != "match=")
        self.sites.sort(key=lambda s: (s.path, s.line, s.col))

        grouped: dict[Identity, list[Site]] = {}
        for site in self.sites:
            grouped.setdefault(site.identity, []).append(site)
        self.by_identity = {i: SiteGroup(i, tuple(s)) for i, s in grouped.items()}
        # Every anchor in the map keys on the groups partitioning the estate, so
        # it is checked here rather than asserted in the map's prose.
        counted = sum(g.multiplicity for g in self.by_identity.values())
        if counted != len(self.sites):
            raise SystemExit(f"site groups do not partition the estate at {tree}: {counted} of {len(self.sites)}")
        self._functions: dict[str, list[Function] | None] = {}

    @property
    def ties(self) -> list[SiteGroup]:
        """Identities naming more than one site, which is the population the
        multiplicity exists for."""
        return sorted(
            (g for g in self.by_identity.values() if g.multiplicity > 1),
            key=lambda g: g.identity,
        )

    def functions(self, path: str) -> list[Function] | None:
        """The function index of one file, or None when the file is gone."""
        if path not in self._functions:
            if not path.endswith(".py") or not self.tree.exists(path):
                self._functions[path] = None
            else:
                self._functions[path] = functions_in(self.tree, path)
        return self._functions[path]

    def function(self, path: str, qualname: str) -> Function | None:
        for candidate in self.functions(path) or []:
            if candidate.qualname == qualname:
                return candidate
        return None

    def enclosing(self, path: str, line: int) -> Function | None:
        """The innermost function holding `line`, or None."""
        holders = [f for f in self.functions(path) or [] if f.holds(line)]
        return max(holders, key=lambda f: (f.start, -f.end)) if holders else None

    def why_unnamed(self, path: str, line: int) -> str:
        """Why a line sits in no function here, which is what a line anchor
        records on its row so a reader knows whether it can ever be fixed."""
        if not path.endswith(".py"):
            return "not Python"
        if not self.tree.exists(path):
            return "file gone"
        for node in ast.iter_child_nodes(self.tree.parse(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.stmt) and node.lineno <= line <= (node.end_lineno or node.lineno):
                return "module level"
        return "between functions"

    def site_at(self, path: str, line: int) -> Site | None:
        return next((s for s in self.sites if s.path == path and s.line == line), None)

    def near(self, identity: Identity) -> list[SiteGroup]:
        """Groups in the same test asserting the same type against a DIFFERENT
        needle, which is what a reworded message leaves behind."""
        return [
            g
            for g in self.by_identity.values()
            if g.identity.path == identity.path
            and g.identity.qualname == identity.qualname
            and g.identity.type_name == identity.type_name
            and g.identity.digest != identity.digest
        ]
