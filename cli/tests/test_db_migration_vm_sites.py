"""Migration fixtures against prior schemas.

Migration v27 (the vm-site refactor): fixture databases at the prior
schema covering all four platforms plus remote-Lima rows, asserting the
platform_metadata / hostname backfills, the platform -> site rename, the
printed site-manifest snippets, the NOT NULL table rebuild, the
empty-legacy behavior, and the settings table.

Migration v28 (the workspaces.last_seen_at drop): a v27 fixture with the
column populated and child rows keyed on the workspace name, asserting
the row and its children survive the rebuild, the column is gone, and the
name-based FKs still hold.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from agentworks.db import LATEST_VERSION, MIGRATIONS, Database, MigrationContext

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import CapturedOutput


def _create_v26_db(path: str) -> sqlite3.Connection:
    """A database at schema version 26 (the last pre-vm-sites version)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "    version    INTEGER NOT NULL,"
        "    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        ")"
    )
    for version in range(1, 27):
        step = MIGRATIONS[version]
        assert isinstance(step, str), "pre-27 migrations are all SQL strings"
        for stmt in step.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
    return conn


def _create_v27_db(path: str) -> sqlite3.Connection:
    """A database at schema version 27 (the last version before the
    workspaces.last_seen_at drop).

    Runs migrations 1..26 (SQL) then the v27 Python step exactly as
    ``Database._migrate`` does: FKs off around the rebuild, an empty
    legacy context (so no vms rows are needed), then record the version.
    """
    conn = _create_v26_db(path)
    step = MIGRATIONS[27]
    assert callable(step), "v27 is the Python vm-sites step"
    conn.execute("PRAGMA foreign_keys = OFF")
    step(conn, MigrationContext(legacy={}))
    conn.execute("INSERT INTO schema_version (version) VALUES (27)")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    return conn


def _seed_all_platforms(conn: sqlite3.Connection) -> None:
    """One row per platform, one remote-Lima row, one shadow-named host."""
    conn.execute(
        "INSERT INTO vm_hosts (name, ssh_host) VALUES ('gpu-box', 'me@gpu-box')"
    )
    conn.execute(
        "INSERT INTO vm_hosts (name, ssh_host) VALUES ('wsl2', 'me@wsl2-host')"
    )
    conn.execute(
        "INSERT INTO vm_hosts (name, ssh_host) VALUES ('azure-vm', 'me@az-box')"
    )
    conn.executescript("""
        INSERT INTO vms (name, platform, admin_username)
            VALUES ('lvm', 'lima', 'admin');
        INSERT INTO vms (name, platform, admin_username, vm_host_name)
            VALUES ('rvm', 'lima', 'admin', 'gpu-box');
        INSERT INTO vms (name, platform, admin_username, vm_host_name)
            VALUES ('zvm', 'lima', 'admin', 'azure-vm');
        INSERT INTO vms (name, platform, admin_username, vm_host_name)
            VALUES ('rvm2', 'lima', 'admin', 'gpu-box');
        INSERT INTO vms (name, platform, admin_username, vm_host_name)
            VALUES ('svm', 'lima', 'admin', 'wsl2');
        INSERT INTO vms (name, platform, admin_username, wsl_distro_name)
            VALUES ('wvm', 'wsl2', 'admin', 'wvm');
        INSERT INTO vms (name, platform, admin_username, azure_resource_id)
            VALUES ('avm', 'azure', 'admin', '/subscriptions/s/rg/r/p/vm/avm');
        INSERT INTO vms (name, platform, admin_username, proxmox_vmid)
            VALUES ('pvm', 'proxmox', 'admin', '104');
    """)
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _empty_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no readable operator config (tests override as needed)."""
    monkeypatch.setattr("agentworks.db._load_legacy_toml", dict)


def _migrate(tmp_path: Path) -> Database:
    db_path = tmp_path / "m27.db"
    _seed_all_platforms(_create_v26_db(str(db_path)))
    return Database(db_path)


def test_backfill_and_site_rename(tmp_path: Path, captured_output: CapturedOutput) -> None:
    db = _migrate(tmp_path)
    try:
        by_name = {vm.name: vm for vm in db.list_vms()}

        # Site: local lima rows land on the renamed lima-local bundled
        # site; the other legacy names are unchanged; the host name (or
        # its '-host'-suffixed form on reserved-name shadowing) for
        # remote-Lima rows.
        assert by_name["lvm"].site == "lima-local"
        assert by_name["rvm"].site == "gpu-box"
        assert by_name["rvm2"].site == "gpu-box"
        assert by_name["svm"].site == "wsl2-host"
        # A host literally named after the renamed platform shadows the
        # NEW reserved name and gets the suffix.
        assert by_name["zvm"].site == "azure-vm-host"
        assert by_name["wvm"].site == "wsl2"
        assert by_name["avm"].site == "azure"
        assert by_name["pvm"].site == "proxmox"

        # platform_metadata via the owning platform's hook.
        assert by_name["lvm"].platform_metadata == {"instance_name": "lvm"}
        assert by_name["rvm"].platform_metadata == {"instance_name": "rvm"}
        assert by_name["wvm"].platform_metadata == {"distro_name": "wvm"}
        assert by_name["avm"].platform_metadata == {
            "resource_id": "/subscriptions/s/rg/r/p/vm/avm"
        }
        # Empty legacy context: proxmox node omitted, never guessed.
        assert by_name["pvm"].platform_metadata == {"vmid": "104"}

        # Hostname backfill uses the PRE-rename platform value (the
        # hostname the create-time bootstrap actually set). The azure
        # row is the load-bearing case: the platform is azure-vm NOW,
        # but the VM's real hostname was set under the legacy name;
        # a "cleanup" to registry keys would break exactly this.
        assert by_name["lvm"].hostname == "lima--lvm"
        assert by_name["rvm"].hostname == "lima--rvm"
        assert by_name["avm"].hostname == "azure--avm"
        assert by_name["pvm"].hostname == "proxmox--pvm"

        assert all(not vm.operator_stopped for vm in by_name.values())
    finally:
        db.close()


def test_table_rebuild_shape(tmp_path: Path, captured_output: CapturedOutput) -> None:
    db = _migrate(tmp_path)
    try:
        info = {
            row[1]: row for row in db._conn.execute("PRAGMA table_info(vms)")
        }
        for legacy in ("platform", "vm_host_name", "azure_resource_id",
                       "wsl_distro_name", "proxmox_vmid"):
            assert legacy not in info
        assert info["hostname"][3] == 1  # NOT NULL
        assert info["site"][3] == 1
        assert info["platform_metadata"][3] == 1
        assert info["operator_stopped"][3] == 1

        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "vm_hosts" not in tables
        assert "settings" in tables
    finally:
        db.close()


def test_prints_site_manifest_snippets(
    tmp_path: Path, captured_output: CapturedOutput
) -> None:
    db = _migrate(tmp_path)
    try:
        # Everything on the warn channel (stderr): migrations run at
        # every Database() open, including under stdout-capturing
        # completion helpers, so stdout must stay clean.
        assert captured_output.info == []
        joined = "\n".join(captured_output.warnings)
        # One snippet per distinct host-named site (two gpu-box VMs,
        # one snippet), carrying the host's ssh target.
        assert joined.count("name: gpu-box") == 1
        assert "vm_host: me@gpu-box" in joined
        assert "name: wsl2-host" in joined
        assert "vm_host: me@wsl2-host" in joined
        # The shadow-name suffix is called out.
        assert "'wsl2' shadows a platform name" in joined
    finally:
        db.close()


def test_proxmox_node_from_legacy_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agentworks.db._load_legacy_toml",
        lambda: {"proxmox": {"node": "pve1"}},
    )
    db = _migrate(tmp_path)
    try:
        pvm = db.get_vm("pvm")
        assert pvm is not None
        assert pvm.platform_metadata == {"vmid": "104", "node": "pve1"}
    finally:
        db.close()


def test_unknown_platform_fails_loudly_and_retry_works_after_fix(
    tmp_path: Path, captured_output: CapturedOutput
) -> None:
    """The loud failure fires BEFORE any DDL, so the DB stays at v26
    and reopening after the operator fixes the corrupt row succeeds
    (a post-DDL failure would brick every retry on duplicate-column).
    """
    db_path = tmp_path / "bad.db"
    conn = _create_v26_db(str(db_path))
    conn.execute(
        "INSERT INTO vms (name, platform, admin_username) "
        "VALUES ('xvm', 'mystery', 'admin')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="unknown platform 'mystery'"):
        Database(db_path)

    fix = sqlite3.connect(str(db_path))
    fix.execute("UPDATE vms SET platform = 'lima' WHERE name = 'xvm'")
    fix.commit()
    fix.close()

    db = Database(db_path)
    try:
        vm = db.get_vm("xvm")
        assert vm is not None
        assert vm.site == "lima-local"
        assert vm.platform_metadata == {"instance_name": "xvm"}
    finally:
        db.close()


def test_remote_lima_site_name_collision_fails_loudly(tmp_path: Path) -> None:
    """A '-host'-suffixed site landing on another real host's name
    would silently merge two hosts; the pre-DDL scan refuses instead.
    """
    db_path = tmp_path / "clash.db"
    conn = _create_v26_db(str(db_path))
    conn.executescript("""
        INSERT INTO vm_hosts (name, ssh_host) VALUES ('wsl2', 'me@a');
        INSERT INTO vm_hosts (name, ssh_host) VALUES ('wsl2-host', 'me@b');
        INSERT INTO vms (name, platform, admin_username, vm_host_name)
            VALUES ('v1', 'lima', 'admin', 'wsl2');
        INSERT INTO vms (name, platform, admin_username, vm_host_name)
            VALUES ('v2', 'lima', 'admin', 'wsl2-host');
    """)
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="site name collision"):
        Database(db_path)


def test_earlier_versions_checkpoint_when_a_later_one_fails(
    tmp_path: Path,
) -> None:
    """Per-version commit checkpoints (Phase 6 hardening): when v27's
    pre-DDL scan raises on a multi-version jump, the versions that
    already ran stay recorded, so retry resumes at v27 instead of
    re-running earlier DDL into duplicate-column errors."""
    db_path = tmp_path / "jump.db"
    # A v25 fixture: two migrations (26, 27) must run on open.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "    version    INTEGER NOT NULL,"
        "    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        ")"
    )
    for version in range(1, 26):
        step = MIGRATIONS[version]
        assert isinstance(step, str)
        for stmt in step.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.execute(
        "INSERT INTO vms (name, platform, admin_username) "
        "VALUES ('xvm', 'mystery', 'admin')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="unknown platform 'mystery'"):
        Database(db_path)

    # v26 checkpointed despite v27's failure.
    check = sqlite3.connect(str(db_path))
    (recorded,) = check.execute("SELECT MAX(version) FROM schema_version").fetchone()
    check.close()
    assert recorded == 26

    # Fix the row; retry resumes at v27 and completes.
    fix = sqlite3.connect(str(db_path))
    fix.execute("UPDATE vms SET platform = 'lima' WHERE name = 'xvm'")
    fix.commit()
    fix.close()
    db = Database(db_path)
    try:
        vm = db.get_vm("xvm")
        assert vm is not None
        assert vm.site == "lima-local"
    finally:
        db.close()


def test_fresh_database_lands_on_the_new_schema(tmp_path: Path) -> None:
    """A brand-new DB (no fixture) runs 1..28 cleanly end to end."""
    db = Database(tmp_path / "fresh.db")
    try:
        db.insert_vm("v", site="lima-local", hostname="lima--v")
        vm = db.get_vm("v")
        assert vm is not None
        assert vm.site == "lima-local"
    finally:
        db.close()


def test_v28_drops_workspace_last_seen_at_and_preserves_rows(
    tmp_path: Path,
) -> None:
    """Migration 28 rebuilds workspaces without last_seen_at. A v27 row
    with the column populated, plus child rows keyed on workspaces.name (a
    session and an explicit grant, via an agent), survive the rebuild
    intact; the column is gone, the name-based FKs still hold, and the
    version advances to 28.
    """
    db_path = tmp_path / "m28.db"
    conn = _create_v27_db(str(db_path))
    conn.execute(
        "INSERT INTO vms (name, site, hostname) VALUES ('box', 'lima-local', 'lima--box')"
    )
    conn.execute(
        "INSERT INTO workspaces "
        "(name, vm_name, template, workspace_path, linux_group, created_at, last_seen_at) "
        "VALUES ('ws1', 'box', 'default', '/srv/ws1', 'ws-ws1', "
        "'2020-01-01T00:00:00Z', '2021-06-06T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('a1', 'box', 'agt-a1')"
    )
    conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    conn.execute(
        "INSERT INTO agent_workspace_grants (agent_name, workspace_name, grant_type) "
        "VALUES ('a1', 'ws1', 'explicit')"
    )
    conn.commit()
    conn.close()

    db = Database(db_path)  # opening runs migration 28
    try:
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(workspaces)")}
        assert "last_seen_at" not in cols

        ws = db.get_workspace("ws1")
        assert ws is not None
        assert ws.workspace_path == "/srv/ws1"
        assert ws.created_at == "2020-01-01T00:00:00Z"
        assert ws.linux_group == "ws-ws1"

        # Child rows keyed on workspaces.name survived the rebuild.
        assert db.get_session("s1") is not None
        assert db.has_any_grant("a1", "ws1")

        # No dangling FKs, and the version advanced to the latest (opening
        # the DB always migrates all the way forward).
        assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        (version,) = db._conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        assert version == LATEST_VERSION
    finally:
        db.close()
