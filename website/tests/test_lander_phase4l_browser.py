"""Real-Chromium witnesses for the Phase 4L controls and Retry contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from lander_chromium_phase4k import (
    DevToolsConnection,
    _button_names,
    _devtools_target,
    _QuietHandler,
    dispatch_key,
)
from site_test_support import RepositoryFixture, mock, snapshot
from test_lander_phase4k_browser_cleanup import _FakeProcess, _NullRootRaceConnection

PROBE = r"""
import { landerGameController as controller } from "/static/lander-game.js";
import { createRun, stepFlight } from "/static/lander-model.js";

const shell = document.querySelector("#lander-scene-shell");
const restart = document.querySelector("#lander-restart");
const rail = document.querySelector("#lander-controls-rail");
const controls = document.querySelector("#lander-controls");
const stage = document.querySelector("#lander-scene-stage");
const lines = [...controls.querySelectorAll(":scope > .lander-controls-line")];
const outside = document.querySelector(".breadcrumbs a");
outside.id = "phase4l-outside";
let retryClicks = 0;
restart.addEventListener("click", () => { retryClicks += 1; });
const pause = () => {
    controller.paused = true;
    if (controller.frameId !== null) cancelAnimationFrame(controller.frameId);
    controller.frameId = null;
};
const checkpointRun = () => {
    let model = createRun({seed: 1, reducedMotion: true});
    const target = model.retainedSites[0];
    model = {...model, pose: {x: target.center, y: target.platformTop + 0.001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0}};
    return stepFlight(model, {left: 0, right: 0});
};
const projection = (model) => ({
    state: model.state, seed: model.seed, completedSites: model.completedSites,
    refuelRatio: model.refuelRatio, generatorCursor: model.generatorCursor,
    pose: structuredClone(model.pose), fuel: model.fuel, fuelGaugeReference: model.fuelGaugeReference,
    activeSiteId: model.activeSiteId, targetSiteId: model.targetSiteId,
    targetRouteProof: structuredClone(model.targetRouteProof),
    retainedChunks: structuredClone(model.retainedChunks), retainedSites: structuredClone(model.retainedSites),
    commanded: structuredClone(model.commanded), refuel: structuredClone(model.refuel),
    agent: structuredClone(model.agent), checkpoint: structuredClone(model.checkpoint),
    failureCause: model.failureCause, crash: structuredClone(model.crash), status: model.status,
    launchStarted: model.launchStarted, launchCleared: model.launchCleared,
    sequenceSeconds: model.sequenceSeconds, nocStage: model.nocStage, crashOrdinal: model.crashOrdinal,
});
const fail = (model) => {
    controller.model = {...model, state: "failed", fuel: 0,
        pose: {...model.pose, x: model.pose.x + 7}, failureCause: "terrain"};
    controller.render();
};
const layout = () => {
    if (controller.model.state === "preflight") controller.start(false, performance.now());
    pause();
    const rect = (node) => {
        const box = node.getBoundingClientRect();
        return {top: box.top, bottom: box.bottom, width: box.width, height: box.height,
            clientWidth: node.clientWidth, scrollWidth: node.scrollWidth};
    };
    return {lineCount: lines.length, lines: lines.map((line) => ({...rect(line),
        rects: line.getClientRects().length, display: getComputedStyle(line).display,
        whiteSpace: getComputedStyle(line).whiteSpace})), controls: rect(controls), rail: rect(rail),
        stage: rect(stage), exit: rect(document.querySelector("#lander-exit")),
        game: rect(document.querySelector("#lander-game")),
        document: {clientWidth: document.documentElement.clientWidth,
            scrollWidth: document.documentElement.scrollWidth}};
};
const powered = checkpointRun();
const expected = projection({...powered, ...powered.checkpoint, state: "launching"});
const order = [];
const nativeRender = controller.render.bind(controller);
controller.render = () => { order.push("render"); nativeRender(); };
const nativeFocus = shell.focus.bind(shell);
shell.focus = (options) => { order.push({focus: structuredClone(options)}); nativeFocus(options); };
pause();
fail(powered);
outside.focus();
const crashFocus = document.activeElement.id;
const scrollBefore = {x: scrollX, y: scrollY};
const retryContract = {
    children: [...restart.children].map((child) => ({tag: child.localName, className: child.className})),
    label: restart.firstElementChild.textContent.trim(),
    labelRects: restart.firstElementChild.getClientRects().length,
    hint: restart.lastElementChild.textContent.trim(),
    hintHidden: restart.lastElementChild.getAttribute("aria-hidden"),
    shortcut: restart.getAttribute("aria-keyshortcuts"),
};
order.length = 0;
window.phase4l = {
    controller, shell, restart, powered, expected, order, layout, projection, fail,
    firstResult() {
        return {projection: projection(controller.model), focus: document.activeElement.id,
            order: structuredClone(order), scrollBefore, scrollAfter: {x: scrollX, y: scrollY},
            crashFocus, retryClicks, retry: structuredClone(retryContract)};
    },
    prepareSecond() { fail(controller.model); shell.focus(); order.length = 0; },
    secondResult() { return {projection: projection(controller.model), focus: document.activeElement.id,
        order: structuredClone(order)}; },
    initialRetry() {
        const initial = createRun({seed: 1, reducedMotion: true});
        controller.model = {...initial, state: "failed", fuel: 0};
        controller.render(); restart.click();
        return {actual: projection(controller.model), expected: projection(initial), retryClicks};
    },
};
document.documentElement.dataset.phase4lReady = "true";
"""


def _readiness_expression(url: str) -> str:
    return (
        f"location.href === {json.dumps(url)} && "
        "document.readyState === 'complete' && "
        "document.documentElement?.dataset.phase4lReady === 'true'"
    )


def browser_phase4l_contract(
    output: Path,
    *,
    chromium_path: str | None = None,
    connection_factory: Callable[[str], DevToolsConnection] = DevToolsConnection,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    target_factory: Callable[[Path, subprocess.Popen[bytes]], str] = _devtools_target,
    tempdir_factory: Callable[[], tempfile.TemporaryDirectory[str]] = tempfile.TemporaryDirectory,
) -> dict[str, object]:
    chromium = chromium_path or next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser") if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Phase 4L browser contracts")
    page = output / "lander/index.html"
    probe_path = output / "phase4l-browser.js"
    source = page.read_text(encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="phase4l-browser-server")
    profile = tempdir_factory()
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    try:
        probe_path.write_text(PROBE, encoding="utf-8")
        page.write_text(
            source.replace("</body>", '<script type="module" src="/phase4l-browser.js"></script></body>', 1),
            encoding="utf-8",
        )
        thread.start()
        process = popen_factory(
            (
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
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "HOME": profile.name},
        )
        connection = connection_factory(target_factory(Path(profile.name), process))
        for domain in ("Runtime", "Page", "Accessibility"):
            connection.call(f"{domain}.enable")
        connection.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 320,
                "height": 640,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        loaded_url = f"http://127.0.0.1:{server.server_address[1]}/lander/"
        connection.call("Page.navigate", {"url": loaded_url})
        readiness = _readiness_expression(loaded_url)
        for _ in range(200):
            if connection.evaluate(readiness):
                break
            time.sleep(0.025)
        else:
            raise AssertionError("Phase 4L browser probe did not initialize")
        result = {"narrow": connection.evaluate("phase4l.layout()")}
        connection.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 320,
                "height": 900,
                "deviceScaleFactor": 4,
                "mobile": False,
            },
        )
        result["zoomEquivalent"] = connection.evaluate("phase4l.layout()")
        retry_tree = connection.call("Accessibility.getFullAXTree")
        hint_remote = connection.call(
            "Runtime.evaluate",
            {
                "expression": "phase4l.restart.lastElementChild",
                "returnByValue": False,
            },
        )
        hint_tree = connection.call(
            "Accessibility.getPartialAXTree",
            {
                "objectId": hint_remote["result"]["objectId"],
                "fetchRelatives": False,
            },
        )
        connection.evaluate("phase4l.restart.click()")
        result["clickRetry"] = connection.evaluate("phase4l.firstResult()")
        result["retryButtonNames"] = _button_names(retry_tree)
        result["retryHintAxIgnored"] = [node.get("ignored", False) for node in hint_tree["nodes"]]
        connection.evaluate("phase4l.prepareSecond()")
        dispatch_key(connection, "KeyR")
        result["keyRetry"] = connection.evaluate("phase4l.secondResult()")
        result["initialRetry"] = connection.evaluate("phase4l.initialRetry()")
        result["expected"] = connection.evaluate("phase4l.expected")
        return result
    finally:
        if connection is not None:
            connection.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        if thread.is_alive():
            thread.join(timeout=5)
        page.write_text(source, encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        profile.cleanup()


class Phase4LBrowserTests(RepositoryFixture):
    def test_null_root_navigation_race_keeps_polling_and_cleans_up(self) -> None:
        output = self.build()
        before = snapshot(output)
        process = _FakeProcess()
        connection = _NullRootRaceConnection()
        profile_paths: list[Path] = []

        def profile_factory() -> tempfile.TemporaryDirectory[str]:
            profile = tempfile.TemporaryDirectory()
            profile_paths.append(Path(profile.name))
            return profile

        with (
            mock.patch.object(time, "sleep", return_value=None),
            self.assertRaises(AssertionError),
        ):
            browser_phase4l_contract(
                output,
                chromium_path="chromium-test",
                connection_factory=lambda url: connection,
                popen_factory=lambda *args, **kwargs: process,
                target_factory=lambda profile, owned: "ws://phase4l.invalid",
                tempdir_factory=profile_factory,
            )
        navigation = next(params for method, params in connection.calls if method == "Page.navigate")
        loaded_url = str(navigation["url"])
        self.assertTrue(loaded_url.endswith("/lander/"))
        self.assertEqual(connection.expressions, [_readiness_expression(loaded_url)] * 200)
        readiness = connection.expressions[0]
        self.assertIn(f"location.href === {json.dumps(loaded_url)}", readiness)
        self.assertIn("document.readyState === 'complete'", readiness)
        self.assertIn("document.documentElement?.dataset.phase4lReady", readiness)
        self.assertTrue(connection.closed)
        self.assertTrue(process.terminated)
        self.assertEqual(snapshot(output), before)
        self.assertFalse((output / "phase4l-browser.js").exists())
        self.assertTrue(all(not path.exists() for path in profile_paths))
        self.assertFalse(
            any(thread.name == "phase4l-browser-server" and thread.is_alive() for thread in threading.enumerate())
        )

    def test_real_chromium_two_line_reflow_and_exact_retry_restore(self) -> None:
        result = browser_phase4l_contract(self.build())
        for label in ("narrow", "zoomEquivalent"):
            with self.subTest(layout=label):
                layout = result[label]
                self.assertEqual(layout["lineCount"], 2)
                self.assertTrue(all(line["rects"] == 1 for line in layout["lines"]))
                self.assertTrue(all(line["display"] == "block" for line in layout["lines"]))
                self.assertTrue(all(line["whiteSpace"] == "nowrap" for line in layout["lines"]))
                for box in (*layout["lines"], layout["controls"], layout["rail"], layout["game"], layout["document"]):
                    self.assertLessEqual(box["scrollWidth"], box["clientWidth"])
                self.assertGreaterEqual(layout["exit"]["top"], layout["controls"]["bottom"])
                self.assertGreaterEqual(layout["exit"]["width"], 44)
                self.assertGreaterEqual(layout["exit"]["height"], 44)
        expected = result["expected"]
        click = result["clickRetry"]
        self.assertEqual(click["crashFocus"], "phase4l-outside")
        self.assertEqual(click["projection"], expected)
        self.assertEqual(click["focus"], "lander-scene-shell")
        self.assertEqual(click["scrollBefore"], click["scrollAfter"])
        self.assertEqual(click["order"], ["render", {"focus": {"preventScroll": True}}])
        self.assertEqual(click["retryClicks"], 1)
        retry = click["retry"]
        self.assertEqual(
            retry["children"],
            [
                {"tag": "span", "className": "lander-action-label"},
                {"tag": "span", "className": "lander-key-hint"},
            ],
        )
        self.assertTrue(retry["label"])
        self.assertEqual(retry["labelRects"], 1)
        self.assertTrue(retry["hint"])
        self.assertEqual(retry["hintHidden"], "true")
        self.assertEqual(retry["shortcut"], "r")
        self.assertEqual(result["retryButtonNames"].count(retry["label"]), 1)
        self.assertNotIn(f"{retry['label']} {retry['hint']}", result["retryButtonNames"])
        self.assertTrue(result["retryHintAxIgnored"])
        self.assertTrue(all(result["retryHintAxIgnored"]))
        key = result["keyRetry"]
        self.assertEqual(key["projection"], expected)
        self.assertEqual(key["focus"], "lander-scene-shell")
        self.assertEqual(key["order"], ["render", {"focus": {"preventScroll": True}}])
        self.assertEqual(result["initialRetry"]["actual"], result["initialRetry"]["expected"])
        self.assertEqual(result["initialRetry"]["retryClicks"], 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
