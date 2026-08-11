"""Cleanup-safe Chromium witnesses for the Phase 4K Lander contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class DevToolsConnection:
    """Minimal bounded WebSocket client for one Chromium page target."""

    def __init__(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        self.socket = socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=10)
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
        self._send(json.dumps({"id": identifier, "method": method, "params": params or {}},
                              separators=(",", ":")).encode())
        while True:
            response = self._receive()
            if response.get("id") != identifier:
                continue
            if "error" in response:
                raise AssertionError(f"DevTools {method} failed: {response['error']}")
            return response.get("result", {})

    def evaluate(self, expression: str) -> object:
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        if "exceptionDetails" in result:
            raise AssertionError(f"Chromium evaluation failed: {result['exceptionDetails']}")
        return result["result"].get("value")


KEY_DATA = {
    "Tab": ("Tab", "Tab", 9),
    "Space": (" ", "Space", 32),
    "Enter": ("Enter", "Enter", 13),
    "Escape": ("Escape", "Escape", 27),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39),
    "KeyH": ("h", "KeyH", 72),
    "KeyL": ("l", "KeyL", 76),
    "KeyR": ("r", "KeyR", 82),
}


def dispatch_key(connection: DevToolsConnection, code: str, *, down: bool = True, up: bool = True) -> None:
    key, physical_code, virtual = KEY_DATA[code]
    parameters = {"key": key, "code": physical_code, "windowsVirtualKeyCode": virtual,
                  "nativeVirtualKeyCode": virtual, "modifiers": 0}
    if down:
        connection.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", **parameters})
        if code == "Enter":
            connection.call("Input.dispatchKeyEvent", {
                "type": "char", "text": "\r", "unmodifiedText": "\r", **parameters,
            })
    if up:
        connection.call("Input.dispatchKeyEvent", {"type": "keyUp", **parameters})


def _probe_source() -> str:
    return r'''
import { landerGameController as controller } from "/static/lander-game.js";
import { createRun, createSimulationClock } from "/static/lander-model.js";

const shell = document.querySelector("#lander-scene-shell");
const stage = document.querySelector("#lander-scene-stage");
const restart = document.querySelector("#lander-restart");
const exit = document.querySelector("#lander-exit");
const targets = {
    header: document.querySelector(".service-links a span"),
    breadcrumb: document.querySelector(".breadcrumb-separator"),
};
for (const [name, target] of Object.entries(targets)) {
    target.id = `phase4k-${name}-target`;
    target.tabIndex = 0;
}
const clicks = {restart: 0, exit: 0};
const keyEvents = [];
restart.addEventListener("click", () => { clicks.restart += 1; });
exit.addEventListener("click", () => { clicks.exit += 1; });
for (const type of ["keydown", "keyup"]) {
    document.addEventListener(type, (event) => {
        keyEvents.push({type, key: event.key, defaultPrevented: event.defaultPrevented});
    });
}

const pause = () => {
    controller.paused = true;
    if (controller.frameId !== null) cancelAnimationFrame(controller.frameId);
    controller.frameId = null;
};

const reset = (state = "flying") => {
    if (controller.model.state !== "preflight") controller.exit();
    const now = performance.now();
    controller.start(false, now);
    pause();
    controller.clearAllInput(now);
    controller.model = {...createRun({seed: 1}), state, launchStarted: false};
    controller.clock = createSimulationClock(now);
    controller.previousFrame = now;
    controller.render();
    return now;
};

const snapshot = () => ({
    state: controller.model.state,
    launchStarted: controller.model.launchStarted,
    pose: structuredClone(controller.model.pose),
    fuel: controller.model.fuel,
    commanded: structuredClone(controller.model.commanded),
    held: [...controller.heldKeys].sort(),
    queue: structuredClone(controller.clock.queue),
    pulse: structuredClone(controller.collectivePulse),
    focus: document.activeElement?.id ?? "",
    clicks: structuredClone(clicks),
});

window.phase4k = {
    controller, shell, stage, restart, exit, targets, clicks, keyEvents, pause, reset, snapshot,
    clearEvents() { keyEvents.length = 0; },
    focusTarget(name) { targets[name].focus(); return document.activeElement.id; },
    prepareReady() {
        reset("launching");
        stage.scrollIntoView({block: "center", inline: "nearest"});
        const beforeFuel = controller.model.fuel;
        const rect = stage.getBoundingClientRect();
        controller.paused = false;
        controller.root.dataset.paused = "false";
        controller.requestFrame();
        return {beforeFuel, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
    },
    actionCopy() {
        const copy = (button) => ({label: button.firstElementChild.textContent.trim(),
            hint: button.lastElementChild.textContent.trim()});
        return {restart: copy(restart), exit: copy(exit)};
    },
    accessibilitySetup() {
        reset("failed");
        const label = document.querySelector("#lander-fuel-label");
        const value = document.querySelector("#lander-fuel-value");
        const status = document.querySelector("#lander-status");
        return {ids: shell.getAttribute("aria-describedby").split(" "), label: label.textContent.trim(),
            value: value.textContent.trim(), status: status.textContent.trim(), actionCopy: this.actionCopy()};
    },
    refreshFuel() {
        const label = document.querySelector("#lander-fuel-label");
        const value = document.querySelector("#lander-fuel-value");
        const status = document.querySelector("#lander-status");
        const before = {label: label.textContent.trim(), value: value.textContent.trim(),
            status: status.textContent.trim()};
        controller.model = {...controller.model, fuel: controller.model.fuel - 1};
        controller.render();
        return {before, after: {label: label.textContent.trim(), value: value.textContent.trim(),
            status: status.textContent.trim()}};
    },
};
document.documentElement.dataset.phase4kReady = "true";
'''


def normalize_accessible(value: str) -> str:
    return " ".join(value.split())


def _application_node(tree: dict[str, object]) -> dict[str, object]:
    return next(node for node in tree["nodes"] if node.get("role", {}).get("value") == "application")


def _button_names(tree: dict[str, object]) -> list[str]:
    return [str(node.get("name", {}).get("value", "")) for node in tree["nodes"]
            if node.get("role", {}).get("value") == "button"]


def _wait_for_launch(connection: DevToolsConnection, authority: str) -> None:
    for _ in range(200):
        if connection.evaluate("phase4k.controller.model.launchStarted"):
            return
        time.sleep(0.005)
    state = connection.evaluate("phase4k.snapshot()")
    raise AssertionError(f"{authority} browser input did not depart from launch-ready: {state}")


def _mouse(connection: DevToolsConnection, kind: str, x: float, y: float) -> None:
    parameters: dict[str, object] = {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
    if kind == "mousePressed":
        parameters["buttons"] = 1
    elif kind == "mouseReleased":
        parameters["buttons"] = 0
    connection.call("Input.dispatchMouseEvent", parameters)


def _touch(connection: DevToolsConnection, kind: str, x: float, y: float) -> None:
    points = [] if kind == "touchEnd" else [{"x": x, "y": y, "id": 1}]
    connection.call("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})


def _departure_witness(connection: DevToolsConnection, authority: str) -> dict[str, object]:
    setup = connection.evaluate("phase4k.prepareReady()")
    x, y = setup["x"], setup["y"]
    if authority.startswith("keyboard"):
        dispatch_key(connection, "Space", up=False)
        if authority.endswith("tap"):
            time.sleep(0.03)
            dispatch_key(connection, "Space", down=False)
        _wait_for_launch(connection, authority)
        if authority.endswith("hold"):
            dispatch_key(connection, "Space", down=False)
    elif authority.startswith("vi"):
        dispatch_key(connection, "KeyH", up=False)
        dispatch_key(connection, "ArrowUp", up=False)
        if authority.endswith("tap"):
            time.sleep(0.03)
            dispatch_key(connection, "ArrowUp", down=False)
            dispatch_key(connection, "KeyH", down=False)
        _wait_for_launch(connection, authority)
        if authority.endswith("hold"):
            dispatch_key(connection, "ArrowUp", down=False)
            dispatch_key(connection, "KeyH", down=False)
    elif authority.startswith("mouse"):
        _mouse(connection, "mousePressed", x, y)
        if authority.endswith("tap"):
            _mouse(connection, "mouseReleased", x, y)
        _wait_for_launch(connection, authority)
        if authority.endswith("hold"):
            _mouse(connection, "mouseReleased", x, y)
    else:
        connection.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
        _touch(connection, "touchStart", x, y)
        if authority.endswith("tap"):
            _touch(connection, "touchEnd", x, y)
        _wait_for_launch(connection, authority)
        if authority.endswith("hold"):
            _touch(connection, "touchEnd", x, y)
    state = connection.evaluate(
        "phase4k.pause(); ({launchStarted:phase4k.controller.model.launchStarted,"
        "fuel:phase4k.controller.model.fuel,state:phase4k.controller.model.state})"
    )
    return {"authority": authority, "launchStarted": state["launchStarted"],
            "fuelSpent": state["fuel"] < setup["beforeFuel"], "state": state["state"]}


def _devtools_target(profile_path: Path, process: subprocess.Popen[bytes]) -> str:
    port_path = profile_path / "DevToolsActivePort"
    for _ in range(200):
        if port_path.exists():
            break
        if process.poll() is not None:
            raise AssertionError("Chromium exited before opening its DevTools endpoint")
        time.sleep(0.025)
    else:
        raise AssertionError("Chromium did not publish its DevTools endpoint")
    port = int(port_path.read_text(encoding="utf-8").splitlines()[0])
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        targets = json.load(response)
    return next(item["webSocketDebuggerUrl"] for item in targets if item["type"] == "page")


def browser_phase4k_contract(
    output: Path,
    *,
    connection_factory: Callable[[str], DevToolsConnection] = DevToolsConnection,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer,
    tempdir_factory: Callable[[], tempfile.TemporaryDirectory[str]] = tempfile.TemporaryDirectory,
    target_factory: Callable[[Path, subprocess.Popen[bytes]], str] = _devtools_target,
    chromium_path: str | None = None,
) -> dict[str, object]:
    chromium = chromium_path or next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser")
         if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Phase 4K browser contracts")
    page = output / "lander/index.html"
    probe_path = output / "phase4k-browser.js"
    source: str | None = None
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    profile: tempfile.TemporaryDirectory[str] | None = None
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    thread_started = False
    try:
        source = page.read_text(encoding="utf-8")
        probe_path.write_text(_probe_source(), encoding="utf-8")
        page.write_text(
            source.replace("</body>", '<script type="module" src="/phase4k-browser.js"></script></body>', 1),
            encoding="utf-8",
        )
        server = server_factory(("127.0.0.1", 0), partial(_QuietHandler, directory=str(output)))
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="phase4k-browser-server")
        thread.start()
        thread_started = True
        profile = tempdir_factory()
        process = popen_factory((
            chromium, "--headless", "--disable-gpu", "--no-sandbox", "--no-first-run",
            "--no-default-browser-check", "--remote-allow-origins=*", "--remote-debugging-port=0",
            f"--user-data-dir={profile.name}", "about:blank",
        ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = target_factory(Path(profile.name), process)
        connection = connection_factory(target)
        for domain in ("Runtime", "Page", "Accessibility"):
            connection.call(f"{domain}.enable")
        connection.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}/lander/"})
        for _ in range(200):
            if connection.evaluate("document.documentElement.dataset.phase4kReady === 'true'"):
                break
            time.sleep(0.025)
        else:
            raise AssertionError("Phase 4K browser probe did not initialize")

        result: dict[str, object] = {}
        connection.evaluate("phase4k.reset('flying'); phase4k.shell.focus()")
        dispatch_key(connection, "Tab")
        result["activeTabOrder"] = [connection.evaluate("document.activeElement.id")]
        connection.evaluate("phase4k.reset('failed'); phase4k.shell.focus()")
        dispatch_key(connection, "Tab")
        first_failed = connection.evaluate("document.activeElement.id")
        dispatch_key(connection, "Tab")
        result["failedTabOrder"] = [first_failed, connection.evaluate("document.activeElement.id")]

        activations = []
        for action, state in (("exit", "flying"), ("restart", "failed")):
            for key in ("Space", "Enter"):
                connection.evaluate(
                    f"phase4k.reset('{state}'); phase4k.clicks.{action}=0; phase4k.{action}.focus()"
                )
                dispatch_key(connection, key)
                activations.append(connection.evaluate(
                    f"({{action:'{action}',key:'{key}',clicks:phase4k.clicks.{action},"
                    "state:phase4k.controller.model.state,held:[...phase4k.controller.heldKeys],"
                    "thrustEdges:phase4k.controller.clock.queue.some((edge)=>edge.left+edge.right>0),"
                    "commanded:structuredClone(phase4k.controller.model.commanded)})"
                ))
        result["activations"] = activations

        passive_actions = []
        for action, state in (("exit", "flying"), ("restart", "failed")):
            for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "KeyH", "KeyL"):
                connection.evaluate(
                    f"phase4k.reset('{state}'); phase4k.clicks.{action}=0; phase4k.{action}.focus()"
                )
                before = connection.evaluate("phase4k.snapshot()")
                dispatch_key(connection, key)
                passive_actions.append({"action": action, "key": key, "before": before,
                                        "after": connection.evaluate("phase4k.snapshot()")})
        result["passiveActions"] = passive_actions

        outside = []
        outside_keys = ("Escape", "KeyR", "Space", "ArrowUp", "ArrowDown", "ArrowLeft",
                        "ArrowRight", "KeyH", "KeyL")
        for state in ("flying", "launching", "failed"):
            for target_name in ("header", "breadcrumb"):
                for key in outside_keys:
                    connection.evaluate(
                        f"phase4k.reset('{state}'); phase4k.focusTarget('{target_name}'); phase4k.clearEvents()"
                    )
                    before = connection.evaluate("phase4k.snapshot()")
                    dispatch_key(connection, key)
                    outside.append({"state": state, "target": target_name, "key": key, "before": before,
                                    "after": connection.evaluate("phase4k.snapshot()"),
                                    "events": connection.evaluate("structuredClone(phase4k.keyEvents)")})
        result["outside"] = outside

        result["departures"] = [_departure_witness(connection, authority) for authority in (
            "keyboard-hold", "keyboard-tap", "vi-hold", "vi-tap",
            "mouse-hold", "mouse-tap", "touch-hold", "touch-tap",
        )]

        accessibility = connection.evaluate("phase4k.accessibilitySetup()")
        first_tree = connection.call("Accessibility.getFullAXTree")
        refresh = connection.evaluate("phase4k.refreshFuel()")
        second_tree = connection.call("Accessibility.getFullAXTree")
        result["accessibility"] = {
            "dom": accessibility,
            "refresh": refresh,
            "beforeDescription": _application_node(first_tree).get("description", {}).get("value", ""),
            "afterDescription": _application_node(second_tree).get("description", {}).get("value", ""),
            "buttonNames": _button_names(second_tree),
        }
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
        if process is not None and process.poll() is None:
            cleanup(process.terminate)
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cleanup(process.kill)
                    cleanup(lambda: process.wait(timeout=5))
        if server is not None:
            if thread_started or (thread is not None and thread.is_alive()):
                cleanup(server.shutdown)
            cleanup(server.server_close)
        if thread is not None and (thread_started or thread.ident is not None):
            cleanup(lambda: thread.join(timeout=5))
        if profile is not None:
            cleanup(profile.cleanup)
        if source is not None:
            cleanup(lambda: page.write_text(source, encoding="utf-8"))
        cleanup(lambda: probe_path.unlink(missing_ok=True))
        if active_error is None and cleanup_errors:
            raise cleanup_errors[0]
