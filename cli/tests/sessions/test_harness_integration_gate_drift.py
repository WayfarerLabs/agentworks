"""Drift guard: every caller of a session-node factory gates the harness integration (R14).

The harness integration enablement gate (``ensure_harness_integration_enabled``) sits at the session
BUILD CALL SITES, not inside ``pending_session_node`` / ``live_session_node``
(those factories thread no registry, so gating inside them would mean threading
a registry through, which is not additive). That places a standing risk: a
future third caller of either factory could construct a session on a disabled
plugin harness integration without gating it.

This guard is the analog of the ``CAPABILITY_ADAPTERS.keys()`` adapter-drift
test: it must FAIL when a real bypass is introduced. The protection is
per-FUNCTION, not per-file: every function whose body calls a session-node
factory must also call ``ensure_harness_integration_enabled`` within that same function body.
A per-file substring check would be defeated by a second, ungated factory call
in a file that already gates elsewhere; a per-function check is not. It also
resolves aliased imports (``from ... import live_session_node as lsn``), so an
alias cannot dodge the scan. A per-file count-equality assertion backs it up,
catching a second ungated call added to a function that already gates once.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import agentworks

_ROOT = Path(agentworks.__file__).parent
_FACTORIES = frozenset({"pending_session_node", "live_session_node"})
_GATE = frozenset({"ensure_harness_integration_enabled"})


@dataclass
class _FuncReport:
    name: str
    lineno: int
    calls_factory: bool = False
    calls_gate: bool = False


def _local_names(tree: ast.AST, wanted: frozenset[str]) -> frozenset[str]:
    """Local names bound to any name in ``wanted`` by a ``from ... import`` (bare
    or ``as``-aliased), plus the bare names themselves (an attribute-style call
    ``nodes.live_session_node`` is matched on the attribute name instead)."""
    names = set(wanted)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in wanted and alias.asname:
                    names.add(alias.asname)
    return frozenset(names)


def _is_call_to(call: ast.Call, local_names: frozenset[str], attrs: frozenset[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in local_names
    if isinstance(func, ast.Attribute):
        return func.attr in attrs
    return False


def _per_function_reports(source: str) -> list[_FuncReport]:
    """One :class:`_FuncReport` per function in ``source``, recording whether its
    OWN body (innermost scope, so a nested closure is its own function) calls a
    session-node factory and whether it calls the harness integration gate."""
    tree = ast.parse(source)
    factory_names = _local_names(tree, _FACTORIES)
    gate_names = _local_names(tree, _GATE)
    reports: list[_FuncReport] = []

    def visit(node: ast.AST, current: _FuncReport | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            current = _FuncReport(name=node.name, lineno=node.lineno)
            reports.append(current)
        elif isinstance(node, ast.Call) and current is not None:
            if _is_call_to(node, factory_names, _FACTORIES):
                current.calls_factory = True
            if _is_call_to(node, gate_names, _GATE):
                current.calls_gate = True
        for child in ast.iter_child_nodes(node):
            visit(child, current)

    visit(tree, None)
    return reports


def _call_counts(source: str) -> tuple[int, int]:
    """Module-wide ``(factory-call count, gate-call count)``."""
    tree = ast.parse(source)
    factory_names = _local_names(tree, _FACTORIES)
    gate_names = _local_names(tree, _GATE)
    factory = gate = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            factory += _is_call_to(node, factory_names, _FACTORIES)
            gate += _is_call_to(node, gate_names, _GATE)
    return factory, gate


def test_every_session_factory_caller_gates_the_harness_integration() -> None:
    offenders: list[str] = []
    count_offenders: list[str] = []
    total_factory_calls = 0
    for path in _ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "session_node" not in source:  # cheap pre-filter; the AST below is the truth
            continue
        rel = path.relative_to(_ROOT).as_posix()
        for report in _per_function_reports(source):
            if report.calls_factory:
                total_factory_calls += 1
                if not report.calls_gate:
                    offenders.append(f"{rel}:{report.name} (line {report.lineno})")
        factory_count, gate_count = _call_counts(source)
        if factory_count and gate_count < factory_count:
            count_offenders.append(f"{rel}: {factory_count} factory call(s) but only {gate_count} gate call(s)")

    assert not offenders, (
        "a function builds a session node without gating the harness integration in the SAME function body. Call "
        "ensure_harness_integration_enabled(registry, template.harness_integration) before the factory call (R14, see "
        "sessions/nodes.py):\n" + "\n".join(offenders)
    )
    assert not count_offenders, (
        "a module has more session-node factory calls than harness-integration-gate calls; a second call may be "
        "ungated (each factory call must be preceded by its own gate):\n" + "\n".join(count_offenders)
    )
    # Non-vacuity: the two known build sites must still be found, so a factory
    # rename or import change that makes the scan see nothing fails loudly.
    assert total_factory_calls >= 2, (
        "the drift guard found fewer than the two known session-node factory call sites; the scan has "
        "drifted from HEAD (factory rename, import change?) and is no longer protecting anything."
    )


def test_guard_is_not_vacuous() -> None:
    """The per-function detector flags an ungated caller and stays quiet on a
    gated one, and resolves an aliased import, so a passing guard is watching."""
    ungated = "from agentworks.sessions.nodes import live_session_node\ndef f():\n    live_session_node(x)\n"
    (report,) = _per_function_reports(ungated)
    assert report.calls_factory and not report.calls_gate

    gated = (
        "from agentworks.sessions.nodes import live_session_node\n"
        "from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled\n"
        "def f():\n    ensure_harness_integration_enabled(r, t.harness_integration)\n    live_session_node(x)\n"
    )
    (report2,) = _per_function_reports(gated)
    assert report2.calls_factory and report2.calls_gate

    aliased = "from agentworks.sessions.nodes import live_session_node as lsn\ndef f():\n    lsn(x)\n"
    (report3,) = _per_function_reports(aliased)
    assert report3.calls_factory and not report3.calls_gate  # alias resolved, still counted
