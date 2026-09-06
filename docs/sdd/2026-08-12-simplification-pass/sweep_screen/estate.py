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
from collections import Counter
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


#: Context managers that assert a raise, by the bare name `call_name` reads.
RAISES_CM = frozenset({"raises", "assertRaises", "assertRaisesRegex", "assertWarns", "assertWarnsRegex"})


def assertions_of(node: ast.AST) -> tuple[str, ...]:
    """Every assertion a function makes in its own body, unparsed, in order.

    Three shapes, which are the three ways this suite says a test checked
    something: an `assert` statement, a context manager that asserts a raise,
    and a call to any `assert*` method. The third is not decoration: most of
    `website/tests` asserts only that way, and a rule that skipped it would read
    a suite of eight assertions as empty.

    A function nested inside this one is skipped, because the walk names it
    separately and it carries its own assertions.

    Unparsed AS WRITTEN, string literals included. This is deliberately the
    opposite of a site anchor's digest, which blanks interpolations so a local
    rename cannot orphan a row: here a reworded literal inside an anchored test
    is exactly what the row is about, so it has to be loud.
    """
    found: list[tuple[int, int, str]] = []

    def walk(here: ast.AST) -> None:
        for child in ast.iter_child_nodes(here):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Assert):
                found.append((child.lineno, child.col_offset, ast.unparse(child.test)))
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call) and call_name(expr) in RAISES_CM:
                        found.append((expr.lineno, expr.col_offset, ast.unparse(expr)))
            elif isinstance(child, ast.Call) and call_name(child).startswith("assert"):
                found.append((child.lineno, child.col_offset, ast.unparse(child)))
            walk(child)

    walk(node)
    return tuple(text for _, _, text in sorted(found))


def assertion_digest(texts: list[str]) -> str:
    """Six hex over what a function asserts, `e3b0c4` when it asserts nothing."""
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()[:6]


@dataclass(frozen=True, order=True)
class Function:
    """One function definition, from its first decorator to its last line."""

    qualname: str
    start: int
    end: int
    #: What this body asserts, unparsed and in source order. A span anchor
    #: digests it so that changing an assertion under a surviving function is
    #: visible. Empty means the body asserts nothing itself, which is usually a
    #: test that hands its assertions to a helper.
    assertions: tuple[str, ...] = ()

    def holds(self, line: int) -> bool:
        return self.start <= line <= self.end


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A typing overload stub, which shares its name with the real definition."""
    return any(exc_name(d) == "overload" for d in node.decorator_list)


def functions_in(tree: Tree, path: str) -> list[Function]:
    """Every function in one module, innermost last within a nest.

    Only `def`, `async def` and `class` open a qualname segment; the walk passes
    through everything else, so a test under `if sys.platform`, in an `except`
    arm or in a `match` case is found and named as though it sat at the top of
    its enclosing scope. One name may therefore have several ranges, which is
    the platform-conditional idiom of defining the same test in sibling
    branches, and an anchor answers for all of them. Two definitions of one name in
    the SAME block are a different thing, since only the second ever runs, and
    they are refused because a span anchor naming it cannot say which is meant.
    """
    found: list[Function] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for _, value in ast.iter_fields(node):
            block = value if isinstance(value, list) else [value]
            here: set[str] = set()
            for child in block:
                if not isinstance(child, ast.AST):
                    continue
                if isinstance(child, ast.ClassDef):
                    walk(child, f"{prefix}{child.name}.")
                    continue
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, prefix)
                    continue
                qualname = f"{prefix}{child.name}"
                if not _is_overload(child):
                    if qualname in here:
                        raise SystemExit(
                            f"{path}:{child.lineno}: {qualname} is defined twice in one block,"
                            " so only the second runs and a span anchor naming it cannot say which;"
                            " rename one or the map cannot address it"
                        )
                    here.add(qualname)
                    start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                    found.append(Function(qualname, start, child.end_lineno or child.lineno, assertions_of(child)))
                walk(child, f"{qualname}.")

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
        #: What the per-root filter drops, counted rather than dropped in
        #: silence: the sweep's estate is `match=` under `cli/tests` and the
        #: regex family under `website/tests`, and a suite that starts using
        #: the other spelling would otherwise vanish from every count here.
        self.excluded: Counter[str] = Counter()
        for path in tree.files(TEST_ROOT):
            for site in sites_in(tree, path):
                if site.kind == "match=":
                    self.sites.append(site)
                else:
                    self.excluded[f"{site.kind} under {TEST_ROOT}"] += 1
        for path in tree.files(WEB_ROOT):
            for site in sites_in(tree, path):
                if site.kind != "match=":
                    self.sites.append(site)
                else:
                    self.excluded[f"match= under {WEB_ROOT}"] += 1
        self.sites.sort(key=lambda s: (s.path, s.line, s.col))

        grouped: dict[Identity, list[Site]] = {}
        for site in self.sites:
            grouped.setdefault(site.identity, []).append(site)
        self.by_identity = {i: SiteGroup(i, tuple(s)) for i, s in grouped.items()}
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

    def function(self, path: str, qualname: str) -> list[Function]:
        """Every range this name covers here, which is more than one where the
        same test is defined in sibling branches."""
        return [f for f in self.functions(path) or [] if f.qualname == qualname]

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
