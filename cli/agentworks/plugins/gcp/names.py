"""Exact bounded GCE identities derived from an Agentworks hostname."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from uuid import UUID, uuid4

_RFC1035 = re.compile(r"^[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_INVALID_RUN = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class GceNames:
    """Every stable provider identity retained for one VM."""

    backend_name: str
    network_tag: str
    deny_rule: str
    allow_rule: str


def _stem(hostname: str) -> str:
    stem = _INVALID_RUN.sub("-", hostname.lower()).strip("-") or "agw"
    return stem if stem[0].isalpha() else f"agw-{stem}"


def _digest(role: str, hostname: str) -> str:
    return hashlib.sha256(f"{role}\0{hostname}".encode()).hexdigest()[:10]


def derive_names(hostname: str) -> GceNames:
    """Apply the reviewed exact formulas for all stable GCE names."""
    stem = _stem(hostname)
    backend_name = (
        hostname
        if len(hostname) <= 63 and _RFC1035.fullmatch(hostname) is not None
        else f"{stem[:52].rstrip('-')}-{_digest('instance', hostname)}"
    )
    return GceNames(
        backend_name=backend_name,
        network_tag=f"{stem[:48].rstrip('-')}-tag-{_digest('tag', hostname)}",
        deny_rule=f"{stem[:47].rstrip('-')}-deny-{_digest('deny', hostname)}",
        allow_rule=f"{stem[:46].rstrip('-')}-allow-{_digest('allow', hostname)}",
    )


def transient_route_name(hostname: str, nonce: UUID | None = None) -> str:
    """Return one non-retained UUID-scoped native-route firewall name."""
    token = (nonce or uuid4()).hex[:20]
    return f"{_stem(hostname)[:36].rstrip('-')}-route-{token}"
