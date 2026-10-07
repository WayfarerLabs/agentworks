"""Private HTTP worker whose owning carrier enforces its total lifetime.

Connection authority and request bodies arrive on stdin, never process argv.
Only a bounded provider response leaves stdout; errors produce no provider text.
This module exposes no production CLI or credential discovery.
"""

from __future__ import annotations

import json
import math
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from http.client import HTTPMessage

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_WORKER_INPUT_BYTES = 8 * 1024 * 1024
# PVE stores the entire UPID as a task-log filename. This is a conservative
# Linux filename-component capacity, not an API schema or identity constraint.
_MAX_TASK_ID_BYTES = 255


class _Endpoint(StrEnum):
    GUEST_AGENT = "guest-agent"
    POWER = "power"
    CURRENT_CONFIG = "current-config"
    VM_START = "vm-start"
    TASK_STATUS = "task-status"
    GUEST_INFO = "guest-info"


def _valid_control_timeout(value: object) -> bool:
    if type(value) is not int and type(value) is not float:
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _valid_task_id(value: object) -> bool:
    if type(value) is not str or not value or len(value) > _MAX_TASK_ID_BYTES:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_TASK_ID_BYTES
    except UnicodeEncodeError:
        return False


def _task_component(value: object) -> str:
    if not _valid_task_id(value):
        raise ValueError("Task status requires a bounded nonempty task identifier")
    assert isinstance(value, str)
    # quote always preserves unreserved dots; encode them too so a literal
    # identifier cannot put dot segments on the request path.
    return urllib.parse.quote(value, safe="").replace(".", "%2E")


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
    if set(payload) != {"connection", "endpoint", "method", "suffix", "body", "timeout"}:
        raise ValueError("Invalid fixed worker request")
    endpoint = _Endpoint(payload["endpoint"])
    connection = payload["connection"]
    node = urllib.parse.quote(connection["node"], safe="")
    origin = connection["api_url"].rstrip("/")
    node_base = f"{origin}/api2/json/nodes/{node}"
    base = f"{node_base}/qemu/{connection['vmid']}"
    method, suffix, body = payload["method"], payload["suffix"], payload["body"]
    if endpoint is _Endpoint.GUEST_AGENT:
        if not (
            method == "POST"
            and suffix == "exec"
            and type(body) is str
            or method == "GET"
            and type(suffix) is str
            and re.fullmatch(r"exec-status\?pid=[1-9][0-9]*", suffix)
            and body is None
        ):
            raise ValueError("Guest execution requires a fixed method, route and body")
        url = f"{base}/agent/{suffix}"
    elif endpoint in (_Endpoint.POWER, _Endpoint.CURRENT_CONFIG, _Endpoint.GUEST_INFO):
        if suffix is not None or method != "GET" or body is not None:
            raise ValueError("Provider observation requires a fixed body-free GET")
        route = {
            _Endpoint.POWER: "status/current",
            _Endpoint.CURRENT_CONFIG: "config?current=1",
            _Endpoint.GUEST_INFO: "agent/info",
        }[endpoint]
        url = f"{base}/{route}"
    elif endpoint is _Endpoint.VM_START:
        if suffix is not None or method != "POST" or body is not None:
            raise ValueError("VM start requires a fixed body-free POST")
        url = f"{base}/status/start"
    else:
        if method != "GET" or body is not None:
            raise ValueError("Task status requires a fixed body-free GET")
        url = f"{node_base}/tasks/{_task_component(suffix)}/status"
    if endpoint in (_Endpoint.VM_START, _Endpoint.TASK_STATUS, _Endpoint.GUEST_INFO) and not _valid_control_timeout(
        payload["timeout"]
    ):
        raise ValueError("VM control requires a positive finite timeout")
    request = urllib.request.Request(
        url, data=body.encode("ascii") if body is not None else None, method=payload["method"]
    )
    request.add_header("Authorization", f"PVEAPIToken={connection['token_id']}={connection['token_secret']}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    context = ssl.create_default_context(cafile=connection["ca_bundle"])
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
