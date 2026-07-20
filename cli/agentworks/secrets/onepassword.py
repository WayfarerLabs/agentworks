"""The ``onepassword`` secret backend: resolves values through the
1Password ``op`` CLI (v2). A capability implementation, consumed by the
resolution loop through the ``SecretBackend`` API.

Transport is a subprocess shell-out to ``op read op://<vault>/<item>/<field>``
(explicit argv, never a shell string; resolved values are never logged).
1Password Connect and any Python SDK are deliberately out of scope: the
backend depends only on the operator's own signed-in ``op`` state and its
ambient env, so there is no backend-level config channel today (ADR 0016).

Mapping-required (no derive-from-name convention): a secret is attempted
only when it carries a ``backend_mappings.onepassword`` entry, which is
either an ``op://vault/item/field`` string or a structured
``{vault, item, field}`` dict that resolves to the same URI.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import (
    ConfigError,
    ConnectivityError,
    ExternalError,
    SecretMappingError,
)

if TYPE_CHECKING:
    from agentworks.secrets.base import MappingValue, SecretDecl

_OP_BINARY = "op"
"""The 1Password CLI executable, resolved on PATH."""

# ``op`` exposes a flat exit status: 0 on success, 1 for essentially every
# failure (auth, missing item, transport). It does NOT give distinct exit
# codes for "not signed in" vs "no such item", so we classify by matching
# stderr substrings. To keep that classification honest we check sign-in
# ONCE per batch_get (``op whoami``); after a clean whoami, a failing
# ``op read`` is confidently a lookup problem, not an auth problem. An
# unrecognized stderr is surfaced as an ``ExternalError`` (which halts the
# chain) rather than guessed into a soft miss: the safer classification.
_SIGNED_OUT_MARKERS = (
    "not currently signed in",
    "no account found",
    "session expired",
    "account is not signed in",
)
_NOT_FOUND_MARKERS = (
    "isn't an item",
    "isn't a field",
    "not found",
    "no such",
    "doesn't exist",
    "could not find",
)


@dataclass(frozen=True)
class _OpResult:
    """The outcome of one ``op`` invocation: the single subprocess seam's
    return shape. Tests fake ``_run_op`` to return these without touching a
    real ``op``."""

    returncode: int
    stdout: str
    stderr: str


def _run_op(args: list[str]) -> _OpResult:
    """Run the 1Password CLI with an explicit argv (no shell). THE
    subprocess boundary for this module: tests monkeypatch this one seam.

    Raises ``FileNotFoundError`` if ``op`` is not on PATH; callers convert
    that to a ``ConnectivityError`` via ``_op``.
    """
    proc = subprocess.run(  # noqa: S603 - explicit argv, no shell
        [_OP_BINARY, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return _OpResult(proc.returncode, proc.stdout, proc.stderr)


def _op(args: list[str]) -> _OpResult:
    """``_run_op`` plus the one shared failure translation: a missing ``op``
    binary becomes a ``ConnectivityError`` with an install hint. Every
    ``op`` call in this module goes through here so that translation lives
    in one place."""
    try:
        return _run_op(args)
    except FileNotFoundError as exc:
        raise ConnectivityError(
            "the 1Password CLI ('op') was not found on PATH",
            hint=(
                "install the 1Password CLI v2 "
                "(https://developer.1password.com/docs/cli/get-started/), "
                "then run `op signin`"
            ),
        ) from exc


def _matches(lowered_stderr: str, markers: tuple[str, ...]) -> bool:
    return any(marker in lowered_stderr for marker in markers)


def _uri_from_dict(owner: str, mapping: dict[str, object]) -> str:
    """Build ``op://vault/item/field`` from a structured mapping, validating
    that all three keys are present and non-empty strings. ``owner`` is
    display context for the error."""
    parts = []
    for key in ("vault", "item", "field"):
        value = mapping.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(
                f"{owner}: backend_mappings for the onepassword backend, "
                f"when given as a table, needs non-empty string 'vault', "
                f"'item', and 'field' keys (missing or blank: {key!r})"
            )
        parts.append(value)
    vault, item, field = parts
    return f"op://{vault}/{item}/{field}"


def _validate_op_uri(owner: str, uri: str) -> None:
    """Reject an ``op://`` reference that is clearly malformed. A valid
    reference is ``op://`` followed by at least three non-empty path
    segments (vault, item, field; a section segment may add a fourth).
    Query attributes (``?attribute=otp``) are left to ``op`` itself."""
    prefix = "op://"
    if not uri.startswith(prefix):
        raise ConfigError(
            f"{owner}: backend_mappings for the onepassword backend must be "
            f"an 'op://vault/item/field' reference or a "
            f"{{vault, item, field}} table (got {uri!r})"
        )
    path = uri[len(prefix) :].split("?", 1)[0]
    segments = path.split("/")
    if len(segments) < 3 or not all(segments[:3]):
        raise ConfigError(
            f"{owner}: onepassword reference {uri!r} is malformed; expected "
            f"'op://vault/item/field' with non-empty vault, item, and field"
        )


class OnePasswordBackend:
    """Resolves secret values from 1Password via the ``op`` CLI.

    Mapping-required: ``would_attempt`` is True only for a secret that
    carries a ``backend_mappings.onepassword`` entry; unmapped secrets
    soft-skip (fall through to the next backend). There is no
    derive-from-name convention: 1Password addressing is
    vault/item/field, which cannot be inferred from a bare secret name.

    Miss / failure contract (``batch_get``):

    - A found value goes in the returned dict.
    - A mapping that definitively resolves to "no such item/field" raises
      ``SecretMappingError`` (HARD miss): halts the chain so a stale
      mapping cannot silently fall through to a prompt.
    - Not signed in / binary missing raises ``ConnectivityError``; any
      other unexpected ``op`` failure raises ``ExternalError``. Both halt
      the chain, which is intended.
    """

    name = "onepassword"
    description = "resolves via the 1Password CLI (op read op://vault/item/field)"

    # interactive = True means "do not probe this backend in
    # preview_resolution". Probing would run `op` (firing a biometric and
    # tens of seconds of latency) at every preflight and once per secret in
    # `agw doctor`, so preview treats onepassword optimistically instead.
    #
    # The flag currently fuses two ideas: "probing bothers the user" (TRUE
    # here) and "cannot run unattended" (FALSE here: the biometric is auth,
    # not a human decision). onepassword sets the flag for the first reason
    # only. Guardrail: a future --non-interactive / controller path must NOT
    # filter the active chain on `interactive` without first splitting out a
    # separate "cannot run unattended" axis, or it would wrongly drop
    # onepassword in the headless context it is meant for. Today nothing
    # reads `interactive` outside preview_resolution, so this is latent.
    interactive = True

    def validate_mapping(self, owner: str, mapping: MappingValue) -> None:
        # Load-time gate (called by validate_chain). ``_resolved_uri`` keeps
        # its own defensive check for hand-built decls that never pass
        # through validate_chain, mirroring env_var's ``_resolved_name``.
        if isinstance(mapping, str):
            if not mapping:
                raise ConfigError(
                    f"{owner}: backend_mappings for the onepassword backend "
                    f"must be a non-empty 'op://vault/item/field' string"
                )
            _validate_op_uri(owner, mapping)
            return
        if isinstance(mapping, dict):
            _uri_from_dict(owner, mapping)
            return
        raise ConfigError(
            f"{owner}: backend_mappings for the onepassword backend must be "
            f"an 'op://vault/item/field' string or a {{vault, item, field}} "
            f"table (got {type(mapping).__name__})"
        )

    def _resolved_uri(
        self, secret: SecretDecl, mapping: MappingValue | None
    ) -> str:
        owner = f"secret {secret.name!r}"
        if isinstance(mapping, str):
            _validate_op_uri(owner, mapping)
            return mapping
        if isinstance(mapping, dict):
            return _uri_from_dict(owner, mapping)
        # would_attempt gates this out, so reaching here means a hand-built
        # decl bypassed validate_chain (defense in depth, like env_var).
        raise ConfigError(
            f"{owner}: the onepassword backend needs a "
            f"backend_mappings.onepassword entry (an 'op://vault/item/field' "
            f"string or a {{vault, item, field}} table)"
        )

    def would_attempt(
        self,
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> bool:
        # Mapping-required: only mapped secrets are attempted. (The generic
        # ``False`` opt-out is stripped by the resolve loop before it gets
        # here, so ``mapping`` is either a real value or ``None``.)
        return mapping is not None

    def describe_lookup(
        self,
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> str | None:
        # The op:// URI, for the operator-facing "Resolved X via onepassword
        # (op://...)" line. Never a value.
        if mapping is None:
            return None
        return self._resolved_uri(secret, mapping)

    def batch_get(
        self,
        wants: list[tuple[SecretDecl, MappingValue | None]],
    ) -> dict[str, str]:
        # Amortize the sign-in / availability check once for the whole
        # batch, not per secret.
        self._ensure_signed_in()
        out: dict[str, str] = {}
        for secret, mapping in wants:
            uri = self._resolved_uri(secret, mapping)
            out[secret.name] = self._read_one(secret, uri)
        return out

    @staticmethod
    def _ensure_signed_in() -> None:
        """Confirm ``op`` is present and has a live session before reading
        any secret, so a signed-out state is reported once (as a
        ``ConnectivityError``) instead of being misread per secret as a
        missing item."""
        result = _op(["whoami"])
        if result.returncode != 0:
            raise ConnectivityError(
                "not signed in to the 1Password CLI",
                hint=(
                    "run `op signin` (or enable the 1Password app's CLI "
                    "integration) and retry"
                ),
            )

    @staticmethod
    def _read_one(secret: SecretDecl, uri: str) -> str:
        result = _op(["read", "--no-newline", uri])
        if result.returncode == 0:
            # Faithful value: no trailing-newline stripping (--no-newline
            # already omits it). The resolve loop guards embedded control
            # characters that would corrupt SSH SetEnv transport.
            return result.stdout
        lowered = result.stderr.lower()
        if _matches(lowered, _SIGNED_OUT_MARKERS):
            # Session lapsed between the whoami check and this read.
            raise ConnectivityError(
                "not signed in to the 1Password CLI",
                hint="run `op signin` and retry",
            )
        if _matches(lowered, _NOT_FOUND_MARKERS):
            raise SecretMappingError(
                f"secret {secret.name!r}: 1Password has no value at {uri}",
                hint=(
                    "check the vault, item, and field in "
                    "backend_mappings.onepassword"
                ),
            )
        # Unrecognized failure. op's flat exit status means we cannot safely
        # call this a missing item, so surface it (halts the chain) rather
        # than guessing it into a soft miss.
        detail = result.stderr.strip() or f"op exited {result.returncode}"
        raise ExternalError(
            f"secret {secret.name!r}: reading {uri} from 1Password failed: "
            f"{detail}"
        )
