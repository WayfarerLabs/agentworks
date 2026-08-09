"""Drift guard: every command path that reaches a declarable-consuming runner
gates its recipe first. The gate must remain beside each consumption site so a
new caller cannot silently bypass disabled-resource enforcement.

The recipe gate (``ensure_recipe_enabled``) sits at each COMMAND ENTRY, not at
the runners (``_run_install_commands`` / ``_run_agent_install_commands``) nor at
the choreography (``create_agent_on_vm`` / ``realize_agent``), which are never
themselves gated. The runner call graph is multi-hop and fans out, so a shallow
"the immediate caller of the runner gates" check would pass while a hole stays
open (notably the ``session create --new-agent`` path, whose realize happens in
``_realize_ephemerals`` while the gate lives in ``_build_session_graph``). This
guard is therefore two structural assertions, mirroring the
capability-switchboard drift guard (which asserts every switchboard site derives
from the descriptor table) and the harness-integration-factory-caller pattern:

1. The CALLER SETS of the two runners and the two choreography functions are
   exactly the enumerated set, so a NEW caller of any of them fails the test
   until its command entry is added here and gated.
2. Each of the six COMMAND ENTRIES calls ``ensure_recipe_enabled``, and
   the two entries that call the realize/init function in-body gate BEFORE it.
   ``resume_session`` is entry-only: its gate guards the restart/reattach
   recipe merge and it sits on NO runner chain (restart re-runs no install
   commands), so the caller-set walk cannot anchor it and gate PRESENCE is
   its whole pin.

If you legitimately add a new caller (or a new entry command), update the
enumerated sets here AND add the entry's recipe gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import agentworks

_ROOT = Path(agentworks.__file__).parent

# The runners (declarable consumers) and the choreography that reaches them,
# walked FULLY back to a command entry on every chain (a one-hop-short guard
# would miss a future `vm repair` calling `run_initialization`, or a new session
# path calling `_realize_ephemerals`). A new caller of ANY of these fails the
# test until its chain terminates at a gated command entry below. The terminal
# entries are `create_vm` / `reinit_vm` (gate directly) and `create_session`
# (whose build phase `_build_session_graph` gates).
_EXPECTED_CALLERS: dict[str, set[str]] = {
    # agent-install runner chain -> create_agent / reinit_agent (+ session --new-agent)
    "_run_agent_install_commands": {"create_agent_on_vm"},
    "create_agent_on_vm": {"realize_agent", "reinit_agent"},
    "realize_agent": {"create_agent", "_realize_ephemerals"},
    "_realize_ephemerals": {"_roll_forward"},
    "_roll_forward": {"create_session"},
    # vm-install runner chain -> create_vm / reinit_vm
    "_run_install_commands": {"_phase_b_setup"},
    "_phase_b_setup": {"run_initialization"},
    "run_initialization": {"create_vm", "reinit_vm"},
}

# The gate-bearing functions (each must call `ensure_recipe_enabled`) and the
# realize/init call each must gate BEFORE (None = the realize/init happens
# downstream, so gate-PRESENCE plus the caller-set walk above is the protection).
# The session entry `create_session` is NOT here: its gate lives one function
# deeper, in `_build_session_graph`, which it calls; the caller-set walk pins
# that the runner chain reaches `create_session`, and `_build_session_graph`
# gates.
_ENTRY_GATES: dict[str, str | None] = {
    "create_vm": "run_initialization",
    "reinit_vm": "run_initialization",
    "create_agent": "realize_agent",
    "reinit_agent": "create_agent_on_vm",
    "_build_session_graph": None,  # session create + --new-agent
    "resume_session": None,  # session resume / reattach; no runner chain (see docstring)
}

_GATE = "ensure_recipe_enabled"


def _aliases(tree: ast.AST, wanted: set[str]) -> set[str]:
    """``wanted`` plus any ``from ... import x as y`` local aliases of them, so
    an aliased import cannot dodge the scan."""
    names = set(wanted)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in wanted and alias.asname:
                    names.add(alias.asname)
    return names


def _called_name(call: ast.Call, local: set[str]) -> str | None:
    func = call.func
    if isinstance(func, ast.Name) and func.id in local:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in local:
        return func.attr
    return None


def _scan() -> tuple[dict[str, set[str]], dict[str, dict[str, int]]]:
    """Return ``(caller_sets, entry_calls)``.

    ``caller_sets[target]`` is the set of function names whose OWN body calls
    ``target``. ``entry_calls[func]`` maps a watched call name to the first line
    it is called at within ``func`` (for the gate-before-realize ordering)."""
    watched = set(_EXPECTED_CALLERS) | {_GATE} | {v for v in _ENTRY_GATES.values() if v is not None}
    caller_sets: dict[str, set[str]] = {t: set() for t in _EXPECTED_CALLERS}
    entry_calls: dict[str, dict[str, int]] = {}

    def visit(node: ast.AST, current: str | None, local: set[str]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            current = node.name
        elif isinstance(node, ast.Call) and current is not None:
            name = _called_name(node, local)
            if name is not None:
                if name in caller_sets:
                    caller_sets[name].add(current)
                if current in _ENTRY_GATES:
                    entry_calls.setdefault(current, {}).setdefault(name, node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, current, local)

    for path in _ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        visit(tree, None, _aliases(tree, watched))
    return caller_sets, entry_calls


def test_runner_caller_sets_are_exactly_the_enumerated_entries() -> None:
    caller_sets, _ = _scan()
    assert caller_sets == _EXPECTED_CALLERS, (
        "the caller set of a recipe-runner / choreography function drifted from HEAD. A NEW caller "
        "means a new path can reach a declarable runner; add a gated command entry and update "
        f"_EXPECTED_CALLERS.\nexpected: {_EXPECTED_CALLERS}\nactual:   {caller_sets}"
    )


def test_every_command_entry_gates_its_recipe() -> None:
    _, entry_calls = _scan()
    for entry, realize in _ENTRY_GATES.items():
        calls = entry_calls.get(entry, {})
        assert _GATE in calls, (
            f"command entry {entry!r} does not call {_GATE}; a disabled plugin's declarable "
            "resource could be consumed ungated on this path."
        )
        if realize is not None:
            assert realize in calls, f"expected {entry!r} to call {realize!r} (the scan drifted from HEAD)"
            assert calls[_GATE] < calls[realize], (
                f"{entry!r} must call {_GATE} BEFORE {realize!r} so the gate refuses before any realize/init work."
            )


def test_guard_is_not_vacuous() -> None:
    """The scan must actually find the enumerated callers and entries, so a
    rename or import change that makes it see nothing fails loudly here."""
    caller_sets, entry_calls = _scan()
    assert all(caller_sets[t] for t in _EXPECTED_CALLERS), "a caller set came back empty; the scan drifted"
    for entry in _ENTRY_GATES:
        assert _GATE in entry_calls.get(entry, {}), f"entry {entry!r} not found by the scan"
