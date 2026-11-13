"""Stdlib-only wire and lifecycle checks for the owned native-resume fixture."""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import signal
import socket
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/testing/harnesses/native_resume_fixture.py"
SPEC = importlib.util.spec_from_file_location("native_resume_fixture", FIXTURE_PATH)
assert SPEC is not None and SPEC.loader is not None
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = io.StringIO()
        with (
            patch("socket.getfqdn", side_effect=AssertionError("DNS lookup")),
            patch("socket.gethostbyaddr", side_effect=AssertionError("DNS lookup")),
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS lookup")),
        ):
            self.server = fixture.FixtureServer(output=self.output)
        self.address = ("127.0.0.1", self.server.server_port)
        self.thread = threading.Thread(target=self.server.run, args=(30,))
        self.thread.start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self) -> None:
        self.server.stop()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        with socket.socket() as connection:
            connection.settimeout(1)
            self.assertNotEqual(connection.connect_ex(self.address), 0)
        events = self.events()
        self.assertEqual(events[0]["event"], "ready")
        self.assertEqual(events[0]["port"], self.address[1])
        self.assertEqual(events[-1]["event"], "stopped")
        self.assertEqual([row["sequence"] for row in events], list(range(1, len(events) + 1)))

    def events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def request(
        self,
        path: str,
        data: object = None,
        *,
        headers: dict[str, str] | None = None,
        method: str = "POST",
        body: bytes | None = None,
    ) -> tuple[int, str | None, bytes]:
        connection = http.client.HTTPConnection(*self.address, timeout=3)
        try:
            connection.request(method, path, json.dumps(data).encode() if body is None else body, headers or {})
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def mcp(
        self,
        method: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        identifier: int | str = 1,
    ) -> tuple[int, str | None, bytes]:
        return self.request(
            "/mcp",
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}},
            headers={"Authorization": "Bearer " + fixture.MCP_TOKEN} if headers is None else headers,
        )

    def test_mcp_requires_exact_public_canary_and_performs_real_discovery(self) -> None:
        for headers in (
            {},
            {"Authorization": "Bearer wrong-private-value"},
            {"Authorization": "Basic private-value"},
            {"Authorization": "Bearer " + fixture.MCP_TOKEN, "x-api-key": "private-value"},
        ):
            with self.subTest(headers_present=bool(headers)):
                status, _, body = self.mcp("tools/list", headers=headers)
                self.assertEqual(status, 401)
                self.assertNotIn("result", json.loads(body))
        status, _, body = self.mcp("initialize", params={"protocolVersion": "2025-11-25"}, identifier="native-request")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["id"], "native-request")
        self.assertEqual(result["result"]["protocolVersion"], "2025-11-25")
        self.assertIn("tools", result["result"]["capabilities"])
        status, _, body = self.mcp("tools/list")
        self.assertEqual(status, 200)
        tools = json.loads(body)["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], [fixture.CANARY_TOOL])
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
        observed = [row for row in self.events() if row["event"] == "mcp_request"]
        self.assertEqual([row["method"] for row in observed], ["initialize", "tools/list"])
        self.assertTrue(all(row["auth_valid"] for row in observed))

    def test_canary_call_is_constant_and_unknown_tool_has_no_healthy_result(self) -> None:
        status, _, body = self.mcp("tools/call", params={"name": fixture.CANARY_TOOL, "arguments": {}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["content"], [{"type": "text", "text": fixture.MARKER}])
        status, _, body = self.mcp("tools/call", params={"name": "not-the-canary"})
        self.assertEqual(status, 200)
        self.assertIn("error", json.loads(body))
        self.assertNotIn("result", json.loads(body))
        status, _, _ = self.request(
            "/mcp",
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Authorization": "Bearer " + fixture.MCP_TOKEN},
        )
        self.assertEqual(status, 202)
        for method in ("ping", "resources/list", "resources/templates/list", "prompts/list"):
            self.assertEqual(self.mcp(method)[0], 200)

    def test_provider_json_and_native_tested_sse_frames(self) -> None:
        for endpoint, key in (("messages", "messages"), ("responses", "input")):
            for stream in (False, True):
                with self.subTest(endpoint=endpoint, stream=stream):
                    status, content_type, body = self.request(
                        "/v1/" + endpoint, {key: [{"role": "user", "content": fixture.PROMPT1}], "stream": stream}
                    )
                    self.assertEqual(status, 200)
                    if not stream:
                        value = json.loads(body)
                        message = value if endpoint == "messages" else value["output"][0]
                        self.assertEqual(message["content"][0]["text"], fixture.MARKER)
                        self.assertEqual(message["role"], "assistant")
                        continue
                    self.assertEqual(content_type, "text/event-stream")
                    events = []
                    for frame in body.decode().strip().split("\n\n"):
                        event_name, data = frame.split("\n")
                        value = json.loads(data.removeprefix("data: "))
                        self.assertEqual(event_name, "event: " + value["type"])
                        events.append(value)
                    if endpoint == "messages":
                        self.assertEqual(events[0]["type"], "message_start")
                        self.assertEqual(events[-1]["type"], "message_stop")
                        deltas = [row["delta"]["text"] for row in events if row["type"] == "content_block_delta"]
                    else:
                        self.assertEqual(events[0]["type"], "response.created")
                        self.assertEqual(events[-1]["type"], "response.completed")
                        self.assertEqual([row["sequence_number"] for row in events], list(range(len(events))))
                        deltas = [row["delta"] for row in events if row["type"] == "response.output_text.delta"]
                        self.assertEqual(events[-1]["response"]["output"][0]["content"][0]["text"], fixture.MARKER)
                    self.assertEqual("".join(deltas), fixture.MARKER)

    def test_role_aware_context_and_nested_canary_metadata(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "input_text", "text": fixture.PROMPT1}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": fixture.MARKER}]},
            {"role": "user", "content": fixture.PROMPT2},
        ]
        tool_forms = [
            [{"name": "mcp__owned__" + fixture.CANARY_TOOL}],
            [{"type": "namespace", "name": "mcp__owned", "tools": [{"type": "function", "name": fixture.CANARY_TOOL}]}],
            [{"type": "function", "function": {"name": "mcp__owned__" + fixture.CANARY_TOOL}}],
        ]
        for tools in tool_forms:
            status, _, _ = self.request("/v1/responses", {"input": messages, "tools": tools})
            self.assertEqual(status, 200)
            row = self.events()[-1]
            self.assertEqual(row["message_or_input_count"], 3)
            self.assertTrue(
                all(
                    row[key]
                    for key in (
                        "fresh_prompt_present",
                        "resume_prompt_present",
                        "prior_synthetic_reply_present",
                        "canary_tool_present",
                    )
                )
            )
        self.request("/v1/messages", {"messages": [{"role": "user", "content": fixture.PROMPT1}]})
        fresh = self.events()[-1]
        self.assertTrue(fresh["fresh_prompt_present"])
        self.assertFalse(fresh["resume_prompt_present"])
        self.assertFalse(fresh["prior_synthetic_reply_present"])

    def test_markers_in_wrong_roles_or_tool_data_do_not_report_continuation(self) -> None:
        user_markers = fixture.PROMPT1 + fixture.PROMPT2
        messages = [
            {"role": "system", "content": user_markers + fixture.MARKER},
            {"role": "assistant", "content": user_markers},
            {"role": "user", "content": fixture.MARKER},
            {"role": "tool", "content": user_markers + fixture.MARKER},
            {"role": "user", "content": [{"type": "tool_result", "content": user_markers}]},
            {"role": "assistant", "content": [{"type": "tool_use", "text": fixture.MARKER}]},
        ]
        tools = [
            {
                "type": "namespace",
                "name": "mcp__codex_tui",
                "tools": [{"name": "run", "description": fixture.CANARY_TOOL}],
            },
            {"name": "unrelated", "description": fixture.CANARY_TOOL},
        ]
        self.request("/v1/responses", {"input": messages, "instructions": user_markers, "tools": tools})
        row = self.events()[-1]
        self.assertFalse(
            any(
                row[key]
                for key in (
                    "fresh_prompt_present",
                    "resume_prompt_present",
                    "prior_synthetic_reply_present",
                    "canary_tool_present",
                )
            )
        )

    def test_provider_auth_accepts_only_absence_or_fixed_public_keys(self) -> None:
        for headers in ({}, {"x-api-key": fixture.PROVIDER_KEY}, {"Authorization": "Bearer " + fixture.PROVIDER_KEY}):
            self.assertEqual(self.request("/v1/messages", {"messages": []}, headers=headers)[0], 200)
        for headers in (
            {"x-api-key": "private-value"},
            {"Authorization": "Bearer private-value"},
            {"Proxy-Authorization": "private-value"},
        ):
            status, _, body = self.request("/v1/responses", {"input": []}, headers=headers)
            self.assertEqual(status, 401)
            self.assertNotIn(fixture.MARKER.encode(), body)
        rejected = [row for row in self.events() if row["event"] == "request_rejected"]
        self.assertTrue(all(row["auth_valid"] is False for row in rejected))

    def test_count_tokens_and_maximum_valid_body_use_the_same_listener(self) -> None:
        status, _, body = self.request("/v1/messages/count_tokens?ignored=true", {"messages": []})
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(body)["input_tokens"], 0)
        body = b'{"messages": []}'
        body += b" " * (fixture.MAX_BODY - len(body))
        self.assertEqual(self.request("/v1/messages", body=body)[0], 200)

    def test_query_is_ignored_and_no_raw_secret_body_or_header_is_logged(self) -> None:
        secret = "private-request-body-and-query-641902"
        error_output = io.StringIO()
        with contextlib.redirect_stderr(error_output):
            self.assertEqual(
                self.request(
                    "/v1/messages?private=" + secret,
                    {
                        "messages": [{"role": "user", "content": fixture.PROMPT1 + secret}],
                        "tools": [{"name": "private-tool-" + secret}],
                    },
                )[0],
                200,
            )
            self.assertEqual(self.request("/unknown?private=" + secret, {"raw": secret})[0], 404)
            self.assertEqual(self.request("/v1/messages", {"raw": secret}, headers={"x-api-key": secret})[0], 401)
            self.assertEqual(self.request("/v1/messages", body=('{"private":"' + secret).encode())[0], 400)
        logged = self.output.getvalue() + error_output.getvalue()
        self.assertNotIn(secret, logged)
        self.assertNotIn(fixture.PROMPT1, logged)
        self.assertNotIn("private-tool", logged)
        self.assertEqual(error_output.getvalue(), "")
        for row in self.events():
            self.assertTrue(all(isinstance(value, (str, int, float, bool)) for value in row.values()))

    def test_malformed_and_bounded_requests_are_refused_without_stopping_listener(self) -> None:
        for body in (
            b"{invalid",
            b"[]",
            b'{"stream": "true"}',
            b'{"messages": 1}',
            b'{"messages": [1]}',
            b'{"tools": 1}',
            b'{"ignored": NaN}',
        ):
            with self.subTest(body_type=type(body).__name__):
                self.assertEqual(self.request("/v1/messages", body=body)[0], 400)
        for length, status in ((str(fixture.MAX_BODY + 1), 413), ("-1", 400), ("bad", 400)):
            self.assertEqual(self.request("/v1/messages", body=b"{}", headers={"Content-Length": length})[0], status)
        status, _, _ = self.request(
            "/mcp",
            {"jsonrpc": "2.0", "id": True, "method": "initialize"},
            headers={"Authorization": "Bearer " + fixture.MCP_TOKEN},
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.request("/health", method="GET")[0], 200)
        self.assertEqual(self.request("/unknown", method="GET")[0], 404)
        self.assertEqual(self.request("/mcp", method="DELETE")[0], 401)

    def test_duplicate_auth_headers_are_refused(self) -> None:
        connection = http.client.HTTPConnection(*self.address, timeout=3)
        try:
            connection.putrequest("POST", "/mcp")
            connection.putheader("Authorization", "Bearer " + fixture.MCP_TOKEN)
            connection.putheader("Authorization", "Bearer " + fixture.MCP_TOKEN)
            connection.putheader("Content-Length", "0")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            connection.close()


class LifecycleTests(unittest.TestCase):
    def assert_closed(self, address: tuple[str, int]) -> None:
        with socket.socket() as connection:
            connection.settimeout(1)
            self.assertNotEqual(connection.connect_ex(address), 0)

    def test_lifetime_closes_active_partial_request_and_joins_timer(self) -> None:
        server = fixture.FixtureServer(output=io.StringIO())
        address = ("127.0.0.1", server.server_port)
        thread = threading.Thread(target=server.run, args=(0.2,))
        thread.start()
        connection = socket.socket()
        try:
            connection.connect(address)
            connection.sendall(b"POST /v1/messages HTTP/1.0\r\nContent-Length: 100\r\n\r\n{")
            thread.join(3)
            self.assertFalse(thread.is_alive())
        finally:
            connection.close()
            server.stop()
            thread.join(3)
            server.server_close()
        self.assert_closed(address)

    def run_signal_case(self, signum: int) -> None:
        class Output(io.StringIO):
            def write(self, value: str) -> int:
                result = super().write(value)
                if value == "\n" and '"event": "ready"' in self.getvalue():
                    ready.set()
                return result

        output = Output()
        ready = threading.Event()

        def terminate() -> None:
            if ready.wait(3):
                signal.raise_signal(signum)

        thread = threading.Thread(target=terminate)
        previous = signal.getsignal(signum)
        thread.start()
        try:
            with contextlib.redirect_stdout(output):
                fixture.main(["--lifetime", "2"])
        finally:
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(signal.getsignal(signum), previous)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]["event"], "stopped")
        self.assert_closed(("127.0.0.1", events[0]["port"]))

    def test_sigint_stops_and_restores_handler(self) -> None:
        self.run_signal_case(signal.SIGINT)

    def test_sigterm_stops_and_restores_handler(self) -> None:
        self.run_signal_case(signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
