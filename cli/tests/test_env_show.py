"""Tests for ``agw env show`` (the service-layer ``agentworks.env.show.show_env``).

Pins:
- context-required validation (raises ValidationError on no flags)
- auto-resolution from --session / --workspace / --agent down to the VM
- precedence-sorted, scope-annotated rendering
- secret redaction by default; --resolve resolves via the resolver
- per-context identity vars overlay user env (identity wins per FRD R1)
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.db import Database, SessionMode, VersionedPayload
from agentworks.env.show import ResolvedEnvRow, show_env
from agentworks.errors import StateError, ValidationError
from agentworks.instance_specs import parse_instance_spec
from agentworks.secrets.policy import TtyInteractionPolicy
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# ---------------------------------------------------------------------------
# Test config fixture
# ---------------------------------------------------------------------------


def _write_config(
    tmp_path: Path,
    *,
    settings: str = "",
    vm_template: ManifestDoc | None = None,
    admin: ManifestDoc | None = None,
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """Write a settings-only config.toml plus its resources/ manifests and
    return the config path.

    The base always declares an empty ``default`` vm-template and the
    ``default`` admin-template (shell=zsh), the two singletons env-show
    resolves against; pass ``vm_template`` / ``admin`` to extend either
    (e.g. with an ``env`` block), and ``manifests`` for the additional
    resources a test needs (session / workspace templates, secrets).
    ``settings`` carries any settings-only TOML ([secret_config], ...).
    """
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + dedent(settings)
    )
    write_manifests(
        tmp_path,
        vm_template or ManifestDoc("vm-template", "default"),
        admin or ManifestDoc("admin-template", "default", {"shell": "zsh"}),
        *manifests,
    )
    return cfg


@pytest.fixture(autouse=True)
def _all_platforms_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests resolve VM rows against real registries; make the
    bundled sites publish regardless of the test host's tooling."""
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)


def _seed_db(
    db: Database, *, with_workspace: bool = True, with_agent: bool = False, with_session: bool = False
) -> None:
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host) "
        "VALUES ('vm-1', 'lima-local', 'h', 'agentworks', '100.64.0.5')"
    )
    if with_workspace:
        db._conn.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES ('ws-a', 'vm-1', '/home/agentworks/ws-a', 'ws-ws-a')"
        )
    if with_agent:
        db.insert_agent("claude", "vm-1", "aw-claude")
    if with_session:
        db.insert_session(
            "s1",
            "ws-a",
            "default",
            SessionMode.AGENT,
            agent_name="claude",
            socket_path="/run/agentworks/agent-tmux-sockets/aw-claude/s1.sock",
        )
    db._conn.commit()


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


def test_no_flags_raises_validation_error(db: Database, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)
    with pytest.raises(ValidationError, match="requires a context"):
        show_env(db, config, interaction=TtyInteractionPolicy.REFUSE)


def test_unknown_vm_raises_validation_error(db: Database, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)
    with pytest.raises(ValidationError, match="VM 'nope' not found"):
        show_env(db, config, vm_name="nope", interaction=TtyInteractionPolicy.REFUSE)


def test_session_flag_auto_resolves_workspace_agent_vm(
    db: Database,
    tmp_path: Path,
) -> None:
    """--session s1 should infer workspace, agent, and vm from the session
    row. The dynamic identity vars (AGENTWORKS_SESSION, AGENTWORKS_WORKSPACE)
    show up in env-show output because they're per-context. AGENTWORKS_AGENT
    does NOT -- it's per-user-static under the new identity taxonomy and
    lives in the agent's on-disk profile fragment, not in inline env."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)

    rows = show_env(db, config, session_name="s1", interaction=TtyInteractionPolicy.REFUSE)
    # Per-context dynamic identity vars are surfaced.
    keys = {r.key: r for r in rows}
    assert keys["AGENTWORKS_SESSION"].rendered_value == "s1"
    assert keys["AGENTWORKS_WORKSPACE"].rendered_value == "ws-a"
    # Per-user static identity (AGENTWORKS_AGENT) is NOT in env-show
    # output: it comes from the on-disk profile fragment, same shape as
    # the VM-stable vars (AGENTWORKS_VM etc.).
    assert "AGENTWORKS_AGENT" not in keys


def test_session_context_reads_stored_instance_overlays_at_every_live_scope(
    db: Database,
    tmp_path: Path,
) -> None:
    config = load_config(_write_config(tmp_path), warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)
    for kind, name, key in (
        ("vm", "vm-1", "VM_SPEC"),
        ("workspace", "ws-a", "WORKSPACE_SPEC"),
        ("agent", "claude", "AGENT_SPEC"),
        ("session", "s1", "SESSION_SPEC"),
    ):
        if kind == "vm":
            from agentworks.instance_specs import parse_vm_instance_specs

            vm_overlays = parse_vm_instance_specs(f'{{"env":{{"{key}":"set"}}}}', None)
            assert vm_overlays is not None
            payload = vm_overlays.payload
        else:
            payload = parse_instance_spec(kind, f'{{"env":{{"{key}":"set"}}}}').payload  # type: ignore[arg-type]
        db.instance_state.put_desired_overlay(kind, name, payload)  # type: ignore[arg-type]

    rows = show_env(db, config, session_name="s1", interaction=TtyInteractionPolicy.REFUSE)

    by_key = {row.key: row for row in rows}
    assert by_key["VM_SPEC"].scope == "vm"
    assert by_key["WORKSPACE_SPEC"].scope == "workspace"
    assert by_key["AGENT_SPEC"].scope == "agent"
    assert by_key["SESSION_SPEC"].scope == "session"


def test_workspace_flag_auto_resolves_vm(db: Database, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, workspace_name="ws-a", interaction=TtyInteractionPolicy.REFUSE)
    keys = {r.key: r for r in rows}
    assert "AGENTWORKS_WORKSPACE" in keys
    assert keys["AGENTWORKS_WORKSPACE"].rendered_value == "ws-a"
    # No agent / session vars because no agent context.
    assert "AGENTWORKS_AGENT" not in keys
    assert "AGENTWORKS_SESSION" not in keys


def test_copied_workspace_template_shows_no_workspace_env_and_does_not_crash(
    db: Database,
    tmp_path: Path,
) -> None:
    """A copied workspace records the synthetic ``template="copied"`` marker,
    which is not a real template. ``env show --workspace <copied>`` (and
    ``--session`` whose workspace is copied) must NOT crash with
    ``unknown_template_error`` (issue #285): the workspace contributes no
    template env, while the vm/identity scopes stay intact. A real workspace
    template still contributes its env, proving the tolerance is scoped to
    the failure."""
    cfg = _write_config(
        tmp_path,
        manifests=[ManifestDoc("workspace-template", "proj", {"env": {"WS_VAR": "ws-val"}})],
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=False)
    # A copied workspace (synthetic marker) and, on it, a session; plus a
    # separate workspace on a real template as the positive control.
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group, template) "
        "VALUES ('ws-copied', 'vm-1', '/home/agentworks/ws-copied', 'ws-ws-copied', 'copied')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group, template) "
        "VALUES ('ws-proj', 'vm-1', '/home/agentworks/ws-proj', 'ws-ws-proj', 'proj')"
    )
    db.insert_session(
        "s-copied",
        "ws-copied",
        "default",
        SessionMode.ADMIN,
        socket_path="/run/agentworks/admin-tmux-sockets/agentworks/s-copied.sock",
    )
    db._conn.commit()

    # --workspace on the copied marker: renders, with no workspace-scope env.
    copied_rows = show_env(db, config, workspace_name="ws-copied", interaction=TtyInteractionPolicy.REFUSE)
    assert {r.key for r in copied_rows}  # rendered something (identity vars)
    assert not any(r.scope == "workspace" for r in copied_rows)

    # --session whose workspace is the copied marker: same tolerance via the
    # session-inferred workspace.
    session_rows = show_env(db, config, session_name="s-copied", interaction=TtyInteractionPolicy.REFUSE)
    assert not any(r.scope == "workspace" for r in session_rows)

    # Positive control: a real workspace template still shows its env, at
    # the workspace scope.
    proj_rows = show_env(db, config, workspace_name="ws-proj", interaction=TtyInteractionPolicy.REFUSE)
    ws_var = next(r for r in proj_rows if r.key == "WS_VAR")
    assert ws_var.scope == "workspace"
    assert ws_var.rendered_value == "ws-val"


def test_missing_workspace_base_keeps_stored_overlay_env(
    db: Database,
    tmp_path: Path,
) -> None:
    config = load_config(_write_config(tmp_path), warn_issues=False)
    _seed_db(db, with_workspace=False)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group, template) "
        "VALUES ('ws-overlay', 'vm-1', '/home/agentworks/ws-overlay', 'ws-ws-overlay', 'removed')"
    )
    db._conn.commit()
    overlay = parse_instance_spec("workspace", '{"env":{"OVERLAY_ONLY":"available"}}')
    db.instance_state.put_desired_overlay("workspace", "ws-overlay", overlay.payload)

    rows = show_env(db, config, workspace_name="ws-overlay", interaction=TtyInteractionPolicy.REFUSE)

    overlay_row = next(row for row in rows if row.key == "OVERLAY_ONLY")
    assert overlay_row.scope == "workspace"
    assert overlay_row.rendered_value == "available"


def test_unsupported_workspace_overlay_warns_and_uses_base_env(
    db: Database,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            manifests=[ManifestDoc("workspace-template", "proj", {"env": {"BASE_ONLY": "available"}})],
        ),
        warn_issues=False,
    )
    _seed_db(db, with_workspace=False)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group, template) "
        "VALUES ('ws-future', 'vm-1', '/home/agentworks/ws-future', 'ws-ws-future', 'proj')"
    )
    db._conn.commit()
    plaintext = "do-not-print-this-value"
    db.instance_state.put_desired_overlay(
        "workspace",
        "ws-future",
        VersionedPayload(1, {"future_field": plaintext}),
    )

    rows = show_env(db, config, workspace_name="ws-future", interaction=TtyInteractionPolicy.REFUSE)

    base_row = next(row for row in rows if row.key == "BASE_ONLY")
    assert base_row.scope == "workspace"
    assert base_row.rendered_value == "available"
    assert captured_output.warnings
    assert plaintext not in repr(captured_output.lines)


def test_malformed_workspace_overlay_remains_fatal_for_env_show(
    db: Database,
    tmp_path: Path,
) -> None:
    config = load_config(
        _write_config(tmp_path, manifests=[ManifestDoc("workspace-template", "proj")]),
        warn_issues=False,
    )
    _seed_db(db, with_workspace=False)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group, template) "
        "VALUES ('ws-broken', 'vm-1', '/home/agentworks/ws-broken', 'ws-ws-broken', 'proj')"
    )
    db._conn.commit()
    db.instance_state.put_desired_overlay(
        "workspace",
        "ws-broken",
        VersionedPayload(1, {"env": {"TOKEN": {"unexpected": "value"}}}),
    )

    with pytest.raises(StateError) as caught:
        show_env(db, config, workspace_name="ws-broken", interaction=TtyInteractionPolicy.REFUSE)

    assert type(caught.value) is StateError


# ---------------------------------------------------------------------------
# Scope precedence + provenance
# ---------------------------------------------------------------------------


def test_session_scope_wins_over_vm_for_same_key(
    db: Database,
    tmp_path: Path,
) -> None:
    """When the same key is set at both vm and session scope, the session
    value wins AND the row's scope label is 'session'."""
    cfg = _write_config(
        tmp_path,
        vm_template=ManifestDoc("vm-template", "default", {"env": {"EDITOR": "vim"}}),
        manifests=[ManifestDoc("session-template", "shell", {"inherits": ["default"], "env": {"EDITOR": "nvim"}})],
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)
    # Session was created with template='default'; rewrite to 'shell' for this test.
    db._conn.execute("UPDATE sessions SET template = 'shell' WHERE name = 's1'")
    db._conn.commit()

    rows = show_env(db, config, session_name="s1", interaction=TtyInteractionPolicy.REFUSE)
    editor = next(r for r in rows if r.key == "EDITOR")
    assert editor.rendered_value == "nvim"
    assert editor.scope == "session"


def test_admin_env_appears_only_when_no_agent_context(
    db: Database,
    tmp_path: Path,
) -> None:
    cfg = _write_config(
        tmp_path,
        admin=ManifestDoc("admin-template", "default", {"shell": "zsh", "env": {"HTTP_PROXY": "http://proxy:3128"}}),
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_agent=True)

    # --vm only: admin scope applies (no agent context).
    rows_admin = show_env(db, config, vm_name="vm-1", interaction=TtyInteractionPolicy.REFUSE)
    assert any(r.key == "HTTP_PROXY" and r.scope == "admin" for r in rows_admin)

    # --agent: admin scope does NOT apply (agent context excludes it).
    rows_agent = show_env(db, config, agent_name="claude", interaction=TtyInteractionPolicy.REFUSE)
    assert not any(r.key == "HTTP_PROXY" for r in rows_agent)


# ---------------------------------------------------------------------------
# Secret rendering
# ---------------------------------------------------------------------------


def test_secret_redacted_by_default(db: Database, tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        admin=ManifestDoc(
            "admin-template", "default", {"shell": "zsh", "env": {"API_KEY": {"secret": "shared-token"}}}
        ),
        manifests=[ManifestDoc("secret", "shared-token", description="shared API token")],
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, vm_name="vm-1", interaction=TtyInteractionPolicy.REFUSE)
    api = next(r for r in rows if r.key == "API_KEY")
    assert api.is_secret
    assert api.rendered_value == "<from secret: shared-token>"


def test_secret_revealed_with_flag(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--resolve resolves secret-backed entries through the active source chain."""
    monkeypatch.setenv("AW_SECRET_SHARED_TOKEN", "from-operator-env")
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        admin=ManifestDoc(
            "admin-template", "default", {"shell": "zsh", "env": {"API_KEY": {"secret": "shared-token"}}}
        ),
        manifests=[ManifestDoc("secret", "shared-token", description="shared API token")],
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, vm_name="vm-1", reveal_secrets=True, interaction=TtyInteractionPolicy.REFUSE)
    api = next(r for r in rows if r.key == "API_KEY")
    assert api.is_secret
    assert api.rendered_value == "from-operator-env"


def test_show_env_rejects_non_enum_policy_before_any_source_work(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied ``"refuse"`` is equal to the enum but not identical to it.

    ``show_env`` builds no policy of its own, and on the non-reveal path it builds none at all,
    so its own check is the only thing that can reject the value. Without it a revealing call
    reaches the source chain carrying a policy every consumer will read as not-refuse.
    """
    monkeypatch.setenv("AW_SECRET_SHARED_TOKEN", "from-operator-env")
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        admin=ManifestDoc(
            "admin-template", "default", {"shell": "zsh", "env": {"API_KEY": {"secret": "shared-token"}}}
        ),
        manifests=[ManifestDoc("secret", "shared-token", description="shared API token")],
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("a rejected policy must not reach the sources")

    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", _boom)
    monkeypatch.setattr("agentworks.secrets.resolve.resolve_partial_for_reveal", _boom)

    with pytest.raises(StateError):
        show_env(db, config, vm_name="vm-1", reveal_secrets=True, interaction="refuse")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("sentinel-lf\nforged-row\n", id="lf"),
        pytest.param("sentinel-crlf\r\nforged-row\r\n", id="crlf"),
    ],
)
def test_secret_reveal_rejects_multiline_before_rendering(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
    value: str,
) -> None:
    monkeypatch.setenv("AW_SECRET_SHARED_TOKEN", value)
    cfg = _write_config(
        tmp_path,
        settings='[secret_config]\nsources = ["env-var"]\n',
        admin=ManifestDoc(
            "admin-template", "default", {"shell": "zsh", "env": {"API_KEY": {"secret": "shared-token"}}}
        ),
        manifests=[ManifestDoc("secret", "shared-token", description="shared API token")],
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, vm_name="vm-1", reveal_secrets=True, interaction=TtyInteractionPolicy.REFUSE)

    api = next(row for row in rows if row.key == "API_KEY")
    assert api.rendered_value.startswith("<error: secret 'shared-token' cannot be used")
    assert value not in repr(rows)
    assert value not in repr(captured_output.lines)


def test_secret_reveal_guard_rejects_nul_before_building_render_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from agentworks.env import EnvEntry
    from agentworks.env.show import _reveal_values
    from agentworks.secrets import SecretDecl

    value = "sentinel-prefix\0sentinel-suffix"
    monkeypatch.setattr(
        "agentworks.resources.access.secret_decls",
        lambda _registry: {"shared-token": SecretDecl(name="shared-token", description="")},
    )
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda *_args: ())
    monkeypatch.setattr(
        "agentworks.secrets.resolve.resolve_partial_for_reveal",
        lambda *_args, **_kwargs: SimpleNamespace(values={"shared-token": value}, outcomes=()),
    )

    values, errors = _reveal_values(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        {"API_KEY": EnvEntry({"secret": "shared-token"})},
        reveal=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert values == {}
    assert errors == {"shared-token": "secret 'shared-token' cannot be used for environment reveal"}
    assert value not in repr((values, errors))


# ---------------------------------------------------------------------------
# Identity overlay
# ---------------------------------------------------------------------------


def test_identity_var_overlays_user_env(
    db: Database,
    tmp_path: Path,
) -> None:
    """User env that tries to set AGENTWORKS_SESSION gets the identity value
    at render time (per FRD R1; the operator's value is replaced)."""
    cfg = _write_config(
        tmp_path,
        manifests=[
            ManifestDoc(
                "session-template",
                "shell",
                {"inherits": ["default"], "env": {"AGENTWORKS_SESSION": "operator-override"}},
            )
        ],
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)
    db._conn.execute("UPDATE sessions SET template = 'shell' WHERE name = 's1'")
    db._conn.commit()

    rows = show_env(db, config, session_name="s1", interaction=TtyInteractionPolicy.REFUSE)
    session_row = next(r for r in rows if r.key == "AGENTWORKS_SESSION")
    assert session_row.rendered_value == "s1"  # identity wins
    assert session_row.scope == "identity"


def test_identity_subset_skips_vm_stable_vars(
    db: Database,
    tmp_path: Path,
) -> None:
    """The inline (env show) identity output mirrors the inline prelude
    subset: VM-stable vars (AGENTWORKS_VM / _VM_HOST / _PLATFORM) come from
    VM-side profile fragments (Phase 4) and don't appear in the env-show
    output."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)

    rows = show_env(db, config, session_name="s1", interaction=TtyInteractionPolicy.REFUSE)
    keys = {r.key for r in rows}
    for excluded in ("AGENTWORKS_VM", "AGENTWORKS_VM_HOST", "AGENTWORKS_PLATFORM"):
        assert excluded not in keys, f"{excluded} should not appear in env show output"


# ---------------------------------------------------------------------------
# Shape of the return value
# ---------------------------------------------------------------------------


def test_return_type_is_list_of_resolved_env_rows(
    db: Database,
    tmp_path: Path,
) -> None:
    """``show_env`` returns the structured rows in addition to printing,
    so tests can pin contracts without parsing formatted output."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, vm_name="vm-1", interaction=TtyInteractionPolicy.REFUSE)
    assert isinstance(rows, list)
    assert all(isinstance(r, ResolvedEnvRow) for r in rows)


def test_a_workspace_layer_needing_replacement_warns_with_its_own_repair_and_uses_the_base(
    db: Database,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.instance_specs import ExplicitReplacementRequired, decode_stored_overlay
    from agentworks.vms.manager._helpers import _resolve_vm_admin_env_scopes

    cfg = _write_config(
        tmp_path, manifests=[ManifestDoc("workspace-template", "default", {"env": {"WS_VAR": "ws-val"}})]
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)
    db.instance_state.put_desired_overlay("workspace", "ws-a", VersionedPayload(1, {"harness_integrations": ["codex"]}))
    record = db.instance_state.get_desired_overlay("workspace", "ws-a")
    assert record is not None
    with pytest.raises(ExplicitReplacementRequired) as refused:
        decode_stored_overlay(record)

    rows = show_env(db, config, workspace_name="ws-a", interaction=TtyInteractionPolicy.REFUSE)
    from agentworks.bootstrap import load_request_registry

    vm, ws = db.get_vm("vm-1"), db.get_workspace("ws-a")
    assert vm is not None and ws is not None
    scopes = _resolve_vm_admin_env_scopes(db, load_request_registry(config, live_database=db), vm, ws=ws)

    assert {row.key: row.rendered_value for row in rows}["WS_VAR"] == "ws-val"
    assert scopes.workspace is not None and "WS_VAR" in scopes.workspace
    warnings = [warning for warning in captured_output.warnings if str(refused.value) in warning]
    assert len(warnings) == 2
    assert all(refused.value.hint is not None and refused.value.hint in warning for warning in warnings)


def test_show_plaintext_matches_execution_with_live_instance_context(db: Database, tmp_path: Path) -> None:
    from agentworks.env import ResourceContext, compose_env
    from agentworks.env.entry import EnvEntry

    source = {
        "CONFIG": "${agw:user_home_dir}/.config/tool",
        "PROJECT": "${agw:workspace_dir}",
        "LABEL": "${agw:agent_name}/${agw:session_name}",
        "LITERAL": "$${agw:user_name}",
    }
    config = load_config(
        _write_config(tmp_path, vm_template=ManifestDoc("vm-template", "default", {"env": source})), warn_issues=False
    )
    _seed_db(db, with_agent=True, with_session=True)
    rows = show_env(db, config, session_name="s1", interaction=TtyInteractionPolicy.REFUSE)
    actual = {row.key: row.rendered_value for row in rows}
    expected = compose_env(
        values={},
        vm={key: EnvEntry(value) for key, value in source.items()},
        ctx=ResourceContext(
            vm_name="vm-1",
            platform="lima",
            site="lima-local",
            user="aw-claude",
            agent_name="claude",
            workspace_name="ws-a",
            workspace_dir="/home/agentworks/ws-a",
            session_name="s1",
            session_kind="agent",
        ),
    )
    assert actual == expected
    assert all(row.scope == "vm" for row in rows if row.key in source)


@pytest.mark.parametrize("entry", ["${agw:agent_name}", "${agw:unknown}", {"secret": "ignored"}])
def test_admin_preview_discards_identity_collisions_before_render_or_reveal(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str | dict[str, str]
) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            vm_template=ManifestDoc(
                "vm-template", "default", {"env": {"AGENTWORKS_SESSION_KIND": entry, "AGENTWORKS_WORKSPACE": entry}}
            ),
        ),
        warn_issues=False,
    )
    _seed_db(db)
    db.insert_session("admin-s", "ws-a", "default", SessionMode.ADMIN)

    def unexpected_reveal(*args: object, **kwargs: object) -> None:
        pytest.fail("An ignored secret must not reach resolution")

    monkeypatch.setattr("agentworks.secrets.resolve.resolve_partial_for_reveal", unexpected_reveal)
    rows = show_env(db, config, session_name="admin-s", reveal_secrets=True, interaction=TtyInteractionPolicy.REFUSE)
    actual = {row.key: row for row in rows}
    assert actual["AGENTWORKS_SESSION_KIND"] == ResolvedEnvRow("AGENTWORKS_SESSION_KIND", "admin", "identity", False)
    assert actual["AGENTWORKS_WORKSPACE"] == ResolvedEnvRow("AGENTWORKS_WORKSPACE", "ws-a", "identity", False)


def test_session_legacy_preview_matches_execution_after_inheritance_and_stored_overlays(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.env import EnvEntry, ResourceContext, compose_env
    from agentworks.sessions.templates import resolve_live_template

    opaque = "opaque:{{session_name}}/${agw:session_name}"
    monkeypatch.setenv("AW_SECRET_LEGACY_TOKEN", opaque)
    vm_env = {"VM_LITERAL": "{{session_name}}/{{workspace_name}}"}
    config = load_config(
        _write_config(
            tmp_path,
            settings='[secret_config]\nsources = ["env-var"]\n',
            vm_template=ManifestDoc("vm-template", "default", {"env": vm_env}),
            manifests=[
                ManifestDoc(
                    "session-template",
                    "parent",
                    {
                        "inherits": ["default"],
                        "env": {
                            "INHERITED": "{{session_name}}/{{workspace_name}}",
                            "OVERRIDDEN": "{{workspace_name}}",
                            "SECRET": {"secret": "legacy-token"},
                        },
                    },
                ),
                ManifestDoc(
                    "session-template",
                    "child",
                    {"inherits": ["parent"], "env": {"OVERRIDDEN": "modern-${agw:session_name}"}},
                ),
                ManifestDoc("secret", "legacy-token", description="public fixture"),
            ],
        ),
        warn_issues=False,
    )
    _seed_db(db, with_agent=True, with_session=True)
    db._conn.execute("UPDATE sessions SET template = 'child' WHERE name = 's1'")
    db._conn.commit()
    db.instance_state.put_desired_overlay(
        "session", "s1", VersionedPayload(1, {"env": {"INSTANCE": "${{session_name}}/${agw:workspace_name}"}})
    )
    overlay_before = db.instance_state.get_desired_overlay("session", "s1")

    redacted = show_env(db, config, session_name="s1", interaction=TtyInteractionPolicy.REFUSE)
    assert next(row for row in redacted if row.key == "SECRET").is_secret
    assert opaque not in repr(redacted)
    rows = show_env(db, config, session_name="s1", reveal_secrets=True, interaction=TtyInteractionPolicy.REFUSE)
    actual = {row.key: row.rendered_value for row in rows}
    from agentworks.bootstrap import build_registry

    session_env = resolve_live_template(db, build_registry(config), "s1", "child").env
    expected = compose_env(
        values={"legacy-token": opaque},
        vm={key: EnvEntry(value) for key, value in vm_env.items()},
        session=session_env,
        ctx=ResourceContext(
            vm_name="vm-1",
            platform="lima",
            site="lima-local",
            user="aw-claude",
            agent_name="claude",
            workspace_name="ws-a",
            workspace_dir="/home/agentworks/ws-a",
            session_name="s1",
            session_kind="agent",
        ),
    )

    assert actual == expected
    assert actual["INHERITED"] == "s1/ws-a"
    assert actual["OVERRIDDEN"] == "modern-s1"
    assert actual["INSTANCE"] == "$s1/ws-a"
    assert actual["VM_LITERAL"] == "{{session_name}}/{{workspace_name}}"
    assert actual["SECRET"] == opaque
    assert {row.scope for row in rows if row.key in session_env} == {"session"}
    assert next(row for row in rows if row.key == "VM_LITERAL").scope == "vm"
    assert db.instance_state.get_desired_overlay("session", "s1") == overlay_before
