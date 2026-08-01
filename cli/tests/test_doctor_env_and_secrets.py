"""Tests for the env-and-secrets doctor health groups (FRD R6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.doctor import Status, _check_config, _check_secrets, run_checks
from agentworks.errors import ConfigError, ValidationError


def _write_config(tmp_path: Path, *, extras: str = "") -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""\
[operator]
ssh_public_key = "{pub.as_posix()}"
ssh_private_key = "{priv.as_posix()}"

[vm_templates.default]

[admin.config]
shell = "zsh"

[defaults]
{extras}
"""
    )
    return cfg


# ---------------------------------------------------------------------------
# _check_secrets
# ---------------------------------------------------------------------------


def test_auto_declared_secrets_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor reports EVERY registry secret, auto-declared included --
    they are exactly the ones most likely to prompt at command time,
    so hiding them made doctor unable to predict the next command.
    A bare config still carries the framework-auto-declared
    ``tailscale-auth-key`` (vm-template requirement); it shows with an
    ``(auto)`` marker and an honest would-resolve-via-prompt heads-up.
    """
    # Hosts that operate real VMs export this; clear it so the
    # would-resolve-via-prompt assertion reflects the bare config, not the
    # test host's environment (mirrors the sibling tests' delenv discipline).
    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY", raising=False)
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config))
    assert g.name == "Secrets"
    statuses = [(c.name, c.status, c.message) for c in g.checks]
    assert statuses == [("Secret 'tailscale-auth-key' (auto)", Status.OK, "would resolve via prompt")], statuses


def test_secret_resolves_via_env_var_when_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AW_SECRET_<NAME> is set, doctor reports the secret as resolving
    via env-var."""
    monkeypatch.setenv("AW_SECRET_SHARED", "from-operator-env")
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "Shared API token"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config))
    msgs = [(c.status, c.name, c.message) for c in g.checks]
    assert any(
        status == Status.OK and "shared" in name and "would resolve via env-var" in (msg or "")
        for status, name, msg in msgs
    ), msgs


def test_secret_resolves_via_prompt_when_env_var_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When env-var has nothing and prompt is in the chain, doctor reports
    the secret as resolving via prompt -- prompt is just another backend."""
    monkeypatch.delenv("AW_SECRET_SHARED", raising=False)
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "Shared API token"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config))
    oks = [c for c in g.checks if c.status == Status.OK]
    assert any("shared" in c.name and "would resolve via prompt" in (c.message or "") for c in oks), [
        (c.name, c.message) for c in oks
    ]


def test_secret_not_available_when_env_var_unset_and_prompt_opted_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When prompt is opted out via backend_mappings.prompt = false AND
    env-var has no value, doctor reports the secret as WARN (config is
    valid but no backend in the chain would resolve it)."""
    monkeypatch.delenv("AW_SECRET_OPTED_OUT", raising=False)
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "opted-out" }

[secrets.opted-out]
description = "Must come from env-var"
backend_mappings.prompt = false

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config))
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any("opted-out" in c.name and "not available" in (c.message or "") for c in warns), [
        (c.name, c.message) for c in warns
    ]


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
        _write_config(tmp_path, extras='[plugins]\nsystem = ["onepassword"]\n'),
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

    # The System plugins roster IS the enablement authority: it lists the
    # disabled backend's plugin as disabled.
    roster = {c.name: c for c in _check_plugins(config).checks}
    assert roster["plugin onepassword"].status is Status.INFO
    assert "not enabled in [plugins].system" in (roster["plugin onepassword"].message or "")


def test_check_secrets_flags_a_not_ready_only_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R9.6: a secret whose only attempting backend is not-ready is at-risk;
    ``_check_secrets`` warns and names the not-ready backend rather than
    falsely predicting resolution via it (lockstep with the resolution skip)."""
    monkeypatch.setattr("shutil.which", lambda name: None)  # op absent
    cfg = _write_config(
        tmp_path,
        extras="""
[plugins]
system = ["onepassword"]

[admin.env]
TOKEN = { secret = "op-only" }

[secrets.op-only]
description = "resolves only via onepassword"
backend_mappings.onepassword = "op://Vault/item/field"
backend_mappings.env-var = false

[secret_config]
backends = ["onepassword"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config))
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any(
        "op-only" in c.name and "not ready" in (c.message or "") and "op CLI not installed" in (c.message or "")
        for c in warns
    ), [(c.name, c.message) for c in warns]


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
    cfg = _write_config(tmp_path, extras='[plugins]\nsystem = ["azure"]')
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir()
    (resources_dir / "res.yaml").write_text(
        "apiVersion: agentworks/v1\n"
        "kind: git-credential\n"
        "metadata:\n"
        "  name: ado\n"
        "spec:\n"
        "  provider: azdo\n"
        "  provider_config:\n"
        "    org: my-org\n"
        "    bogus: 1\n"
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, registry = _check_config()

    fails = {c.name: c for c in g.checks if c.status == Status.FAIL}
    assert "Resource registry" in fails
    assert "unknown azdo provider field" in (fails["Resource registry"].message or "")
    assert "Manifest" not in fails  # the malformed block is no longer a decode/load failure
    assert registry is None  # the registry-dependent tail is skipped after the failure


def test_mapping_to_undeclared_kind_hard_errors_at_build(tmp_path: Path) -> None:
    """R9.11: a ``backend_mappings`` entry naming a backend that is not a
    registered ``secret-backend`` capability is a DANGLING ``secret ->
    secret-backend`` edge, which the ``secret-backend`` kind's ``"error"``
    miss policy turns into a hard ``build_registry`` failure (where the old
    tolerant ``_check_secrets`` pinpointed it as one per-secret FAIL row).

    Doctor-granularity regression (acknowledged, R9.11): because the build
    now raises, a doctor run collapses its whole registry-dependent tail to
    one "Resource registry: FAIL" row rather than pinpointing the secret.
    """
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "shared token"
backend_mappings.bogusvault = "x"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError, match="unknown secret-backend 'bogusvault'"):
        build_registry(config)


def test_mapping_to_multiple_undeclared_kinds_hard_errors_at_build(tmp_path: Path) -> None:
    """R9.11: with two unknown-backend mappings, the first dangling edge the
    resolve pass reaches hard-errors at ``build_registry`` (naming that
    backend); the build never gets far enough to enumerate both, unlike the
    old tolerant per-secret doctor row that listed them sorted."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "shared token"
backend_mappings.zeta-vault = "z"
backend_mappings.alpha-vault = "a"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError, match="unknown secret-backend '(alpha-vault|zeta-vault)'"):
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
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
AGENTWORKS_SESSION = "operator-override"
""",
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, _ = _check_config()
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any("AGENTWORKS_SESSION" in (c.message or "") for c in warns), [(c.name, c.message) for c in warns]


def test_doctor_surfaces_deprecation_nudges(tmp_path: Path, monkeypatch) -> None:
    """Deprecations moved off config_issues onto their own channel (so
    --no-deprecations can silence the ambient per-command warning);
    doctor is the explicit full-health surface and must still show them
    -- the channel split silently dropped them from doctor once.

    Doctor renders the FACT as a tidy one-liner (maintainer ruling,
    2026-07-06): one next step (`agw resource migrate`), no section
    list, no teaching text -- that stays on the ambient warning."""
    cfg = _write_config(tmp_path)  # has [vm_templates.default] + [admin.config]
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, _ = _check_config()
    warns = [(c.name, c.message or "") for c in g.checks if c.status == Status.WARN]
    ((name, message),) = [w for w in warns if "deprecated TOML resource" in w[0]]
    # Maintainer-specified row shape: the check NAME carries the fact,
    # the parenthetical carries the one next step.
    assert name == "Config has deprecated TOML resource declarations"
    assert message == "migrate to YAML with `agw resource migrate`"
    # The tidy pin: none of the ambient teaching text leaks into doctor.
    line = f"{name} {message}"
    assert "--no-deprecations" not in line
    assert "resource sample" not in line
    assert "[vm_templates.*]" not in line


def test_doctor_shows_noop_secret_backend_sections(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        extras="""
[secret_backends.env-var]
""",
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, _ = _check_config()
    warns = [(c.name, c.message or "") for c in g.checks if c.status == Status.WARN]
    assert any("[secret_backends.env-var]" in name and "remove it" in message for name, message in warns), warns


def test_manifest_issues_surface_as_doctor_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    """A typo'd key on a manifest-declared resource (e.g.
    ``github_credentials`` for ``git_credentials`` on an agent-template)
    used to warn ambiently above the report while the Config row said
    ok. Doctor now renders manifest issues as warn rows, and passing
    the pre-loaded set into build_registry keeps the ambient print out
    of doctor's output entirely."""
    from textwrap import dedent

    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "agent.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: agent-template
        metadata:
          name: other
        spec:
          github_credentials: ["github"]
        """)
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
    assert "github_credentials" in (manifest_rows[0].message or "")
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


def test_manifest_load_failure_keeps_other_rows(tmp_path: Path, monkeypatch) -> None:
    """A broken manifest FILE (parse error) gets a fail row without
    short-circuiting the rest of the report: TOML issue rows still
    render, and only the registry-dependent tail is skipped."""
    cfg = _write_config(
        tmp_path,
        extras="""\
[named_console]
bogus_key = 1
""",
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "broken.yaml").write_text("kind: [unclosed\n")
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, config, registry = _check_config()

    assert config is not None
    assert registry is None
    fails = [c for c in g.checks if c.name == "Manifest" and c.status == Status.FAIL]
    assert fails and "broken.yaml" in (fails[0].message or "")
    # The TOML unknown-key warn row still rendered after the fail.
    assert any(c.name == "Config" and c.status == Status.WARN and "bogus_key" in (c.message or "") for c in g.checks)
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
  platform: azure-vm
  platform_config:
    subscription_id: "0000"
    resource_group: agw-dev
    region: eastus
    service_principal:
      tenant_id: tenant-1
      client_id: client-1
      secret: az-sp
"""


def _sp_site_config(tmp_path: Path) -> Path:
    """An operator config declaring an azure site with a service
    principal, whose client secret only the prompt backend could
    supply."""
    cfg = _write_config(
        tmp_path,
        extras="""
[plugins]
system = ["azure"]

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "site.yaml").write_text(_AZURE_SP_SITE)
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
    assert row.status is Status.OK, (row.status, row.message)
    assert "azure-vm" in (row.message or "")


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

    g = _check_secrets(config, registry)
    row = next(c for c in g.checks if "az-sp" in c.name)
    assert row.status is Status.OK
    assert "would resolve via prompt" in (row.message or "")


def test_config_load_validation_error_yields_fail_row_not_abort(tmp_path: Path, monkeypatch) -> None:
    """Regression for #310: a non-conforming explicit [secrets.*] name makes
    _load_secrets raise ValidationError at config load. ValidationError is a
    SIBLING of ConfigError under AgentworksError (not a subclass), so the
    config-load guard must name it explicitly; otherwise it escapes _check_config
    and aborts the whole doctor run. Here it must instead produce a Config FAIL
    row (carrying the ValidationError's hint) and return cleanly."""
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.Bad-Name]
description = "uppercase name is non-conforming"
""",
    )
    # Precondition: this config really does raise ValidationError at load.
    with pytest.raises(ValidationError):
        load_config(cfg, warn_issues=False)

    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, config, registry = _check_config()  # must not raise / abort

    assert config is None
    assert registry is None
    fails = [c for c in g.checks if c.name == "Config" and c.status == Status.FAIL]
    assert fails and "Bad-Name" in (fails[0].message or "")


@pytest.mark.integration
def test_run_checks_renders_full_report_on_config_validation_error(tmp_path: Path, monkeypatch) -> None:
    """Regression for #310, at the run_checks level: a load-time ValidationError
    must not abort the run. Doctor's contract is maximal visibility in one run,
    so the full report still renders (config-free groups present) with a
    Configuration FAIL row, and report.has_failures stays True (the signal the
    CLI uses to exit nonzero). Integration for the same reason as the other
    run_checks tests: the config-free groups probe the real environment."""
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.Bad-Name]
description = "uppercase name is non-conforming"
""",
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)

    report = run_checks()  # must not raise / abort

    group_names = [g.name for g in report.groups]
    # The rest of the report still rendered: config-free groups are present.
    for name in ("System", "Python", "Required tools"):
        assert name in group_names
    # The Configuration group carries the load failure as a FAIL row.
    config_group = next(g for g in report.groups if g.name == "Configuration")
    assert any(c.name == "Config" and c.status == Status.FAIL for c in config_group.checks)
    # The fail row is what drives a nonzero exit at the CLI surface.
    assert report.has_failures is True
