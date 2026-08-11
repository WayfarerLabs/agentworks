# ruff: noqa: F405

import base64
import hashlib
import html
import json
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import zlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from site_test_support import *  # noqa: F403


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class DevToolsConnection:
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
        self.socket.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(self.socket.recv(4096))
        expected = base64.b64encode(hashlib.sha1(f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode()).digest())
        if b" 101 " not in response.split(b"\r\n", 1)[0] or expected not in response:
            self.socket.close()
            raise AssertionError(f"Chromium rejected DevTools WebSocket: {response[:500]!r}")
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
        message = {"id": identifier, "method": method, "params": params or {}}
        self._send(json.dumps(message, separators=(",", ":")).encode())
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


def png_pixel(path: Path, x: int, y: int) -> tuple[int, int, int]:
    data = path.read_bytes()
    offset = 8
    chunks: list[bytes] = []
    image_width = image_height = color_type = 0
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += length + 12
        if kind == b"IHDR":
            image_width, image_height, depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", payload)
            if depth != 8 or color_type not in {2, 6} or interlace:
                raise AssertionError("Chromium screenshot used an unsupported PNG encoding")
        elif kind == b"IDAT":
            chunks.append(payload)
    channels = 3 if color_type == 2 else 4
    packed = zlib.decompress(b"".join(chunks))
    stride = image_width * channels
    previous = bytearray(stride)
    rows: list[bytearray] = []
    cursor = 0
    for _ in range(image_height):
        filter_type = packed[cursor]
        cursor += 1
        source = packed[cursor : cursor + stride]
        cursor += stride
        row = bytearray(stride)
        for index, value in enumerate(source):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            else:
                raise AssertionError(f"unsupported PNG filter {filter_type}")
            row[index] = (value + predictor) & 0xFF
        rows.append(row)
        previous = row
    start = x * channels
    return tuple(rows[y][start : start + 3])


def browser_arcade_contract(output: Path, width: int, screenshot: bool = False) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser")
         if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Lander arcade browser contracts")
    page = output / "lander/index.html"
    source = page.read_text(encoding="utf-8")
    probe_script = """
const game = document.querySelector("#lander-game");
const stage = document.querySelector("#lander-scene-stage");
const rail = document.querySelector("#lander-controls-rail");
const controls = document.querySelector("#lander-controls");
const fuel = document.querySelector("#lander-fuel");
const fuelLabel = document.querySelector("#lander-fuel-label");
const fuelValue = document.querySelector("#lander-fuel-value");
const gauge = document.querySelector("#lander-fuel-gauge");
const outcome = document.querySelector("#lander-outcome");
const status = document.querySelector("#lander-status");
const restart = document.querySelector("#lander-restart");
fuel.hidden = false;
rail.hidden = false;
outcome.hidden = false;
restart.hidden = false;
restart.disabled = false;
status.textContent = "x";
game.dataset.banner = "crashed";
game.dataset.refueling = new URLSearchParams(location.search).get("refuel") !== "off" ? "true" : "false";
game.dataset.fuelLevel = "danger";
game.style.setProperty("--fuel-level-color", "#ff5a36");
game.style.setProperty("--fuel-gauge-level", "0.25");
game.style.setProperty("--fuel-transfer-x", "120px");
game.style.setProperty("--fuel-transfer-y", "120px");
const rect = (element) => {
    const value = element.getBoundingClientRect();
    return {top: value.top, right: value.right, bottom: value.bottom, left: value.left,
        width: value.width, height: value.height};
};
const pseudo = getComputedStyle(stage, "::after");
const actions = [restart, document.querySelector("#lander-exit")].filter((button) => !button.hidden);
const result = {
    stage: rect(stage), rail: rect(rail), controls: rect(controls), gauge: rect(gauge), outcome: rect(outcome),
    status: rect(status), actions: actions.map((button) => ({id: button.id, rect: rect(button)})),
    pseudo: {width: pseudo.width, height: pseudo.height, pointerEvents: pseudo.pointerEvents,
        imageRendering: pseudo.imageRendering, backgroundColor: pseudo.backgroundColor,
        backgroundImage: pseudo.backgroundImage, backgroundSize: pseudo.backgroundSize,
        backgroundPosition: pseudo.backgroundPosition, backgroundRepeat: pseudo.backgroundRepeat,
        transform: pseudo.transform},
    fontFamily: getComputedStyle(status).fontFamily,
    hiddenFuel: [fuelLabel, fuelValue].map((element) => ({rect: rect(element),
        clip: getComputedStyle(element).clip, display: getComputedStyle(element).display,
        visibility: getComputedStyle(element).visibility})),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    resources: performance.getEntriesByType("resource").map((entry) => ({name: entry.name,
        initiatorType: entry.initiatorType})),
};
game.dataset.missionState = "flying";
result.touch = {stage: getComputedStyle(stage).touchAction,
    shell: getComputedStyle(document.querySelector("#lander-scene-shell")).touchAction,
    controls: getComputedStyle(controls).touchAction};
document.querySelector("#phase4j-result").textContent = JSON.stringify(result);
"""
    probe = '<pre id="phase4j-result">pending</pre><script src="/phase4j.js"></script>'
    probe_path = output / "phase4j.js"
    probe_path.write_text(probe_script, encoding="utf-8")
    stable_source = re.sub(r'<script type="module" src="[^"]*/lander-game\.js"></script>', "", source)
    page.write_text(stable_source.replace("</body>", f"{probe}</body>", 1), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.TemporaryDirectory()
    screenshot_paths: list[Path] = []
    try:
        completed = subprocess.run(
            (
                chromium,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--user-data-dir={profile.name}",
                f"--window-size={width},1000",
                "--virtual-time-budget=1000",
                "--dump-dom",
                f"http://127.0.0.1:{server.server_address[1]}/lander/",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "HOME": profile.name},
        )
        if screenshot:
            for state in ("on", "off"):
                screenshot_path = Path(profile.name) / f"can-{state}.png"
                subprocess.run(
                    (
                        chromium,
                        "--headless",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--hide-scrollbars",
                        f"--user-data-dir={Path(profile.name) / f'profile-{state}'}",
                        f"--window-size={width},1000",
                        "--virtual-time-budget=1000",
                        f"--screenshot={screenshot_path}",
                        f"http://127.0.0.1:{server.server_address[1]}/lander/?refuel={state}",
                    ),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    env={**os.environ, "HOME": profile.name},
                )
                screenshot_paths.append(screenshot_path)
            match = re.search(r'<pre id="phase4j-result">([^<]+)</pre>', completed.stdout)
            if match is None:
                raise AssertionError("Chromium omitted Phase 4J screenshot geometry")
            geometry = json.loads(html.unescape(match.group(1)))
            center_x = round(geometry["stage"]["left"] + 120)
            center_y = round(geometry["stage"]["top"] + 120)
            probes = ((5, 1), (7, 3), (18, 9), (16, 11), (1, 5), (3, 7), (0, 0), (19, 21))
            geometry["canPixels"] = [png_pixel(screenshot_paths[0], center_x - 10 + x, center_y - 11 + y)
                                     for x, y in probes]
            geometry["offPixels"] = [png_pixel(screenshot_paths[1], center_x - 10 + x, center_y - 11 + y)
                                     for x, y in probes]
    finally:
        page.write_text(source, encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        profile.cleanup()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    match = re.search(r'<pre id="phase4j-result">([^<]+)</pre>', completed.stdout)
    if match is None or match.group(1) == "pending":
        raise AssertionError(f"Chromium did not return Phase 4J geometry: {completed.stderr[-1000:]}")
    result = json.loads(html.unescape(match.group(1)))
    if screenshot:
        result.update({key: geometry[key] for key in ("canPixels", "offPixels")})
    return result


def browser_description_contract(output: Path) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser")
         if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Lander description contracts")
    page = output / "lander/index.html"
    source = page.read_text(encoding="utf-8")
    probe_script = """
import { landerGameController as controller } from "/static/lander-game.js";
const shell = document.querySelector("#lander-scene-shell");
const direction = document.querySelector("#lander-target-direction");
const resultElement = document.querySelector("#phase4j-description-result");
controller.start(false, performance.now());
const targetId = controller.model.targetSiteId;
const cameraLeft = Math.max(0, controller.model.pose.x - 35);
const withTargetLeft = (platformLeft) => {
    controller.model = {...controller.model, retainedSites: controller.model.retainedSites.map((site) =>
        site.id === targetId ? {...site, platformLeft} : site)};
    controller.render();
};
const snapshot = () => ({
    ids: shell.getAttribute("aria-describedby").split(" "),
    resolved: shell.ariaDescribedByElements.map((element) => element.id),
    directionHidden: direction.hidden,
});
withTargetLeft(cameraLeft + 100);
const boundary = snapshot();
withTargetLeft(cameraLeft + 100 + 1e-9);
const offscreen = snapshot();
resultElement.textContent = JSON.stringify({boundary, offscreen});
controller.destroy();
"""
    probe = ('<pre id="phase4j-description-result">pending</pre>'
             '<script type="module" src="/phase4j-description.js"></script>')
    probe_path = output / "phase4j-description.js"
    probe_path.write_text(probe_script, encoding="utf-8")
    page.write_text(source.replace("</body>", f"{probe}</body>", 1), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.TemporaryDirectory()
    try:
        completed = subprocess.run(
            (
                chromium,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--user-data-dir={profile.name}",
                "--window-size=960,1000",
                "--virtual-time-budget=1000",
                "--dump-dom",
                f"http://127.0.0.1:{server.server_address[1]}/lander/",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "HOME": profile.name},
        )
    finally:
        page.write_text(source, encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        profile.cleanup()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    match = re.search(r'<pre id="phase4j-description-result">([^<]+)</pre>', completed.stdout)
    if match is None or match.group(1) == "pending":
        raise AssertionError(f"Chromium did not return Phase 4J descriptions: {completed.stderr[-1000:]}")
    return json.loads(html.unescape(match.group(1)))


def _phase4k_probe_source() -> str:
    return r'''
import { landerGameController as controller } from "/static/lander-game.js";
import { createRun, createSimulationClock } from "/static/lander-model.js";

const shell = document.querySelector("#lander-scene-shell");
const stage = document.querySelector("#lander-scene-stage");
const restart = document.querySelector("#lander-restart");
const exit = document.querySelector("#lander-exit");
const outside = document.querySelector("header a");
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
    controller.model = {...createRun({seed: 1}), state,
        launchStarted: state === "launching" ? false : controller.model.launchStarted};
    controller.clock = createSimulationClock(now);
    controller.previousFrame = now;
    controller.render();
    return now;
};

const snapshot = () => ({
    state: controller.model.state,
    pose: structuredClone(controller.model.pose),
    fuel: controller.model.fuel,
    held: [...controller.heldKeys].sort(),
    queue: structuredClone(controller.clock.queue),
    pulse: structuredClone(controller.collectivePulse),
    focus: document.activeElement?.id ?? "",
    clicks: structuredClone(clicks),
});

const keyboardEvent = (target, timestamp, key, code) => ({
    target, key, code, timeStamp: timestamp, repeat: false,
    ctrlKey: false, altKey: false, metaKey: false, shiftKey: false,
    composedPath: () => [target, shell, document, window],
    preventDefault() {},
});

const pointerEvent = (timestamp, pointerType) => ({
    type: "pointerdown", target: stage, pointerId: pointerType === "touch" ? 12 : 11,
    pointerType, isPrimary: true, button: 0, clientX: 500, clientY: 320,
    timeStamp: timestamp, composedPath: () => [stage, shell, document, window],
    preventDefault() {},
});

const departure = (authority) => {
    const now = reset("launching");
    controller.model = {...controller.model, status: "sentinel", launchStarted: false};
    controller.clock = createSimulationClock(now);
    controller.previousFrame = now;
    const beforeFuel = controller.model.fuel;
    if (authority === "space") {
        controller.onKeyDown(keyboardEvent(shell, now, " ", "Space"));
    } else if (authority === "up-vi") {
        controller.onKeyDown(keyboardEvent(shell, now, "ArrowUp", "ArrowUp"));
        controller.onKeyDown(keyboardEvent(shell, now, "h", "KeyH"));
    } else {
        const originalCapture = stage.setPointerCapture;
        Object.defineProperty(stage, "setPointerCapture", {configurable: true, value() {}});
        try { controller.onPointer(pointerEvent(now, authority)); }
        finally { Object.defineProperty(stage, "setPointerCapture", {configurable: true, value: originalCapture}); }
    }
    controller.frame(now + 1000 / 120);
    return {authority, launchStarted: controller.model.launchStarted,
        state: controller.model.state, statusCleared: controller.model.status === "",
        fuelSpent: controller.model.fuel < beforeFuel};
};

window.phase4k = {
    controller, shell, restart, exit, outside, clicks, keyEvents, pause, reset, snapshot, departure,
    focus(id) { document.querySelector(`#${id}`).focus(); return document.activeElement.id; },
    focusOutside() { outside.focus(); return document.activeElement === outside; },
    clearEvents() { keyEvents.length = 0; },
    actionCopy() {
        const copy = (button) => ({label: button.firstElementChild.textContent.trim(),
            hint: button.lastElementChild.textContent.trim()});
        return {restart: copy(restart), exit: copy(exit)};
    },
    accessibilitySetup() {
        reset("failed");
        const label = document.querySelector("#lander-fuel-label");
        const value = document.querySelector("#lander-fuel-value");
        return {ids: shell.getAttribute("aria-describedby").split(" "), label: label.textContent.trim(),
            value: value.textContent.trim(), actionCopy: this.actionCopy()};
    },
    refreshFuel() {
        const value = document.querySelector("#lander-fuel-value");
        const before = value.textContent.trim();
        controller.model = {...controller.model, fuel: controller.model.fuel - 1};
        controller.render();
        return {before, value: value.textContent.trim(), label:
            document.querySelector("#lander-fuel-label").textContent.trim()};
    },
};
document.documentElement.dataset.phase4kReady = "true";
'''


def _normalize_accessible(value: str) -> str:
    return " ".join(value.split())


def _application_node(tree: dict[str, object]) -> dict[str, object]:
    nodes = tree["nodes"]
    return next(node for node in nodes if node.get("role", {}).get("value") == "application")


def _button_names(tree: dict[str, object]) -> list[str]:
    return [str(node.get("name", {}).get("value", "")) for node in tree["nodes"]
            if node.get("role", {}).get("value") == "button"]


def browser_phase4k_contract(output: Path) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser")
         if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Phase 4K browser contracts")
    page = output / "lander/index.html"
    source = page.read_text(encoding="utf-8")
    probe_path = output / "phase4k-browser.js"
    probe_path.write_text(_phase4k_probe_source(), encoding="utf-8")
    page.write_text(source.replace("</body>", '<script type="module" src="/phase4k-browser.js"></script></body>', 1),
                    encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.TemporaryDirectory()
    process = subprocess.Popen((
        chromium,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile.name}",
        "about:blank",
    ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "HOME": profile.name})
    connection = None
    try:
        port_path = Path(profile.name) / "DevToolsActivePort"
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
        target = next(item for item in targets if item["type"] == "page")
        connection = DevToolsConnection(target["webSocketDebuggerUrl"])
        connection.call("Runtime.enable")
        connection.call("Page.enable")
        connection.call("Accessibility.enable")
        url = f"http://127.0.0.1:{server.server_address[1]}/lander/"
        connection.call("Page.navigate", {"url": url})
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
                    f"phase4k.reset('{state}'); phase4k.clicks.{action} = 0; phase4k.{action}.focus()"
                )
                dispatch_key(connection, key)
                activations.append(connection.evaluate(
                    f"({{action:'{action}',key:'{key}',clicks:phase4k.clicks.{action},"
                    "state:phase4k.controller.model.state,held:[...phase4k.controller.heldKeys],"
                    "thrustEdges:phase4k.controller.clock.queue.some((edge) => edge.left + edge.right > 0),"
                    "commanded:structuredClone(phase4k.controller.model.commanded)})"
                ))
        result["activations"] = activations

        passive_actions = []
        for action, state in (("exit", "flying"), ("restart", "failed")):
            for key in ("ArrowUp", "ArrowLeft", "ArrowRight", "KeyH", "KeyL"):
                connection.evaluate(
                    f"phase4k.reset('{state}'); phase4k.clicks.{action} = 0; phase4k.{action}.focus()"
                )
                before = connection.evaluate("phase4k.snapshot()")
                dispatch_key(connection, key)
                after = connection.evaluate("phase4k.snapshot()")
                passive_actions.append({"action": action, "key": key, "before": before, "after": after})
        result["passiveActions"] = passive_actions

        connection.evaluate("phase4k.reset('flying'); phase4k.focusOutside(); phase4k.clearEvents()")
        outside_before = connection.evaluate("phase4k.snapshot()")
        for key in ("Escape", "KeyR", "Space", "ArrowUp", "ArrowLeft", "KeyH", "KeyL"):
            dispatch_key(connection, key)
        result["outside"] = {"before": outside_before, "after": connection.evaluate("phase4k.snapshot()"),
                             "events": connection.evaluate("structuredClone(phase4k.keyEvents)")}

        connection.evaluate("phase4k.reset('flying'); phase4k.shell.focus(); phase4k.clearEvents()")
        dispatch_key(connection, "Space", up=False)
        connection.evaluate("phase4k.focusOutside()")
        transition_before = connection.evaluate("phase4k.snapshot()")
        connection.evaluate("phase4k.clearEvents()")
        dispatch_key(connection, "Space", down=False)
        result["outsideRelease"] = {"before": transition_before, "after": connection.evaluate("phase4k.snapshot()"),
                                    "events": connection.evaluate("structuredClone(phase4k.keyEvents)")}

        result["departures"] = connection.evaluate(
            "['space','up-vi','mouse','touch'].map((authority) => phase4k.departure(authority))"
        )

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
        if connection is not None:
            try:
                connection.close()
            except (ConnectionError, OSError):
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        page.write_text(source, encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        profile.cleanup()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class ArcadeBrowserTests(RepositoryFixture):
    def test_phase4k_native_input_focus_departure_and_accessibility_contracts(self) -> None:
        result = browser_phase4k_contract(self.build())
        self.assertEqual(result["activeTabOrder"], ["lander-exit"])
        self.assertEqual(result["failedTabOrder"], ["lander-restart", "lander-exit"])
        for activation in result["activations"]:
            with self.subTest(action=activation["action"], key=activation["key"]):
                self.assertEqual(activation["clicks"], 1)
                self.assertEqual(activation["held"], [])
                self.assertFalse(activation["thrustEdges"])
                self.assertEqual(activation["commanded"], {"left": 0, "right": 0, "vectorAngle": 0})
                self.assertNotEqual(activation["state"],
                                    "flying" if activation["action"] == "exit" else "failed")
        for witness in result["passiveActions"]:
            with self.subTest(action=witness["action"], key=witness["key"]):
                self.assertEqual(witness["after"], witness["before"])

        outside = result["outside"]
        self.assertEqual(outside["after"], outside["before"])
        self.assertEqual(len(outside["events"]), 14)
        self.assertTrue(all(not event["defaultPrevented"] for event in outside["events"]))
        outside_release = result["outsideRelease"]
        self.assertEqual(outside_release["before"]["held"], [])
        self.assertEqual(outside_release["after"], outside_release["before"])
        self.assertEqual(outside_release["events"], [{"type": "keyup", "key": " ", "defaultPrevented": False}])

        self.assertEqual([witness["authority"] for witness in result["departures"]],
                         ["space", "up-vi", "mouse", "touch"])
        for witness in result["departures"]:
            self.assertTrue(witness["launchStarted"], witness["authority"])
            self.assertTrue(witness["statusCleared"], witness["authority"])
            self.assertTrue(witness["fuelSpent"], witness["authority"])

        accessibility = result["accessibility"]
        dom = accessibility["dom"]
        refresh = accessibility["refresh"]
        self.assertEqual(dom["ids"].index("lander-fuel-value"), dom["ids"].index("lander-fuel-label") + 1)
        before_segment = _normalize_accessible(f'{dom["label"]} {dom["value"]}')
        after_segment = _normalize_accessible(f'{refresh["label"]} {refresh["value"]}')
        before_description = _normalize_accessible(accessibility["beforeDescription"])
        after_description = _normalize_accessible(accessibility["afterDescription"])
        self.assertNotEqual(refresh["before"], refresh["value"])
        self.assertEqual(before_description.count(before_segment), 1)
        self.assertEqual(after_description.count(after_segment), 1)
        self.assertNotIn(before_segment, after_description)
        for action in ("restart", "exit"):
            copy = dom["actionCopy"][action]
            self.assertIn(copy["label"], accessibility["buttonNames"])
            self.assertNotIn(f'{copy["label"]} {copy["hint"]}', accessibility["buttonNames"])

    def test_active_description_resolves_only_visible_conditional_relationships(self) -> None:
        result = browser_description_contract(self.build())
        permanent = ["lander-scene-description", "lander-controls", "lander-fuel-label",
                     "lander-fuel-value", "lander-status"]
        self.assertEqual(result["boundary"], {
            "ids": permanent,
            "resolved": permanent,
            "directionHidden": True,
        })
        offscreen = [*permanent[:-1], "lander-target-direction", permanent[-1]]
        self.assertEqual(result["offscreen"], {
            "ids": offscreen,
            "resolved": offscreen,
            "directionHidden": False,
        })

    def test_narrow_and_full_width_chrome_stays_inside_the_stage_above_the_rail(self) -> None:
        output = self.build()
        for width in (320, 960):
            with self.subTest(width=width):
                result = browser_arcade_contract(output, width)
                stage = result["stage"]
                rail = result["rail"]
                gauge = result["gauge"]
                outcome = result["outcome"]
                self.assertLessEqual(stage["bottom"], rail["top"])
                self.assertGreaterEqual(gauge["left"], stage["left"])
                self.assertLessEqual(gauge["right"], stage["right"])
                self.assertGreaterEqual(outcome["left"], stage["left"])
                self.assertLessEqual(outcome["right"], stage["right"])
                self.assertTrue(gauge["right"] <= outcome["left"] or outcome["right"] <= gauge["left"])
                self.assertEqual([action["id"] for action in result["actions"]],
                                 ["lander-restart", "lander-exit"])
                exit_rect = result["actions"][1]["rect"]
                self.assertLessEqual(result["controls"]["right"], exit_rect["left"])
                for action in result["actions"]:
                    self.assertGreaterEqual(action["rect"]["width"], 44)
                    self.assertGreaterEqual(action["rect"]["height"], 44)
                self.assertLessEqual(result["status"]["bottom"], result["actions"][0]["rect"]["top"])
                self.assertEqual(result["overflow"], 0)

    def test_computed_pseudo_can_font_touch_scope_and_requests_match_the_contract(self) -> None:
        result = browser_arcade_contract(self.build(), 960, screenshot=True)
        pseudo = result["pseudo"]
        self.assertEqual(pseudo["width"], "20px")
        self.assertEqual(pseudo["height"], "22px")
        self.assertEqual(pseudo["pointerEvents"], "none")
        self.assertEqual(pseudo["imageRendering"], "pixelated")
        self.assertEqual(pseudo["backgroundColor"], "rgba(0, 0, 0, 0)")
        self.assertEqual(len(pseudo["backgroundImage"].split("linear-gradient")) - 1, 6)
        self.assertEqual(pseudo["backgroundSize"],
                         "6px 2px, 10px 6px, 2px 4px, 4px 8px, 12px 14px, 16px 18px")
        self.assertEqual(pseudo["backgroundPosition"],
                         "6px 2px, 4px 0px, 16px 10px, 16px 8px, 2px 6px, 0px 4px")
        self.assertEqual(pseudo["backgroundRepeat"],
                         "no-repeat, no-repeat, no-repeat, no-repeat, no-repeat, no-repeat")
        graphite = (41, 43, 48)
        orange = (217, 74, 30)
        self.assertEqual(result["canPixels"][:6], [graphite, orange, graphite, orange, graphite, orange])
        self.assertEqual(result["canPixels"][6:], result["offPixels"][6:])
        self.assertIn("Cascadia Mono", result["fontFamily"])
        self.assertEqual(result["touch"], {"stage": "none", "shell": "auto", "controls": "auto"})
        for hidden in result["hiddenFuel"]:
            self.assertEqual(hidden["rect"]["width"], 1)
            self.assertEqual(hidden["rect"]["height"], 1)
            self.assertEqual(hidden["clip"], "rect(0px, 0px, 0px, 0px)")
            self.assertNotEqual(hidden["display"], "none")
            self.assertEqual(hidden["visibility"], "visible")
        self.assertTrue(all(resource["initiatorType"] != "font" for resource in result["resources"]))
        self.assertTrue(all(resource["name"].startswith("http://127.0.0.1:") for resource in result["resources"]))


if __name__ == "__main__":
    unittest.main()
