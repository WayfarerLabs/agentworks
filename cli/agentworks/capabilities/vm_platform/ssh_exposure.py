"""Provider-neutral SSH-exposure helpers shared by cloud VM platforms:
operator egress-IP detection and the ``operator.ssh_allow_cidrs`` fold.

Every cloud platform that opens an ephemeral, scoped SSH route to a VM
during bootstrap and native transport needs the SAME notion of "which
source prefixes may reach port 22": the operator's detected public egress
address, plus any explicit ``operator.ssh_allow_cidrs`` extras. This
module owns that computation so azure (NSG rules) and aws (security-group
rules) share one detector, one cache, one detection-failure policy, and
one CIDR normalization, rather than each reimplementing it. The
provider-specific rule mechanics (an Azure ``SecurityRule`` vs an EC2
ingress permission) stay in each plugin's own network module.

Hoisted out of ``plugins/azure/network.py`` (which now re-exports these
for its own callers and tests); the behavior is byte-identical to what
azure shipped, so azure's behavioral assertions are unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ConfigError, ConnectivityError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config


# The what's-my-ip service the egress detection queries.
_EGRESS_IP_URL = "https://checkip.amazonaws.com"

# Per-process cache for the detected egress IP: one probe per command,
# not one per poke.
_egress_ip_cache: str | None = None


def detect_egress_ip() -> str:
    """The operator's public IPv4 address, detected via a what's-my-ip
    probe and cached per process (one probe per command, not per poke).

    Raises whatever the probe raises (URLError on unreachability,
    ValueError on a non-IPv4 response body); callers decide the policy
    (see :func:`operator_ssh_prefixes`).
    """
    global _egress_ip_cache
    if _egress_ip_cache is not None:
        return _egress_ip_cache

    import ipaddress
    import urllib.request

    with urllib.request.urlopen(_EGRESS_IP_URL, timeout=5) as response:  # noqa: S310  # fixed https URL
        body = response.read().decode("ascii", errors="strict").strip()
    # Strict parse: anything that is not a bare IPv4 address is a
    # detection failure, never a prefix we would poke into a firewall.
    _egress_ip_cache = str(ipaddress.IPv4Address(body))
    return _egress_ip_cache


def normalize_allow_cidrs(entries: Sequence[str]) -> list[str]:
    """Normalize ``operator.ssh_allow_cidrs`` entries to canonical IPv4
    prefixes (a bare IP becomes its /32). The config loader validates
    and normalizes at load, so this mostly re-normalizes already-clean
    values; a bad entry that reached here anyway (a hand-built config
    object) raises the same shape of typed ConfigError."""
    import ipaddress

    prefixes: list[str] = []
    for entry in entries:
        text = str(entry).strip()
        try:
            prefixes.append(str(ipaddress.IPv4Network(text, strict=False)))
        except ValueError as exc:
            raise ConfigError(
                f"operator.ssh_allow_cidrs: invalid entry {text!r}: must be an IPv4 address or CIDR"
            ) from exc
    return prefixes


def config_allow_cidrs(config: Config | None) -> list[str]:
    """The ``operator.ssh_allow_cidrs`` extras from an operator
    config, or none when no config was threaded in. Same defensive
    getattr chain as a platform's ``native_transport`` identity-file
    read (callers may thread partial config stand-ins)."""
    operator = getattr(config, "operator", None)
    return list(getattr(operator, "ssh_allow_cidrs", None) or [])


def operator_ssh_prefixes(extra_cidrs: Sequence[str] = ()) -> list[str]:
    """The source prefixes for the ephemeral SSH allow rule: the detected
    operator egress IPv4 as a /32, plus the ``operator.ssh_allow_cidrs``
    config extras handed in by the caller. Recomputed at every poke
    (detection caches per process) so the scope stays current.

    Detection-failure policy: with extras configured, proceed on the
    extras alone with a warning; with none, raise a typed
    ConnectivityError whose hint names the config setting as the escape
    hatch (an unscoped allow is never poked as a fallback).
    """
    extras = normalize_allow_cidrs(extra_cidrs)
    try:
        detected = f"{detect_egress_ip()}/32"
    except Exception as exc:
        if extras:
            output.warn(
                f"could not detect the operator's public IP ({exc}); "
                f"scoping SSH access to the operator.ssh_allow_cidrs entries only"
            )
            return extras
        raise ConnectivityError(
            f"could not detect the operator's public IP for the scoped SSH allow rule: {exc}",
            hint=(
                "set operator.ssh_allow_cidrs in your agentworks config to a list "
                "of IPv4 addresses and/or CIDRs (e.g. your VPN or NAT egress "
                "addresses) to grant SSH access explicitly"
            ),
        ) from exc
    return [detected, *(p for p in extras if p != detected)]
