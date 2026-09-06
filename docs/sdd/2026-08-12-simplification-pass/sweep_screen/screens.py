"""The two screens every delete row runs before its edit lands.

**The callee-side raise screen** asks, for each `match=` site, whether the
operation under test can raise the asserted type from more than one path, and
whether a structural handle tells the targeted raise apart. `hla.md`'s case 1
holds only where it cannot.

**The injected-marker screen** asks who wrote the matched string. A test that
injects a failure and matches on the string it injected is not pinning authored
prose; it is proving the injected failure is the observed one.

**Positives are sound; negatives are not.** A site reported multi-raise-path
genuinely has more than one reachable raise of that type. A site reported
single-raise-path is single as far as resolution reached, and an unresolved
site is not screened at all. Resolution is deliberately conservative: a call on
a value whose type is not known statically is reported rather than guessed, so
the counts understate the multi-raise population instead of inventing one.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .estate import Snapshot, sites_in
from .tree import PROD_ROOT, TEST_ROOT, Tree, call_name, exc_name, template

if TYPE_CHECKING:
    from collections.abc import Iterator

#: How deep the walk follows first-party calls. Six is past the point where the
#: verdict distribution stops moving; the bound exists to stop a cycle, not to
#: approximate anything.
MAX_DEPTH = 6

EXCEPTION_NAME = re.compile(r"(?:Error|Exception|Interrupt|Abort|Failure)$")

#: What the screen puts in place of an interpolation: the message says anything
#: at all there, so a needle that matches the fixed parts selects the raise.
WILDCARD = "\x00"


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

    def __init__(self, tree: Tree) -> None:
        self.tree = tree
        self.modules: dict[str, Module] = {}
        self.by_path: dict[str, Module] = {}
        for path in tree.files(PROD_ROOT):
            self._add(path, importable=True)

    def _add(self, path: str, *, importable: bool) -> Module:
        name = path[len("cli/") :] if path.startswith(f"{PROD_ROOT}/") else path
        name = name[:-3].removesuffix("/__init__").replace("/", ".")
        module = Module(path=path, name=name)
        _Collector(module).visit(self.tree.parse(path))
        self.by_path[path] = module
        if importable:
            self.modules[name] = module
        return module

    def add_test(self, path: str) -> Module:
        return self.by_path.get(path) or self._add(path, importable=False)

    def resolve(self, module: Module, call: ast.Call) -> list[Func]:
        """First-party functions this call can reach, or [] when the callee
        cannot be resolved from `module`'s own namespace."""
        fn = call.func
        if isinstance(fn, ast.Name):
            if fn.id in module.funcs:
                return [module.funcs[fn.id]]
            target = module.imports.get(fn.id)
            return self._in_module(target[0], target[1]) if target and target[1] else []
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


def subclass_closure(tree: Tree) -> dict[str, set[str]]:
    """Exception name -> itself plus every first-party name deriving from it."""
    parents: dict[str, list[str]] = {}
    for path in tree.files(PROD_ROOT, TEST_ROOT):
        for node in ast.walk(tree.parse(path)):
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
        for node in ast.walk(world.tree.parse(path)):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            call = node.exc
            fact: dict[str, str | None] = {
                "message": template(call.args[0], wildcard=WILDCARD) if call.args else None,
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
        if re.search(needle, message.replace(WILDCARD, "")):
            return True
    except re.error:
        pass
    pattern = ".*".join(re.escape(part) for part in message.split(WILDCARD))
    return bool(re.search(pattern, needle))


def raises_sites(tree: Tree, path: str) -> Iterator[tuple[int, str, str, list[ast.Call]]]:
    """Each `pytest.raises(..., match=)` this screen can walk, with its body.

    The screen's question is what the operation under test can raise, so it
    needs the operation, which is the `with` body. Everything this yields has
    one; what it cannot reach is not enumerated here but subtracted from the
    estate afterwards, so a site spelled in a way this walk does not recognise
    is reported rather than quietly absent.
    """
    for node in ast.walk(tree.parse(path)):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call) or not call.args:
                continue
            keyword = next((k for k in call.keywords if k.arg == "match"), None)
            if call_name(call) != "raises" or keyword is None:
                continue
            asserted = exc_name(call.args[0])
            if asserted is None:
                continue
            body = [c for stmt in node.body for c in ast.walk(stmt) if isinstance(c, ast.Call)]
            yield call.lineno, asserted, template(keyword.value, wildcard=WILDCARD) or "<expr>", body


def screen(tree: Tree) -> None:
    world = World(tree)
    families = subclass_closure(tree)
    test_paths = tree.files(TEST_ROOT)
    for path in test_paths:
        world.add_test(path)
    facts = raise_facts(world)
    verdicts: Counter[str] = Counter()
    walked: set[tuple[str, int]] = set()

    print("site\tasserted\tverdict\ttargeted-raise\tevidence")
    for path in test_paths:
        module = world.by_path.get(path)
        if module is None:
            continue
        for line, asserted, needle, body in raises_sites(tree, path):
            walked.add((path, line))
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

    # The estate is the authority on what exists, so what the screen did not
    # reach is the estate minus what it walked, rather than a second walk
    # guessing at the shapes it might have missed. That covers a bare call, a
    # context manager entered elsewhere, a tuple of asserted types and a
    # `raises` with no positional argument, without naming any of them.
    for site in Snapshot(tree).sites:
        if site.kind != "match=" or (site.path, site.line) in walked:
            continue
        verdicts["unscreened"] += 1
        print(f"{site.where}\t{site.identity.type_name}\tunscreened\t-\tno `with` body this walk could reach")

    print("\n# verdict totals", file=sys.stderr)
    for name, count in sorted(verdicts.items()):
        print(f"# {name}\t{count}", file=sys.stderr)
    print(f"# total\t{sum(verdicts.values())}", file=sys.stderr)


def injected_markers(tree: Tree, path: str) -> list[str]:
    """Every string a test module hands to an exception constructor.

    These are markers the test wrote, not prose the repository ships, so a
    `match=` against one proves the injected failure is the one observed rather
    than pinning anything authored.
    """
    out: list[str] = []
    for node in ast.walk(tree.parse(path)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if EXCEPTION_NAME.search(call_name(node)):
            text = template(node.args[0])
            if text:
                out.append(text)
    return out


def shipped_strings(tree: Tree) -> str:
    """Every string literal production can emit, joined for substring tests.

    Comments and docstrings are excluded deliberately. The question this
    answers is whether a needle pins prose the code SAYS, and a phrase that
    appears only in a comment about the behavior is not that.
    """
    parts: list[str] = []
    for path in tree.files(PROD_ROOT):
        module = tree.parse(path)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(module)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(module):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                parts.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                parts.extend(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return "\n".join(parts)


def injected(tree: Tree) -> None:
    """Report every `match=` site whose needle is a marker its own test wrote.

    A needle that ALSO appears in production is excluded, however it reaches
    the test: a fake copying a shipped sentence, or a test-side `AssertionError`
    guard that happens to quote one, is pinning our prose through a longer
    route, which is what the batch deletes. What survives is a phrase that
    exists nowhere but the test, so the assertion can only be proof that the
    injected failure is the observed one.
    """
    shipped = shipped_strings(tree)
    hits = 0
    print("identity\tsite\tneedle\tinjected-string")
    for path in tree.files(TEST_ROOT):
        markers = injected_markers(tree, path)
        if not markers:
            continue
        for site in sites_in(tree, path):
            if site.needle == "<expr>" or site.needle in shipped:
                continue
            source = next((m for m in markers if site.needle in m), None)
            if source is not None:
                hits += 1
                print(f"{site.identity}\t{site.where}\t{site.needle}\t{source}")
    print(f"\n# injected-marker sites: {hits}", file=sys.stderr)
