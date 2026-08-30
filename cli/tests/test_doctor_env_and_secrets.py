"""Tests for the env-and-secrets doctor health groups (FRD R6)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from agentworks.bootstrap import build_registry
from agentworks.capabilities.secret_backend import BlockReason, TtyInteractionAccess
from agentworks.cli import app
from agentworks.config import load_config
from agentworks.db import LATEST_VERSION, Database, VersionedPayload
from agentworks.doctor import (
    SecretSourceStatus,
    Status,
    _build_doctor_registry,
    _check_config,
    _check_secrets,
    _secret_source_check,
    checks_for_resource,
)
from agentworks.errors import ConfigError, StateError
from agentworks.manifests import load_manifests
from agentworks.resources.access import ResourceIdentity
from agentworks.secrets.preview import PreviewStatus
from agentworks.secrets.sources import SourceProvenance
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence


def _write_config(
    tmp_path: Path,
    *,
    settings: str = "",
    admin_env: dict[str, object] | None = None,
    admin_fields: dict[str, object] | None = None,
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """Write a settings-only config.toml plus its resources/ manifests and
    return the config path.

    The base always declares an empty ``default`` vm-template (whose
    tailscale requirement auto-declares ``tailscale-auth-key``, the secret
    the doctor tests assert on) and the ``default`` admin-template
    (shell=zsh). ``admin_env`` seeds that admin-template's env block,
    ``manifests`` adds further resources (secrets, sites), and ``settings``
    carries settings-only TOML ([plugins], [secret_config])."""
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
    admin_spec: dict[str, object] = {"shell": "zsh"}
    if admin_fields is not None:
        admin_spec.update(admin_fields)
    if admin_env is not None:
        admin_spec["env"] = admin_env
    write_manifests(
        tmp_path,
        ManifestDoc("vm-template", "default"),
        ManifestDoc("admin-template", "default", admin_spec),
        *manifests,
    )
    return cfg


def test_focused_checks_are_the_exact_bulk_resource_rows(tmp_path: Path) -> None:
    from agentworks.doctor import (
        _check_secret_backends,
        _check_secret_sources,
        _check_vm_platforms,
    )

    config = load_config(_write_config(tmp_path), warn_issues=False)
    registry = build_registry(config)
    cases = (
        (ResourceIdentity("vm-platform", "lima"), _check_vm_platforms(registry)),
        (ResourceIdentity("secret-backend", "env-var"), _check_secret_backends(registry)),
        (ResourceIdentity("secret-source", "env-var"), _check_secret_sources(config, registry)),
        (
            ResourceIdentity("secret", "tailscale-auth-key"),
            _check_secrets(config, registry, tty_access=TtyInteractionAccess.UNAVAILABLE),
        ),
    )

    for identity, group in cases:
        focused = checks_for_resource(
            config,
            registry,
            identity,
            tty_access=TtyInteractionAccess.UNAVAILABLE,
        )
        assert len(focused) == 1
        assert focused[0] in group.checks


def test_focused_checks_reject_non_exact_tty_access_before_resource_work() -> None:
    with pytest.raises(StateError):
        checks_for_resource(
            cast("Any", object()),
            cast("Any", object()),
            ResourceIdentity("secret", "token"),
            tty_access=cast("Any", "unavailable"),
        )


def test_focused_admin_dotfiles_matches_bulk_and_empty_source_has_no_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import config as config_module

    empty_path = tmp_path / "empty"
    empty_path.mkdir()
    empty_config = load_config(_write_config(empty_path), warn_issues=False)
    empty_registry = build_registry(empty_config)
    identity = ResourceIdentity("admin-template", "default")
    assert (
        checks_for_resource(
            empty_config,
            empty_registry,
            identity,
            tty_access=TtyInteractionAccess.UNAVAILABLE,
        )
        == ()
    )

    configured_path = tmp_path / "configured"
    configured_path.mkdir()
    config_path = _write_config(
        configured_path,
        admin_fields={"dotfiles_source": "github:operator/dotfiles"},
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    bulk_group, config, registry = _check_config()
    assert config is not None
    assert registry is not None
    focused = checks_for_resource(
        config,
        registry,
        identity,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    assert len(focused) == 1
    assert focused[0] in bulk_group.checks


@pytest.mark.parametrize(
    ("active", "enabled", "not_ready_reason", "expected"),
    [
        (True, True, None, Status.OK),
        (False, True, None, Status.INFO),
        (True, False, None, Status.INFO),
        (True, True, "unavailable", Status.INFO),
    ],
)
def test_secret_source_is_ok_only_when_active_enabled_and_ready(
    active: bool,
    enabled: bool,
    not_ready_reason: str | None,
    expected: Status,
) -> None:
    check = _secret_source_check(
        SecretSourceStatus(
            name="fixture",
            backend="fixture",
            provenance=SourceProvenance.DECLARED,
            active=active,
            enabled=enabled,
            not_ready_reason=not_ready_reason,
        )
    )

    assert check.status is expected


# ---------------------------------------------------------------------------
# _check_secrets
# ---------------------------------------------------------------------------


def test_secret_checks_use_only_explicit_tty_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import output

    config = load_config(_write_config(tmp_path), warn_issues=False)
    registry = build_registry(config)
    monkeypatch.setattr(output, "non_interactive", lambda: pytest.fail("doctor read ambient TTY policy"))
    unavailable = _check_secrets(
        config,
        registry,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    available = _check_secrets(
        config,
        registry,
        tty_access=TtyInteractionAccess.AVAILABLE,
    )
    unavailable_row = next(
        check
        for check in unavailable.checks
        if check.secret_preview is not None and check.secret_preview.name == "tailscale-auth-key"
    )
    available_row = next(
        check
        for check in available.checks
        if check.secret_preview is not None and check.secret_preview.name == "tailscale-auth-key"
    )
    assert unavailable_row.secret_preview is not None
    assert available_row.secret_preview is not None
    assert unavailable_row.secret_preview.status is PreviewStatus.BLOCKED
    assert unavailable_row.status is Status.WARN
    assert available_row.secret_preview.status is PreviewStatus.INDETERMINATE
    assert available_row.secret_preview.reason == "operator-input-required"
    assert available_row.status is Status.OK
    assert available_row.hint is None


def test_secret_checks_preview_the_sorted_declared_union_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.secrets import preview as preview_module

    config = load_config(
        _write_config(
            tmp_path,
            manifests=[
                ManifestDoc("secret", "z-last", description="z"),
                ManifestDoc("secret", "a-first", description="a"),
            ],
        ),
        warn_issues=False,
    )
    registry = build_registry(config)
    spy = Mock(wraps=preview_module.preview_batch)
    monkeypatch.setattr(preview_module, "preview_batch", spy)

    group = _check_secrets(config, registry, tty_access=TtyInteractionAccess.UNAVAILABLE)

    assert spy.call_count == 1
    declarations = spy.call_args.args[0]
    requested_names = [decl.name for decl in declarations]
    assert requested_names == sorted(requested_names)
    rendered_names = [check.secret_preview.name for check in group.checks if check.secret_preview is not None]
    assert rendered_names == requested_names


def test_auto_declared_secrets_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor reports EVERY registry secret, auto-declared included --
    they are exactly the ones most likely to prompt at command time,
    so hiding them made doctor unable to predict the next command.
    A bare config still carries the framework-auto-declared
    ``tailscale-auth-key`` (vm-template requirement); it shows with an
    honest provider preview like every other secret.
    """
    # Hosts that operate real VMs export this; clear it so the
    # provider outcome reflects the bare config, not the
    # test host's environment (mirrors the sibling tests' delenv discipline).
    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY", raising=False)
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config), tty_access=TtyInteractionAccess.UNAVAILABLE)
    assert g.name == "Secrets"
    (check,) = g.checks
    assert check.status is Status.WARN
    assert check.secret_preview is not None
    assert check.secret_preview.status is PreviewStatus.BLOCKED
    assert check.secret_preview.reason == BlockReason.TTY_UNAVAILABLE.value


def test_secret_resolves_via_env_var_when_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AW_SECRET_<NAME> is set, doctor reports the secret as resolving
    via env-var."""
    monkeypatch.setenv("AW_SECRET_SHARED", "from-operator-env")
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        admin_env={"TOKEN": {"secret": "shared"}},
        manifests=[ManifestDoc("secret", "shared", description="Shared API token")],
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config), tty_access=TtyInteractionAccess.UNAVAILABLE)
    shared = next(
        check for check in g.checks if check.secret_preview is not None and check.secret_preview.name == "shared"
    )
    assert shared.status is Status.OK
    assert shared.secret_preview is not None
    assert shared.secret_preview.status is PreviewStatus.AVAILABLE
    assert shared.secret_preview.source == "env-var"


def test_doctor_accepts_mapping_keyed_by_differently_named_declared_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALT_SHARED_TOKEN", "from-operator-env")
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["work-env"]
        """,
        manifests=[
            ManifestDoc("secret-source", "work-env", {"backend": {"name": "env-var"}}),
            ManifestDoc(
                "secret",
                "shared",
                {"backend_mappings": {"work-env": "ALT_SHARED_TOKEN"}},
                description="Shared API token",
            ),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    group = _check_secrets(config, build_registry(config), tty_access=TtyInteractionAccess.UNAVAILABLE)

    shared = next(
        check
        for check in group.checks
        if check.secret_preview is not None and check.secret_preview.source == "work-env"
    )
    assert shared.status is Status.OK
    assert shared.secret_preview is not None
    assert shared.secret_preview.status is PreviewStatus.AVAILABLE
    assert shared.secret_preview.source == "work-env"


def test_secret_resolves_via_prompt_when_env_var_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When env-var has nothing and prompt is in the chain, doctor reports
    the secret as resolving via prompt, which is another active source."""
    monkeypatch.delenv("AW_SECRET_SHARED", raising=False)
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        admin_env={"TOKEN": {"secret": "shared"}},
        manifests=[ManifestDoc("secret", "shared", description="Shared API token")],
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config), tty_access=TtyInteractionAccess.UNAVAILABLE)
    shared = next(
        check for check in g.checks if check.secret_preview is not None and check.secret_preview.name == "shared"
    )
    assert shared.status is Status.WARN
    assert shared.secret_preview is not None
    assert shared.secret_preview.status is PreviewStatus.BLOCKED
    assert shared.secret_preview.reason == BlockReason.TTY_UNAVAILABLE.value


def test_secret_not_available_when_env_var_unset_and_prompt_opted_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When prompt is opted out via backend_mappings.prompt = false AND
    env-var has no value, doctor reports the secret as WARN (config is
    valid but no source in the chain would resolve it)."""
    monkeypatch.delenv("AW_SECRET_OPTED_OUT", raising=False)
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        admin_env={"TOKEN": {"secret": "opted-out"}},
        manifests=[
            ManifestDoc(
                "secret",
                "opted-out",
                {"backend_mappings": {"prompt": False}},
                description="Must come from env-var",
            )
        ],
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config), tty_access=TtyInteractionAccess.UNAVAILABLE)
    row = next(c for c in g.checks if "opted-out" in c.name)
    assert row.status is Status.WARN
    assert row.secret_preview is not None
    assert row.secret_preview.status is PreviewStatus.MISSING


# ---------------------------------------------------------------------------
# _check_secret_backends (the new R9.7 backend-readiness group)
# ---------------------------------------------------------------------------


def test_secret_backends_group_reports_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The new secret-backends group lists one readiness row per backend off
    the stored graph verdict (R11), parallel to VM platforms: env-var / prompt
    are ready (``ok``); onepassword with no ``op`` on PATH is INFO with
    ``not ready: op CLI not installed``."""
    from agentworks.doctor import _check_secret_backends

    monkeypatch.setattr("shutil.which", lambda name: None)  # op absent
    config = load_config(
        _write_config(tmp_path, settings='[plugins]\nsystem = ["onepassword"]\n'),
        warn_issues=False,
    )
    g = _check_secret_backends(build_registry(config))

    assert g.name == "Secret backends"
    by_name = {c.name: c for c in g.checks}
    assert by_name["env-var"].status is Status.OK
    assert by_name["prompt"].status is Status.OK
    assert by_name["onepassword"].status is Status.INFO
    assert by_name["onepassword"].message == "not ready: op CLI not installed"


def test_secret_backends_group_skips_disabled_plugin_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DISABLED plugin backend (``onepassword`` with the plugin not enabled) is
    NOT listed in the Secret backends section: the System plugins roster is the
    enablement authority and lists it as disabled instead. This is the fix for
    the bug where a disabled backend rendered a misleading ``[ok]`` off its
    ready-placeholder readiness, and where ENABLING onepassword made doctor look
    worse (it flipped from ``[ok]`` to not-ready). The two core backends
    (``env-var`` / ``prompt``) still list as ``ok``.
    """
    from agentworks.doctor import _check_plugins, _check_secret_backends

    monkeypatch.setattr("shutil.which", lambda name: None)  # op absent
    config = load_config(_write_config(tmp_path), warn_issues=False)  # no [plugins] system
    registry = build_registry(config)

    g = _check_secret_backends(registry)
    by_name = {c.name: c for c in g.checks}
    assert by_name["env-var"].status is Status.OK
    assert by_name["prompt"].status is Status.OK
    # Disabled: skipped here, never a misleading [ok].
    assert "onepassword" not in by_name
    assert (
        checks_for_resource(
            config,
            registry,
            ResourceIdentity("secret-backend", "onepassword"),
            tty_access=TtyInteractionAccess.UNAVAILABLE,
        )
        == ()
    )

    # The System plugins roster IS the enablement authority: it lists the
    # disabled backend's plugin as disabled.
    roster = {c.name: c for c in _check_plugins(config).checks}
    assert roster["plugin onepassword"].status is Status.INFO
    assert "not enabled in [plugins].system" in (roster["plugin onepassword"].message or "")


def test_check_secrets_flags_a_not_ready_only_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R9.6: a secret whose only attempting source selects a not-ready backend
    is at-risk. ``_check_secrets`` warns rather than falsely predicting
    resolution through that source (lockstep with the resolution skip)."""
    monkeypatch.setattr("shutil.which", lambda name: None)  # op absent
    cfg = _write_config(
        tmp_path,
        settings="""
        [plugins]
        system = ["onepassword"]

        [secret_config]
        sources = ["onepassword"]
        """,
        admin_env={"TOKEN": {"secret": "op-only"}},
        manifests=[
            ManifestDoc(
                "secret-source",
                "onepassword",
                {"backend": {"name": "onepassword"}},
            ),
            ManifestDoc(
                "secret",
                "op-only",
                {"backend_mappings": {"onepassword": "op://Vault/item/field", "env-var": False}},
                description="resolves only via onepassword",
            ),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config), tty_access=TtyInteractionAccess.UNAVAILABLE)
    op_only = next(
        check
        for check in g.checks
        if check.secret_preview is not None and check.secret_preview.reason == BlockReason.SOURCE_NOT_READY.value
    )
    assert op_only.status is Status.WARN
    assert op_only.secret_preview is not None
    assert op_only.secret_preview.status is PreviewStatus.BLOCKED
    assert op_only.secret_preview.reason == BlockReason.SOURCE_NOT_READY.value


def test_r9_3_manifest_malformed_block_surfaces_under_resource_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9.3 doctor consequence: a malformed capability block in a MANIFEST now
    surfaces under the "Resource registry" check row, not "Manifest". Capability
    validation moved out of decode/load into the finalize ``validate`` pass, so
    ``load_manifests`` accepts the block and ``build_registry`` fails it. Uses an
    azdo git-credential; azdo ships in the opt-in ``azure`` system plugin, whose
    validation is deferred while disabled, so the plugin is enabled here for the
    block to validate (host-independent, so it always validates once enabled)."""
    cfg = _write_config(tmp_path, settings='[plugins]\nsystem = ["azure"]')
    write_manifests(
        tmp_path,
        ManifestDoc("git-credential", "ado", {"provider": {"name": "azdo", "org": "my-org", "bogus": 1}}),
        filename="res.yaml",
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, registry = _check_config()

    fails = {c.name: c for c in g.checks if c.status == Status.FAIL}
    assert "Resource registry" in fails
    assert "bogus: unknown field" in (fails["Resource registry"].message or "")
    assert "Manifest" not in fails  # the malformed block is no longer a decode/load failure
    assert registry is None  # the registry-dependent tail is skipped after the failure


def test_mapping_to_undeclared_kind_hard_errors_at_build(tmp_path: Path) -> None:
    """A ``backend_mappings`` entry naming an undeclared source is a dangling
    ``secret -> secret-source`` validation edge, which the source kind's ``error``
    miss policy turns into a hard ``build_registry`` failure (where the old
    tolerant ``_check_secrets`` pinpointed it as one per-secret FAIL row).

    Doctor-granularity regression (acknowledged, R9.11): because the build
    now raises, a doctor run collapses its whole registry-dependent tail to
    one "Resource registry: FAIL" row rather than pinpointing the secret.
    """
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        admin_env={"TOKEN": {"secret": "shared"}},
        manifests=[
            ManifestDoc("secret", "shared", {"backend_mappings": {"bogusvault": "x"}}, description="shared token")
        ],
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError, match="unknown secret-source 'bogusvault'"):
        build_registry(config)


def test_mapping_to_multiple_undeclared_kinds_hard_errors_at_build(tmp_path: Path) -> None:
    """With two unknown-source mappings, the first dangling edge the
    resolve pass reaches hard-errors at ``build_registry`` (naming that
    source); the build never gets far enough to enumerate both, unlike the
    old tolerant per-secret doctor row that listed them sorted."""
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        admin_env={"TOKEN": {"secret": "shared"}},
        manifests=[
            ManifestDoc(
                "secret",
                "shared",
                {"backend_mappings": {"zeta-vault": "z", "alpha-vault": "a"}},
                description="shared token",
            )
        ],
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError, match="unknown secret-source '(alpha-vault|zeta-vault)'"):
        build_registry(config)


# ---------------------------------------------------------------------------
# AGENTWORKS_* identity overrides surface in the Configuration group
# ---------------------------------------------------------------------------


def test_agentworks_identity_override_surfaces_in_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who sets AGENTWORKS_SESSION in their env table triggers
    a config-load warning. Doctor surfaces it once, in the Configuration
    group (there used to be a separate Env group; it was removed as
    redundant since ``agw env show`` is the authoritative inspection
    surface)."""
    cfg = _write_config(tmp_path, admin_env={"AGENTWORKS_SESSION": "operator-override"})
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, _ = _check_config()
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any("AGENTWORKS_SESSION" in (c.message or "") for c in warns), [(c.name, c.message) for c in warns]


def test_manifest_issues_surface_as_doctor_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    """A load-time advisory on a manifest-declared resource (here an
    ``AGENTWORKS_*`` env key the runtime prelude will override) used to
    warn ambiently above the report while the Config row said ok. Doctor
    now renders manifest issues as warn rows, and passing the pre-loaded
    set into build_registry keeps the ambient print out of doctor's output
    entirely."""
    cfg = _write_config(tmp_path)
    write_manifests(
        tmp_path,
        dedent("""\
        apiVersion: agentworks/v1
        kind: agent-template
        metadata:
          name: other
        spec:
          env:
            AGENTWORKS_AGENT: override
        """),
        filename="agent.yaml",
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, registry = _check_config()

    # The suppression half of the fix: doctor passes the pre-loaded set
    # into build_registry, so the ambient "Manifest: ..." print must not
    # appear above the report.
    captured = capsys.readouterr()
    assert "Manifest:" not in captured.out + captured.err

    manifest_rows = [c for c in g.checks if c.name == "Manifest"]
    assert manifest_rows, [c.name for c in g.checks]
    assert manifest_rows[0].status == Status.WARN
    assert "AGENTWORKS_AGENT" in (manifest_rows[0].message or "")
    assert "agent.yaml" in (manifest_rows[0].message or "")
    # The ok row is withheld when any issue exists.
    assert not any(c.name == "Config is valid" for c in g.checks)
    # The registry still builds (warn, not fail).
    assert registry is not None


def test_clean_manifests_keep_config_valid_row(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, registry = _check_config()
    assert any(c.name == "Config is valid" for c in g.checks)
    assert not any(c.name == "Manifest" for c in g.checks)
    assert registry is not None


def test_stale_database_keeps_declared_doctor_registry_without_migrating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import db as db_module

    cfg = _write_config(tmp_path)
    database_path = tmp_path / "state.db"
    Database(database_path).close()
    connection = sqlite3.connect(database_path)
    connection.execute("UPDATE schema_version SET version = ?", (LATEST_VERSION - 1,))
    connection.commit()
    connection.close()
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setattr(db_module, "DB_PATH", database_path)

    config = load_config(cfg, warn_issues=False)
    registry, coverage = _build_doctor_registry(config, load_manifests(cfg.parent / "resources"))

    assert registry is not None
    assert registry.lookup("vm-template", "default") is not None
    assert coverage is not None
    assert coverage.status is Status.INFO
    connection = sqlite3.connect(database_path)
    version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    connection.close()
    assert version == LATEST_VERSION - 1


@pytest.mark.parametrize(
    "stored_value",
    [
        {"future_field": True},
        {"env": {"TOKEN": {"unexpected": "value"}}},
    ],
    ids=["unsupported", "malformed"],
)
def test_invalid_live_overlay_keeps_declared_doctor_registry(
    tmp_path: Path,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    stored_value: dict[str, object],
) -> None:
    from agentworks import db as db_module

    cfg = _write_config(tmp_path, manifests=[ManifestDoc("agent-template", "default")])
    db.insert_vm("box", "lima-local", "box", template="default")
    db.insert_agent("dev", "box", "dev", template="default")
    db.instance_state.put_desired_overlay(
        "agent",
        "dev",
        VersionedPayload(1, stored_value),
    )
    database_path = tmp_path / "test.db"
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setattr(db_module, "DB_PATH", database_path)

    config = load_config(cfg, warn_issues=False)
    registry, live_failure = _build_doctor_registry(config, load_manifests(cfg.parent / "resources"))

    assert registry is not None
    assert registry.lookup("agent-template", "default") is not None
    assert registry.graph.is_live("agent", "dev") is False
    assert live_failure is not None
    assert live_failure.status is Status.FAIL


def test_doctor_cli_keeps_database_inspection_value_free_after_live_publication_failure(
    tmp_path: Path,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import config as config_module
    from agentworks import db as db_module

    unsafe_key = "future\n\u202ePRIVATE_KEY_SENTINEL"
    unsafe_value = "PRIVATE_PAYLOAD_SENTINEL"
    cfg = _write_config(tmp_path, manifests=[ManifestDoc("agent-template", "default")])
    db.insert_vm("box", "lima-local", "box", template="default")
    db.insert_agent("dev", "box", "dev", template="default")
    db.instance_state.put_desired_overlay(
        "agent",
        "dev",
        VersionedPayload(1, {unsafe_key: unsafe_value}),
    )
    database_path = Path(db._conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)
    monkeypatch.setattr(db_module, "DB_PATH", database_path)

    result = CliRunner().invoke(app, ["doctor", "--output", "json"])

    combined = result.stdout + result.stderr
    assert unsafe_key not in combined
    assert "PRIVATE_KEY_SENTINEL" not in combined
    assert unsafe_value not in combined
    document = cast("dict[str, object]", json.loads(result.stdout))
    data = cast("dict[str, object]", document["data"])
    groups = cast("list[dict[str, object]]", data["groups"])
    database = next(group for group in groups if group["name"] == "Database")
    checks = cast("list[dict[str, object]]", database["checks"])
    state_facts = [
        cast("dict[str, object]", check["instance_state"])
        for check in checks
        if check.get("instance_state") is not None
    ]
    assert any(
        fact["fact_type"] == "unconsumed-record" and fact["instance_kind"] == "agent" and fact["instance_name"] == "dev"
        for fact in state_facts
    )


def test_malformed_current_schema_keeps_declared_doctor_registry(
    tmp_path: Path,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import db as db_module

    cfg = _write_config(tmp_path)
    db.insert_vm("box", "lima-local", "box")
    db._conn.execute("ALTER TABLE vms DROP COLUMN admin_template")
    db._conn.commit()
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    config = load_config(cfg, warn_issues=False)
    registry, live_failure = _build_doctor_registry(config, load_manifests(cfg.parent / "resources"))

    assert registry is not None
    assert registry.lookup("vm-template", "default") is not None
    assert live_failure is not None
    assert live_failure.status is Status.FAIL


def test_manifest_load_failure_keeps_other_rows(tmp_path: Path, monkeypatch) -> None:
    """A broken manifest FILE (parse error) gets a fail row without
    short-circuiting the rest of the report: TOML issue rows still
    render, and only the registry-dependent tail is skipped."""
    # A settings-side warning (an unexpected [operator] key) stands in for the TOML issue row: the old [named_console]
    # unknown-key warn is impossible now, since [named_console] is a resource
    # section that hard-errors rather than soft-warning.
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
        oops = true
        """)
    )
    write_manifests(tmp_path, "kind: [unclosed\n", filename="broken.yaml")
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, config, registry = _check_config()

    assert config is not None
    assert registry is None
    fails = [c for c in g.checks if c.name == "Manifest" and c.status == Status.FAIL]
    assert fails and "broken.yaml" in (fails[0].message or "")
    # The TOML unknown-key warn row still rendered after the fail.
    assert any(c.name == "Config" and c.status == Status.WARN and "oops" in (c.message or "") for c in g.checks)
    assert not any(c.name == "Config is valid" for c in g.checks)


# ---------------------------------------------------------------------------
# Where resolvability renders: the Secrets group, and not every resource
# that names a secret
# ---------------------------------------------------------------------------


_AZURE_SP_SITE = """\
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: azure-dev
spec:
  platform:
    name: azure-vm
    subscription_id: "0000"
    resource_group: agw-dev
    region: eastus
    auth:
      mode: service-principal
      tenant_id: tenant-1
      client_id: client-1
      secret: az-sp
"""


def _sp_site_config(tmp_path: Path) -> Path:
    """An operator config declaring an azure site with a service
    principal, whose client secret only the prompt source could
    supply."""
    cfg = _write_config(
        tmp_path,
        settings="""
        [plugins]
        system = ["azure"]

        [secret_config]
        sources = ["env-var", "prompt"]
        """,
    )
    write_manifests(tmp_path, _AZURE_SP_SITE, filename="site.yaml")
    return cfg


def test_prompt_only_site_secret_leaves_the_site_row_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site whose credential is obtainable only by prompting is a
    HEALTHY site, and doctor says so.

    Resolvability is a property of the operation's runtime world, not of
    the site, so it is predicted by the operation's preflight sweep and
    not by the node. Doctor invokes ``node.preflight`` per row and never
    sweeps, which is what keeps this row clean with no doctor-side
    wording machinery at all.
    """
    from agentworks.doctor import _check_vm_sites

    monkeypatch.delenv("AW_SECRET_AZ_SP", raising=False)
    config = load_config(_sp_site_config(tmp_path), warn_issues=False)
    registry = build_registry(config)

    g = _check_vm_sites(config, registry)
    row = next(c for c in g.checks if c.name == "azure-dev")
    focused = checks_for_resource(
        config,
        registry,
        ResourceIdentity("vm-site", "azure-dev"),
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    assert row.status is Status.OK, (row.status, row.message)
    assert "azure-vm" in (row.message or "")
    assert focused == (row,)


def test_focused_vm_site_preserves_bulk_preflight_exception_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import doctor
    from agentworks.errors import StateError
    from agentworks.vms import nodes

    class _FailingNode:
        def preflight(self, _context: object) -> None:
            raise StateError("preflight failed", hint="repair the local prerequisite")

    config = load_config(_sp_site_config(tmp_path), warn_issues=False)
    registry = build_registry(config)
    monkeypatch.setattr(nodes, "vm_site_node", lambda *_args: _FailingNode())

    bulk = doctor._check_vm_sites(config, registry)
    row = next(check for check in bulk.checks if check.name == "azure-dev")
    focused = checks_for_resource(
        config,
        registry,
        ResourceIdentity("vm-site", "azure-dev"),
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )

    assert focused == (row,)
    assert row.status is Status.WARN
    assert row.hint == "repair the local prerequisite"


def test_prompt_only_site_secret_still_renders_on_its_own_secret_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: resolvability renders ONCE, on the secret's own
    row in the Secrets group, where an operator can act on it, rather
    than being smeared across every resource that names it."""
    monkeypatch.delenv("AW_SECRET_AZ_SP", raising=False)
    config = load_config(_sp_site_config(tmp_path), warn_issues=False)
    registry = build_registry(config)

    g = _check_secrets(config, registry, tty_access=TtyInteractionAccess.UNAVAILABLE)
    row = next(c for c in g.checks if "az-sp" in c.name)
    assert row.status is Status.WARN
    assert row.secret_preview is not None
    assert row.secret_preview.status is PreviewStatus.BLOCKED
    assert row.secret_preview.reason == BlockReason.TTY_UNAVAILABLE.value


# The #310 regression pair (``test_config_load_validation_error_yields_fail_row_not_abort``
# and its ``run_checks``-level sibling ``test_run_checks_renders_full_report_on_config_validation_error``)
# was removed here. Both drove a load-time ValidationError from a non-conforming
# explicit [secrets.*] name so that _check_config's guard (which catches
# ValidationError, a SIBLING of ConfigError, not just ConfigError) could be
# proven not to abort the run. The TOML resource sunset (ADR 0022) makes that
# scenario structurally impossible: a [secrets.*] section now hard-errors as a
# resource-section ConfigError before any name validation, and a non-conforming
# name in a YAML manifest surfaces as ConfigError too (decode wraps every
# spec-level AgentworksError). No settings-side load path raises ValidationError,
# so the sibling-catch behavior can no longer be exercised through config load.
# The guard's explicit ValidationError branch is retained as defensive coverage.
