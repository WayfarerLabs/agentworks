"""Real-Chromium witnesses for fixed-height Phase 4Q terrain presentation."""

from __future__ import annotations

import os
import shutil
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
import { createRun, updateRetention } from "/static/lander-model.js";

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

const box = (node) => {
    const rect = node.getBoundingClientRect();
    return {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
        width: rect.width, height: rect.height, clientWidth: node.clientWidth,
        scrollWidth: node.scrollWidth, clientHeight: node.clientHeight,
        scrollHeight: node.scrollHeight};
};
const snapshot = () => ({
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
    worldTransform: getComputedStyle(world).transform,
    stageRatio: stage.getBoundingClientRect().width / stage.getBoundingClientRect().height,
});
const useSeed = (seed, x = 36) => {
    const model = createRun({seed, reducedMotion: true});
    controller.model = updateRetention({...model, pose: {...model.pose, x}});
    controller.render();
    return snapshot();
};
const fail = () => {
    controller.model = {...controller.model, state: "failed", failureCause: "terrain", fuel: 0};
    controller.render();
    return snapshot();
};
const retry = () => { restart.click(); return snapshot(); };
const leave = () => { exit.click(); return snapshot(); };
window.phase4q = {snapshot, useSeed, fail, retry, leave};
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

        result: dict[str, object] = {}
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
            stages = [connection.evaluate("phase4q.useSeed(11, 36)")]
            stages.append(connection.evaluate("phase4q.useSeed(41, 140)"))
            stages.append(connection.evaluate("phase4q.fail()"))
            stages.append(connection.evaluate("phase4q.retry()"))
            stages.append(connection.evaluate("phase4q.leave()"))
            result[name] = stages
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
        for name, stages in result.items():
            width, height = expected[name]
            self.assertEqual([stage["state"] for stage in stages],
                             ["flying", "flying", "failed", "flying", "preflight"])
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
                    for component in ("body", "main", "game", "shell", "stage", "rail"):
                        metrics = stage[component]
                        self.assertLessEqual(metrics["scrollWidth"], ceil(metrics["width"]) + 1)
                        self.assertLessEqual(metrics["scrollHeight"], ceil(metrics["height"]) + 1)
                        self.assertGreaterEqual(metrics["left"], 0)
                        self.assertGreaterEqual(metrics["top"], 0)
                        self.assertLessEqual(metrics["right"], width)
                        self.assertLessEqual(metrics["bottom"], height)
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
