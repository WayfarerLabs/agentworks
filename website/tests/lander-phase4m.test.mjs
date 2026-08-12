import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { independentMaximumClearFace } from "./lander-clear-faces-test-helper.mjs";
import { controllerClasses, controllerFixture } from "./lander-test-dom.mjs";
import {
    advanceMissionSequence,
    createRun,
    fuelGaugeLevel,
    stepFlight,
} from "../static/lander-model.js";
import {
    STATIC_WORLD_SEED,
    cameraLeftForPose,
    siteScaffoldMembers,
    siteStructure,
    skyProjectionForCamera,
    skyProjectionIdentityForCamera,
    targetDirectionForViewport,
    terrainHeightAt,
} from "../static/lander-world.js";

const ROOT = new URL("../", import.meta.url);
const ZERO = Object.freeze({ left: 0, right: 0 });
const COLLECTIVE = Object.freeze({ left: 0.72, right: 0.72 });

function close(actual, expected, tolerance = 1e-12) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

function contactTarget(seed, reducedMotion = false) {
    const run = createRun({ seed, reducedMotion });
    const target = run.retainedSites.find((site) => site.id === run.targetSiteId);
    const approach = { ...run, pose: { x: target.center, y: target.platformTop + 0.001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
    return stepFlight(approach, ZERO);
}

test("opening and post-award fuel use honest uncapped gauge references", async () => {
    const run = createRun({ seed: 1 });
    assert.equal(run.fuel, 15);
    assert.equal(run.fuelGaugeReference, 30);
    assert.equal(fuelGaugeLevel(run), 0.5);
    const production = await Promise.all(["static/lander-model.js", "static/lander-game.js"].map(
        (path) => readFile(new URL(path, ROOT), "utf8")));
    assert.ok(production.every((source) => !source.includes("legDepartureFuel")));

    const powered = contactTarget(1, true);
    assert.equal(powered.state, "launching");
    assert.equal(powered.fuel, powered.fuelGaugeReference);
    assert.ok(powered.fuel > 30);
    assert.equal(fuelGaugeLevel(powered), 1);
    assert.equal(powered.checkpoint.fuel, powered.fuel);
    assert.equal(powered.checkpoint.fuelGaugeReference, powered.fuelGaugeReference);
});

test("half-tank opening lands at every deck tier with independent off-on-off schedules", () => {
    const cases = [
        { seed: 1, level: 83, off: 396, on: 108, steps: 554, reserve: 13.70399999999995 },
        { seed: 8, level: 91, off: 396, on: 96, steps: 501, reserve: 13.847999999999956 },
        { seed: 13, level: 99, off: 384, on: 96, steps: 512, reserve: 13.847999999999956 },
    ];
    for (const witness of cases) {
        let model = createRun({ seed: witness.seed });
        assert.equal(model.retainedSites[0].deckLevel, witness.level);
        let steps = 0;
        for (; steps < witness.off; steps += 1) model = stepFlight(model, ZERO);
        for (let index = 0; index < witness.on; index += 1, steps += 1) model = stepFlight(model, COLLECTIVE);
        while (model.state === "flying" && steps < 600) { model = stepFlight(model, ZERO); steps += 1; }
        assert.equal(model.state, "landed");
        assert.equal(steps, witness.steps);
        close(model.refuel.fromLevel * 30, witness.reserve);
    }
});

test("horizontal exploration is unbounded while the ceiling remains a failure", () => {
    const run = createRun({ seed: 1 });
    for (const [x, vx] of [[-5.01, -1], [101.01, 1], [140, 1], [-40, -1]]) {
        const explored = stepFlight({ ...run, pose: { x, y: 30, vx, vy: 0,
            angle: 0, angularVelocity: 0 } }, ZERO);
        assert.equal(explored.state, "flying");
        assert.equal(explored.completedSites, 0);
    }
    assert.equal(stepFlight({ ...run, fuel: 0, pose: { x: 140, y: 30, vx: 1, vy: 0,
        angle: 0, angularVelocity: 0 } }, COLLECTIVE).state, "flying");
    const ceiling = stepFlight({ ...run, reducedMotion: true, pose: { x: 12, y: 56, vx: 0, vy: 10,
        angle: 0, angularVelocity: 0 } }, ZERO);
    assert.equal(ceiling.state, "failed");
    assert.equal(ceiling.failureCause, "ceiling");
});

test("camera, bidirectional target cue, and deterministic bounded sky cover both directions", () => {
    assert.equal(cameraLeftForPose({ x: -20 }), -25);
    assert.equal(cameraLeftForPose({ x: 20 }), 0);
    assert.equal(cameraLeftForPose({ x: 80 }), 45);
    const target = { platformLeft: 120, platformRight: 129.6 };
    assert.equal(targetDirectionForViewport(target, 0), "right");
    assert.equal(skyProjectionIdentityForCamera(-1, 0),
        skyProjectionIdentityForCamera(0xffffffff, 0));
    assert.equal(targetDirectionForViewport(target, 120), null);
    assert.equal(targetDirectionForViewport(target, 130), "left");
    assert.equal(targetDirectionForViewport(target, 0), "right");

    for (const camera of [0, 50, -50]) {
        const projection = skyProjectionForCamera(STATIC_WORLD_SEED, camera);
        assert.equal(projection.chunks.length, 5);
        assert.equal(new Set(projection.chunks).size, 5);
        assert.equal((projection.starsPath.match(/M/g) ?? []).length, 20);
        assert.ok(projection.landmarksPath.length > 0);
        for (let offset = 0; offset < 4; offset += 1) {
            const count = projection.chunks.filter((chunk) => ((chunk - offset) % 4 + 4) % 4 === 0).length;
            assert.ok(count >= 1 && count <= 2);
        }
    }
    const realisticLandmarks = skyProjectionForCamera(1, -200).landmarksPath;
    assert.match(realisticLandmarks,
        /A16 16 0 1 0 [^A]+A16 16 0 1 0 [^Z]+ZM[^A]+A30 10 0 1 0 [^A]+A30 10 0 1 0 [^Z]+Z/);
    assert.doesNotMatch(realisticLandmarks, /Q/);
    assert.deepEqual(skyProjectionForCamera(STATIC_WORLD_SEED, 0), skyProjectionForCamera(STATIC_WORLD_SEED, 0));
});

test("sky controller keys reconciliation by normalized seed and chunk before projection", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture();
    const calls = [];
    const project = (seed, cameraLeft) => {
        calls.push([seed, cameraLeft]);
        return skyProjectionForCamera(seed, cameraLeft);
    };
    const controller = new LanderGameController(fixture.root, [], fixture.root.cloneNode(true), {
        freshSeed: () => 11,
        skyProjectionForCamera: project,
    });
    const stars = fixture.elements["scene-stars"];
    const landmarks = fixture.elements["scene-landmarks"];
    const preflight = [stars.getAttribute("d"), landmarks.getAttribute("d")];
    controller.start(false, 0);
    assert.equal(controller.model.seed, 11);
    assert.equal(calls.length, 2);
    assert.notDeepEqual([stars.getAttribute("d"), landmarks.getAttribute("d")], preflight);

    const initialWrites = [stars.setCount, landmarks.setCount];
    controller.render();
    const initial = [stars.getAttribute("d"), landmarks.getAttribute("d")];
    assert.equal(calls.length, 2);
    assert.deepEqual([stars.setCount, landmarks.setCount], initialWrites);

    controller.model = { ...controller.model, seed: 12 };
    controller.render();
    assert.equal(calls.length, 3);
    assert.notDeepEqual([stars.getAttribute("d"), landmarks.getAttribute("d")], initial);

    controller.model = { ...controller.model, pose: { ...controller.model.pose, x: 260 } };
    controller.render();
    assert.equal(fixture.root.style.getPropertyValue("--sky-camera-x"), "-540px");
    controller.model = { ...controller.model, seed: 11,
        pose: { ...controller.model.pose, x: 20 } };
    controller.render();
    assert.deepEqual([stars.getAttribute("d"), landmarks.getAttribute("d")], initial);
    assert.equal(fixture.elements["lander-sky-world"].children.length, 2);
    controller.destroy();
});

test("deployment travel is 0.9 seconds while refuel and power timings remain independent", () => {
    const landed = contactTarget(1);
    assert.equal(landed.state, "landed");
    const deploying = advanceMissionSequence(landed, 0.3);
    assert.equal(deploying.state, "deploying");
    close(advanceMissionSequence(deploying, 0.45).agent.progress, 0.5);
    assert.equal(advanceMissionSequence(deploying, 0.899).state, "deploying");
    const powering = advanceMissionSequence(deploying, 0.9);
    assert.equal(powering.state, "powering");
    assert.equal(advanceMissionSequence(powering, 1.399).state, "powering");
    assert.equal(advanceMissionSequence(powering, 1.4).state, "launching");
    assert.equal(advanceMissionSequence(landed, 0.1, true).state, "launching");
});

test("three native-foot lattice columns integrate with the one-path scaffold and honest colliders", async () => {
    const derived = JSON.parse(await readFile(new URL("tests/fixtures/lander-route-derived-v4.json", ROOT), "utf8"));
    for (const site of derived.worldWitnesses[0].descriptor.sites) {
        const descriptor = { seed: derived.worldWitnesses[0].descriptor.seed,
            platformLeft: site.platform.left, platformRight: site.platform.right,
            platformTop: site.platform.top, platformBottom: site.platform.bottom };
        const structure = siteStructure(descriptor);
        const members = siteScaffoldMembers(descriptor);
        assert.equal(structure.supportColumns.length, 3);
        assert.ok(members.length >= 41 && members.length <= 95);
        structure.supportColumns.forEach((column, index) => {
            assert.equal(column.left, descriptor.platformLeft + [0, 8.8, 17.6][index]);
            assert.equal(column.right, descriptor.platformLeft + [1, 9.8, 18.6][index]);
            assert.equal(column.leftFoot, terrainHeightAt(descriptor.seed, column.left));
            assert.equal(column.rightFoot, terrainHeightAt(descriptor.seed, column.right));
            assert.equal(column.collider.bottom, Math.min(column.leftFoot, column.rightFoot) - 0.1);
            assert.equal(column.collider.top, descriptor.platformBottom + 0.1);
            assert.equal(members.slice(14).filter((member) =>
                member.start[0] >= column.left && member.start[0] <= column.right &&
                member.end[0] >= column.left && member.end[0] <= column.right).length,
            2 * column.levels.length + 1);
        });
        assert.deepEqual(structure.supportColumns, site.supportColumns);
        assert.deepEqual(members, site.scaffoldMembers);
        const apertures = site.clearApertures;
        assert.equal(apertures.actualMaximumConnectedFace.diameter, 3.1894356867634124);
        assert.ok(Object.values(apertures).every(({ diameter }) => diameter < 3.2));
    }
});

test("independent planar overlay derives every connected clear-face maximum and kills member mutations", async () => {
    const derived = JSON.parse(await readFile(new URL("tests/fixtures/lander-route-derived-v4.json", ROOT), "utf8"));
    for (const witness of derived.worldWitnesses) {
        for (const site of witness.descriptor.sites) {
            assert.deepEqual(independentMaximumClearFace(site), site.clearApertures.actualMaximumConnectedFace);
        }
    }

    const site = derived.worldWitnesses[0].descriptor.sites[0];
    const removedDiagonal = site.scaffoldMembers.filter((_, index) => index !== 4);
    assert.ok(independentMaximumClearFace(site, removedDiagonal).diameter >
        site.clearApertures.actualMaximumConnectedFace.diameter);
    const shiftedDiagonal = structuredClone(site.scaffoldMembers);
    shiftedDiagonal[4].end[0] += 0.2;
    assert.ok(independentMaximumClearFace(site, shiftedDiagonal).diameter >
        site.clearApertures.actualMaximumConnectedFace.diameter);
});

test("static sky and footer navigation preserve structural no-script parity", async () => {
    const template = await readFile(new URL("templates/lander-game.html", ROOT), "utf8");
    const projection = skyProjectionForCamera(STATIC_WORLD_SEED, 0);
    const stars = template.match(/id="scene-stars"[^>]+d="([^"]*)"/)?.[1];
    const landmarks = template.match(/id="scene-landmarks"[^>]+d="([^"]*)"/)?.[1];
    assert.equal(stars, projection.starsPath);
    assert.equal(landmarks, projection.landmarksPath);
    assert.equal((template.match(/id="lander-sky-world"/g) ?? []).length, 1);
    assert.equal((template.match(/id="scene-(?:stars|landmarks)"/g) ?? []).length, 2);
    for (const name of ["index", "manifesto", "security", "lander", "404"]) {
        const shell = await readFile(new URL(`templates/${name}.html`, ROOT), "utf8");
        const tag = shell.match(/<a\b[^>]*href="\{\{SITE_BASE\}\}lander\/"[^>]*>/)?.[0];
        const anchor = tag?.match(/aria-label="([^"]+)"[\s\S]*title="([^"]+)"/);
        assert.ok(anchor, `${name} has a direct Lander footer route`);
        assert.ok(anchor[1]);
        assert.equal(anchor[1], anchor[2]);
    }
});
