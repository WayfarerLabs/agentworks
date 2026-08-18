"""Cleanup-safe Chromium witnesses for the Phase 4K Lander contracts."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from chromium_test_support import DevToolsConnection, cleanup_profile, devtools_target


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


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


def _readiness_expression(url: str) -> str:
    return (
        f"location.href === {json.dumps(url)} && "
        "document.readyState === 'complete' && "
        "document.documentElement?.dataset.phase4kReady === 'true'"
    )


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
    input: structuredClone(controller.clock.input),
    queue: structuredClone(controller.clock.queue),
    pointer: controller.pointer ? structuredClone(controller.pointer) : null,
    pointerInput: structuredClone(controller.pointerInput),
    pulse: structuredClone(controller.collectivePulse),
    releasedCapture: controller.releasedCapture ? structuredClone(controller.releasedCapture) : null,
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
        controller.model = {...controller.model, fuel: controller.model.fuel - 0.1};
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


def browser_phase4k_contract(
    output: Path,
    *,
    connection_factory: Callable[[str], DevToolsConnection] = DevToolsConnection,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer,
    tempdir_factory: Callable[[], tempfile.TemporaryDirectory[str]] = tempfile.TemporaryDirectory,
    target_factory: Callable[[Path, subprocess.Popen[bytes]], str] = devtools_target,
    probe_source_factory: Callable[[], str] = _probe_source,
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
        probe_path.write_text(probe_source_factory(), encoding="utf-8")
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
        loaded_url = f"http://127.0.0.1:{server.server_address[1]}/lander/"
        connection.call("Page.navigate", {"url": loaded_url})
        readiness = _readiness_expression(loaded_url)
        for _ in range(200):
            if connection.evaluate(readiness):
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

        connection.evaluate("phase4k.reset('flying'); phase4k.shell.focus(); phase4k.clearEvents()")
        native_release_before = connection.evaluate("phase4k.snapshot()")
        dispatch_key(connection, "Space", up=False)
        native_release_pressed = connection.evaluate("phase4k.snapshot()")
        dispatch_key(connection, "Space", down=False)
        result["nativeRelease"] = {
            "before": native_release_before,
            "pressed": native_release_pressed,
            "released": connection.evaluate("phase4k.snapshot()"),
            "events": connection.evaluate("structuredClone(phase4k.keyEvents)"),
        }

        connection.evaluate("phase4k.reset('flying'); phase4k.shell.focus(); phase4k.clearEvents()")
        focusout_before = connection.evaluate("phase4k.snapshot()")
        dispatch_key(connection, "Space", up=False)
        focusout_pressed = connection.evaluate("phase4k.snapshot()")
        connection.evaluate("phase4k.focusTarget('header')")
        focusout_released = connection.evaluate("phase4k.snapshot()")
        connection.evaluate("phase4k.clearEvents()")
        dispatch_key(connection, "Space", down=False)
        result["focusoutRelease"] = {
            "before": focusout_before,
            "pressed": focusout_pressed,
            "focusout": focusout_released,
            "afterOutsideKeyup": connection.evaluate("phase4k.snapshot()"),
            "outsideEvents": connection.evaluate("structuredClone(phase4k.keyEvents)"),
        }

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
            cleanup(lambda: cleanup_profile(profile))
        if source is not None:
            cleanup(lambda: page.write_text(source, encoding="utf-8"))
        cleanup(lambda: probe_path.unlink(missing_ok=True))
        if active_error is None and cleanup_errors:
            raise cleanup_errors[0]
