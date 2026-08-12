"""Real-Chromium witnesses for the Phase 4P terrain and camera projection."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from lander_chromium_phase4k import DevToolsConnection, _devtools_target, _QuietHandler
from site_test_support import RepositoryFixture

PROBE = r"""
import { landerGameController as controller } from "/static/lander-game.js";
import { createRun, stepFlight, updateRetention } from "/static/lander-model.js";
import { STATIC_WORLD_SEED, cameraForPose, siteScaffoldMembers,
    terrainHeightAt, terrainSurfacePath, terrainVerticesForWindow } from "/static/lander-world.js";

try {
if (controller.frameId !== null) cancelAnimationFrame(controller.frameId);
controller.frameId = null;
controller.paused = true;
const root = document.querySelector("#lander-game");
const world = document.querySelector("#lander-world");
let model = updateRetention(createRun({seed: STATIC_WORLD_SEED}));
const site = model.retainedSites[0];
const staticTerrain = document.querySelector(".terrain-surface").getAttribute("d");
const staticTerrainParity = staticTerrain === terrainSurfacePath(
    terrainVerticesForWindow(STATIC_WORLD_SEED, [site], 0, 100));
controller.model = model;
controller.render();
const runtimeTerrain = document.querySelector(".terrain-surface").getAttribute("d");
const group = document.querySelector(`[data-site-id="${site.id}"]`);
const columns = siteScaffoldMembers(site);
const opening = {
    cameraX: root.style.getPropertyValue("--camera-x"),
    cameraY: root.style.getPropertyValue("--camera-y"),
    deckLevel: site.deckLevel,
    staticTerrainParity,
    runtimeTerrainParity: runtimeTerrain === terrainSurfacePath(model.terrainVertices),
    scaffoldMembers: (group.querySelector(".site-scaffold").getAttribute("d").match(/M/g) || []).length,
    expectedMembers: columns.length,
    feet: [site.platformLeft, site.platformLeft + 1, site.platformLeft + 8.8,
        site.platformLeft + 9.8, site.platformLeft + 17.6, site.platformLeft + 18.6]
        .map((x) => terrainHeightAt(site.seed, x)),
};
model = {...model, pose: {...model.pose, x: 160, y: 55, vx: 0, vy: 0}};
controller.model = model;
controller.render();
const expectedCamera = cameraForPose(model.pose);
const explored = {
    cameraX: root.style.getPropertyValue("--camera-x"),
    cameraY: root.style.getPropertyValue("--camera-y"),
    expectedCamera,
    descendants: world.querySelectorAll("*").length,
    targetDirection: root.dataset.targetDirection,
};
const zero = {left: 0, right: 0};
const collective = {left: 0.72, right: 0.72};
let warmup = updateRetention(createRun({seed: 0x12345678, reducedMotion: true}));
const warmupTarget = warmup.retainedSites.find((candidate) => candidate.id === warmup.targetSiteId);
warmup = {...warmup, pose: {x: warmupTarget.center, y: warmupTarget.platformTop + .001,
    vx: 0, vy: -1, angle: 0, angularVelocity: 0}};
warmup = updateRetention(stepFlight(warmup, zero));
controller.model = warmup;
controller.render();
const generation = [];
const rendering = [];
let maximumDescendants = 0;
let maximumSites = 0;
let maximumChunks = 0;
let mission = updateRetention(createRun({seed: 0x12345678, reducedMotion: true}));
for (let completed = 0; completed < 100; completed += 1) {
    for (let step = 0; step < 90 && mission.state === "launching"; step += 1) {
        mission = updateRetention(stepFlight(mission, collective));
    }
    const target = mission.retainedSites.find((candidate) => candidate.id === mission.targetSiteId);
    mission = {...mission, pose: {x: target.center, y: target.platformTop + .001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0}};
    const generationStart = performance.now();
    mission = updateRetention(stepFlight(mission, zero));
    generation.push(performance.now() - generationStart);
    const renderStart = performance.now();
    controller.model = mission;
    controller.render();
    rendering.push(performance.now() - renderStart);
    maximumDescendants = Math.max(maximumDescendants, world.querySelectorAll("*").length);
    maximumSites = Math.max(maximumSites, mission.retainedSites.length);
    maximumChunks = Math.max(maximumChunks, mission.retainedChunks.length);
}
const percentile95 = (values) => [...values].sort((left, right) => left - right)[Math.ceil(.95 * values.length) - 1];
const hundredSites = {
    completed: mission.completedSites,
    generationP95: percentile95(generation),
    generationMaximum: Math.max(...generation),
    renderP95: percentile95(rendering),
    maximumDescendants,
    maximumSites,
    maximumChunks,
};
window.phase4p = {opening, explored, hundredSites};
document.documentElement.dataset.phase4pReady = "true";
} catch (error) {
    window.phase4p = {error: String(error), stack: error.stack};
    document.documentElement.dataset.phase4pReady = "true";
}
"""


def browser_phase4p_contract(output: Path) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser") if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Phase 4P browser contracts")
    page = output / "lander/index.html"
    probe_path = output / "phase4p-browser.js"
    source = page.read_text(encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="phase4p-browser-server")
    profile = tempfile.TemporaryDirectory()
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    try:
        probe_path.write_text(PROBE, encoding="utf-8")
        page.write_text(
            source.replace("</body>", '<script type="module" src="/phase4p-browser.js"></script></body>', 1),
            encoding="utf-8",
        )
        thread.start()
        process = subprocess.Popen(
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
        connection = DevToolsConnection(_devtools_target(Path(profile.name), process))
        for domain in ("Runtime", "Page"):
            connection.call(f"{domain}.enable")
        connection.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}/lander/"})
        for _ in range(240):
            if connection.evaluate("document.documentElement.dataset.phase4pReady === 'true'"):
                return connection.evaluate("phase4p")
            time.sleep(0.025)
        raise AssertionError("Phase 4P browser probe did not initialize")
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


class Phase4PBrowserTests(RepositoryFixture):
    def test_real_chromium_projects_canonical_relief_lattice_and_vertical_camera(self) -> None:
        result = browser_phase4p_contract(self.build())
        self.assertNotIn("error", result, result)
        opening = result["opening"]
        self.assertEqual(opening["cameraX"], "0px")
        self.assertEqual(opening["cameraY"], "79px")
        self.assertEqual(opening["deckLevel"], 116)
        self.assertTrue(opening["staticTerrainParity"])
        self.assertTrue(opening["runtimeTerrainParity"])
        self.assertEqual(opening["scaffoldMembers"], opening["expectedMembers"])
        self.assertEqual(len(opening["feet"]), 6)
        explored = result["explored"]
        self.assertAlmostEqual(float(explored["cameraX"].removesuffix("px")), -10 * explored["expectedCamera"]["left"])
        self.assertAlmostEqual(float(explored["cameraY"].removesuffix("px")), explored["expectedCamera"]["down"])
        self.assertLessEqual(explored["descendants"], 80)
        self.assertIn(explored["targetDirection"], ("left", "right"))
        hundred_sites = result["hundredSites"]
        self.assertEqual(hundred_sites["completed"], 100)
        self.assertLessEqual(hundred_sites["maximumDescendants"], 80)
        self.assertLessEqual(hundred_sites["maximumSites"], 3)
        self.assertLessEqual(hundred_sites["maximumChunks"], 5)
        self.assertLess(hundred_sites["renderP95"], 4)
        self.assertLess(hundred_sites["generationP95"], 25)
        self.assertLess(hundred_sites["generationMaximum"], 50)
