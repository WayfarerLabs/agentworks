"""Phase 3b tests: per-kind ``instances(db, registry, resource)`` hook.

The hook projects "what live DB instances depend on this Resource per
the current config?" -- the dynamic dimension that backs the USED BY
column and the ``Used by:`` describe section. This module covers:

- Template kinds (vm_template, agent_template, workspace_template,
  session_template, admin_template, named_console_template): DB-row
  count by template name. Defaults-NULL-as-default semantics for the
  kinds whose DB column allows NULL.
- ``secret``: per-session subgraph walk via ``collect_secrets_for``,
  emits InstanceRef per session whose subgraph reaches the secret
  (env-block, system secret, git-credential paths all covered).
- Kinds with no instance concept (catalog, providers, backends):
  inherit the default-empty fallback in ``inspect._count_used_by``;
  list rows render ``USED BY`` as ``-``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.db import Database, SessionMode
from agentworks.resources import Registry


def _write_base(config_path: Path, *, extras: str = "") -> None:
    pub = config_path.parent / "id.pub"
    priv = config_path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + dedent(extras),
    )


def _seed_basic(tmp_path: Path) -> tuple[Database, Registry]:
    """Two VMs, two workspaces, two agents, two sessions. Two vm_templates
    (``default`` + ``custom``); one VM on each. All other rows pick up
    ``default`` (or NULL on the optional template column).
    """
    cfg = tmp_path / "config.toml"
    _write_base(
        cfg,
        extras="""
        [vm_templates.custom]
        cpus = 4
        """,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "test.db")
    db.insert_vm("vm-default", platform="lima", template=None)
    db.insert_vm("vm-custom", platform="lima", template="custom")
    db.insert_workspace(
        "ws-a", workspace_path="/tmp/ws-a", vm_name="vm-default", linux_group="ws-ws-a"
    )
    db.insert_workspace(
        "ws-b", workspace_path="/tmp/ws-b", vm_name="vm-custom", linux_group="ws-ws-b"
    )
    db.insert_agent("agent-a", "vm-default", "agt-agent-a")
    db.insert_agent("agent-b", "vm-custom", "agt-agent-b")
    db.insert_session(
        "sess-a", "ws-a", template="default", mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-a.sock",
    )
    db.insert_session(
        "sess-b", "ws-b", template="default", mode=SessionMode.AGENT,
        agent_name="agent-b", socket_path="/tmp/sess-b.sock",
    )
    db._conn.commit()
    return db, registry


# -- Template kinds ---------------------------------------------------------


def test_vm_template_instances_counts_matching_vms(tmp_path: Path) -> None:
    db, registry = _seed_basic(tmp_path)
    from agentworks.resources import KIND_REGISTRY

    vm_default = registry.lookup("vm_template", "default")
    vm_custom = registry.lookup("vm_template", "custom")

    default_instances = list(
        KIND_REGISTRY["vm_template"].instances(db, registry, vm_default)
    )
    custom_instances = list(
        KIND_REGISTRY["vm_template"].instances(db, registry, vm_custom)
    )

    # vm-default has template=NULL (defaults to ``default``); vm-custom
    # explicitly uses ``custom``.
    assert {r.instance_name for r in default_instances} == {"vm-default"}
    assert {r.instance_name for r in custom_instances} == {"vm-custom"}
    assert all(r.instance_kind == "vm" for r in default_instances)
    assert all(r.instance_kind == "vm" for r in custom_instances)


def test_workspace_template_instances_counts_matching_workspaces(
    tmp_path: Path,
) -> None:
    db, registry = _seed_basic(tmp_path)
    from agentworks.resources import KIND_REGISTRY

    ws_default = registry.lookup("workspace_template", "default")
    instances = list(
        KIND_REGISTRY["workspace_template"].instances(db, registry, ws_default)
    )
    # Both workspaces are NULL-template; both fall back to ``default``.
    assert {r.instance_name for r in instances} == {"ws-a", "ws-b"}
    assert all(r.instance_kind == "workspace" for r in instances)


def test_agent_template_instances_counts_matching_agents(tmp_path: Path) -> None:
    db, registry = _seed_basic(tmp_path)
    from agentworks.resources import KIND_REGISTRY

    agent_default = registry.lookup("agent_template", "default")
    instances = list(
        KIND_REGISTRY["agent_template"].instances(db, registry, agent_default)
    )
    # Both agents are NULL-template; both fall back to ``default``.
    assert {r.instance_name for r in instances} == {"agent-a", "agent-b"}


def test_session_template_instances_counts_matching_sessions(
    tmp_path: Path,
) -> None:
    db, registry = _seed_basic(tmp_path)
    from agentworks.resources import KIND_REGISTRY

    sess_default = registry.lookup("session_template", "default")
    instances = list(
        KIND_REGISTRY["session_template"].instances(db, registry, sess_default)
    )
    # SessionRow.template is non-optional; both sessions explicitly use
    # ``default`` so the NULL-fallback path doesn't apply.
    assert {r.instance_name for r in instances} == {"sess-a", "sess-b"}


def test_admin_template_instances_counts_every_vm(tmp_path: Path) -> None:
    """Every VM uses the singleton admin_template:default. The kind is
    plurified at the framework level (Phase 2a.3) but the operator
    surface is still singleton, so any name other than ``default``
    yields no instances.
    """
    db, registry = _seed_basic(tmp_path)
    from agentworks.resources import KIND_REGISTRY

    admin = registry.lookup("admin_template", "default")
    instances = list(
        KIND_REGISTRY["admin_template"].instances(db, registry, admin)
    )
    assert {r.instance_name for r in instances} == {"vm-default", "vm-custom"}


def test_named_console_template_instances_counts_every_console(
    tmp_path: Path,
) -> None:
    """Same shape as admin_template: every console uses the singleton."""
    db, registry = _seed_basic(tmp_path)
    db._conn.execute(
        "INSERT INTO consoles (name, vm_name) VALUES ('con-a', 'vm-default')"
    )
    db._conn.execute(
        "INSERT INTO consoles (name, vm_name) VALUES ('con-b', 'vm-custom')"
    )
    db._conn.commit()
    from agentworks.resources import KIND_REGISTRY

    nc = registry.lookup("named_console_template", "default")
    instances = list(
        KIND_REGISTRY["named_console_template"].instances(db, registry, nc)
    )
    assert {r.instance_name for r in instances} == {"con-a", "con-b"}


# -- Secret kind ------------------------------------------------------------


def test_secret_instances_finds_sessions_via_admin_env(tmp_path: Path) -> None:
    """A secret referenced from ``[admin.env]`` reaches every session
    (every VM has an admin pulling from the admin template, so every
    session implicitly reaches admin's env-block references).
    """
    cfg = tmp_path / "config.toml"
    _write_base(
        cfg,
        extras="""
        [admin.env]
        API_KEY = { secret = "shared-key" }
        """,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "test.db")
    db.insert_vm("vm-1", platform="lima")
    db.insert_workspace(
        "ws-1", workspace_path="/tmp/ws-1", vm_name="vm-1", linux_group="ws-ws-1"
    )
    db.insert_session(
        "sess-1", "ws-1", template="default", mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-1.sock",
    )
    db._conn.commit()

    from agentworks.resources import KIND_REGISTRY

    secret = registry.lookup("secret", "shared-key")
    instances = list(KIND_REGISTRY["secret"].instances(db, registry, secret))
    assert [r.instance_name for r in instances] == ["sess-1"]
    assert instances[0].instance_kind == "session"


def test_secret_instances_finds_sessions_via_vm_template_env(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    _write_base(
        cfg,
        extras="""
        [vm_templates.prod]
        cpus = 8

        [vm_templates.prod.env]
        DB_TOKEN = { secret = "prod-db-token" }
        """,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "test.db")
    db.insert_vm("vm-prod", platform="lima", template="prod")
    db.insert_vm("vm-default", platform="lima")
    db.insert_workspace(
        "ws-prod", workspace_path="/tmp/ws-prod", vm_name="vm-prod", linux_group="ws-ws-prod"
    )
    db.insert_workspace(
        "ws-default", workspace_path="/tmp/ws-default", vm_name="vm-default",
        linux_group="ws-ws-default",
    )
    db.insert_session(
        "sess-prod", "ws-prod", template="default", mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-prod.sock",
    )
    db.insert_session(
        "sess-default", "ws-default", template="default", mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-default.sock",
    )
    db._conn.commit()

    from agentworks.resources import KIND_REGISTRY

    secret = registry.lookup("secret", "prod-db-token")
    instances = list(KIND_REGISTRY["secret"].instances(db, registry, secret))
    # Only sess-prod's VM uses the prod template -> only it reaches this secret.
    assert {r.instance_name for r in instances} == {"sess-prod"}


def test_secret_instances_finds_sessions_via_tailscale_system_secret(
    tmp_path: Path,
) -> None:
    """The framework auto-declares ``tailscale-auth-key`` via the
    vm_template's system-secret reference. Every session whose VM uses a
    vm_template needing tailscale reaches the auto-declared secret.
    """
    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "test.db")
    db.insert_vm("vm-1", platform="lima")
    db.insert_workspace(
        "ws-1", workspace_path="/tmp/ws-1", vm_name="vm-1", linux_group="ws-ws-1"
    )
    db.insert_session(
        "sess-1", "ws-1", template="default", mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-1.sock",
    )
    db._conn.commit()

    from agentworks.resources import KIND_REGISTRY

    ts = registry.lookup("secret", "tailscale-auth-key")
    instances = list(KIND_REGISTRY["secret"].instances(db, registry, ts))
    assert [r.instance_name for r in instances] == ["sess-1"]


def test_secret_instances_empty_when_no_session_reaches_it(
    tmp_path: Path,
) -> None:
    """An operator-declared secret that no env block / template
    references has zero ``Used by`` entries -- the dead-secret signal.
    """
    cfg = tmp_path / "config.toml"
    _write_base(
        cfg,
        extras="""
        [secrets.dead-key]
        description = "Declared but nothing references it"

        [secret_config]
        backends = ["env-var"]
        """,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "test.db")
    db.insert_vm("vm-1", platform="lima")
    db.insert_workspace(
        "ws-1", workspace_path="/tmp/ws-1", vm_name="vm-1", linux_group="ws-ws-1"
    )
    db.insert_session(
        "sess-1", "ws-1", template="default", mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-1.sock",
    )
    db._conn.commit()

    from agentworks.resources import KIND_REGISTRY

    dead = registry.lookup("secret", "dead-key")
    instances = list(KIND_REGISTRY["secret"].instances(db, registry, dead))
    assert instances == []


# -- Kinds with no instance concept -----------------------------------------


# -- CLI surface ------------------------------------------------------------


def test_list_resources_populates_used_by_count_when_db_provided(
    tmp_path: Path,
) -> None:
    """``list_resources(registry, db)`` populates per-row
    ``used_by_count``. With ``db=None`` (or unset), the field is
    ``None`` for every row -- the list renderer would show ``-``.
    """
    db, registry = _seed_basic(tmp_path)
    from agentworks.resources.inspect import list_resources

    listing = list_resources(registry, db, kinds=("vm_template",))
    by_name = {row.name: row for row in listing.rows}
    assert by_name["default"].used_by_count == 1  # vm-default
    assert by_name["custom"].used_by_count == 1  # vm-custom

    # Without db, the field stays None (renderer shows ``-``).
    listing_no_db = list_resources(registry, None, kinds=("vm_template",))
    assert all(row.used_by_count is None for row in listing_no_db.rows)


def test_describe_resource_populates_used_by_when_db_provided(
    tmp_path: Path,
) -> None:
    """``describe_resource(registry, kind, name, db=...)`` populates the
    ``used_by`` tuple. Without ``db``, ``used_by`` stays ``None`` and the
    describe renderer omits the section.
    """
    db, registry = _seed_basic(tmp_path)
    from agentworks.resources.inspect import describe_resource

    desc = describe_resource(registry, "vm_template", "custom", db=db)
    assert desc.used_by is not None
    assert {r.instance_name for r in desc.used_by} == {"vm-custom"}

    desc_no_db = describe_resource(registry, "vm_template", "custom")
    assert desc_no_db.used_by is None


def test_describe_resource_returns_empty_used_by_for_no_instance_kinds(
    tmp_path: Path,
) -> None:
    """A kind without an `instances` hook (e.g. apt_package) yields
    ``used_by = None`` even with a db: the renderer treats None as
    "kind has no instance concept" and omits the section.
    """
    db, registry = _seed_basic(tmp_path)
    from agentworks.resources.inspect import describe_resource

    # Pick any apt_package the catalog publishes by default; if no
    # builtins are registered we skip.
    try:
        any_apt = next(iter(registry.iter_kind_items("apt_package")))
    except StopIteration:
        return
    desc = describe_resource(registry, "apt_package", any_apt[0], db=db)
    assert desc.used_by is None


def test_kinds_without_instances_hook_inherit_dash(tmp_path: Path) -> None:
    """Catalog kinds, git_credential_provider, and secret_backend don't
    implement ``instances`` -- ``inspect._count_used_by`` returns
    ``None`` for them, which the list view renders as ``-``.
    """
    from agentworks.resources import KIND_REGISTRY

    expected_no_instances = (
        "apt_package",
        "system_install_command",
        "user_install_command",
        "git_credential_provider",
        "secret_backend",
        "git_credentials",
    )
    for kind in expected_no_instances:
        handler = KIND_REGISTRY[kind]
        # The Protocol declares ``instances`` but kinds without the
        # instance concept don't define the method on their class.
        # ``inspect._count_used_by`` keys off this absence.
        assert not hasattr(handler, "instances"), (
            f"{kind} unexpectedly implements instances() -- the kind has no "
            f"live-instance concept; remove the override or override it to "
            f"return () with a docstring explaining the carve-out"
        )
