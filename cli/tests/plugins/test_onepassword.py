"""Tests for the ``onepassword`` secret backend.

The subprocess boundary is faked: every ``op`` call in the module goes
through the module-level ``_run_op`` seam, which these tests monkeypatch
so no real ``op`` binary is ever invoked. The only call the backend makes
is ``op read`` (there is no ``op whoami`` sign-in pre-check; the read is
the liveness probe), so the fake dispatches on the ``read`` argv.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import (
    ConfigError,
    ConnectivityError,
    ExternalError,
    SecretMappingError,
)
from agentworks.plugins.onepassword import backend as op_mod
from agentworks.plugins.onepassword.backend import OnePasswordBackend, _OpResult
from agentworks.resources.graph import Readiness
from agentworks.secrets import active_backends, resolve_secrets
from agentworks.secrets.base import SecretDecl
from agentworks.secrets.resolve import ActiveBackend, preview_resolution
from tests.conftest import ManifestDoc, write_manifests


def _decl(name: str, **kw: Any) -> SecretDecl:
    return SecretDecl(name=name, description=f"{name} description", **kw)


def _install_runner(monkeypatch: pytest.MonkeyPatch, runner: Any) -> None:
    monkeypatch.setattr(op_mod, "_run_op", runner)


def _fake_op(
    *,
    values: dict[str, str] | None = None,
    read_errors: dict[str, tuple[int, str]] | None = None,
) -> Any:
    """A ``_run_op`` double for ``op read``. ``read <uri>`` returns the
    mapped value, or the mapped (returncode, stderr) failure, or a generic
    not-found. The op:// reference is always the last argv element, in both
    the bare and ``--account`` forms."""
    values = values or {}
    read_errors = read_errors or {}
    calls: list[list[str]] = []

    def run(args: list[str]) -> _OpResult:
        calls.append(args)
        # op read --no-newline [--account <acct>] <uri>
        uri = args[-1]
        if uri in read_errors:
            code, stderr = read_errors[uri]
            return _OpResult(code, "", stderr)
        if uri in values:
            return _OpResult(0, values[uri], "")
        return _OpResult(1, "", f'"{uri}" isn\'t an item in the vault')

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _backend_chain() -> list[ActiveBackend]:
    return [ActiveBackend(capability=OnePasswordBackend(), readiness=Readiness.ready())]


# -- would_attempt -----------------------------------------------------------


def test_would_attempt_only_for_mapped_secret() -> None:
    backend = OnePasswordBackend()
    mapped = _decl("s1", backend_mappings={"onepassword": "op://Work/npm/token"})
    unmapped = _decl("s2")
    assert backend.would_attempt(mapped, mapped.backend_mappings["onepassword"])
    # Unmapped secrets soft-skip (no derive-from-name convention).
    assert not backend.would_attempt(unmapped, None)


# -- mapping resolution / describe_lookup ------------------------------------


def test_bare_string_resolves_without_account_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _fake_op(values={"op://Work/npm/token": "secret-value"})
    _install_runner(monkeypatch, runner)
    backend = OnePasswordBackend()

    uri = "op://Work/npm/token"
    secret = _decl("s-str", backend_mappings={"onepassword": uri})
    assert backend.describe_lookup(secret, secret.backend_mappings["onepassword"]) == uri
    got = backend.batch_get([(secret, secret.backend_mappings["onepassword"])])
    assert got == {"s-str": "secret-value"}
    # The bare string uses op's default account: no --account flag anywhere.
    assert all("--account" not in c for c in runner.calls)
    read_calls = [c for c in runner.calls if c[0] == "read"]
    assert read_calls == [["read", "--no-newline", uri]]


def test_table_form_resolves_with_account_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The {account, reference} table passes --account <acct> to `op read`
    and reads the native op:// reference."""
    uri = "op://Work/npm/token"
    runner = _fake_op(values={uri: "secret-value"})
    _install_runner(monkeypatch, runner)
    backend = OnePasswordBackend()

    mapping = {"account": "my.1password.com", "reference": uri}
    secret = _decl("s-tbl", backend_mappings={"onepassword": mapping})
    got = backend.batch_get([(secret, secret.backend_mappings["onepassword"])])
    assert got == {"s-tbl": "secret-value"}
    read_calls = [c for c in runner.calls if c[0] == "read"]
    assert read_calls == [["read", "--no-newline", "--account", "my.1password.com", uri]]


def test_section_bearing_reference_validates_and_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reference carrying the optional section segment (which the removed
    {vault, item, field} table could not express) validates and reads."""
    uri = "op://Work/npm/section/token"
    runner = _fake_op(values={uri: "sectioned-value"})
    _install_runner(monkeypatch, runner)
    backend = OnePasswordBackend()

    backend.validate_mapping("secret 's'", uri)
    secret = _decl("s-sec", backend_mappings={"onepassword": uri})
    got = backend.batch_get([(secret, secret.backend_mappings["onepassword"])])
    assert got == {"s-sec": "sectioned-value"}


def test_describe_lookup_includes_account_when_set() -> None:
    backend = OnePasswordBackend()
    uri = "op://Work/npm/token"
    with_account = _decl(
        "s-acct",
        backend_mappings={"onepassword": {"account": "my.1password.com", "reference": uri}},
    )
    bare = _decl("s-bare", backend_mappings={"onepassword": uri})
    # Account-first: "<account>: <reference>" (position conveys "account").
    assert (
        backend.describe_lookup(with_account, with_account.backend_mappings["onepassword"])
        == f"my.1password.com: {uri}"
    )
    assert backend.describe_lookup(bare, bare.backend_mappings["onepassword"]) == uri


def test_describe_lookup_none_when_unmapped() -> None:
    backend = OnePasswordBackend()
    assert backend.describe_lookup(_decl("s"), None) is None


# -- validate_mapping --------------------------------------------------------


def test_validate_mapping_accepts_valid_forms() -> None:
    backend = OnePasswordBackend()
    backend.validate_mapping("secret 's'", "op://Work/npm/token")
    backend.validate_mapping(
        "secret 's'",
        {"account": "my.1password.com", "reference": "op://Work/npm/token"},
    )
    # A section segment (4 parts) is allowed, in both forms.
    backend.validate_mapping("secret 's'", "op://Work/npm/section/token")
    backend.validate_mapping(
        "secret 's'",
        {"account": "acct", "reference": "op://Work/npm/section/token"},
    )


@pytest.mark.parametrize(
    "mapping",
    [
        pytest.param(123, id="wrong-type"),
        pytest.param("", id="empty-string"),
        pytest.param("Work/npm/token", id="missing-scheme"),
        pytest.param("op://Work/token", id="too-few-segments"),
        pytest.param("op://Work//token", id="blank-segment"),
        pytest.param({"reference": "op://Work/npm/token"}, id="table-missing-account"),
        pytest.param(
            {"account": "", "reference": "op://Work/npm/token"},
            id="table-blank-account",
        ),
        pytest.param({"account": "acct"}, id="table-missing-reference"),
        pytest.param({"account": "acct", "reference": ""}, id="table-blank-reference"),
        pytest.param(
            {"account": "acct", "reference": "not-a-ref"},
            id="table-bad-reference",
        ),
        pytest.param(
            {"account": "acct", "reference": "op://Work/npm/token", "x": 1},
            id="table-unknown-key",
        ),
    ],
)
def test_validate_mapping_rejects_bad_forms(mapping: Any) -> None:
    backend = OnePasswordBackend()
    with pytest.raises(ConfigError):
        backend.validate_mapping("secret 's'", mapping)


def test_validate_mapping_rejects_vault_item_field_table() -> None:
    """A {vault, item, field} table is rejected as a plain unknown-key
    ConfigError (naming the keys), with no migration language: those keys
    never shipped, so there is nothing to migrate from."""
    backend = OnePasswordBackend()
    with pytest.raises(ConfigError, match="unknown key") as excinfo:
        backend.validate_mapping(
            "secret 's'",
            {"vault": "Work", "item": "npm", "field": "token"},
        )
    message = str(excinfo.value)
    assert "'field', 'item', 'vault'" in message
    for word in ("no longer", "migrat", "legacy"):
        assert word not in message.lower()


def _config(tmp_path: Path, body: str = "", *, manifests: Sequence[ManifestDoc | str] = ()) -> Any:
    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + dedent(body)
    )
    if manifests:
        write_manifests(tmp_path, *manifests)
    return load_config(cfg, warn_issues=False)


def test_validate_chain_surfaces_malformed_mapping_at_build_registry(
    tmp_path: Path,
) -> None:
    """A malformed onepassword mapping on an active-chain secret fails at
    build_registry (the load-time gate), not at first resolution."""
    config = _config(
        tmp_path,
        """
        [plugins]
        system = ["onepassword"]

        [secret_config]
        backends = ["onepassword", "prompt"]
        """,
        manifests=[
            ManifestDoc(
                "secret",
                "npm-token",
                {"backend_mappings": {"onepassword": "not-a-valid-ref"}},
                description="npm token",
            )
        ],
    )
    with pytest.raises(ConfigError, match="onepassword"):
        build_registry(config)


def test_valid_mapping_passes_build_registry(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [plugins]
        system = ["onepassword"]

        [secret_config]
        backends = ["onepassword", "prompt"]
        """,
        manifests=[
            ManifestDoc(
                "secret",
                "npm-token",
                {"backend_mappings": {"onepassword": "op://Work/npm/token"}},
                description="npm token",
            )
        ],
    )
    registry = build_registry(config)
    names = [b.name for b in active_backends(config, registry)]
    assert names == ["onepassword", "prompt"]


# -- batch_get: miss / failure semantics -------------------------------------


def test_batch_get_returns_found_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(monkeypatch, _fake_op(values={"op://Work/npm/token": "the-token"}))
    backend = OnePasswordBackend()
    secret = _decl("npm", backend_mappings={"onepassword": "op://Work/npm/token"})
    got = backend.batch_get([(secret, secret.backend_mappings["onepassword"])])
    assert got == {"npm": "the-token"}


def test_batch_get_absent_item_raises_secret_mapping_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runner(monkeypatch, _fake_op(values={}))  # every read is not-found
    backend = OnePasswordBackend()
    secret = _decl("gone", backend_mappings={"onepassword": "op://Work/gone/token"})
    with pytest.raises(SecretMappingError, match="op://Work/gone/token"):
        backend.batch_get([(secret, secret.backend_mappings["onepassword"])])


def test_hard_miss_halts_chain_before_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mapped-but-absent 1Password secret raises SecretMappingError, which
    the resolve loop re-raises: a later backend NEVER runs, so a stale
    mapping cannot silently fall through to a prompt."""
    _install_runner(monkeypatch, _fake_op(values={}))

    class _ExplodingBackend:
        name = "later"
        interactive = False

        def would_attempt(self, secret: Any, mapping: Any) -> bool:
            return True

        def describe_lookup(self, secret: Any, mapping: Any) -> str | None:
            return None

        def batch_get(self, wants: list[tuple[Any, Any]]) -> dict[str, str]:
            raise AssertionError("later backend must not run after a hard miss")

    op_chain = ActiveBackend(capability=OnePasswordBackend(), readiness=Readiness.ready())
    later = ActiveBackend(capability=_ExplodingBackend(), readiness=Readiness.ready())  # type: ignore[arg-type]
    secret = _decl("gone", backend_mappings={"onepassword": "op://Work/gone/token"})
    with pytest.raises(SecretMappingError):
        resolve_secrets([secret], [op_chain, later])


def _signed_out_read(uri: str) -> Any:
    """A ``_run_op`` double whose `op read` for ``uri`` fails with a
    signed-out marker (the real-world app-integration failure mode: no
    whoami gate, the read itself reports it)."""
    return _fake_op(read_errors={uri: (1, "[ERROR] account is not signed in.")})


def test_signed_out_read_default_account_raises_connectivity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare op:// string reads against op's default account. A signed-out
    `op read` (there is no whoami pre-check) raises ConnectivityError with
    the generic message and a hint pointing at OP_ACCOUNT / the table form."""
    uri = "op://Work/npm/token"
    _install_runner(monkeypatch, _signed_out_read(uri))
    backend = OnePasswordBackend()
    secret = _decl("npm", backend_mappings={"onepassword": uri})
    with pytest.raises(ConnectivityError) as excinfo:
        backend.batch_get([(secret, secret.backend_mappings["onepassword"])])
    err = excinfo.value
    assert str(err) == "not signed in to the 1Password CLI"
    assert "OP_ACCOUNT" in (err.hint or "")
    assert "{account, reference}" in (err.hint or "")


def test_signed_out_read_pinned_account_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A {account, reference} table reads against the pinned account; a
    signed-out `op read` raises ConnectivityError naming that account."""
    uri = "op://Work/npm/token"
    _install_runner(monkeypatch, _signed_out_read(uri))
    backend = OnePasswordBackend()
    secret = _decl(
        "npm",
        backend_mappings={"onepassword": {"account": "my.1password.com", "reference": uri}},
    )
    with pytest.raises(ConnectivityError, match="my.1password.com"):
        backend.batch_get([(secret, secret.backend_mappings["onepassword"])])


def test_signed_out_read_halts_before_later_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed-out read on the first want halts the batch: the second
    secret's `op read` never fires (the read is the only probe, and its
    ConnectivityError propagates)."""
    first, second = "op://Work/a/f", "op://Work/b/f"
    runner = _fake_op(
        values={second: "vb"},
        read_errors={first: (1, "[ERROR] account is not signed in.")},
    )
    _install_runner(monkeypatch, runner)
    backend = OnePasswordBackend()
    a = _decl("a", backend_mappings={"onepassword": first})
    b = _decl("b", backend_mappings={"onepassword": second})
    with pytest.raises(ConnectivityError):
        backend.batch_get(
            [
                (a, a.backend_mappings["onepassword"]),
                (b, b.backend_mappings["onepassword"]),
            ]
        )
    read_calls = [c for c in runner.calls if c[0] == "read"]
    assert read_calls == [["read", "--no-newline", first]]


def test_batch_get_empty_wants_does_not_run_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty batch returns {} without running op at all."""

    def exploding(args: list[str]) -> _OpResult:
        raise AssertionError("empty batch must not run op")

    _install_runner(monkeypatch, exploding)
    assert OnePasswordBackend().batch_get([]) == {}


def test_batch_get_missing_binary_raises_connectivity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(args: list[str]) -> _OpResult:
        raise FileNotFoundError("op")

    _install_runner(monkeypatch, boom)
    backend = OnePasswordBackend()
    secret = _decl("npm", backend_mappings={"onepassword": "op://Work/npm/token"})
    with pytest.raises(ConnectivityError, match="not found on PATH"):
        backend.batch_get([(secret, secret.backend_mappings["onepassword"])])


def test_batch_get_unexpected_failure_raises_external_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runner(
        monkeypatch,
        _fake_op(read_errors={"op://Work/npm/token": (1, "unexpected server error 503")}),
    )
    backend = OnePasswordBackend()
    secret = _decl("npm", backend_mappings={"onepassword": "op://Work/npm/token"})
    with pytest.raises(ExternalError, match="server error 503"):
        backend.batch_get([(secret, secret.backend_mappings["onepassword"])])


# -- interactive optimism (preview never probes) -----------------------------


def test_preview_reports_onepassword_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """preview_resolution reports onepassword for a mapped secret WITHOUT
    invoking op (interactive = True)."""

    def exploding(args: list[str]) -> _OpResult:
        raise AssertionError("preview must not run op for an interactive backend")

    _install_runner(monkeypatch, exploding)
    secret = _decl("npm", backend_mappings={"onepassword": "op://Work/npm/token"})
    assert preview_resolution(secret, _backend_chain(), interactive_available=True) == "onepassword"


def test_preview_returns_none_for_unmapped_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding(args: list[str]) -> _OpResult:
        raise AssertionError("preview must not run op")

    _install_runner(monkeypatch, exploding)
    # Unmapped: would_attempt is False, so onepassword is skipped entirely.
    assert preview_resolution(_decl("npm"), _backend_chain(), interactive_available=True) is None


# -- registry ----------------------------------------------------------------


def test_onepassword_seated_by_plugin() -> None:
    """The onepassword backend ships as the ``onepassword`` system plugin,
    whose adapter seats the backend instance into the code registry at import
    (so ``_impl_for`` can stamp it onto the graph node)."""
    from agentworks.plugins import SYSTEM_PLUGINS
    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    assert "onepassword" in SYSTEM_PLUGINS
    assert "onepassword" in SECRET_BACKEND_REGISTRY


def test_onepassword_descriptor_row_is_disabled_system_plugin_by_default(
    tmp_path: Path,
) -> None:
    """The secret-backend row publishes present-but-disabled with a
    ``system-plugin`` origin until the operator opts in via [plugins]. (Post
    migration: the row is no longer a built-in.)"""
    from agentworks.resources.graph import Enablement

    config = _config(tmp_path)
    registry = build_registry(config)
    row = registry.lookup("secret-backend", "onepassword")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "onepassword"
    assert row.description  # capability-supplied, for inspection surfaces
    assert registry.graph.enablement_of("secret-backend", "onepassword") is Enablement.disabled


# -- Phase 8 migration: the opt-in breaking change (R11.1) --------------------


# A secret whose only path is the onepassword backend (env-var opted out),
# declared as a manifest (config.toml is settings-only now, ADR 0022).
_OP_ONLY_SECRET = ManifestDoc(
    "secret",
    "op-only",
    {"backend_mappings": {"env-var": False, "onepassword": "op://Vault/item/field"}},
    description="resolvable only via onepassword",
)


def _op_only_settings(*, enabled: bool) -> str:
    """The settings half of the ``op-only`` fixture: onepassword in the chain,
    and ``enabled`` toggling the [plugins] opt-in. Pair with
    ``manifests=[_OP_ONLY_SECRET]``."""
    plugins = '[plugins]\nsystem = ["onepassword"]\n\n' if enabled else ""
    return (
        plugins
        + """
        [secret_config]
        backends = ["env-var", "onepassword"]
        """
    )


def test_disabled_plugin_backends_maps_onepassword_only_when_disabled(tmp_path: Path) -> None:
    """The hint's data source: ``disabled_plugin_backends`` reports onepassword
    (backend name -> plugin name) while the plugin is disabled, and is empty
    once it is enabled (so the failure message is verbatim today's)."""
    from agentworks.secrets.resolve import disabled_plugin_backends

    disabled = build_registry(_config(tmp_path, _op_only_settings(enabled=False), manifests=[_OP_ONLY_SECRET]))
    assert disabled_plugin_backends(disabled) == {"onepassword": "onepassword"}

    enabled = build_registry(_config(tmp_path, _op_only_settings(enabled=True), manifests=[_OP_ONLY_SECRET]))
    assert disabled_plugin_backends(enabled) == {}


def test_secret_mapped_only_to_disabled_onepassword_fails_with_enable_hint(tmp_path: Path) -> None:
    """R11.1 / LLD b: a secret whose sole mapping targets onepassword, with the
    plugin not enabled, is excluded from the active chain and fails resolve with
    the plugin-aware ``enable plugin `onepassword``` hint, not the generic
    unreachable message."""
    from agentworks.env.entry import EnvEntry
    from agentworks.errors import SecretUnavailableError
    from agentworks.secrets import SecretTarget, resolve_for_command

    config = _config(tmp_path, _op_only_settings(enabled=False), manifests=[_OP_ONLY_SECRET])
    registry = build_registry(config)
    target = SecretTarget(vm={"TOKEN": EnvEntry(key="TOKEN", secret="op-only")})
    with pytest.raises(SecretUnavailableError) as exc:
        resolve_for_command([target], config, registry)
    assert "enable plugin `onepassword`" in (exc.value.hint or "")


def test_enabling_the_plugin_resolves_the_onepassword_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling the plugin puts onepassword back in the active chain and the
    same secret resolves (the ``op`` subprocess is faked; ``op`` is present on
    PATH so the backend folds ready)."""
    from agentworks.env.entry import EnvEntry
    from agentworks.secrets import SecretTarget, resolve_for_command

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/op" if name == "op" else None)
    config = _config(tmp_path, _op_only_settings(enabled=True), manifests=[_OP_ONLY_SECRET])
    registry = build_registry(config)
    _install_runner(monkeypatch, _fake_op(values={"op://Vault/item/field": "the-token"}))
    target = SecretTarget(vm={"TOKEN": EnvEntry(key="TOKEN", secret="op-only")})
    values = resolve_for_command([target], config, registry)
    assert values["op-only"] == "the-token"


def test_non_plugin_secret_failure_message_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret that maps no disabled plugin backend fails with the generic
    message (no ``enable plugin`` clause): the hint is additive to the sole
    onepassword-only case, not a blanket change to every resolve failure."""
    from agentworks.env.entry import EnvEntry
    from agentworks.errors import SecretUnavailableError
    from agentworks.secrets import SecretTarget, resolve_for_command

    monkeypatch.delenv("AW_SECRET_PLAIN", raising=False)
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["env-var"]
        """,
        manifests=[ManifestDoc("secret", "plain", description="reachable via env-var's default convention, but unset")],
    )
    registry = build_registry(config)
    target = SecretTarget(vm={"TOKEN": EnvEntry(key="TOKEN", secret="plain")})
    with pytest.raises(SecretUnavailableError) as exc:
        resolve_for_command([target], config, registry)
    assert "plain: tried env-var" in (exc.value.hint or "")
    assert "enable plugin" not in (exc.value.hint or "")


def test_hint_reaches_the_dominant_resolver_path(tmp_path: Path) -> None:
    """The hint reaches the dominant resolution path ``Resolver.resolve()`` (the
    per-operation resolver used by VM / session / agent commands), not only the
    env-chain ``resolve_for_command``. Centralizing the hint in
    ``_fail_unavailable`` is what makes every path carry it."""
    from agentworks.errors import SecretUnavailableError
    from agentworks.secrets.resolver import Resolver

    config = _config(tmp_path, _op_only_settings(enabled=False), manifests=[_OP_ONLY_SECRET])
    registry = build_registry(config)
    resolver = Resolver(config, registry)
    resolver.register_name("op-only")
    with pytest.raises(SecretUnavailableError) as exc:
        resolver.resolve()
    assert "enable plugin `onepassword`" in (exc.value.hint or "")


def test_explicit_false_opt_out_of_onepassword_gets_no_enable_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret that explicitly opts OUT of onepassword (`onepassword = false`)
    and fails for another reason does NOT get the enable-plugin hint: enabling a
    plugin the secret opted out of would not resolve it. The hint loop skips a
    ``False`` mapping, mirroring ``SecretDecl.dependencies`` / ``validate``."""
    from agentworks.env.entry import EnvEntry
    from agentworks.errors import SecretUnavailableError
    from agentworks.secrets import SecretTarget, resolve_for_command

    monkeypatch.delenv("AW_SECRET_OPTED_OUT", raising=False)
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["env-var", "onepassword"]
        """,
        manifests=[
            ManifestDoc(
                "secret",
                "opted-out",
                {"backend_mappings": {"onepassword": False}},
                description="opts out of onepassword; reachable via env-var default, but unset",
            )
        ],
    )
    registry = build_registry(config)
    target = SecretTarget(vm={"TOKEN": EnvEntry(key="TOKEN", secret="opted-out")})
    with pytest.raises(SecretUnavailableError) as exc:
        resolve_for_command([target], config, registry)
    assert "enable plugin" not in (exc.value.hint or "")


def test_doctor_roster_lists_the_onepassword_plugin(tmp_path: Path) -> None:
    """``agw doctor``'s System plugins roster lists onepassword, disabled by
    default and enabled once opted in (the discovery surface the enable hint
    points at)."""
    from agentworks.doctor import Status, _check_plugins

    disabled = _check_plugins(_config(tmp_path))
    op = next(c for c in disabled.checks if c.name == "plugin onepassword")
    assert op.status is Status.INFO
    assert "not enabled in [plugins].system" in (op.message or "")

    enabled = _check_plugins(_config(tmp_path, '[plugins]\nsystem = ["onepassword"]\n'))
    op_on = next(c for c in enabled.checks if c.name == "plugin onepassword")
    assert op_on.status is Status.OK
