"""Shared lifecycle-safe Chromium helpers for website browser tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable

DEVTOOLS_STARTUP_ATTEMPT_TIMEOUT = 20.0
DEVTOOLS_STARTUP_ATTEMPTS = 2


class DevToolsConnection:
    """Minimal bounded WebSocket client for one Chromium page target."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        operation_timeout: float | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(url)
        self.socket = socket.create_connection(
            (parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=timeout
        )
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            "Origin: http://127.0.0.1\r\n\r\n"
        )
        try:
            self.socket.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                block = self.socket.recv(4096)
                if not block:
                    raise ConnectionError("Chromium closed the DevTools handshake before sending headers")
                response.extend(block)
                if len(response) > 65536:
                    raise ConnectionError("Chromium DevTools handshake headers exceeded 64 KiB")
            expected = base64.b64encode(
                hashlib.sha1(f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode()).digest()
            )
            if b" 101 " not in response.split(b"\r\n", 1)[0] or expected not in response:
                raise ConnectionError(f"Chromium rejected DevTools WebSocket: {response[:500]!r}")
        except BaseException:
            self.socket.close()
            raise
        self.socket.settimeout(timeout if operation_timeout is None else operation_timeout)
        self.identifier = 0

    def close(self) -> None:
        try:
            self._send(b"", opcode=8)
        finally:
            self.socket.close()

    def _read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            block = self.socket.recv(length - len(chunks))
            if not block:
                raise ConnectionError("Chromium closed the DevTools WebSocket")
            chunks.extend(block)
        return bytes(chunks)

    def _send(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray((0x80 | opcode, 0x80 | (length if length < 126 else 126 if length <= 0xFFFF else 127)))
        if length >= 126:
            header.extend(struct.pack(">H" if length <= 0xFFFF else ">Q", length))
        header.extend(mask)
        header.extend(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header)

    def _receive(self) -> dict[str, object]:
        payload = bytearray()
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else b""
            frame = self._read_exact(length)
            if mask:
                frame = bytes(value ^ mask[index % 4] for index, value in enumerate(frame))
            if opcode == 8:
                raise ConnectionError("Chromium closed the DevTools WebSocket")
            if opcode == 9:
                self._send(frame, opcode=10)
                continue
            if opcode in {0, 1}:
                payload.extend(frame)
                if final:
                    return json.loads(payload)

    def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.identifier += 1
        identifier = self.identifier
        self._send(
            json.dumps({"id": identifier, "method": method, "params": params or {}}, separators=(",", ":")).encode()
        )
        while True:
            response = self._receive()
            if response.get("id") != identifier:
                continue
            if "error" in response:
                raise AssertionError(f"DevTools {method} failed: {response['error']}")
            return response.get("result", {})

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        if "exceptionDetails" in result:
            raise AssertionError(f"Chromium evaluation failed: {result['exceptionDetails']}")
        return result["result"].get("value")


def devtools_target(
    profile_path: Path,
    process: subprocess.Popen[bytes],
    *,
    connection_factory: Callable[..., DevToolsConnection] = DevToolsConnection,
    timeout: float = DEVTOOLS_STARTUP_ATTEMPT_TIMEOUT,
) -> str:
    port_path = profile_path / "DevToolsActivePort"
    last_error: BaseException | None = None
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if process.poll() is not None:
            raise AssertionError("Chromium exited before opening its DevTools endpoint")
        try:
            lines = port_path.read_text(encoding="utf-8").splitlines()
            if len(lines) < 2 or not all(lines[:2]):
                raise ValueError("Chromium published an incomplete DevTools port file")
            port = int(lines[0])
            browser_path = lines[1]
            if not 0 < port < 65536 or not browser_path.startswith("/"):
                raise ValueError("Chromium published an invalid DevTools endpoint")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Chromium used its discovery attempt before target inspection")
            connection_timeout = min(0.25, remaining)
            connection = connection_factory(
                f"ws://127.0.0.1:{port}{browser_path}",
                timeout=connection_timeout,
                operation_timeout=connection_timeout,
            )
            try:
                result = connection.call("Target.getTargets")
            finally:
                connection.close()
            targets = result.get("targetInfos")
            if isinstance(targets, list):
                target_id = next(
                    (
                        item.get("targetId")
                        for item in targets
                        if isinstance(item, dict) and item.get("type") == "page"
                    ),
                    None,
                )
                if isinstance(target_id, str):
                    return f"ws://127.0.0.1:{port}/devtools/page/{target_id}"
            raise ValueError("Chromium did not publish a page target")
        except (OSError, ValueError) as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.025, remaining))
    raise AssertionError("Chromium did not publish its DevTools endpoint") from last_error


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        # Do not start another browser while an unreaped predecessor can retain its profile.
        process.wait(timeout=5)


def cleanup_profile(
    profile: tempfile.TemporaryDirectory[str],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for attempt in range(40):
        try:
            profile.cleanup()
            return
        except OSError:
            if attempt == 39:
                raise
            sleep(0.025)


def acquire_chromium(
    chromium: str,
    *,
    extra_arguments: tuple[str, ...] = (),
    hide_scrollbars: bool = False,
    connection_factory: Callable[..., DevToolsConnection] = DevToolsConnection,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    target_factory: Callable[..., str] = devtools_target,
    tempdir_factory: Callable[[], tempfile.TemporaryDirectory[str]] = tempfile.TemporaryDirectory,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[tempfile.TemporaryDirectory[str], subprocess.Popen[bytes], DevToolsConnection]:
    """Acquire one responsive browser, retrying a wedged external startup once."""

    last_error: BaseException | None = None
    for attempt in range(DEVTOOLS_STARTUP_ATTEMPTS):
        profile = tempdir_factory()
        process: subprocess.Popen[bytes] | None = None
        attempt_deadline = time.monotonic() + DEVTOOLS_STARTUP_ATTEMPT_TIMEOUT
        try:
            command = [
                chromium,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-allow-origins=*",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile.name}",
            ]
            if hide_scrollbars:
                command.insert(4, "--hide-scrollbars")
            process = popen_factory(
                (*command, *extra_arguments, "about:blank"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "HOME": profile.name},
            )
            remaining = attempt_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Chromium used its startup attempt before discovery")
            target = target_factory(Path(profile.name), process, timeout=remaining)
            remaining = attempt_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Chromium used its startup attempt before opening the page target")
            return profile, process, connection_factory(
                target,
                timeout=min(1.0, remaining),
                operation_timeout=10.0,
            )
        except BaseException as error:
            try:
                if process is not None:
                    stop_process(process)
            finally:
                cleanup_profile(profile, sleep=sleep)
            if not isinstance(error, (AssertionError, ConnectionError, OSError)):
                raise
            last_error = error
            if attempt + 1 == DEVTOOLS_STARTUP_ATTEMPTS:
                raise AssertionError(
                    f"Chromium failed to start after {DEVTOOLS_STARTUP_ATTEMPTS} fresh attempts"
                ) from last_error
    raise AssertionError("Chromium startup attempts were not executed") from last_error


def browser_json_probe(
    chromium: str,
    url: str,
    width: int,
    result_selector: str,
    *,
    reduced_motion: bool = False,
    screenshot_path: Path | None = None,
    connection_factory: Callable[..., DevToolsConnection] = DevToolsConnection,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    target_factory: Callable[..., str] = devtools_target,
    tempdir_factory: Callable[[], tempfile.TemporaryDirectory[str]] = tempfile.TemporaryDirectory,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    profile: tempfile.TemporaryDirectory[str] | None = None
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    try:
        profile, process, connection = acquire_chromium(
            chromium,
            hide_scrollbars=True,
            connection_factory=connection_factory,
            popen_factory=popen_factory,
            target_factory=target_factory,
            tempdir_factory=tempdir_factory,
            sleep=sleep,
        )
        for domain in ("Runtime", "Page"):
            connection.call(f"{domain}.enable")
        connection.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        )
        if reduced_motion:
            connection.call(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
            )
        connection.call("Page.navigate", {"url": url})
        selector = json.dumps(result_selector)
        readiness = (
            f"location.href === {json.dumps(url)} && "
            "document.readyState === 'complete' && "
            f"document.querySelector({selector})?.textContent !== 'pending'"
        )
        for _ in range(200):
            if connection.evaluate(readiness):
                break
            sleep(0.025)
        else:
            raise AssertionError("Chromium did not initialize the browser probe")
        result = connection.evaluate(f"JSON.parse(document.querySelector({selector}).textContent)")
        if not isinstance(result, dict):
            raise AssertionError("Chromium returned invalid browser probe data")
        if screenshot_path is not None:
            screenshot = connection.call(
                "Page.captureScreenshot",
                {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            )
            encoded = screenshot.get("data")
            if not isinstance(encoded, str):
                raise AssertionError("Chromium did not return a browser screenshot")
            screenshot_path.write_bytes(base64.b64decode(encoded, validate=True))
        return result
    finally:
        active_error = sys.exception()
        cleanup_errors: list[BaseException] = []

        def cleanup(action: Callable[[], object]) -> None:
            try:
                action()
            except BaseException as error:
                cleanup_errors.append(error)

        if connection is not None:
            cleanup(connection.close)
        if process is not None:
            cleanup(lambda: stop_process(process))
        if profile is not None:
            cleanup(lambda: cleanup_profile(profile, sleep=sleep))
        if active_error is None and cleanup_errors:
            raise cleanup_errors[0]
