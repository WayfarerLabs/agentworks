"""Git credential providers.

Each provider implementation (``GitHubCredentialProvider``,
``AzDOCredentialProvider``) is the code-side handle for one
``[git_credentials.<name>].provider = "..."`` value (``type`` is the
accepted legacy alias). The framework's ``git-credential-provider``
kind holds one row per known provider so a typo in the
operator's ``provider`` field surfaces as a clean miss-policy error at
``build_registry`` time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agentworks import output
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from agentworks.capabilities.git_credential.base import (
        GitCredentialProvider,
        HelperEntry,
    )
    from agentworks.config import Config
    from agentworks.resources import Registry


class _WarnLogger(Protocol):
    """Just the sink ``runup_and_filter`` needs from an ``SSHLogger``:
    recording a warning is what drives a VM init to PARTIAL."""

    def warning(self, msg: str) -> None: ...


class _MappedSecrets:
    """A resolved-secret view over a plain ``{secret_name: value}`` map,
    for handing already-resolved git tokens to ``runup`` at the write
    step without threading the operation's resolver that far."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        return self._values[name]


def remote_advisories(registry: Registry, url: str) -> list[str]:
    """Ask every declared git credential to review a repo remote URL and
    return the deduped advisories.

    Config-only and wiring-blind by design: each declared credential is
    constructed from its config (no token) and judges the
    URL by its own host and scope semantics (see
    ``GitCredentialProvider.review_remote``). Whether a given credential
    is actually wired to the user who will clone is deliberately not
    considered; that needs per-user resolution this preflight does not
    have. The advisory is about the URL and the declared config, not the
    deployment, so a declared-but-unwired credential can still speak up
    (a mild, acceptable false positive).
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return []
    from agentworks.capabilities.git_credential import (
        GIT_CREDENTIAL_PROVIDER_REGISTRY,
    )

    seen: set[str] = set()
    advisories: list[str] = []
    for name, cred in registry.iter_kind_items("git-credential"):
        provider_cls = GIT_CREDENTIAL_PROVIDER_REGISTRY.get(cred.provider)
        if provider_cls is None:
            continue
        provider = provider_cls(
            name, cred.provider_config, description=cred.description
        )
        for msg in provider.review_remote(url):
            if msg not in seen:
                seen.add(msg)
                advisories.append(msg)
    return advisories


def runup_and_filter(
    providers: dict[str, GitCredentialProvider],
    git_tokens: dict[str, str],
    config: Config,
    logger: _WarnLogger | None = None,
) -> dict[str, GitCredentialProvider]:
    """The deferred git-credential runup, run right before the materials
    op writes anything.

    Authenticate each provider's resolved token against its host; a
    definitive rejection is SKIPPED with a warning (the operator fixes
    the token and re-runs) and dropped from the returned set, rather than
    sinking the whole operation; git-credential provisioning is
    idempotently retryable, so continuing to a partial result and letting
    a reinit recover it is the right call for its callers (vm/agent
    provisioning). Returns the providers whose tokens passed, or all of
    them when the operator disabled the stage via ``[defaults]
    runup_git_credentials``.

    ``logger`` (an ``SSHLogger``, when the caller has one) records the
    skip as a warning so a VM initialization degrades to PARTIAL.

    This is the domain face of the shared skip-and-degrade runup
    policy (``orchestration.readiness.runup_skip_and_degrade``): the
    policy mechanics live there, the git-credential messaging here.
    """
    if not config.defaults.runup_git_credentials:
        return providers

    from agentworks.capabilities.base import RunContext
    from agentworks.errors import TokenRejectedError
    from agentworks.orchestration.readiness import runup_skip_and_degrade

    by_secret = {p.secret_name: git_tokens[name] for name, p in providers.items()}
    # Deliberately scope-less: this write-step context predates the
    # orchestrated model and carries no operation scope until the write
    # step itself migrates (the providers' runup reads only its token,
    # never the scope, so nothing is lost meanwhile).
    ctx = RunContext(config=config, secrets=_MappedSecrets(by_secret))

    def announce(provider: GitCredentialProvider) -> None:
        output.detail(f"Performing runup test for git-credential/{provider.owner_name}...")

    def on_reject(provider: GitCredentialProvider, exc: TokenRejectedError) -> None:
        msg = (
            f"git credential '{provider.owner_name}' rejected; skipping it "
            f"(fix the token and reinit): {exc}"
        )
        output.warn(msg)
        if logger is not None:
            logger.warning(msg)

    passed = runup_skip_and_degrade(
        providers.values(), ctx, announce=announce, on_reject=on_reject
    )
    return {provider.owner_name: provider for provider in passed}


@dataclass(frozen=True)
class CredentialMaterials:
    """Everything the initialization flow writes for git auth.

    ``store_content`` is the full ``~/.git-credentials`` body.
    ``gitconfig_content`` is the agentworks-owned include: exactly the
    ``useHttpPath`` switch (selection lives in the helper; no context
    sections exist). Both overwritten wholesale each init.
    """

    store_content: str
    gitconfig_content: str
    # THE git credential helper (POSIX sh), replacing git's
    # ``credential-store`` in the chain. ``get`` serves from the
    # agentworks-managed store file; ``erase`` never deletes (git
    # invokes it after a rejected auth, which is exactly when the
    # operator needs a diagnosis, not state destruction: with
    # credential-store, every failed auth silently deleted the
    # provisioned line); a foreign embedded username on github.com
    # draws a scoping warning when scoped credentials exist.
    helper_script: str


# The credential helper's on-VM path, registered as "!<path>" in the
# user's global ``credential.helper`` slot (replacing the old
# ``store``): git only shell-executes helper values starting with "!"
# or an absolute path, and the shell is what expands the tilde per-user,
# which is what makes the same content work for admin and agents. The
# old warn-only script (~/.agentworks-git-cred-warn.sh) is orphaned
# harmlessly on VMs initialized by earlier builds.
GIT_CRED_HELPER_PATH = "~/.agentworks-git-cred-helper.sh"

# The agentworks-owned gitconfig include carrying the useHttpPath switch.
# Referenced from the user's global gitconfig via include.path
# (tilde-literal, expanded per-user by git); overwritten wholesale on
# every init, so removing scopes degrades cleanly while at least one
# credential remains (removing ALL credentials leaves both files stale,
# a pre-existing gap shared with the store itself).
GIT_SCOPES_INCLUDE_PATH = "~/.agentworks-git-scopes.gitconfig"



def build_credential_materials(
    providers: dict[str, GitCredentialProvider],
    tokens: dict[str, str],
) -> CredentialMaterials:
    """Assemble the git credential store, the gitconfig include, and
    the selecting helper.

    Selection lives entirely in the generated helper: the include sets
    ``useHttpPath`` (safe: credential-store left the chain, and only
    our helper consumes the queries), so every query carries the remote
    path and the helper picks the most specific credential (exact repo,
    then owner, then the host's default, then the first store line for
    the host, the legacy semantics that keep ``add-git-credential``
    additions serving).

    Store ordering: unscoped credentials' lines first, so the legacy
    first-host-line fallback finds the default. Scope collisions (two
    credentials claiming the same repo or owner on one host) are a hard
    error: silent shadowing is dead-config ambiguity. Two UNSCOPED
    credentials on one host are tolerated first-wins (matching store
    order), not errored: released configs may carry them.
    """
    store_scoped: list[str] = []
    store_unscoped: list[str] = []
    records: list[_CredRecord] = []
    claimed: dict[tuple[str, str, str], str] = {}
    scoped_users: set[str] = set()
    unscoped_users: set[str] = set()
    for name, provider in providers.items():
        entry = provider.helper_entry()
        _assert_sh_safe("store username", entry.username)
        for scope_kind, value in (
            *(("repo", repo) for repo in entry.repos),
            *((("owner", entry.owner),) if entry.owner else ()),
        ):
            _assert_sh_safe(f"{scope_kind} scope", value)
            key = (entry.host, scope_kind, value)
            if key in claimed:
                raise ConfigError(
                    f"git credentials {claimed[key]!r} and {name!r} both "
                    f"claim scope {scope_kind} {value!r} on {entry.host}; "
                    f"scopes must be unambiguous"
                )
            claimed[key] = name
        records.append(_CredRecord(name, provider.secret_name, entry))
        lines = provider.credential_lines(tokens[name])
        if entry.repos or entry.owner:
            store_scoped.extend(lines)
            scoped_users.add(entry.username)
        else:
            store_unscoped.extend(lines)
            unscoped_users.add(entry.username)

    # The helper's host-default fallback keys on the store username and
    # skips any scoped username (host-blind). A username claimed by BOTH a
    # scoped and an unscoped credential would let one shadow the other, so
    # require the two sets disjoint (e.g. no scoped credential whose store
    # username is github's reserved unscoped ``x-access-token``).
    clash = scoped_users & unscoped_users
    if clash:
        raise ConfigError(
            f"git credential store username(s) {sorted(clash)!r} are claimed "
            f"by both a scoped and an unscoped credential; scoped and unscoped "
            f"usernames must be disjoint (rename the offending credential)"
        )

    header = "# Managed by agentworks (git credential selection); do not edit.\n"
    return CredentialMaterials(
        store_content="\n".join(store_unscoped + store_scoped) + "\n",
        # useHttpPath makes git send the remote path to the helper,
        # the whole selection mechanism. Harmless globally now that our
        # helper is the only consumer (the old hazard was
        # credential-store's path matching, and store is gone).
        gitconfig_content=header + "[credential]\n\tuseHttpPath = true\n",
        helper_script=_helper_script(records),
    )


@dataclass(frozen=True)
class _CredRecord:
    """One credential, as the helper generator needs it."""

    name: str
    secret_name: str
    entry: HelperEntry


# Values interpolated into sh case labels / word lists must be glob- and
# quote-inert. Everything reaching this is already charset-validated at
# its source (github scopes via _NAME_RE, azdo org at validate_config,
# store usernames = resource names); this guard makes the generator
# safe by construction rather than by distant invariant.
_SH_SAFE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _assert_sh_safe(what: str, value: str) -> None:
    if not _SH_SAFE_RE.match(value):
        raise ConfigError(
            f"git credential {what} {value!r} contains characters unsafe "
            f"for the generated credential helper"
        )


def _sh_squote(value: str) -> str:
    """Single-quote ``value`` for sh, escaping embedded single quotes.
    Used for free-text (credential and secret names in diagnosis
    messages), which is NOT charset-restricted."""
    return "'" + value.replace("'", "'\\''") + "'"


def _selection_block(records: list[_CredRecord]) -> str:
    """The per-host selection: exact repo, then owner, then default."""
    by_host: dict[str, list[HelperEntry]] = {}
    for record in records:
        by_host.setdefault(record.entry.host, []).append(record.entry)
    out: list[str] = []
    for host, host_entries in sorted(by_host.items()):
        out.append(f"    {host})")
        repo_lines = [
            f'        {"|".join(e.repos)}) echo {e.username}; return ;;'
            for e in host_entries
            if e.repos
        ]
        if repo_lines:
            out.append('        case "$1" in')
            out.extend(repo_lines)
            out.append("        esac")
        owner_lines = [
            f"        {e.owner}) echo {e.username}; return ;;"
            for e in host_entries
            if e.owner
        ]
        if owner_lines:
            out.append('        case "${1%%/*}" in')
            out.extend(owner_lines)
            out.append("        esac")
        # The host default is the UNSCOPED credential's username, or
        # nothing. No phantom fallback (github once hardcoded
        # x-access-token here): if no unscoped credential is declared, an
        # out-of-scope URL must select nothing, never a scoped credential.
        default = next(
            (e.username for e in host_entries if not e.repos and not e.owner),
            None,
        )
        if default:
            out.append(f"        echo {default}")
        out.append("        ;;")
    return "\n".join(out)


def _helper_script(records: list[_CredRecord]) -> str:
    """Render THE git credential helper (POSIX sh).

    ``get``: an explicit query username short-circuits to a direct line
    match (plus the bypasses-scoping warning when scoped credentials
    exist); otherwise selection runs on (host, path), normalized by
    stripping the leading slash and a ``.git`` suffix, and the chosen
    username keys back into the store file. If nothing matches, the first
    UNSCOPED store line for the host is served (a scoped credential never
    serves a URL outside its scope); with no unscoped credential either,
    no credential is served and a diagnosis names the missing scope. A
    pathless query on a host with scoped credentials warns: useHttpPath
    was set at init, but a local git config can override it. ``erase``
    deletes nothing: it fires when the remote rejected the credential, so
    it prints a diagnosis naming the credential and its secret. ``store``
    is a no-op. Queries are read per git's LF protocol; CRLF tolerance is
    deliberately out of scope.
    """
    known = " ".join(sorted({r.entry.username for r in records}))
    scoped_hosts = "|".join(
        sorted({r.entry.host for r in records if r.entry.repos or r.entry.owner})
    )
    # Usernames that own a scope: the fallback ("serve the first line for
    # the host") must skip these so a scoped credential never serves a URL
    # outside its scope.
    scoped_users = " ".join(
        sorted({r.entry.username for r in records if r.entry.repos or r.entry.owner})
    )
    cases: list[str] = []
    seen: set[str] = set()
    for record in records:
        username = record.entry.username
        if username in seen:
            continue  # first credential wins, matching store-line order
        seen.add(username)
        cred_q = _sh_squote(f"agentworks: the remote rejected git credential '{record.name}'.")
        secret_q = _sh_squote(
            f"The token in secret '{record.secret_name}' is likely invalid, "
            f"expired, or lacks access."
        )
        cases.append(
            f"""        {username})
            printf '%s\\n' {cred_q}
            printf '%s\\n' {secret_q}
            printf '%s\\n' "Fix the secret, then re-run 'agw agent reinit <agent>' or 'agw vm reinit <vm>'."
            ;;"""
        )
    warn_block = ""
    if scoped_hosts:
        warn_block = """
    if [ "$host" = "github.com" ] && [ -n "$username" ]; then
        case " @KNOWN@ " in
            *" $username "*) : ;;
            *)
                {
                    echo "agentworks: this remote embeds username '$username', which"
                    echo "bypasses git credential scoping for github.com; use a plain"
                    echo "https remote (scoping selects the credential automatically)"
                } >&2
                ;;
        esac
    fi""".replace("@KNOWN@", known)
    nopath_block = ""
    if scoped_hosts:
        nopath_block = """
    if [ -z "$qpath" ]; then
        case "$host" in
        @SCOPED_HOSTS@)
            {
                echo "agentworks: git sent no repository path for $host, so"
                echo "credential scoping cannot select per-repo/per-owner; check that"
                echo "credential.useHttpPath is still true (agentworks sets it, but a"
                echo "local git config may override it); falling back to the host"
                echo "default, if one is declared"
            } >&2
            ;;
        esac
    fi""".replace("@SCOPED_HOSTS@", scoped_hosts)
    # Diagnosis for the serve-nothing path: nothing matched the URL and no
    # unscoped credential exists, so we served no credential rather than
    # leak a scoped one. Gated to hosts agentworks actually scopes, so it
    # stays silent for unrelated hosts (another helper may serve those).
    nocred_block = ""
    if scoped_hosts:
        nocred_block = """
    case "$host" in
    @SCOPED_HOSTS@)
        echo "agentworks: no credential is scoped to $host/$p; none served" >&2
        echo "(scope a credential to cover it, then reinit the vm/agent)" >&2
        ;;
    esac""".replace("@SCOPED_HOSTS@", scoped_hosts)
    template = """#!/bin/sh
# Managed by agentworks (git credential helper); do not edit.
# Serves the agentworks-owned ~/.git-credentials and never deletes it:
# git invokes 'erase' after a rejected auth, which is exactly when the
# operator needs a diagnosis, not state destruction.
op="$1"
proto=""; host=""; username=""; qpath=""
while IFS='=' read -r key value; do
    case "$key" in
        protocol) proto="$value" ;;
        host) host="$value" ;;
        username) username="$value" ;;
        path) qpath="$value" ;;
    esac
done

creds="$HOME/.git-credentials"

# Most-specific credential for (host, path): exact repo, then owner
# (first path segment), then the host default.
select_username() {
    case "$host" in
@SELECT@
    esac
}

serve() {
    # $1 = required username; "" = the first UNSCOPED line for the host.
    # A scoped credential must never serve a URL outside its scope, so the
    # fallback ($1 empty) skips any username that owns a scope.
    [ -r "$creds" ] || return 1
    while IFS= read -r line; do
        case "$line" in *://*@*) : ;; *) continue ;; esac
        rest="${line#*://}"
        userinfo="${rest%%@*}"
        hostpath="${rest#*@}"
        [ "${hostpath%%/*}" = "$host" ] || continue
        luser="${userinfo%%:*}"
        if [ -n "$1" ]; then
            [ "$luser" = "$1" ] || continue
        else
            case " @SCOPED_USERS@ " in *" $luser "*) continue ;; esac
        fi
        printf 'protocol=%s\\n' "${proto:-https}"
        printf 'host=%s\\n' "$host"
        printf 'username=%s\\n' "$luser"
        printf 'password=%s\\n' "${userinfo#*:}"
        return 0
    done < "$creds"
    return 1
}

case "$op" in
get)@WARN@
    if [ -n "$username" ]; then
        serve "$username"
        exit 0
    fi@NOPATH@
    p="${qpath#/}"
    p="${p%.git}"
    wanted=$(select_username "$p")
    if [ -n "$wanted" ] && serve "$wanted"; then
        exit 0
    fi
    if serve ""; then
        exit 0
    fi@NOCRED@
    exit 0
    ;;
erase)
    # git calls erase when the remote rejected the credential.
    case "$username" in
@CASES@
        *) : ;;
    esac >&2
    exit 0
    ;;
*)
    exit 0
    ;;
esac
"""
    return (
        template.replace("@WARN@", warn_block)
        .replace("@NOPATH@", nopath_block)
        .replace("@NOCRED@", nocred_block)
        .replace("@SELECT@", _selection_block(records))
        .replace("@SCOPED_USERS@", scoped_users)
        .replace("@CASES@", "\n".join(cases) if cases else "        _none_) : ;;")
    )
