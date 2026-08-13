"""Real-Chromium witnesses for fixed-height Phase 4Q terrain presentation."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer
from math import ceil
from pathlib import Path

from lander_chromium_phase4k import DevToolsConnection, _devtools_target, _QuietHandler
from site_test_support import RepositoryFixture

PROBE = r"""
import { landerGameController as controller } from "/static/lander-game.js";
import { advanceMissionSequence, createRun, stepFlight, updateRetention } from "/static/lander-model.js";
import { STATIC_WORLD_SEED, terrainSiteForIndex } from "/static/lander-world.js";

if (controller.frameId !== null) cancelAnimationFrame(controller.frameId);
controller.frameId = null;
controller.paused = true;
const root = document.querySelector("#lander-game");
const main = document.querySelector("main");
const shell = document.querySelector("#lander-scene-shell");
const stage = document.querySelector("#lander-scene-stage");
const rail = document.querySelector("#lander-controls-rail");
const world = document.querySelector("#lander-world");
const restart = document.querySelector("#lander-restart");
const exit = document.querySelector("#lander-exit");
const zero = {left: 0, right: 0};

const box = (node) => {
    const rect = node.getBoundingClientRect();
    return {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
        width: rect.width, height: rect.height, clientWidth: node.clientWidth ?? rect.width,
        scrollWidth: node.scrollWidth ?? rect.width, clientHeight: node.clientHeight ?? rect.height,
        scrollHeight: node.scrollHeight ?? rect.height};
};
const landmarkNodes = () => [...document.querySelectorAll(
    ".site-header, .site-header *, .site-footer, .site-footer *, " +
    "#lander-start, #lander-start *, #lander-restart, #lander-restart *, #lander-exit, #lander-exit *")];
const snapshot = (label = "") => ({
    label,
    state: controller.model.state,
    viewport: {innerWidth, innerHeight},
    document: {clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        clientHeight: document.documentElement.clientHeight,
        scrollHeight: document.documentElement.scrollHeight,
        scrollLeft: document.documentElement.scrollLeft,
        scrollTop: document.documentElement.scrollTop},
    body: box(document.body), main: box(main), game: box(root), shell: box(shell),
    stage: box(stage), rail: box(rail), restart: box(restart), exit: box(exit),
    header: box(document.querySelector(".site-header")),
    footer: box(document.querySelector(".site-footer")),
    footerNav: box(document.querySelector(".site-footer nav")),
    landmarks: landmarkNodes().map((node) => ({tag: node.tagName, id: node.id,
        href: node.getAttribute("href"), hidden: node.closest("[hidden]") !== null, box: box(node)})),
    worldTransform: getComputedStyle(world).transform,
    stageRatio: stage.getBoundingClientRect().width / stage.getBoundingClientRect().height,
});
const useSeed = (seed, x = 36, y = 30, label = "seed") => {
    const model = createRun({seed, reducedMotion: true});
    controller.model = updateRetention({...model, pose: {...model.pose, x, y}});
    controller.render();
    return snapshot(label);
};
const service = () => {
    let model = createRun({seed: 11, reducedMotion: false});
    const target = model.retainedSites[0];
    model = stepFlight({...model, pose: {x: target.center, y: target.platformTop + .001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0}}, zero);
    controller.model = updateRetention(model); controller.render();
    const landed = snapshot("service-landed");
    model = advanceMissionSequence(model, .3, false);
    model = advanceMissionSequence(model, .9, false);
    model = advanceMissionSequence(model, 1.4, false);
    controller.model = updateRetention(model); controller.render();
    return [landed, snapshot("service-powered")];
};
const fail = () => {
    let model = createRun({seed: 41, reducedMotion: true});
    model = stepFlight({...model, pose: {...model.pose, y: 56, vy: 10}}, zero);
    controller.model = updateRetention(model);
    controller.render();
    return snapshot("crash");
};
const retry = () => { restart.click(); return snapshot("retry"); };
const leave = () => { exit.click(); return snapshot("exit"); };
const focusFooter = () => { document.querySelector(".footer-game-link").focus({preventScroll: true});
    return snapshot("focus-footer"); };
const frameTimings = () => {
    const values = [];
    for (let index = 0; index < 250; index += 1) {
        const started = performance.now(); controller.render(); values.push(performance.now() - started);
    }
    values.sort((left, right) => left - right);
    return {p95: values[Math.ceil(values.length * .95) - 1], maximum: values.at(-1)};
};
const valleyX = (seed) => (terrainSiteForIndex(seed, 0).center + terrainSiteForIndex(seed, 1).center) / 2;
window.phase4q = {snapshot, useSeed, service, fail, retry, leave, focusFooter, frameTimings,
    valleyX, STATIC_WORLD_SEED};
document.documentElement.dataset.phase4qReady = "true";
"""


def browser_phase4q_contract(output: Path) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser")
         if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Phase 4Q browser contracts")
    page = output / "lander/index.html"
    probe_path = output / "phase4q-browser.js"
    source = page.read_text(encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="phase4q-browser-server")
    profile = tempfile.TemporaryDirectory()
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    try:
        probe_path.write_text(PROBE, encoding="utf-8")
        page.write_text(
            source.replace("</body>", '<script type="module" src="/phase4q-browser.js"></script></body>', 1),
            encoding="utf-8",
        )
        thread.start()
        process = subprocess.Popen(
            (
                chromium, "--headless", "--disable-gpu", "--no-sandbox", "--no-first-run",
                "--no-default-browser-check", "--remote-allow-origins=*", "--remote-debugging-port=0",
                f"--user-data-dir={profile.name}", "about:blank",
            ),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "HOME": profile.name},
        )
        connection = DevToolsConnection(_devtools_target(Path(profile.name), process))
        for domain in ("Runtime", "Page"):
            connection.call(f"{domain}.enable")
        connection.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}/lander/"})
        for _ in range(240):
            if connection.evaluate("document.documentElement.dataset.phase4qReady === 'true'"):
                break
            time.sleep(0.025)
        else:
            raise AssertionError("Phase 4Q browser probe did not initialize")

        screenshot_directory = output / "_phase4q-screenshots"
        screenshot_directory.mkdir(exist_ok=True)
        result: dict[str, object] = {"viewports": {}, "screenshots": [], "frameTimings": None}
        viewports = (
            ("narrow", 320, 780, False),
            ("zoomEquivalent", 320, 240, False),
            ("touchLandscape", 667, 320, True),
            ("desktop", 1000, 780, False),
        )
        for name, width, height, touch in viewports:
            connection.call("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": 1, "mobile": touch,
            })
            connection.call("Emulation.setTouchEmulationEnabled", {"enabled": touch, "maxTouchPoints": 5})
            time.sleep(0.05)
            stages = [connection.evaluate("phase4q.snapshot('preflight')")]
            stages.append(connection.evaluate("phase4q.useSeed(11, 36, 30, 'seed11-summit')"))
            stages.append(connection.evaluate(
                "phase4q.useSeed(11, phase4q.valleyX(11), 2.5, 'seed11-low-valley')"
            ))
            stages.append(connection.evaluate("phase4q.useSeed(41, 140, 30, 'seed41-summit')"))
            stages.append(connection.evaluate(
                "phase4q.useSeed(phase4q.STATIC_WORLD_SEED, phase4q.valleyX(phase4q.STATIC_WORLD_SEED), "
                "3, 'static-valley')"
            ))
            stages.append(connection.evaluate("phase4q.useSeed(11, 36, 55, 'ceiling-flight')"))
            stages.append(connection.evaluate("phase4q.useSeed(41, -40, 30, 'reverse-left')"))
            stages.append(connection.evaluate("phase4q.useSeed(41, 160, 30, 'reverse-right')"))
            stages.extend(connection.evaluate("phase4q.service()"))
            stages.append(connection.evaluate("phase4q.fail()"))
            stages.append(connection.evaluate("phase4q.retry()"))
            stages.append(connection.evaluate("phase4q.focusFooter()"))
            connection.call("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": width // 2, "y": height // 2, "deltaX": 0, "deltaY": 180,
            })
            stages.append(connection.evaluate("phase4q.snapshot('wheel')"))
            if touch:
                connection.call("Input.dispatchTouchEvent", {
                    "type": "touchStart", "touchPoints": [{"x": width // 2, "y": height // 2}],
                })
                connection.call("Input.dispatchTouchEvent", {"type": "touchCancel", "touchPoints": []})
                stages.append(connection.evaluate("phase4q.snapshot('touch')"))
            stages.append(connection.evaluate("phase4q.leave()"))
            result["viewports"][name] = stages
            if name == "desktop":
                for seed, x in ((11, 55), (41, 55), (1095194417, 55)):
                    connection.evaluate(f"phase4q.useSeed({seed}, {x}, 30, 'screenshot-{seed}')")
                    png = base64.b64decode(connection.call("Page.captureScreenshot", {
                        "format": "png", "captureBeyondViewport": False,
                    })["data"])
                    screenshot = screenshot_directory / f"phase4q-{seed}-{width}x{height}.png"
                    screenshot.write_bytes(png)
                    png_width, png_height = struct.unpack(">II", png[16:24])
                    result["screenshots"].append({
                        "seed": seed, "width": png_width, "height": png_height,
                        "bytes": len(png), "sha256": hashlib.sha256(png).hexdigest(),
                        "path": str(screenshot),
                    })
                result["frameTimings"] = connection.evaluate("phase4q.frameTimings()")
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


class Phase4QBrowserTests(RepositoryFixture):
    def test_fixed_scene_has_no_page_growth_or_scroll_across_lifecycle(self) -> None:
        result = browser_phase4q_contract(self.build())
        expected = {
            "narrow": (320, 780),
            "zoomEquivalent": (320, 240),
            "touchLandscape": (667, 320),
            "desktop": (1000, 780),
        }
        expected_labels = [
            "preflight", "seed11-summit", "seed11-low-valley", "seed41-summit", "static-valley",
            "ceiling-flight", "reverse-left", "reverse-right", "service-landed", "service-powered",
            "crash", "retry", "focus-footer", "wheel",
        ]
        for name, stages in result["viewports"].items():
            width, height = expected[name]
            labels = expected_labels + (["touch"] if name == "touchLandscape" else []) + ["exit"]
            self.assertEqual([stage["label"] for stage in stages], labels)
            self.assertEqual(stages[0]["state"], "preflight")
            self.assertEqual(stages[8]["state"], "landed")
            self.assertEqual(stages[9]["state"], "launching")
            self.assertEqual(stages[10]["state"], "failed")
            self.assertEqual(stages[11]["state"], "flying")
            self.assertEqual(stages[-1]["state"], "preflight")
            first_height = stages[0]["stage"]["height"]
            first_width = stages[0]["stage"]["width"]
            for stage in stages:
                with self.subTest(viewport=name, state=stage["state"]):
                    self.assertEqual(stage["viewport"], {"innerWidth": width, "innerHeight": height})
                    document = stage["document"]
                    self.assertEqual(document, {
                        "clientWidth": width, "scrollWidth": width,
                        "clientHeight": height, "scrollHeight": height,
                        "scrollLeft": 0, "scrollTop": 0,
                    })
                    for component in (
                        "body", "header", "main", "game", "shell", "stage", "rail", "footer", "footerNav"
                    ):
                        metrics = stage[component]
                        self.assertLessEqual(metrics["scrollWidth"], ceil(metrics["width"]) + 1)
                        self.assertLessEqual(metrics["scrollHeight"], ceil(metrics["height"]) + 1)
                        self.assertGreaterEqual(metrics["left"], 0)
                        self.assertGreaterEqual(metrics["top"], 0)
                        self.assertLessEqual(metrics["right"], width)
                        self.assertLessEqual(metrics["bottom"], height)
                    for landmark in stage["landmarks"]:
                        metrics = landmark["box"]
                        if not landmark["hidden"] and metrics["width"]:
                            self.assertGreaterEqual(metrics["left"], 0)
                            self.assertGreaterEqual(metrics["top"], 0)
                            self.assertLessEqual(metrics["right"], width)
                            self.assertLessEqual(metrics["bottom"], height)
                            self.assertLessEqual(metrics["scrollWidth"], ceil(metrics["width"]) + 1)
                            self.assertLessEqual(metrics["scrollHeight"], ceil(metrics["height"]) + 1)
                    self.assertAlmostEqual(stage["stageRatio"], 25 / 16, places=3)
                    self.assertAlmostEqual(stage["stage"]["width"], first_width, places=5)
                    self.assertAlmostEqual(stage["stage"]["height"], first_height, places=5)
                    transform = stage["worldTransform"]
                    self.assertTrue(transform == "none" or transform.endswith(", 0)"), transform)
                    for action in (stage["restart"], stage["exit"]):
                        if action["width"]:
                            self.assertGreaterEqual(action["width"], 44)
                            self.assertGreaterEqual(action["height"], 44)
            self.assertGreater(first_width, 0)
            self.assertGreater(first_height, 0)
        screenshots = result["screenshots"]
        self.assertEqual([item["seed"] for item in screenshots], [11, 41, 1095194417])
        self.assertTrue(all(item["width"] == 1000 and item["height"] == 780 for item in screenshots))
        self.assertTrue(all(item["bytes"] > 20_000 for item in screenshots))
        self.assertTrue(all(Path(item["path"]).is_file() for item in screenshots))
        self.assertEqual(len({item["sha256"] for item in screenshots}), 3)
        self.assertLess(result["frameTimings"]["p95"], 4)
        self.assertLess(result["frameTimings"]["maximum"], 25)
