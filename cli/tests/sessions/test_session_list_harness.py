"""The HARNESS column on ``session list`` and the shared table render.

``session list`` resolves every session's template to its concrete
harness name (a config-only, no-SSH derivation) and shows it in a
HARNESS column between TEMPLATE and MODE. These pins cover the column
value for the default (``shell``) and a declared ``claude-code``
template, the guard that a template which fails to resolve shows ``-``
without aborting the render, the 20-char truncation the shared
``output.render_table`` helper applies, that ``--no-status`` still shows
the harness, and that ``--names-only`` stays pure (no registry cost).

All tests drive ``no_status=True`` so the listing never touches SSH; the
HARNESS derivation is orthogonal to the STATUS batch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import SessionMode
from agentworks.sessions import manager as session_manager

if TYPE_CHECKING:
    from agentworks.db import Database

CLAUDE_TEMPLATE = """
[session_templates.claude]
harness = "claude-code"
description = "Claude Code session"
"""


def _seed_vm(db: Database, name: str, ws: str) -> None:
    db.insert_vm(name, site="proxmox", hostname=name)
    db.update_vm_tailscale(name, "100.64.0.9")
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
        (ws, name, f"/srv/{ws}", f"ws-{ws}"),
    )
    db._conn.commit()


def _seed_session(db: Database, name: str, ws: str, template: str) -> None:
    db.insert_session(name, ws, template, SessionMode.ADMIN, socket_path=f"/tmp/{name}.sock")


def _header_and_rows(info: list[str]) -> tuple[str, list[str]]:
    """Split the captured info into the table header and its data rows.

    The rule line and the header sit at the top; data rows follow until
    the first blank line (warnings block) or end of output.
    """
    header_idx = next(i for i, line in enumerate(info) if line.startswith("NAME"))
    rows = []
    for line in info[header_idx + 2 :]:
        if line == "":
            break
        rows.append(line)
    return info[header_idx], rows


def test_list_shows_harness_column_between_template_and_mode(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(CLAUDE_TEMPLATE)
    _seed_vm(db, "box", "ws-box")
    _seed_session(db, "s-shell", "ws-box", "default")
    _seed_session(db, "s-claude", "ws-box", "claude")

    session_manager.list_sessions(db, config, no_status=True)

    header, rows = _header_and_rows(captured_output.info)
    # Column order: NAME, WORKSPACE, VM, TEMPLATE, HARNESS, MODE, STATUS.
    assert header.split() == ["NAME", "WORKSPACE", "VM", "TEMPLATE", "HARNESS", "MODE", "STATUS"]
    by_name = {row.split()[0]: row.split() for row in rows}
    # The default (undeclared) template resolves to the built-in shell harness.
    assert "shell" in by_name["s-shell"]
    # The declared claude template resolves to its claude-code harness.
    assert "claude-code" in by_name["s-claude"]


def test_list_unresolvable_template_shows_dash_and_still_renders(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output,  # noqa: ANN001
) -> None:
    # A session pointing at a template that is not declared must not
    # abort the listing: its HARNESS cell is "-" and the good row renders.
    config = make_config()
    _seed_vm(db, "box", "ws-box")
    _seed_session(db, "s-good", "ws-box", "default")
    _seed_session(db, "s-bad", "ws-box", "ghost-template")

    session_manager.list_sessions(db, config, no_status=True)

    _header, rows = _header_and_rows(captured_output.info)
    by_name = {row.split()[0]: row.split() for row in rows}
    assert "shell" in by_name["s-good"]
    # TEMPLATE cell is "ghost-template", HARNESS cell falls back to "-".
    assert by_name["s-bad"][4] == "-"


def test_list_truncates_over_cap_values_with_ellipsis(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    long_name = "session-with-a-very-long-name"  # 29 chars, over the 20 cap
    _seed_vm(db, "box", "ws-box")
    _seed_session(db, long_name, "ws-box", "default")

    session_manager.list_sessions(db, config, no_status=True)

    _header, rows = _header_and_rows(captured_output.info)
    assert rows[0].startswith(long_name[:17] + "...")
    assert long_name not in rows[0]


def test_list_no_status_still_shows_harness(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output,  # noqa: ANN001
) -> None:
    # --no-status skips only the SSH STATUS batch; HARNESS is cheap and
    # stays. STATUS renders as "-" for every row.
    config = make_config()
    _seed_vm(db, "box", "ws-box")
    _seed_session(db, "s1", "ws-box", "default")

    session_manager.list_sessions(db, config, no_status=True)

    header, rows = _header_and_rows(captured_output.info)
    assert "HARNESS" in header
    fields = rows[0].split()
    assert "shell" in fields
    assert fields[-1] == "-"


def test_list_resolves_each_distinct_template_at_most_once(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    # Many sessions sharing a template resolve that template once.
    config = make_config()
    _seed_vm(db, "box", "ws-box")
    for i in range(3):
        _seed_session(db, f"s{i}", "ws-box", "default")

    seen: list[str] = []
    real = session_manager._display_harness

    def _counting(registry: object, template_name: str) -> str:
        seen.append(template_name)
        return real(registry, template_name)  # type: ignore[arg-type]

    monkeypatch.setattr(session_manager, "_display_harness", _counting)

    session_manager.list_sessions(db, config, no_status=True)

    assert seen == ["default"]


def test_list_bad_registry_degrades_harness_to_dash_and_still_renders(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    # build_registry runs config validation that can raise for reasons
    # unrelated to session templates (bad secret chain, bad defaults.site,
    # a resource collision). A read-only listing must not abort: the
    # HARNESS column degrades to "-" for every row and the rest renders.
    import agentworks.bootstrap as bootstrap
    from agentworks.errors import ConfigError

    config = make_config()
    _seed_vm(db, "box", "ws-box")
    _seed_session(db, "s1", "ws-box", "default")

    def _boom(_config: object) -> object:
        raise ConfigError("unrelated config problem")

    monkeypatch.setattr(bootstrap, "build_registry", _boom)

    session_manager.list_sessions(db, config, no_status=True)

    header, rows = _header_and_rows(captured_output.info)
    assert "HARNESS" in header
    assert rows[0].split()[0] == "s1"
    assert rows[0].split()[4] == "-"


def test_names_only_stays_pure_and_pays_no_registry_cost(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    # --names-only short-circuits before any harness/registry work: even
    # a build_registry that would blow up leaves the name list intact.
    import agentworks.bootstrap as bootstrap

    config = make_config()
    _seed_vm(db, "box", "ws-box")
    _seed_session(db, "s1", "ws-box", "default")

    def _boom(_config: object) -> object:
        raise AssertionError("build_registry must not run under --names-only")

    monkeypatch.setattr(bootstrap, "build_registry", _boom)

    session_manager.list_sessions(db, config, no_status=True, names_only=True)

    assert captured_output.info == ["s1"]
