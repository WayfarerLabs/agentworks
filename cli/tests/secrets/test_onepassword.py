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
    values: dict[str, str] | None = None,
    read_errors: dict[str, tuple[int, str]] | None = None,
) -> Any:
    """A ``_run_op`` double. ``whoami`` succeeds unless ``signed_in`` is
    False; ``read <uri>`` returns the mapped value, or the mapped
    (returncode, stderr) failure, or a generic not-found."""
    values = values or {}
    read_errors = read_errors or {}
    calls: list[list[str]] = []

    def run(args: list[str]) -> _OpResult:
        calls.append(args)
        if args[0] == "whoami":
            if signed_in:
                return _ok_whoami()
            return _OpResult(1, "", "[ERROR] You are not currently signed in.")
        # op read --no-newline <uri>
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


def test_string_and_dict_mappings_resolve_to_same_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _fake_op(values={"op://Work/npm/token": "secret-value"})
    _install_runner(monkeypatch, runner)
    backend = OnePasswordBackend()

    string_secret = _decl(
        "s-str", backend_mappings={"onepassword": "op://Work/npm/token"}
    )
    dict_secret = _decl(
        "s-dict",
        backend_mappings={
            "onepassword": {"vault": "Work", "item": "npm", "field": "token"}
        },
    )
    uri = "op://Work/npm/token"
    assert backend.describe_lookup(
        string_secret, string_secret.backend_mappings["onepassword"]
    ) == uri
    assert backend.describe_lookup(
        dict_secret, dict_secret.backend_mappings["onepassword"]
    ) == uri

    got_str = backend.batch_get(
        [(string_secret, string_secret.backend_mappings["onepassword"])]
    )
    got_dict = backend.batch_get(
        [(dict_secret, dict_secret.backend_mappings["onepassword"])]
    )
    assert got_str == {"s-str": "secret-value"}
    assert got_dict == {"s-dict": "secret-value"}
    # Both forms reached the runner as the same op:// URI.
    read_calls = [c for c in runner.calls if c[0] == "read"]
    assert all(c[-1] == uri for c in read_calls)


def test_describe_lookup_none_when_unmapped() -> None:
    backend = OnePasswordBackend()
    assert backend.describe_lookup(_decl("s"), None) is None


# -- validate_mapping --------------------------------------------------------


def test_validate_mapping_accepts_valid_forms() -> None:
    backend = OnePasswordBackend()
    backend.validate_mapping("secret 's'", "op://Work/npm/token")
    backend.validate_mapping(
        "secret 's'", {"vault": "Work", "item": "npm", "field": "token"}
    )
    # A section segment (4 parts) is allowed.
    backend.validate_mapping("secret 's'", "op://Work/npm/section/token")


@pytest.mark.parametrize(
    "mapping",
    [
        pytest.param(123, id="wrong-type"),
        pytest.param("", id="empty-string"),
        pytest.param("Work/npm/token", id="missing-scheme"),
        pytest.param("op://Work/token", id="too-few-segments"),
        pytest.param("op://Work//token", id="blank-segment"),
        pytest.param({"vault": "Work", "item": "npm"}, id="dict-missing-field"),
        pytest.param(
            {"vault": "Work", "item": "npm", "field": ""}, id="dict-blank-field"
        ),
    ],
)
def test_validate_mapping_rejects_bad_forms(mapping: Any) -> None:
    backend = OnePasswordBackend()
    with pytest.raises(ConfigError):
        backend.validate_mapping("secret 's'", mapping)


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


def test_signin_check_amortized_once_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
