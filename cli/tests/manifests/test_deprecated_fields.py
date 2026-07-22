"""The general deprecated-field notices facility (FRD R11).

Table-driven coverage of the generic checker, the ``session-template``
flat-field seed entries (which replaced the bespoke reject in
``_decode_session_template``), the doctor scan, and the doctor finding.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests
from agentworks.manifests.deprecated_fields import (
    DEPRECATED_FIELDS,
    DeprecatedField,
    check_deprecated_fields,
    manifest_deprecation_notices,
)

# ---------------------------------------------------------------------------
# The generic checker (synthetic table via a fake kind key)
# ---------------------------------------------------------------------------


@pytest.fixture()
def synthetic_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake kind's entries: two ``error`` fields sharing one message and
    one ``warn`` field. ``check_deprecated_fields`` keys purely on the
    string, so the kind need not be a real one."""
    monkeypatch.setitem(
        DEPRECATED_FIELDS,
        "test-kind",
        (
            DeprecatedField("old_a", "error", "are gone; do X instead"),
            DeprecatedField("old_b", "error", "are gone; do X instead"),
            DeprecatedField("stale", "warn", "are vestigial and ignored"),
        ),
    )


def test_error_fields_raise_one_grouped_message(synthetic_table: None) -> None:
    with pytest.raises(ConfigError) as exc:
        check_deprecated_fields("test-kind", {"old_a": 1, "old_b": 2, "ok": 3})
    # Fields sharing a message group onto one line, alphabetized.
    assert str(exc.value) == "test-kind spec field(s) old_a, old_b are gone; do X instead"


def test_single_error_field_raises(synthetic_table: None) -> None:
    with pytest.raises(ConfigError, match=r"test-kind spec field\(s\) old_a are gone; do X instead"):
        check_deprecated_fields("test-kind", {"old_a": 1})


def test_warn_field_returns_notice_without_raising(synthetic_table: None) -> None:
    assert check_deprecated_fields("test-kind", {"stale": 1}) == [
        "test-kind spec field(s) stale are vestigial and ignored"
    ]


def test_warn_field_is_stripped_from_spec(synthetic_table: None) -> None:
    # "ignore the field" is self-contained: the checker removes the warn
    # field from the spec it forwards, so the per-kind decoder never sees
    # it and cannot re-reject it.
    spec = {"stale": 1, "keep": 2}
    check_deprecated_fields("test-kind", spec)
    assert "stale" not in spec
    assert spec == {"keep": 2}


def test_error_takes_precedence_over_warn(synthetic_table: None) -> None:
    # An error-level match still raises even when a warn field is present;
    # the warn notice is not emitted (the load fails first).
    with pytest.raises(ConfigError):
        check_deprecated_fields("test-kind", {"old_a": 1, "stale": 1})


def test_clean_spec_is_unaffected(synthetic_table: None) -> None:
    assert check_deprecated_fields("test-kind", {"fine": 1, "also_fine": 2}) == []


def test_kind_without_a_table_returns_empty() -> None:
    assert check_deprecated_fields("no-such-kind", {"whatever": 1}) == []


# ---------------------------------------------------------------------------
# The session-template seed entries: parity with the old bespoke reject
# ---------------------------------------------------------------------------


def _manifest(tmp_path: Path, spec_lines: list[str], name: str = "sess") -> Path:
    """Write a session-template manifest whose ``spec`` is ``spec_lines``
    (each already at its intended relative indentation under ``spec:``)."""
    root = tmp_path / "resources"
    root.mkdir(parents=True, exist_ok=True)
    body = "".join(f"  {line}\n" for line in spec_lines)
    (root / "res.yaml").write_text(
        f"apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: {name}\nspec:\n{body}"
    )
    return root


def test_session_template_flat_fields_rejected_with_preserved_message(
    tmp_path: Path,
) -> None:
    root = _manifest(
        tmp_path,
        [
            "command: htop",
            "restart_command: htop",
            "required_commands: [htop]",
        ],
    )
    with pytest.raises(ConfigError) as exc:
        load_manifests(root)
    # The exact operator-facing phrasing the bespoke reject used, grouped
    # into one message (alphabetized), now data-driven from the table.
    assert (
        "session-template spec field(s) command, required_commands, "
        "restart_command are the 'shell' harness's config; set harness: "
        "shell and move them under spec.harness_config"
    ) in str(exc.value)


def test_session_template_single_flat_field_rejected(tmp_path: Path) -> None:
    root = _manifest(tmp_path, ["command: htop"])
    with pytest.raises(ConfigError, match="move them under spec.harness_config"):
        load_manifests(root)


def test_clean_session_template_manifest_loads(tmp_path: Path) -> None:
    root = _manifest(
        tmp_path,
        [
            "harness: shell",
            "harness_config:",
            "  command: htop",
            "  required_commands: [htop]",
        ],
    )
    manifests = load_manifests(root)
    assert len(manifests.entries) == 1
    assert not manifests.issues


# ---------------------------------------------------------------------------
# Doctor scan + finding (warn level)
# ---------------------------------------------------------------------------


@pytest.fixture()
def warn_seeded_session_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a warn-level entry on the real ``session-template`` kind so the
    doctor path can be exercised (no warn entries ship today)."""
    monkeypatch.setitem(
        DEPRECATED_FIELDS,
        "session-template",
        (DeprecatedField("legacy_note", "warn", "are vestigial and ignored"),),
    )


def test_manifest_deprecation_notices_finds_warn_field(tmp_path: Path, warn_seeded_session_template: None) -> None:
    root = _manifest(
        tmp_path,
        ["harness: shell", "legacy_note: something"],
    )
    notices = manifest_deprecation_notices(root)
    assert len(notices) == 1
    assert notices[0].endswith("session-template spec field(s) legacy_note are vestigial and ignored")
    assert "res.yaml:" in notices[0]


def test_manifest_deprecation_notices_ignores_clean_and_error_fields(
    tmp_path: Path,
) -> None:
    # With the real (error-only) table, a clean manifest yields nothing,
    # and an error-level field is not reported here (it fails the load and
    # is reported by doctor's config-load check instead).
    root = _manifest(
        tmp_path,
        ["harness: shell", "harness_config:", "  command: htop"],
    )
    assert manifest_deprecation_notices(root) == []


def _doctor_config(tmp_path: Path) -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub.as_posix()}"
            ssh_private_key = "{priv.as_posix()}"

            [vm_templates.default]

            [admin.config]
            shell = "zsh"
            """
        )
    )
    return cfg


def test_doctor_surfaces_warn_level_deprecated_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    warn_seeded_session_template: None,
) -> None:
    from agentworks.doctor import Status, _check_config

    cfg = _doctor_config(tmp_path)
    _manifest(
        tmp_path,
        ["harness: shell", "legacy_note: something"],
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)

    group, _config, _registry = _check_config()

    dedicated = [
        c
        for c in group.checks
        if c.name == "Deprecated manifest field" and c.status is Status.WARN and "legacy_note" in (c.message or "")
    ]
    assert len(dedicated) == 1
    # The dedicated finding is not also emitted as a generic Manifest row
    # (the notice string is filtered out of that channel).
    assert not any(c.name == "Manifest" and c.message == dedicated[0].message for c in group.checks)
