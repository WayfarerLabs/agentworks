"""Tailscale connectivity preflight/rejoin and git-credential setup on the
VM. Grouped together because both are the provisioning-time credential
family: the local machine's Tailscale membership and the VM's git tokens.
"""

from __future__ import annotations

import ipaddress
import shlex
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ConnectivityError, NotFoundError, StateError
from agentworks.ssh import SSHError, SSHLogger

if TYPE_CHECKING:
    from agentworks.capabilities.git_credential.base import GitCredentialProvider
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources.registry import Registry
    from agentworks.transports import Transport


def verify_tailscale_available() -> None:
    """Pre-flight: verify the local machine is on Tailscale."""
    try:
        result = subprocess.run(
            ["tailscale", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except FileNotFoundError:
        raise ConnectivityError("'tailscale' command not found. Install Tailscale on this machine.") from None
    except subprocess.TimeoutExpired:
        raise ConnectivityError("'tailscale status' timed out. Is Tailscale running?") from None

    if result.returncode != 0:
        raise ConnectivityError(
            "This machine is not connected to Tailscale. "
            "VM initialization requires Tailscale to switch from the provisioning "
            "transport to direct SSH. Run 'tailscale up' first."
        )


def resolve_git_credential_providers(
    registry: Registry,
    names: list[str],
) -> dict[str, GitCredentialProvider]:
    """Construct git credential provider (capability) instances from the
    registry.

    ``names`` are the credential names to construct (from the admin
    row's or an agent template's ``git_credentials`` list). Each
    provider is built from its ``provider_config`` and re-validates that
    config at construct (so a bad scope value fails loudly here, never
    silently WIDENING the credential). The declared token secrets join
    an operation's boundary union through the holding node's
    ``secret_refs``; construction touches no secret machinery.
    """
    from agentworks.capabilities.git_credential import (
        GIT_CREDENTIAL_PROVIDER_REGISTRY,
    )
    from agentworks.resources.access import git_credential

    providers: dict[str, GitCredentialProvider] = {}
    if not names:
        return providers

    for name in names:
        cred_config = git_credential(registry, name)
        if cred_config is None:
            raise NotFoundError(
                f"git credential '{name}' not found in config",
                entity_kind="git-credential",
                entity_name=name,
            )
        provider_cls = GIT_CREDENTIAL_PROVIDER_REGISTRY.get(cred_config.provider)
        if provider_cls is None:
            # Unknown provider names are caught by the framework's
            # git-credential-provider miss policy at build_registry; this
            # guards direct callers that bypass that path.
            raise NotFoundError(
                f"git credential '{name}' names unknown provider {cred_config.provider!r}",
                entity_kind="git-credential-provider",
                entity_name=cred_config.provider,
            )
        providers[name] = provider_cls(
            name,
            cred_config.provider_config,
            description=cred_config.description,
        )
    return providers


def announce_git_credentials(providers: dict[str, GitCredentialProvider]) -> None:
    """Echo the git credentials the operation will configure, one
    ``Checking git-credential/<name>...`` line each so the Preflight
    context matches the ``vm-site`` / ``vm-template`` lines above, in the
    ``<kind>/<name>`` form operators pass to ``agw resource`` and the
    like. (The former per-provider auth pre-flight was vestigial: every
    provider's check returned True unconditionally once token resolution
    moved to the secret framework. Token health reports through doctor's
    Secrets group and resolution failures surface as
    ``SecretUnavailableError`` at collect time.)
    """
    for name in providers:
        output.info(f"Checking git-credential/{name}...")


def rejoin_tailscale(
    db: Database,
    vm_name: str,
    exec_target: Transport,
    *,
    auth_key: str,
) -> str:
    """Re-join Tailscale on a VM that lost its node (e.g. ephemeral key).

    Installs Tailscale if needed, joins the tailnet, and updates the DB
    with the new Tailscale IP. ``auth_key`` is resolved by the caller via
    the framework's eager-resolve.

    Returns the new Tailscale IP.
    """
    output.info("Tailscale node not reachable. Re-joining tailnet...")

    # Ensure Tailscale is installed (idempotent)
    exec_target.run(
        "bash -c 'command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh'",
        sudo=True,
        check=False,
    )

    return _join_tailscale(db, vm_name, exec_target, auth_key=auth_key)


def _join_tailscale(
    db: Database,
    vm_name: str,
    exec_target: Transport,
    *,
    auth_key: str,
    logger: SSHLogger | None = None,
) -> str:
    """Join Tailscale, update DB. Returns the Tailscale IP.

    The Tailscale auth key arrives via the ``auth_key`` keyword argument
    from the framework's eager-resolve at manager-entry. The legacy
    env-var fallback and
    prompt-here-if-missing path are gone; callers must thread the
    resolved value in.
    """
    quoted_key = shlex.quote(auth_key)
    # Daemon-side flags (e.g. --tun=userspace-networking for WSL2) live in
    # /etc/default/tailscaled, set during bootstrap. `tailscale up` is the
    # client and only takes client-side flags.
    ts_cmd = f"tailscale up --auth-key {quoted_key}"

    # Redact the auth key from any attached loggers before it appears in logs.
    if exec_target.logger is not None:
        exec_target.logger.add_redaction(auth_key)
    if logger is not None:
        logger.add_redaction(auth_key)

    exec_target.run(ts_cmd, sudo=True)
    result = exec_target.run("tailscale ip -4", sudo=True)

    raw_ip_output = result.stdout.strip()
    tailscale_ip = raw_ip_output.splitlines()[0].strip() if raw_ip_output else ""
    try:
        ipaddress.IPv4Address(tailscale_ip)
    except ValueError:
        raise SSHError(f"tailscale ip -4 returned invalid address: {raw_ip_output!r}") from None
    output.detail(f"Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    return tailscale_ip


def _configure_git_credentials(
    vm_name: str,
    ts_target: Transport,
    providers: dict[str, GitCredentialProvider],
    logger: SSHLogger,
    *,
    git_tokens: dict[str, str],
    config: Config,
) -> None:
    """Configure git credential store on the VM with the pre-resolved
    framework tokens.

    ``git_tokens`` is required
    (no provider-side fallback); the framework resolves every token
    at manager-entry and threads the ``{credential_name: value}``
    dict in. Any name in ``providers`` that doesn't have a matching
    key in ``git_tokens`` is a contract violation (caller bug); we
    raise loudly rather than silently dropping the credential, since
    silently shipping a VM with a missing credential the operator
    asked for is the worst kind of footgun.

    Two distinct error semantics here, deliberately: the deferred RUNUP
    (``runup_and_filter``) is per-credential and forgiving. A token an
    authenticated probe rejects is skipped (warned, so init goes PARTIAL)
    and the rest are still configured, because a bad token is
    idempotently fixable (fix it, reinit). But the MATERIALS BUILD over
    whatever survived runup is atomic: a failure there (e.g. a scope
    collision) is a config error, not a bad token, so it aborts
    credential config as a whole rather than shipping a partial store.
    """
    logger.step("Git credentials")
    output.info("Configuring git credentials...")

    missing = [name for name in providers if name not in git_tokens]
    if missing:
        raise StateError(
            f"git credential setup: token(s) not resolved by the framework "
            f"for {missing!r}; caller must pre-resolve every provider's "
            f"token through the operation's resolve pass before invoking "
            f"this function",
            entity_kind="git-credential",
            entity_name=missing[0],
        )

    from agentworks.git_credentials import (
        GIT_CRED_HELPER_PATH,
        GIT_SCOPES_INCLUDE_PATH,
        build_credential_materials,
        runup_and_filter,
    )

    # Deferred git-credential runup: authenticate each token right before
    # it is written. A rejected credential is skipped (logged as a warning
    # -> PARTIAL) and the rest are still configured; the operator fixes the
    # token and reruns reinit. Runs here, not at the command root, so a
    # bad token never blocks the VM from provisioning.
    providers = runup_and_filter(providers, git_tokens, config, logger)
    if not providers:
        return
    # Store lines + the useHttpPath include + the selecting helper (the
    # helper picks the per-repo/per-owner credential from the remote path
    # git now sends; issue #166). Scope collisions raise here, before
    # anything is written; the check sees only the runup SURVIVORS, so a
    # collision masked by a rejected token surfaces on the next reinit.
    materials = build_credential_materials(providers, git_tokens)

    # Write credentials and configure git on the VM. The context
    # sections live in an agentworks-owned include file (overwritten
    # wholesale every init, so removing scopes from config is
    # idempotent) rather than in ~/.gitconfig itself.
    try:
        ts_target.write_file("~/.git-credentials", materials.store_content, mode="600")
        ts_target.write_file(GIT_SCOPES_INCLUDE_PATH, materials.gitconfig_content, mode="600")
        ts_target.write_file(GIT_CRED_HELPER_PATH, materials.helper_script, mode="700")
        # Our helper REPLACES credential-store in the same config slot
        # (single-value replace also migrates released VMs off 'store'
        # on their next reinit; store deletes the provisioned line on
        # every rejected auth).
        ts_target.run(
            f"git config --global --replace-all credential.helper '!{GIT_CRED_HELPER_PATH}' && "
            f"(git config --global --get-all include.path | grep -qxF '{GIT_SCOPES_INCLUDE_PATH}' "
            f"|| git config --global --add include.path '{GIT_SCOPES_INCLUDE_PATH}')",
        )
        output.detail(f"Git credentials configured for {output.count(len(providers), 'provider')}")
    except SSHError as e:
        msg = f"git credential store setup failed: {e}"
        logger.warning(msg)
        output.warn(msg)
