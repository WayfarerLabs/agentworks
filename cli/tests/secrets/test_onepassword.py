"""Tests for the ``onepassword`` secret backend.

The subprocess boundary is faked: every ``op`` call in the module goes
through the module-level ``_run_op`` seam, which these tests monkeypatch
so no real ``op`` binary is ever invoked. Fakes dispatch on the argv's
first element (``whoami`` for the amortized sign-in check, ``read`` for a
per-secret lookup).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import (
    ConfigError,
    ConnectivityError,
    ExternalError,
    SecretMappingError,
)
from agentworks.secrets import SECRET_BACKEND_REGISTRY, active_backends, resolve_secrets
from agentworks.secrets import onepassword as op_mod
from agentworks.secrets.base import SecretDecl
from agentworks.secrets.onepassword import OnePasswordBackend, _OpResult
from agentworks.secrets.resolve import ActiveBackend, preview_resolution


def _decl(name: str, **kw: Any) -> SecretDecl:
    return SecretDecl(name=name, description=f"{name} description", **kw)


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, runner: Any
) -> None:
    monkeypatch.setattr(op_mod, "_run_op", runner)


def _ok_whoami() -> _OpResult:
    return _OpResult(returncode=0, stdout="me@example.com\n", stderr="")


def _fake_op(
    *,
    signed_in: bool = True,
    signed_out_accounts: tuple[str | None, ...] = (),
    values: dict[str, str] | None = None,
    read_errors: dict[str, tuple[int, str]] | None = None,
) -> Any:
    """A ``_run_op`` double. ``whoami`` succeeds unless ``signed_in`` is
    False or the checked account is in ``signed_out_accounts`` (a bare
    ``op://`` string checks the ``None`` default account); ``read <uri>``
    returns the mapped value, or the mapped (returncode, stderr) failure,
    or a generic not-found. The op:// reference is always the last argv
    element, in both the bare and ``--account`` forms."""
    values = values or {}
    read_errors = read_errors or {}
    calls: list[list[str]] = []

    def run(args: list[str]) -> _OpResult:
        calls.append(args)
        if args[0] == "whoami":
            account = args[2] if "--account" in args else None
            if not signed_in or account in signed_out_accounts:
                return _OpResult(
                    1, "", "[ERROR] You are not currently signed in."
                )
            return _ok_whoami()
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
    return [ActiveBackend(capability=OnePasswordBackend())]


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
    assert (
        backend.describe_lookup(secret, secret.backend_mappings["onepassword"])
        == uri
    )
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
    assert read_calls == [
        ["read", "--no-newline", "--account", "my.1password.com", uri]
    ]
    whoami_calls = [c for c in runner.calls if c[0] == "whoami"]
    assert whoami_calls == [["whoami", "--account", "my.1password.com"]]


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
        backend_mappings={
            "onepassword": {"account": "my.1password.com", "reference": uri}
        },
    )
    bare = _decl("s-bare", backend_mappings={"onepassword": uri})
    assert (
        backend.describe_lookup(
            with_account, with_account.backend_mappings["onepassword"]
        )
        == f"{uri} (account my.1password.com)"
    )
    assert (
        backend.describe_lookup(bare, bare.backend_mappings["onepassword"])
        == uri
    )


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
        pytest.param(
            {"reference": "op://Work/npm/token"}, id="table-missing-account"
        ),
        pytest.param(
            {"account": "", "reference": "op://Work/npm/token"},
            id="table-blank-account",
        ),
        pytest.param({"account": "acct"}, id="table-missing-reference"),
        pytest.param(
            {"account": "acct", "reference": ""}, id="table-blank-reference"
        ),
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


def test_validate_mapping_rejects_legacy_table_with_migration_hint() -> None:
    """The removed {vault, item, field} table raises a ConfigError that
    points at the two supported forms, so anyone on the old shape gets a
    migration error rather than a generic unknown-key message."""
    backend = OnePasswordBackend()
    with pytest.raises(ConfigError, match="reference") as excinfo:
        backend.validate_mapping(
            "secret 's'",
            {"vault": "Work", "item": "npm", "field": "token"},
        )
    message = str(excinfo.value)
    assert "op://vault/item/field" in message
    assert "account, reference" in message


def _config(tmp_path: Path, body: str = "") -> Any:
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
    return load_config(cfg, warn_issues=False)


def test_validate_chain_surfaces_malformed_mapping_at_build_registry(
    tmp_path: Path,
) -> None:
    """A malformed onepassword mapping on an active-chain secret fails at
    build_registry (the load-time gate), not at first resolution."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["onepassword", "prompt"]

        [secrets.npm-token]
        description = "npm token"
        backend_mappings.onepassword = "not-a-valid-ref"
        """,
    )
    with pytest.raises(ConfigError, match="onepassword"):
        build_registry(config)


def test_valid_mapping_passes_build_registry(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["onepassword", "prompt"]

        [secrets.npm-token]
        description = "npm token"
        backend_mappings.onepassword = "op://Work/npm/token"
        """,
    )
    registry = build_registry(config)
    names = [b.name for b in active_backends(config, registry)]
    assert names == ["onepassword", "prompt"]


# -- batch_get: miss / failure semantics -------------------------------------


def test_batch_get_returns_found_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(
        monkeypatch, _fake_op(values={"op://Work/npm/token": "the-token"})
    )
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

    op_chain = ActiveBackend(capability=OnePasswordBackend())
    later = ActiveBackend(capability=_ExplodingBackend())  # type: ignore[arg-type]
    secret = _decl("gone", backend_mappings={"onepassword": "op://Work/gone/token"})
    with pytest.raises(SecretMappingError):
        resolve_secrets([secret], [op_chain, later])


def test_batch_get_signed_out_raises_connectivity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runner(monkeypatch, _fake_op(signed_in=False))
    backend = OnePasswordBackend()
    secret = _decl("npm", backend_mappings={"onepassword": "op://Work/npm/token"})
    with pytest.raises(ConnectivityError, match="signed in"):
        backend.batch_get([(secret, secret.backend_mappings["onepassword"])])


def test_read_signed_out_mid_batch_raises_connectivity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """whoami passed, but the session lapsed before the read: a signed-out
    marker on the failing `op read` (not `op whoami`) is classified as
    ConnectivityError, not a hard mapping miss."""
    _install_runner(
        monkeypatch,
        _fake_op(
            read_errors={
                "op://Work/npm/token": (
                    1,
                    "[ERROR] You are not currently signed in.",
                )
            }
        ),
    )
    backend = OnePasswordBackend()
    secret = _decl("npm", backend_mappings={"onepassword": "op://Work/npm/token"})
    with pytest.raises(ConnectivityError, match="signed in"):
        backend.batch_get([(secret, secret.backend_mappings["onepassword"])])


def test_batch_get_empty_wants_does_not_run_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty batch returns {} without firing `op whoami`."""

    def exploding(args: list[str]) -> _OpResult:
        raise AssertionError("empty batch must not run op")

    _install_runner(monkeypatch, exploding)
    assert OnePasswordBackend().batch_get([]) == {}


def test_signin_check_amortized_once_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two secrets on the same (default) account share one whoami check."""
    runner = _fake_op(
        values={"op://Work/a/f": "va", "op://Work/b/f": "vb"}
    )
    _install_runner(monkeypatch, runner)
    backend = OnePasswordBackend()
    a = _decl("a", backend_mappings={"onepassword": "op://Work/a/f"})
    b = _decl("b", backend_mappings={"onepassword": "op://Work/b/f"})
    backend.batch_get(
        [
            (a, a.backend_mappings["onepassword"]),
            (b, b.backend_mappings["onepassword"]),
        ]
    )
    whoami_calls = [c for c in runner.calls if c[0] == "whoami"]
    assert len(whoami_calls) == 1


def test_signin_check_amortized_once_per_distinct_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch spanning two accounts plus the default runs `op whoami` once
    per distinct account, not once per secret. Two secrets share account
    'acct-x', one is on 'acct-y', and one is on the default account: three
    distinct whoami checks across four secrets."""
    runner = _fake_op(
        values={
            "op://Work/x1/f": "vx1",
            "op://Work/x2/f": "vx2",
            "op://Work/y/f": "vy",
            "op://Work/d/f": "vd",
        }
    )
    _install_runner(monkeypatch, runner)
    backend = OnePasswordBackend()
    wants = [
        _decl(
            "x1",
            backend_mappings={
                "onepassword": {"account": "acct-x", "reference": "op://Work/x1/f"}
            },
        ),
        _decl(
            "x2",
            backend_mappings={
                "onepassword": {"account": "acct-x", "reference": "op://Work/x2/f"}
            },
        ),
        _decl(
            "y",
            backend_mappings={
                "onepassword": {"account": "acct-y", "reference": "op://Work/y/f"}
            },
        ),
        _decl("d", backend_mappings={"onepassword": "op://Work/d/f"}),
    ]
    backend.batch_get([(s, s.backend_mappings["onepassword"]) for s in wants])
    whoami_calls = [c for c in runner.calls if c[0] == "whoami"]
    # One whoami per distinct account (default = bare `whoami`), four reads.
    assert {tuple(c) for c in whoami_calls} == {
        ("whoami",),
        ("whoami", "--account", "acct-x"),
        ("whoami", "--account", "acct-y"),
    }
    assert len(whoami_calls) == 3


def test_signed_out_for_specific_account_raises_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one account in the batch is signed out, the ConnectivityError
    names that account (the default account is fine)."""
    runner = _fake_op(
        signed_out_accounts=("acct-y",),
        values={"op://Work/x/f": "vx", "op://Work/y/f": "vy"},
    )
    _install_runner(monkeypatch, runner)
    backend = OnePasswordBackend()
    x = _decl(
        "x",
        backend_mappings={
            "onepassword": {"account": "acct-x", "reference": "op://Work/x/f"}
        },
    )
    y = _decl(
        "y",
        backend_mappings={
            "onepassword": {"account": "acct-y", "reference": "op://Work/y/f"}
        },
    )
    with pytest.raises(ConnectivityError, match="acct-y"):
        backend.batch_get(
            [
                (x, x.backend_mappings["onepassword"]),
                (y, y.backend_mappings["onepassword"]),
            ]
        )


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
        _fake_op(
            read_errors={"op://Work/npm/token": (1, "unexpected server error 503")}
        ),
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
    assert preview_resolution(secret, _backend_chain()) == "onepassword"


def test_preview_returns_none_for_unmapped_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding(args: list[str]) -> _OpResult:
        raise AssertionError("preview must not run op")

    _install_runner(monkeypatch, exploding)
    # Unmapped: would_attempt is False, so onepassword is skipped entirely.
    assert preview_resolution(_decl("npm"), _backend_chain()) is None


# -- registry ----------------------------------------------------------------


def test_onepassword_registered() -> None:
    assert "onepassword" in SECRET_BACKEND_REGISTRY


def test_onepassword_descriptor_row_published(tmp_path: Path) -> None:
    """The secret-backend row for onepassword publishes with a built-in
    origin and a non-empty description."""
    config = _config(tmp_path)
    registry = build_registry(config)
    row = registry.lookup("secret-backend", "onepassword")
    assert row.origin.variant == "built-in"
    assert row.description
