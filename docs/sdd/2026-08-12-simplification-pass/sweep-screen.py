#!/usr/bin/env python3
"""Derive the sweep's `match=` estate and its callee-side raise screen.

This is the tooling behind [sweep-inventory.md](sweep-inventory.md)'s group-1
site accounting and its callee-side raise screen. It dies with that inventory
when the sweep closes; nothing in the shipped CLI depends on it, which is why
it lives beside the artifact it serves rather than under `cli/` or `scripts/`.

It answers three questions an executor needs and a reader should not have to
take on trust:

* **Estate.** Every `pytest.raises(..., match=)` site under `cli/tests` and
  every `assertRaisesRegex`-family site under `website/tests`, at HEAD.
* **Attribution.** Which inventory row claims each site, and which sites or
  row anchors do not resolve.
* **Screen.** For each site, whether the operation under test can raise the
  asserted type from more than one path, and whether a structural handle tells
  the targeted raise apart. `hla.md`'s case 1 holds only where it cannot.

Run from the repository root with Python 3.12 or newer, which the script
enforces rather than documents: 3.11 cannot parse the PEP 701 f-strings two
estate files use, and a file that fails to parse contributes no sites, so it can
never be reported unowned and the one guarantee here degrades into a smaller
estate that still looks complete. Any unparsed file is fatal for the same
reason.

    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py estate
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py attribute
    python3 docs/sdd/2026-08-12-simplification-pass/sweep-screen.py screen

**Positives are sound; negatives are not.** A site reported multi-raise-path
genuinely has more than one reachable raise of that type. A site reported
single-raise-path is single as far as resolution reached, and an unresolved
site is not screened at all. Resolution is deliberately conservative: a call on
a value whose type is not known statically is reported rather than guessed, so
the counts understate the multi-raise population instead of inventing one.

One file, past the repository's 500-line goal, deliberately: the three commands
share one AST index and one resolver, and splitting a script that dies with its
inventory into a package would cost a reader more than the length does.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_MARKER = "docs/sdd/2026-08-12-simplification-pass"
PROD_ROOT = "cli/agentworks"
TEST_ROOT = "cli/tests"
WEB_ROOT = "website/tests"
INVENTORY = f"{REPO_MARKER}/sweep-inventory.md"

#: How deep the walk follows first-party calls. Six is past the point where the
#: verdict distribution stops moving; the bound exists to stop a cycle, not to
#: approximate anything.
MAX_DEPTH = 6

REGEX_METHODS = frozenset(
    {"assertRaisesRegex", "assertRaisesRegexp", "assertRegex", "assertNotRegex", "assertWarnsRegex"}
)


def git_files(*roots: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", *roots], capture_output=True, text=True, check=True).stdout
    return [f for f in out.split() if f.endswith(".py")]


def parse(path: str) -> ast.Module:
    """Parse one first-party file, or stop.

    Skipping an unparsed file would silently shrink the estate: its sites would
    not exist to be reported unowned, so the "every site is claimed by exactly
    one row" check would pass over a file nobody had looked at. There is no
    honest partial answer here, so this raises rather than returning None.
    """
    try:
        return ast.parse(Path(path).read_text(encoding="utf-8"), path)
    except (SyntaxError, OSError) as exc:
        raise SystemExit(f"{path}: cannot parse ({exc}); the estate would be short by this file")


def exc_name(node: ast.AST | None) -> str | None:
    """The name an exception expression names, ignoring how it is called."""
    if isinstance(node, ast.Call):
        return exc_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def template(node: ast.AST | None) -> str | None:
    """A string expression with its interpolations blanked to \x00, so a
    fixed part can still be matched against a `match=` needle."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "\x00" for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = template(node.left), template(node.right)
        return None if left is None or right is None else left + right
    return None


# ---------------------------------------------------------------------------
# Estate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    kind: str
    needle: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


def sites_in(path: str) -> list[Site]:
    """`match=` and `assertRaisesRegex`-family sites in one test module."""
    tree = parse(path)
    found: list[Site] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        if name == "raises":
            keyword = next((k for k in node.keywords if k.arg == "match"), None)
            if keyword is not None:
                found.append(Site(path, node.lineno, "match=", template(keyword.value) or "<expr>"))
        elif name in REGEX_METHODS:
            arg = node.args[1] if len(node.args) > 1 else None
            found.append(Site(path, node.lineno, name, template(arg) or "<expr>"))
    return found


def estate() -> list[Site]:
    found: list[Site] = []
    for path in git_files(TEST_ROOT):
        found.extend(s for s in sites_in(path) if s.kind == "match=")
    for path in git_files(WEB_ROOT):
        found.extend(s for s in sites_in(path) if s.kind != "match=")
    return sorted(found, key=lambda s: (s.path, s.line))


# ---------------------------------------------------------------------------
# The injected-marker screen
# ---------------------------------------------------------------------------

EXCEPTION_NAME = re.compile(r"(?:Error|Exception|Interrupt|Abort|Failure)$")


def injected_markers(path: str) -> list[str]:
    """Every string a test module hands to an exception constructor.

    These are markers the test wrote, not prose the repository ships, so a
    `match=` against one proves the injected failure is the one observed
    rather than pinning anything authored.
    """
    out: list[str] = []
    for node in ast.walk(parse(path)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        if EXCEPTION_NAME.search(name):
            text = template(node.args[0])
            if text:
                out.append(text)
    return out


def shipped_strings() -> str:
    """Every string literal production can emit, joined for substring tests.

    Comments and docstrings are excluded deliberately. The question this
    answers is whether a needle pins prose the code SAYS, and a phrase that
    appears only in a comment about the behavior is not that: the Lima
    cleanup-masking site matches `"original failure"`, which two production
    docstrings discuss and no production message contains.
    """
    parts: list[str] = []
    for path in git_files(PROD_ROOT):
        tree = parse(path)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                parts.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                parts.extend(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return "\n".join(parts)


def injected() -> None:
    """Report every `match=` site whose needle is a marker its own test wrote.

    A needle that ALSO appears in production is excluded, however it reaches
    the test: a fake copying a shipped sentence, or a test-side
    `AssertionError` guard that happens to quote one, is pinning our prose
    through a longer route, which is what the batch deletes. What survives is
    a phrase that exists nowhere but the test, so the assertion can only be
    proof that the injected failure is the observed one.
    """
    shipped = shipped_strings()
    hits = 0
    print("site\tneedle\tinjected-string")
    for path in git_files(TEST_ROOT):
        markers = injected_markers(path)
        if not markers:
            continue
        for site in sites_in(path):
            if site.needle == "<expr>" or site.needle in shipped:
                continue
            source = next((m for m in markers if site.needle in m), None)
            if source is not None:
                hits += 1
                print(f"{path}:{site.line}\t{site.needle}\t{source}")
    print(f"\n# injected-marker sites: {hits}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Attribution against the inventory's rows
# ---------------------------------------------------------------------------

PATH_RE = re.compile(r"((?:cli|website)/[A-Za-z0-9_./-]+?\.(?:py|mjs))")
SPAN_RE = re.compile(r"\d+(?:-\d+)?")


@dataclass
class Row:
    id: str
    path: str | None
    exact: list[int]
    ranges: list[tuple[int, int]]
    group: str

    def claims(self, site: Site) -> bool:
        if self.path != site.path:
            return False
        # A row addressing a whole file rather than lines: L-101a and L-101b
        # split one file's guards by pattern, and RB-012 names a file to say it
        # needs no rows. The mechanical batch used to be addressed this way and
        # is not any more; every one of its rows now lists its own sites.
        if not self.exact and not self.ranges:
            return True
        return site.line in self.exact or any(lo <= site.line <= hi for lo, hi in self.ranges)


#: A row id, as the inventory's own reading instructions describe it: an
#: original-read prefix, the mechanical batch's, one of the pulled-out blocks,
#: or the re-baseline's, optionally with the letter suffix a split row carries.
#: Header cells that look like ids (`Sub-batch`, `File`, `Recipe`) fail this by
#: construction, which is why the parser needs no list of them.
ROW_ID = re.compile(r"(?:[A-F]|L|RB|G1)-[A-Z]?\d{1,3}[a-z]?$")


def rows() -> list[Row]:
    """Every row, with the file and line spans it addresses and the group it
    sits in.

    This is the reference implementation of the grammar the inventory's
    "reading this file mechanically" section describes, and the reason that
    section is three sentences rather than a specification. Two details are
    where every hand-rolled parser has gone wrong: cells split on UNESCAPED
    pipes only, because `\\|` inside a code span is content and a naive split
    turns one row into three; and only a `##` heading changes the group, since
    the pulled-out blocks are `###` subsections of the group they belong to.
    """
    out: list[Row] = []
    group = ""
    for line in Path(INVENTORY).read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            heading = line.lstrip("# ").strip()
            if level <= 2:
                group = heading.split(":")[0]
            continue
        if not line.startswith("|"):
            continue
        cells = split_cells(line)
        if len(cells) < 3 or not ROW_ID.fullmatch(cells[0]):
            continue
        target = cells[1].replace("`", "")
        match = PATH_RE.search(target)
        exact: list[int] = []
        ranges: list[tuple[int, int]] = []
        tail = target.split(".py:", 1)[1] if ".py:" in target else ""
        for token in SPAN_RE.findall(tail):
            if "-" in token:
                lo, hi = token.split("-")
                ranges.append((int(lo), int(hi)))
            else:
                exact.append(int(token))
        out.append(Row(cells[0], match.group(1) if match else None, exact, ranges, group))
    return out


def split_cells(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    index = 0
    body = line.strip()
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body) and body[index + 1] == "|":
            current.append("\\|")
            index += 2
        elif body[index] == "|":
            cells.append("".join(current).strip())
            current = []
            index += 1
        else:
            current.append(body[index])
            index += 1
    cells.append("".join(current).strip())
    # The leading pipe opens the row, so the first split is always empty.
    return cells[1:]


def attribute() -> None:
    all_sites = estate()
    all_rows = rows()
    g1 = [r for r in all_rows if r.group == "Group 1"]
    claimed: dict[Site, list[Row]] = {s: [r for r in all_rows if r.claims(s)] for s in all_sites}

    print(f"estate at HEAD: {len(all_sites)} sites ({sum(1 for s in all_sites if s.kind == 'match=')} `match=`)")
    print(f"rows: {len(all_rows)} total, {len(g1)} in group 1")

    # The property the inventory claims: group 1 owns every `match=` site, once.
    match_sites = [s for s in all_sites if s.kind == "match="]
    g1_claims = {s: [r.id for r in g1 if r.claims(s)] for s in all_sites}
    unowned = [s for s in match_sites if not g1_claims[s]]
    twice = [s for s in match_sites if len(g1_claims[s]) > 1]
    print(f"\n`match=` sites owned by exactly one group-1 row: {len(match_sites) - len(unowned) - len(twice)}")
    print(f"  owned by no group-1 row:      {len(unowned)}")
    for s in unowned:
        print(f"      {s}")
    print(f"  owned by several group-1 rows: {len(twice)}")
    for s in twice:
        print(f"      {s}  {', '.join(g1_claims[s])}")

    print("\nwebsite regex sites, by the row that claims them:")
    for s in (s for s in all_sites if s.kind != "match="):
        owners = ", ".join(f"{r.id} [{r.group}]" for r in claimed[s]) or "NONE"
        print(f"  {s}  {s.kind}  {owners}")

    # A group-4 row's line span legitimately contains a group-1 site: the two
    # address different assertions on nearby lines. Only same-group overlap is
    # a defect, so cross-group overlap is counted rather than listed.
    cross = sum(1 for hits in claimed.values() if len({r.group for r in hits}) > 1)
    print(f"\nsites addressed by rows in more than one group (expected, not a defect): {cross}")

    # Anchors are only checkable for rows inside this estate. A group-3 row's
    # line span addresses prose assertions, which this scan does not see, so
    # asking whether a `match=` site falls inside it answers nothing.
    print("\ngroup-1 anchors that do not resolve at HEAD:")
    live = defaultdict(set)
    for s in all_sites:
        live[s.path].add(s.line)
    for row in g1:
        if row.path is None:
            continue
        if not Path(row.path).exists():
            print(f"  {row.id}: file gone ({row.path}), which is what `[dead]` marks")
            continue
        missing = [n for n in row.exact if n not in live[row.path]]
        empty = [(lo, hi) for lo, hi in row.ranges if not any(lo <= n <= hi for n in live[row.path])]
        if missing or empty:
            print(f"  {row.id} ({row.path}): stale={missing} empty-ranges={empty}")


# ---------------------------------------------------------------------------
# The callee-side raise screen
# ---------------------------------------------------------------------------


@dataclass
class Func:
    module: str
    path: str
    name: str
    lineno: int
    raises: list[tuple[str, int]] = field(default_factory=list)
    calls: list[ast.Call] = field(default_factory=list)
    in_class: str | None = None


@dataclass
class Module:
    path: str
    name: str
    #: bound name -> (module, symbol); symbol None means the name IS a module
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    funcs: dict[str, Func] = field(default_factory=dict)
    methods: dict[str, list[Func]] = field(default_factory=dict)


class _Collector(ast.NodeVisitor):
    def __init__(self, module: Module) -> None:
        self.module = module
        self.stack: list[Func] = []
        self.classes: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.module.imports[alias.asname or alias.name.split(".")[0]] = (alias.name, None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or node.module is None:
            return
        for alias in node.names:
            self.module.imports[alias.asname or alias.name] = (node.module, alias.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        for child in node.body:
            self.visit(child)
        self.classes.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        fn = Func(
            module=self.module.name,
            path=self.module.path,
            name=node.name,
            lineno=node.lineno,
            in_class=self.classes[-1] if self.classes else None,
        )
        if not self.stack:
            if self.classes:
                self.module.methods.setdefault(node.name, []).append(fn)
            else:
                self.module.funcs[node.name] = fn
        self.stack.append(fn)
        for child in node.body:
            self.visit(child)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Raise(self, node: ast.Raise) -> None:
        name = exc_name(node.exc)
        if self.stack and name:
            self.stack[-1].raises.append((name, node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.stack:
            self.stack[-1].calls.append(node)
        self.generic_visit(node)


class World:
    """First-party modules, indexed for module-scoped name resolution."""

    def __init__(self) -> None:
        self.modules: dict[str, Module] = {}
        self.by_path: dict[str, Module] = {}
        for path in git_files(PROD_ROOT):
            self._add(path, importable=True)

    def _add(self, path: str, *, importable: bool) -> Module:
        tree = parse(path)
        name = path[len("cli/") :] if path.startswith(f"{PROD_ROOT}/") else path
        name = name[:-3].removesuffix("/__init__").replace("/", ".")
        module = Module(path=path, name=name)
        _Collector(module).visit(tree)
        self.by_path[path] = module
        if importable:
            self.modules[name] = module
        return module

    def add_test(self, path: str) -> Module | None:
        return self.by_path.get(path) or self._add(path, importable=False)

    def resolve(self, module: Module, call: ast.Call) -> list[Func]:
        """First-party functions this call can reach, or [] when the callee
        cannot be resolved from `module`'s own namespace."""
        fn = call.func
        if isinstance(fn, ast.Name):
            if fn.id in module.funcs:
                return [module.funcs[fn.id]]
            target = module.imports.get(fn.id)
            return self._in_module(*target) if target and target[1] else []
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            base = fn.value.id
            if base == "self":
                return module.methods.get(fn.attr, [])
            target = module.imports.get(base)
            if target is None:
                return []
            candidate = target[0] if target[1] is None else f"{target[0]}.{target[1]}"
            return self._in_module(candidate, fn.attr) if candidate in self.modules else []
        return []

    def _in_module(self, module_name: str, symbol: str) -> list[Func]:
        module = self.modules.get(module_name)
        if module is None:
            return []
        if symbol in module.funcs:
            return [module.funcs[symbol]]
        target = module.imports.get(symbol)
        if target and target[1]:
            reexport = self.modules.get(target[0])
            if reexport and target[1] in reexport.funcs:
                return [reexport.funcs[target[1]]]
        return module.methods.get(symbol, [])


def subclass_closure() -> dict[str, set[str]]:
    """Exception name -> itself plus every first-party name deriving from it."""
    parents: dict[str, list[str]] = {}
    for path in git_files(PROD_ROOT, TEST_ROOT):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ClassDef):
                parents[node.name] = [b for b in (exc_name(base) for base in node.bases) if b]
    closure: dict[str, set[str]] = defaultdict(set)
    for cls in parents:
        seen: set[str] = set()
        stack = [cls]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(parents.get(current, []))
        for ancestor in seen:
            closure[ancestor].add(cls)
        closure[cls].add(cls)
    return closure


def raise_facts(world: World) -> dict[tuple[str, int], dict[str, str | None]]:
    """(path, line) -> the message template and handle kwargs of each raise."""
    facts: dict[tuple[str, int], dict[str, str | None]] = {}
    for path in list(world.by_path):
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            call = node.exc
            fact: dict[str, str | None] = {
                "message": template(call.args[0]) if call.args else None,
                "entity_kind": None,
                "entity_name": None,
            }
            for keyword in call.keywords:
                if keyword.arg in ("entity_kind", "entity_name"):
                    value = keyword.value
                    fact[keyword.arg] = repr(value.value) if isinstance(value, ast.Constant) else "<expr>"
            facts[(path, node.lineno)] = fact
    return facts


def reachable(world: World, module: Module, calls: list[ast.Call], family: set[str]) -> list[tuple[str, int]]:
    """Distinct raise sites of `family` reachable from `calls`."""
    hits: set[tuple[str, int]] = set()
    seen: set[tuple[str, str, int]] = set()
    frontier = [(module, call, 0) for call in calls]
    while frontier:
        current, call, depth = frontier.pop()
        for fn in world.resolve(current, call):
            key = (fn.path, fn.name, fn.lineno)
            if key in seen:
                continue
            seen.add(key)
            hits.update((fn.path, line) for name, line in fn.raises if name in family)
            owner = world.by_path.get(fn.path)
            if depth < MAX_DEPTH and owner is not None:
                frontier.extend((owner, sub, depth + 1) for sub in fn.calls)
    return sorted(hits)


def selects(needle: str, message: str | None) -> bool:
    """Would `match=needle` match a message built from this template?"""
    if message is None:
        return False
    try:
        if re.search(needle, message.replace("\x00", "")):
            return True
    except re.error:
        pass
    pattern = ".*".join(re.escape(part) for part in message.split("\x00"))
    return bool(re.search(pattern, needle))


def raises_sites(path: str):
    """Each `pytest.raises(..., match=)` in a module, with the calls in its body."""
    for node in ast.walk(parse(path)):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call) or not call.args:
                continue
            fn = call.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
            keyword = next((k for k in call.keywords if k.arg == "match"), None)
            if name != "raises" or keyword is None:
                continue
            asserted = exc_name(call.args[0])
            if asserted is None:
                continue
            body = [c for stmt in node.body for c in ast.walk(stmt) if isinstance(c, ast.Call)]
            yield call.lineno, asserted, template(keyword.value) or "<expr>", body


def screen() -> None:
    world = World()
    families = subclass_closure()
    test_paths = git_files(TEST_ROOT)
    for path in test_paths:
        world.add_test(path)
    facts = raise_facts(world)
    verdicts = Counter()

    print("site\tasserted\tverdict\ttargeted-raise\tevidence")
    for path in test_paths:
        module = world.by_path.get(path)
        if module is None:
            continue
        for line, asserted, needle, body in raises_sites(path):
            family = families.get(asserted, set()) | {asserted}
            hits = reachable(world, module, body, family)
            target: tuple[str, int] | None = None
            if not hits:
                verdict, evidence = "unresolved", "no first-party raise of this type reached"
            elif len(hits) == 1:
                verdict, target, evidence = "single-raise-path", hits[0], "one reachable raise"
            else:
                matched = [] if needle == "<expr>" else [h for h in hits if selects(needle, facts[h]["message"])]
                if len(matched) != 1:
                    verdict = "multi-target-unidentified"
                    evidence = f"match= selects {len(matched)} of {len(hits)} reachable raises"
                else:
                    target = matched[0]
                    mine = facts[target]
                    others = [facts[h] for h in hits if h != target]
                    unique = any(
                        mine[key] is not None and all(o.get(key) != mine[key] for o in others)
                        for key in ("entity_kind", "entity_name")
                    )
                    verdict = "multi-handle-discriminates" if unique else "multi-no-discriminator"
                    evidence = (
                        f"entity_kind={mine['entity_kind']} entity_name={mine['entity_name']}"
                        f" among {len(hits)} reachable raises"
                    )
            verdicts[verdict] += 1
            where = f"{target[0]}:{target[1]}" if target else "-"
            print(f"{path}:{line}\t{asserted}\t{verdict}\t{where}\t{evidence}")

    print("\n# verdict totals", file=sys.stderr)
    for name, count in sorted(verdicts.items()):
        print(f"# {name}\t{count}", file=sys.stderr)
    print(f"# total\t{sum(verdicts.values())}", file=sys.stderr)


#: Below this, `ast.parse` rejects the PEP 701 f-strings two estate files use.
#: Those files would then be skipped rather than counted, and a short estate
#: reports the same "every site is claimed" as a complete one, so this is a
#: refusal rather than a warning.
MIN_PYTHON = (3, 12)


def main() -> None:
    if sys.version_info < MIN_PYTHON:
        running = ".".join(str(n) for n in sys.version_info[:3])
        want = ".".join(str(n) for n in MIN_PYTHON)
        raise SystemExit(f"needs Python {want} or newer to parse the whole estate; this is {running}")
    if not Path(INVENTORY).exists():
        raise SystemExit("run this from the repository root")
    command = sys.argv[1] if len(sys.argv) > 1 else "screen"
    if command == "estate":
        for site in estate():
            print(f"{site}\t{site.kind}\t{site.needle}")
    elif command == "attribute":
        attribute()
    elif command == "injected":
        injected()
    elif command == "screen":
        screen()
    else:
        raise SystemExit(f"unknown command {command!r}; try estate, attribute, injected or screen")


if __name__ == "__main__":
    main()
