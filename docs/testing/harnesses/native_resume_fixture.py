"""Loopback model/MCP fixture for native-created history, without an acceptance verdict."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer
from typing import TextIO

MARKER = "AGW_PUBLIC_SYNTHETIC_PROVIDER_REPLY"
PROMPT1 = "AGW_PUBLIC_FRESH_PROMPT: Reply with a short public marker only. Do not call tools."
PROMPT2 = "AGW_PUBLIC_RESUME_PROMPT: Continue the same conversation with a short public marker only. Do not call tools."
MCP_TOKEN = "AGW_PUBLIC_RESUME_MCP_CANARY"
PROVIDER_KEY = "AGW_PUBLIC_RESUME_PROVIDER_KEY"
CANARY_TOOL = "public_resume_canary"
MAX_BODY = 1024 * 1024


def frame(kind: str, **fields: object) -> dict:
    return {"type": kind, **fields}


def reject_constant(value: str) -> None:
    raise ValueError("non-JSON numeric constant")


def message_text(value: object) -> str:
    """Read conversation text blocks, excluding tool results and arbitrary nested data."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            block["text"]
            for block in value
            if isinstance(block, dict)
            and block.get("type") in ("text", "input_text", "output_text")
            and isinstance(block.get("text"), str)
        )
    return ""


def canary_present(tools: object) -> bool:
    """Recognize tool declarations, including native fully qualified and namespace forms."""
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function", {})
        name = tool.get("name", function.get("name") if isinstance(function, dict) else None)
        if (
            tool.get("type") != "namespace"
            and isinstance(name, str)
            and (name == CANARY_TOOL or (name.startswith("mcp__") and name.endswith("__" + CANARY_TOOL)))
        ):
            return True
        if tool.get("type") == "namespace" and canary_present(tool.get("tools")):
            return True
    return False


class FixtureServer(HTTPServer):
    """One owned IPv4 listener; run/stop never launch clients or write native state."""

    def __init__(self, port: int = 0, output: TextIO | None = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.output = output if output is not None else sys.stdout
        self.stopping = threading.Event()
        self.connection = None
        self.sequence = 0
        self.timeout = 0.1

    def server_bind(self) -> None:
        # HTTPServer's default server_bind performs a hostname lookup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def event(self, event: str, **fields: object) -> None:
        self.sequence += 1
        print(
            json.dumps({"sequence": self.sequence, "time": round(time.time(), 3), "event": event, **fields}),
            file=self.output,
            flush=True,
        )

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        self.connection = connection
        if self.stopping.is_set():
            connection.close()
            raise OSError("fixture stopped")
        return connection, address

    def handle_error(self, request, client_address) -> None:
        self.event("connection_error")

    def stop(self) -> None:
        self.stopping.set()
        if self.connection is not None:
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)

    def run(self, lifetime: float = 600) -> None:
        timer = None
        try:
            if not math.isfinite(lifetime) or not 0 < lifetime <= 3600:
                raise ValueError("lifetime must be in (0, 3600]")
            self.event("ready", port=self.server_port)
            timer = threading.Timer(lifetime, self.stop)
            timer.start()
            while not self.stopping.is_set():
                self.handle_request()
        finally:
            self.stop()
            if timer is not None:
                timer.cancel()
                timer.join()
            self.server_close()
            self.event("stopped")


class Handler(BaseHTTPRequestHandler):
    server: FixtureServer

    def log_message(self, *args: object) -> None:
        pass

    def send_error(self, code: int, message=None, explain=None) -> None:
        self.reply(code, {"error": "request refused"})

    def reply(self, status: int, value: object = None, *, events: list[dict] | None = None) -> None:
        body = (
            "".join("event: " + row["type"] + "\ndata: " + json.dumps(row) + "\n\n" for row in events).encode()
            if events is not None
            else json.dumps(value).encode()
            if value is not None
            else b""
        )
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if events is not None else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self) -> str:
        return {
            "/mcp": "mcp",
            "/v1/messages": "messages",
            "/v1/messages/count_tokens": "count_tokens",
            "/v1/responses": "responses",
            "/health": "health",
        }.get(self.path.split("?", 1)[0], "unknown")

    def authorized(self, endpoint: str) -> bool:
        authorization = self.headers.get_all("Authorization", [])
        api_key = self.headers.get_all("x-api-key", [])
        valid = (
            authorization == ["Bearer " + MCP_TOKEN] and not api_key
            if endpoint == "mcp"
            else authorization in ([], ["Bearer " + PROVIDER_KEY]) and api_key in ([], [PROVIDER_KEY])
        )
        return valid and not self.headers.get_all("Proxy-Authorization", [])

    def reject(self, endpoint: str, status: int, **fields: object) -> None:
        self.server.event("request_rejected", endpoint=endpoint, status=status, **fields)
        self.reply(status, {"error": "request refused"})

    def do_GET(self) -> None:
        endpoint = self.route()
        if endpoint == "health":
            self.reply(200, {"ready": True})
        elif endpoint == "mcp":
            self.reject(endpoint, 405 if self.authorized(endpoint) else 401, auth_valid=self.authorized(endpoint))
        else:
            self.reject("unknown", 404)

    def do_DELETE(self) -> None:
        endpoint = self.route()
        if endpoint != "mcp":
            self.reject("unknown", 404)
        elif not self.authorized(endpoint):
            self.reject(endpoint, 401, auth_valid=False)
        else:
            self.server.event("mcp_request", method="delete", auth_valid=True)
            self.reply(200, {})

    def do_POST(self) -> None:
        endpoint = self.route()
        if endpoint not in ("mcp", "messages", "responses", "count_tokens"):
            return self.reject("unknown", 404)
        if not self.authorized(endpoint):
            return self.reject(endpoint, 401, auth_valid=False)
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (
                len(lengths) != 1
                or not lengths[0].isascii()
                or not lengths[0].isdigit()
                or self.headers.get("Transfer-Encoding")
            ):
                raise ValueError
            size = int(lengths[0])
            if size > MAX_BODY:
                return self.reject(endpoint, 413)
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError
            data = json.loads(body, parse_constant=reject_constant)
            if not isinstance(data, dict):
                raise ValueError
            if endpoint == "mcp":
                return self.mcp(data)
            self.provider(endpoint, data)
        except (ValueError, TypeError, RecursionError, OSError):
            self.reject(endpoint, 400)

    def mcp(self, data: dict) -> None:
        method = data.get("method")
        params = data.get("params", {})
        identifier = data.get("id")
        if (
            data.get("jsonrpc") != "2.0"
            or not isinstance(method, str)
            or not isinstance(params, dict)
            or ("id" in data and (isinstance(identifier, bool) or not isinstance(identifier, (int, str))))
        ):
            raise ValueError
        known = {
            "initialize",
            "tools/list",
            "tools/call",
            "resources/list",
            "resources/templates/list",
            "prompts/list",
            "ping",
            "notifications/initialized",
            "notifications/cancelled",
        }
        self.server.event("mcp_request", method=method if method in known else "unknown", auth_valid=True)
        if "id" not in data:
            return self.reply(202)
        if method == "initialize":
            version = params.get("protocolVersion")
            result = {
                "protocolVersion": version
                if version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
                else "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agw-mcp-resume-canary", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": CANARY_TOOL,
                        "description": "Returns a constant public marker.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        elif method == "tools/call" and params.get("name") == CANARY_TOOL and params.get("arguments", {}) == {}:
            result = {"content": [{"type": "text", "text": MARKER}], "isError": False}
        elif method in ("resources/list", "prompts/list", "resources/templates/list"):
            result = {"resourceTemplates" if method == "resources/templates/list" else method.split("/")[0]: []}
        elif method == "ping":
            result = {}
        else:
            return self.reply(
                200, {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Method not found"}}
            )
        self.reply(200, {"jsonrpc": "2.0", "id": identifier, "result": result})

    def provider(self, endpoint: str, data: dict) -> None:
        messages = data.get("input" if endpoint == "responses" else "messages", [])
        if isinstance(messages, str) and endpoint == "responses":
            messages = [{"role": "user", "content": messages}]
        if (
            not isinstance(messages, list)
            or any(not isinstance(row, dict) for row in messages)
            or not isinstance(data.get("stream", False), bool)
            or not isinstance(data.get("tools", []), list)
        ):
            raise ValueError
        user = "\n".join(message_text(row.get("content")) for row in messages if row.get("role") == "user")
        assistant = "\n".join(message_text(row.get("content")) for row in messages if row.get("role") == "assistant")
        self.server.event(
            "provider_request",
            endpoint=endpoint,
            auth_valid=True,
            stream=data.get("stream", False),
            message_or_input_count=len(messages),
            fresh_prompt_present=PROMPT1 in user,
            resume_prompt_present=PROMPT2 in user,
            prior_synthetic_reply_present=MARKER in assistant,
            canary_tool_present=canary_present(data.get("tools")),
        )
        if endpoint == "count_tokens":
            return self.reply(200, {"input_tokens": 20})
        message_id = "msg_public_fixture_" + str(self.server.sequence)
        model = "public-fixture"
        if endpoint == "messages":
            message = {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": MARKER}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 5},
            }
            beginning = {
                **message,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 20, "output_tokens": 0},
            }
            events = [
                frame("message_start", message=beginning),
                frame("content_block_start", index=0, content_block={"type": "text", "text": ""}),
                frame("content_block_delta", index=0, delta={"type": "text_delta", "text": MARKER}),
                frame("content_block_stop", index=0),
                frame(
                    "message_delta",
                    delta={"stop_reason": "end_turn", "stop_sequence": None},
                    usage={"output_tokens": 5},
                ),
                frame("message_stop"),
            ]
            return self.reply(200, message, events=events if data.get("stream") else None)
        item = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": MARKER, "annotations": []}],
        }
        response = {
            "id": "resp_public_fixture_" + str(self.server.sequence),
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": [item],
            "error": None,
            "incomplete_details": None,
            "usage": {
                "input_tokens": 20,
                "output_tokens": 5,
                "total_tokens": 25,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        position = {"item_id": message_id, "output_index": 0, "content_index": 0}
        events = [
            frame("response.created", response={**response, "status": "in_progress", "output": []}),
            frame("response.output_item.added", output_index=0, item={**item, "status": "in_progress", "content": []}),
            frame(
                "response.content_part.added", **position, part={"type": "output_text", "text": "", "annotations": []}
            ),
            frame("response.output_text.delta", **position, delta=MARKER),
            frame("response.output_text.done", **position, text=MARKER),
            frame("response.content_part.done", **position, part=item["content"][0]),
            frame("response.output_item.done", output_index=0, item=item),
            frame("response.completed", response=response),
        ]
        for sequence, event in enumerate(events):
            event["sequence_number"] = sequence
        self.reply(200, response, events=events if data.get("stream") else None)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--lifetime", type=float, default=600)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or not math.isfinite(args.lifetime) or not 0 < args.lifetime <= 3600:
        parser.error("port must be 0..65535 and lifetime must be in (0, 3600]")
    server = FixtureServer(args.port)
    previous = {}
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, lambda *_: server.stop())
        server.run(args.lifetime)
    finally:
        server.server_close()
        for name, handler in previous.items():
            signal.signal(name, handler)


if __name__ == "__main__":
    main()
