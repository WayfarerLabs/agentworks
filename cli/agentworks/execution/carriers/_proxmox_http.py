"""Private HTTP worker whose owning carrier enforces its total lifetime.

Connection authority and request bodies arrive on stdin, never process argv.
Only a bounded provider response leaves stdout; errors produce no provider text.
This module exposes no production CLI or credential discovery.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from http.client import HTTPMessage

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_WORKER_INPUT_BYTES = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        """Never forward credentials or repeat a dispatch through a redirect."""
        return None


def _request(payload: dict[str, Any]) -> bytes:
    connection = payload["connection"]
    node = urllib.parse.quote(connection["node"], safe="")
    origin = connection["api_url"].rstrip("/")
    url = f"{origin}/api2/json/nodes/{node}/qemu/{connection['vmid']}/agent/{payload['suffix']}"
    body = payload["body"]
    request = urllib.request.Request(
        url, data=body.encode("ascii") if body is not None else None, method=payload["method"]
    )
    request.add_header("Authorization", f"PVEAPIToken={connection['token_id']}={connection['token_secret']}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    context = ssl.create_default_context()
    if not connection["verify_tls"]:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=payload["timeout"]) as response:
            encoded = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise ValueError("Provider response exceeded the observation bound")
    return bytes(encoded)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(_MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > _MAX_WORKER_INPUT_BYTES:
            return 1
        encoded = _request(json.loads(raw))
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
