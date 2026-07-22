"""The ``onepassword`` secret backend: resolves values through the
1Password ``op`` CLI (v2). A capability implementation, consumed by the
resolution loop through the ``SecretBackend`` API.

Transport is a subprocess shell-out to
``op read --no-newline [--account <acct>] op://<vault>/<item>/<field>``
(explicit argv, never a shell string; resolved values are never logged).
1Password Connect and any Python SDK are deliberately out of scope: the
backend depends only on the operator's own ``op`` read access (whether from
``op signin`` or the 1Password app's CLI integration) and its ambient env,
so there is no backend-level config channel today (ADR 0016).

There is no separate sign-in pre-check: the actual ``op read`` is the only
liveness probe. ``op whoami`` is not reliable for this, because under the
1Password app's CLI integration it can report "not signed in" even when
``op read`` works (the app holds auth and there is no CLI session token for
whoami to report). A signed-out state is therefore detected from a failing
``op read`` (see the marker classification below).

The ``--account`` selection path (the flag name, and that it may precede the
positional reference) matches 1Password CLI v2 docs but is asserted from
docs, not exercised: there is no ``op`` binary in the dev environment, so
the tests fake the subprocess seam. Confirm it against a real multi-account
``op`` before relying on the table form in a release. A wrong flag degrades
safely (a nonzero exit with non-marker stderr surfaces as ``ExternalError``,
not a silent wrong value).

Mapping-required (no derive-from-name convention): a secret is attempted
only when it carries a ``backend_mappings.onepassword`` entry, in one of
two forms:

- a bare ``op://vault/item/field`` reference string (the value the
  1Password app's "Copy Secret Reference" produces and ``op read``
  consumes; an optional ``[section/]`` segment is allowed). This uses
  op's default account, or the one named by ``OP_ACCOUNT``.
- a ``{account, reference}`` table, used only when a specific account must
  be pinned. ``reference`` is the same native ``op://`` string; ``account``
  is an ``op`` account selector (shorthand, sign-in address, account ID, or
  user ID) passed through as ``--account``. The account cannot ride the
  ``op://`` string, so it travels in the table beside it.
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
# codes for "not signed in" vs "no such item", so we classify a failing
# ``op read`` by matching stderr substrings. There is NO sign-in pre-check:
# ``op whoami`` is not a reliable liveness probe, because under the 1Password
# app's CLI integration it reports "not signed in" even when ``op read``
# works, so a whoami gate would abort a working setup. Classification of a
# failing read: signed-out markers -> ConnectivityError; the narrow not-found
# markers -> SecretMappingError; anything else -> ExternalError (the fail-safe
# halt). The not-found markers are deliberately NARROW and item/field-specific:
# a broad marker like "no such" would also match a Go-style transport error
# ("dial tcp: lookup ...: no such host") and mislabel connectivity as a hard
# mapping error with a misleading vault/item/field hint. Anything not matched
# by the signed-out or not-found markers falls through to ``ExternalError``
# (which halts the chain and surfaces the raw stderr): the safer
# classification.
_SIGNED_OUT_MARKERS = (
    "not currently signed in",
    "no account found",
    "session expired",
    "account is not signed in",
)
_NOT_FOUND_MARKERS = (
    "isn't an item",
    "isn't a field",
    "no such item",
    "no such field",
)

# The two accepted mapping forms, named in every validation error so an
# operator on a rejected shape sees the path forward in one line.
_FORMS_HINT = (
    "use an 'op://vault/item/field' string, or a {account, reference} table when a specific account must be pinned"
)
_TABLE_KEYS = ("account", "reference")


@dataclass(frozen=True)
class _OpResult:
    """The outcome of one ``op`` invocation: the single subprocess seam's
    return shape. Tests fake ``_run_op`` to return these without touching a
    real ``op``."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _OpRef:
    """A resolved onepassword lookup: the native ``op://`` reference plus an
    optional account selector. The account cannot be encoded in the
    reference string, so the resolution helper carries it alongside for
    ``batch_get`` (``--account`` on the read) and ``describe_lookup`` (the
    operator-facing identifier). ``account is None`` means op's default /
    ``OP_ACCOUNT`` account. Module-private."""

    reference: str
    account: str | None


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


def _signed_out_message(account: str | None) -> str:
    """The ConnectivityError message for a signed-out ``op``, naming the
    account when the mapping pinned one (the default account, ``None``, reads
    as the generic phrasing)."""
    if account is not None:
        return f"not signed in to the 1Password CLI account {account}"
    return "not signed in to the 1Password CLI"


def _validate_op_uri(owner: str, uri: str) -> None:
    """Reject an ``op://`` reference that is clearly malformed. A valid
    reference is ``op://`` followed by at least three non-empty path
    segments (vault, item, field; an optional section segment may add a
    fourth). Query attributes (``?attribute=otp``) are left to ``op``
    itself."""
    prefix = "op://"
    if not uri.startswith(prefix):
        raise ConfigError(
            f"{owner}: onepassword reference {uri!r} must start with 'op://' "
            f"(an 'op://vault/item/field' reference, optionally with a "
            f"section: 'op://vault/item/section/field')"
        )
    path = uri[len(prefix) :].split("?", 1)[0]
    segments = path.split("/")
    if len(segments) < 3 or not all(segments[:3]):
        raise ConfigError(
            f"{owner}: onepassword reference {uri!r} is malformed; expected "
            f"'op://vault/item/field' with non-empty vault, item, and field"
        )


def _ref_from_table(owner: str, mapping: dict[str, object]) -> _OpRef:
    """Validate a ``{account, reference}`` table and return the resolved
    ``_OpRef``. ``owner`` is display context for errors.

    Any key other than ``account`` and ``reference`` is rejected and named;
    ``reference`` must be a valid ``op://`` string and ``account`` a
    non-empty selector."""
    unknown = sorted(set(mapping) - set(_TABLE_KEYS))
    if unknown:
        raise ConfigError(
            f"{owner}: unknown key(s) {unknown} in the onepassword table; "
            f"only 'account' and 'reference' are allowed. {_FORMS_HINT}"
        )
    reference = mapping.get("reference")
    if not isinstance(reference, str) or not reference:
        raise ConfigError(
            f"{owner}: the onepassword table needs a non-empty string "
            f"'reference' (an 'op://vault/item/field' reference)"
        )
    _validate_op_uri(owner, reference)
    account = mapping.get("account")
    if not isinstance(account, str) or not account:
        raise ConfigError(
            f"{owner}: the onepassword table needs a non-empty string "
            f"'account' (a 1Password account selector); to use op's default "
            f"account, drop the table and give the bare op:// string"
        )
    return _OpRef(reference=reference, account=account)


class OnePasswordBackend:
    """Resolves secret values from 1Password via the ``op`` CLI.

    Mapping-required: ``would_attempt`` is True only for a secret that
    carries a ``backend_mappings.onepassword`` entry; unmapped secrets
    soft-skip (fall through to the next backend). There is no
    derive-from-name convention: 1Password addressing is
    vault/item/field, which cannot be inferred from a bare secret name.

    Two mapping forms (see the module docstring): a bare ``op://`` string
    (default account), or a ``{account, reference}`` table when a specific
    account must be pinned.

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

    # interactive = True: resolving a onepassword secret may involve
    # operator interaction, because `op read` can trigger a biometric or
    # re-auth prompt. That is the same property the prompt backend has (it
    # asks the operator for the value), so onepassword carries the flag for
    # the same reason, not as a special case.
    #
    # The practical effect: preview_resolution never probes this backend
    # (probing would fire the biometric at every preflight and once per
    # secret in `agw doctor`); it reports onepassword optimistically on
    # would_attempt alone. A non-interactive transport that authenticates
    # without a human (1Password Connect, a service account; not built
    # here) would not be interactive.
    interactive = True

    def validate_mapping(self, owner: str, mapping: MappingValue) -> None:
        # Load-time gate (called by validate_chain). ``_resolved_ref`` keeps
        # its own defensive check for hand-built decls that never pass
        # through validate_chain, mirroring env_var's ``_resolved_name``.
        if isinstance(mapping, str):
            if not mapping:
                raise ConfigError(
                    f"{owner}: backend_mappings for the onepassword backend "
                    f"must be a non-empty 'op://vault/item/field' string; "
                    f"{_FORMS_HINT}"
                )
            _validate_op_uri(owner, mapping)
            return
        if isinstance(mapping, dict):
            _ref_from_table(owner, mapping)
            return
        raise ConfigError(
            f"{owner}: backend_mappings for the onepassword backend must be "
            f"an 'op://vault/item/field' string or a {{account, reference}} "
            f"table (got {type(mapping).__name__})"
        )

    def _resolved_ref(self, secret: SecretDecl, mapping: MappingValue | None) -> _OpRef:
        owner = f"secret {secret.name!r}"
        if isinstance(mapping, str):
            _validate_op_uri(owner, mapping)
            return _OpRef(reference=mapping, account=None)
        if isinstance(mapping, dict):
            return _ref_from_table(owner, mapping)
        # would_attempt gates this out, so reaching here means a hand-built
        # decl bypassed validate_chain (defense in depth, like env_var).
        raise ConfigError(
            f"{owner}: the onepassword backend needs a backend_mappings.onepassword entry ({_FORMS_HINT})"
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
        # The op:// reference, for the operator-facing "Resolved X via
        # onepassword (...)" line. When an account is pinned it is prefixed
        # account-FIRST ("<account>: <reference>"): the position conveys
        # "account" without spending the word, and a long op:// reference
        # then truncates before the account does in the list view. Never a
        # value.
        if mapping is None:
            return None
        ref = self._resolved_ref(secret, mapping)
        if ref.account is not None:
            return f"{ref.account}: {ref.reference}"
        return ref.reference

    def batch_get(
        self,
        wants: list[tuple[SecretDecl, MappingValue | None]],
    ) -> dict[str, str]:
        # Self-safe on an empty batch: no work, no subprocess. (would_attempt
        # gating already guarantees non-empty from the resolve loop; this
        # makes a direct call cheap too.)
        if not wants:
            return {}
        # No sign-in pre-check: `op read` is the only liveness probe (see the
        # module docstring and the marker block; `op whoami` is unreliable
        # under app integration). A signed-out state surfaces from the read
        # itself, and the first such read halts the batch.
        out: dict[str, str] = {}
        for secret, mapping in wants:
            ref = self._resolved_ref(secret, mapping)
            out[secret.name] = self._read_one(secret, ref)
        return out

    @staticmethod
    def _read_one(secret: SecretDecl, ref: _OpRef) -> str:
        args = ["read", "--no-newline"]
        if ref.account is not None:
            args += ["--account", ref.account]
        args.append(ref.reference)
        result = _op(args)
        if result.returncode == 0:
            # ``--no-newline`` only suppresses the newline ``op`` appends to
            # its OWN output; it does not touch the stored field value. A
            # field whose value ends in ``\n`` still arrives with it. We do
            # NOT rstrip that (unlike env-var, which defensively strips the
            # \r\n copy-paste artifact): for a vault a trailing newline is
            # arguably part of the value, so surfacing it (the resolve
            # loop's control-character guard rejects it) is safer than
            # silently mangling it. The asymmetry with env-var is
            # deliberate; do not "fix" it into an rstrip.
            return result.stdout
        lowered = result.stderr.lower()
        if _matches(lowered, _SIGNED_OUT_MARKERS):
            # The read itself is the sign-in probe (no whoami gate). A bare
            # op:// string uses op's default account, where a signed-out (or,
            # with several accounts, ambiguous) state is fixed by naming an
            # account; a pinned account just needs read access for it.
            if ref.account is None:
                hint = (
                    "run `op signin` (or enable the 1Password app's CLI "
                    "integration). If several accounts are signed in, set "
                    "OP_ACCOUNT or pin the account per secret with a "
                    "{account, reference} mapping."
                )
            else:
                hint = "run `op signin` (or enable the 1Password app's CLI integration) and retry"
            raise ConnectivityError(_signed_out_message(ref.account), hint=hint)
        if _matches(lowered, _NOT_FOUND_MARKERS):
            raise SecretMappingError(
                f"secret {secret.name!r}: 1Password has no value at {ref.reference}",
                hint=("check the vault, item, and field in backend_mappings.onepassword"),
            )
        # Unrecognized failure. op's flat exit status means we cannot safely
        # call this a missing item, so surface it (halts the chain) rather
        # than guessing it into a soft miss.
        detail = result.stderr.strip() or f"op exited {result.returncode}"
        raise ExternalError(f"secret {secret.name!r}: reading {ref.reference} from 1Password failed: {detail}")
