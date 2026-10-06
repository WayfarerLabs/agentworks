"""Core-only named-account admission for fixed early Linux guest operations."""

from __future__ import annotations

import base64
import bz2
from importlib.resources import files

from agentworks.errors import ValidationError
from agentworks.execution import _helper_bundle, _helper_launcher, _runtime_prerequisite


def build_named_guest_bootstrap_argv(
    root_entry: _helper_launcher.IdentityPlan,
    account: str,
    *,
    selection: _runtime_prerequisite.RuntimeSelection,
    fixed_source: str,
    nonce: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """Package only core-selected source under a configured account literal.

    The configured name crosses the operator configuration boundary here.
    This private builder grants no plugin or request-supplied source execution.
    """
    if (
        type(selection) is not _runtime_prerequisite.RuntimeSelection
        or selection.target_os is not _runtime_prerequisite.RuntimeTargetOS.LINUX
        or selection.explicit_path not in (None, _runtime_prerequisite._SYSTEM_LINUX_PYTHON)
        or type(root_entry) is not _helper_launcher.IdentityPlan
        or _helper_launcher._validate_plan(root_entry).euid != 0
        or type(account) is not str
        or not account
        or "\0" in account
    ):
        raise ValidationError("Named guest bootstrap requires bound Linux system Python and root entry")
    try:
        account.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError("Named guest bootstrap requires a UTF-8 account name") from None
    bootstrap = files(__package__).joinpath("_guest_bootstrap.py").read_text(encoding="utf-8")
    source = bootstrap + f"\nraise SystemExit(main_named({account!r},{fixed_source!r}))\n"
    compressed = base64.b64encode(bz2.compress(_helper_bundle._compact_fixed_source(source).encode("utf-8")))
    wrapper = (
        "import base64,bz2\n"
        f"exec(compile(bz2.decompress(base64.b64decode({compressed!r})),"
        "'<agentworks-named-bootstrap>','exec'))\n"
    )
    argv, candidates, shim = _runtime_prerequisite.build_runtime_helper_argv(
        selection=selection, fixed_source=wrapper, nonce=nonce
    )
    capped = ("/usr/bin/setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--", *argv)
    return _helper_launcher.build_identity_argv(root_entry, capped), candidates, shim
