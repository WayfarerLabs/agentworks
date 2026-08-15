"""Settings that NAME resource rows, and the one check that they resolve.

A settings value naming a row that does not exist is a hard error (operator
ruling, 2026-08-07), wherever it appears, in the same shape a dangling
MANIFEST reference gets at finalize. Before this, one question had three
answers: a hard finalize error for manifests, a hand-written per-subsystem
check for ``defaults.site`` and for ``[secret_config].sources`` (each with
its own wording), and a settings-load refusal for ``[secret_backends.*]``
that fired on correctly-spelled plugin backends.

What these tests pin, and why each is here:

- The QUANTIFIER. ``test_every_setting_reference_source_is_covered`` asserts
  the case table below equals ``_SETTING_REF_SOURCES`` exactly, so a settings
  value added to that table without a dangling-name case fails here rather
  than shipping unexercised. Every behavioral test then parametrizes over the
  same table, so a new setting inherits all of them.
- The SEVERITY, per setting: dangling is a ``ConfigError`` at registry build.
- The BOUNDARY: presence resolves a reference; availability does not enter
  into it. A row that exists but is disabled or not-ready is NOT dangling,
  and these tests pin that separately from the error cases, because
  collapsing the two is the specific regression this change could cause.
- The ORDERING against ``secrets.validate_chain``, which reports a different
  and much worse error for the same typo if it runs first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config, setting_references
from agentworks.config.references import _SETTING_REF_SOURCES
from agentworks.errors import ConfigError
from tests.conftest import write_cfg

if TYPE_CHECKING:
    from pathlib import Path

#: One settings-TOML template per settings value that names a resource,
#: ``{name}`` standing in for the named row. Keyed by the setting's
#: operator-facing spelling, which is what ``_SETTING_REF_SOURCES`` keys on
#: and what the error prints.
_SETTING_TOML = {
    "defaults.site": '[defaults]\nsite = "{name}"\n',
    "[secret_config].sources": '[secret_config]\nsources = ["{name}"]\n',
}

#: One name that DOES resolve, per setting: a row the standard publishers
#: always publish. Used by the negative cases (a resolving name must not
#: raise) and by the ordering test.
_RESOLVING_NAME = {
    "defaults.site": "lima-local",
    "[secret_config].sources": "prompt",
}


def _build(tmp_path: Path, settings: str):  # type: ignore[no-untyped-def]
    """Load a settings-only config and build the finalized registry: the
    boundary where settings references are checked."""
    return build_registry(load_config(write_cfg(tmp_path, settings=settings), warn_issues=False))


def test_every_setting_reference_source_is_covered() -> None:
    """The case tables cover ``_SETTING_REF_SOURCES`` exactly.

    This is the quantifier the rest of the module leans on. Every behavioral
    test below parametrizes over ``_SETTING_TOML``, so without this equality
    a settings value added to ``_SETTING_REF_SOURCES`` would be validated in
    production and exercised by nothing: the suite would still pass, and the
    new setting's dangling-name behavior would be assumed rather than shown.
    """
    declared = {source.setting for source in _SETTING_REF_SOURCES}
    assert declared == set(_SETTING_TOML)
    assert declared == set(_RESOLVING_NAME)


def test_every_referenced_kind_is_a_registered_kind() -> None:
    """Each source names a kind the framework actually has.

    A typo in a ``kind`` string would not fail loudly: every lookup in that
    kind would miss, so every value of that setting would be reported as
    dangling, including correct ones. That failure mode is invisible until an
    operator hits it, so pin it here.
    """
    from agentworks.resources import KIND_REGISTRY

    unknown = sorted(s.kind for s in _SETTING_REF_SOURCES if s.kind not in KIND_REGISTRY)
    assert not unknown, f"settings reference unregistered kinds: {unknown}"


@pytest.mark.parametrize("setting", sorted(_SETTING_TOML))
def test_dangling_setting_reference_is_a_hard_error(setting: str, tmp_path: Path) -> None:
    """A name that resolves to nothing fails the registry build, naming the
    setting, the kind, and the value the operator wrote."""
    source = next(s for s in _SETTING_REF_SOURCES if s.setting == setting)
    with pytest.raises(ConfigError) as excinfo:
        _build(tmp_path, _SETTING_TOML[setting].format(name="no-such-row"))
    message = str(excinfo.value)
    assert setting in message
    assert f"references unknown {source.kind}" in message
    assert "no-such-row" in message


@pytest.mark.parametrize("setting", sorted(_SETTING_TOML))
def test_dangling_hint_enumerates_what_is_declared(setting: str, tmp_path: Path) -> None:
    """The hint names the rows that DO exist.

    For both settings today the mistake is overwhelmingly a typo, so the
    declared set is the whole remedy; a hint that only said "declare one"
    would make the operator go and look it up.
    """
    with pytest.raises(ConfigError) as excinfo:
        _build(tmp_path, _SETTING_TOML[setting].format(name="no-such-row"))
    hint = excinfo.value.hint or ""
    assert _RESOLVING_NAME[setting] in hint


def test_declarable_kind_hint_points_at_the_sample_command(tmp_path: Path) -> None:
    """``vm-site`` is declarable, so declaring the missing row is real advice."""
    with pytest.raises(ConfigError) as excinfo:
        _build(tmp_path, _SETTING_TOML["defaults.site"].format(name="no-such-row"))
    assert "agw resource sample vm-site" in (excinfo.value.hint or "")


def test_secret_source_hint_points_at_the_declarable_sample(tmp_path: Path) -> None:
    """The active chain names declarable ``secret-source`` rows."""
    with pytest.raises(ConfigError) as excinfo:
        _build(tmp_path, _SETTING_TOML["[secret_config].sources"].format(name="no-such-row"))
    hint = excinfo.value.hint or ""
    assert "agw resource sample secret-source" in hint


@pytest.mark.parametrize("setting", sorted(_SETTING_TOML))
def test_a_resolving_name_builds_cleanly(setting: str, tmp_path: Path) -> None:
    """The check refuses dangling names and nothing else: a name that
    resolves must not raise. Without this, a check that rejected EVERY value
    would pass every error test above."""
    _build(tmp_path, _SETTING_TOML[setting].format(name=_RESOLVING_NAME[setting]))


def test_unset_scalar_setting_references_nothing(tmp_path: Path) -> None:
    """``defaults.site`` is optional. Unset means it names nothing, not that
    it names ``None``."""
    config = load_config(write_cfg(tmp_path, settings="[defaults]\n"), warn_issues=False)
    assert [ref.name for ref in setting_references(config) if ref.setting == "defaults.site"] == []


def test_list_setting_yields_one_reference_per_name(tmp_path: Path) -> None:
    """A list-valued setting flattens: callers see (setting, kind, name)
    triples regardless of the setting's shape."""
    config = load_config(
        write_cfg(tmp_path, settings='[secret_config]\nsources = ["env-var", "prompt"]\n'),
        warn_issues=False,
    )
    chain = [ref.name for ref in setting_references(config) if ref.setting == "[secret_config].sources"]
    assert chain == ["env-var", "prompt"]


# ---------------------------------------------------------------------------
# Presence, not availability. A row that exists but cannot be used here is NOT
# a dangling reference; conflating the two would break every command on a host
# that merely lacks an optional requirement.
# ---------------------------------------------------------------------------


def test_same_name_declared_source_wins_before_direct_backend_remediation(tmp_path: Path) -> None:
    """A real source named ``onepassword`` wins even while its backend plugin is disabled."""
    from tests.conftest import ManifestDoc, write_manifests

    write_manifests(
        tmp_path,
        ManifestDoc("secret-source", "onepassword", {"backend": {"name": "onepassword"}}),
    )
    registry = _build(tmp_path, '[secret_config]\nsources = ["env-var", "onepassword"]\n')
    assert registry.lookup("secret-source", "onepassword") is not None
    assert not registry.graph.readiness_of("secret-source", "onepassword").is_ready


def test_a_not_ready_site_is_not_dangling(tmp_path: Path) -> None:
    """``lima-local`` is published on every host and is not-ready without
    ``limactl``. ``defaults.site`` naming it stays loadable: using it is a
    typed error at resolve time and doctor warns on the reference, which is
    the vm-sites design and is deliberately untouched here."""
    registry = _build(tmp_path, '[defaults]\nsite = "lima-local"\n')
    assert registry.lookup("vm-site", "lima-local") is not None


# ---------------------------------------------------------------------------
# Ordering against the per-subsystem semantic checks.
# ---------------------------------------------------------------------------


def test_a_misspelled_source_reports_as_a_bad_name_not_an_unreachable_secret(tmp_path: Path) -> None:
    """Reference checking must run BEFORE ``secrets.validate_chain``.

    A misspelled source name matches no edge, so it simply drops out of the
    reachability intersection: run the other way round, this config reports
    "unreachable secret(s): npm-token" and sends the operator to inspect a
    secret that is fine. Pins the ordering in ``bootstrap.build_registry``,
    not just that both checks exist.
    """
    from tests.conftest import ManifestDoc, write_manifests

    write_manifests(tmp_path, ManifestDoc("secret", "npm-token", description="npm token"))
    with pytest.raises(ConfigError) as excinfo:
        _build(tmp_path, '[secret_config]\nsources = ["envvar"]\n')
    message = str(excinfo.value)
    assert "references unknown secret-source 'envvar'" in message
    assert "unreachable" not in message
