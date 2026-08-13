import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { independentMaximumClearFace } from "./lander-clear-faces-test-helper.mjs";
import { controllerClasses, controllerFixture } from "./lander-test-dom.mjs";
import { advanceMissionSequence, createRun, fuelGaugeLevel, stepFlight } from "../static/lander-model.js";
import {
    STATIC_WORLD_SEED,
    cameraLeftForPose,
    siteScaffoldMembers,
    siteStructure,
    skyProjectionForCamera,
    skyProjectionIdentityForCamera,
    targetDirectionForViewport,
    terrainProfileForBlock,
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
    const approach = {
        ...run,
        pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1, angle: 0, angularVelocity: 0 },
    };
    return stepFlight(approach, ZERO);
}

test("opening and post-award fuel use honest uncapped gauge references", async () => {
    const run = createRun({ seed: 1 });
    assert.equal(run.fuel, 15);
    assert.equal(run.fuelGaugeReference, 30);
    assert.equal(fuelGaugeLevel(run), 0.5);
    const production = await Promise.all(
        ["static/lander-model.js", "static/lander-game.js"].map((path) => readFile(new URL(path, ROOT), "utf8")),
    );
    assert.ok(production.every((source) => !source.includes("legDepartureFuel")));

    const powered = contactTarget(1, true);
    assert.equal(powered.state, "launching");
    assert.equal(powered.fuel, powered.fuelGaugeReference);
    assert.ok(powered.fuel > 30);
    assert.equal(fuelGaugeLevel(powered), 1);
    assert.equal(powered.checkpoint.fuel, powered.fuel);
    assert.equal(powered.checkpoint.fuelGaugeReference, powered.fuelGaugeReference);
});
test("half-tank opening lands for every global terrain profile", async () => {
    const derived = JSON.parse(await readFile(new URL("tests/fixtures/lander-route-derived-v7.json", ROOT), "utf8"));
    const seeds = new Map();
    for (let seed = 1; seeds.size < 8; seed += 1) {
        const profile = terrainProfileForBlock(seed, 0).id;
        if (!seeds.has(profile)) seeds.set(profile, seed);
    }
    for (const witness of derived.openings) {
        let model = createRun({ seed: seeds.get(witness.profile) });
        close(model.retainedSites[0].platformTop, witness.deck);
        let steps = 0;
        for (const [command, count] of witness.runs) {
            const request = command === 0 ? ZERO : COLLECTIVE;
            for (let index = 0; index < count; index += 1, steps += 1) {
                model = stepFlight(model, request);
            }
        }
        assert.equal(model.state, "landed");
        assert.equal(steps, witness.contactStep);
        close(model.refuel.fromLevel * 30, witness.reserve, 1e-10);
    }
});

test("horizontal exploration is unbounded while the ceiling remains a failure", () => {
    const run = createRun({ seed: 1 });
    for (const [x, vx] of [
        [-5.01, -1],
        [101.01, 1],
        [140, 1],
        [-40, -1],
    ]) {
        const explored = stepFlight({ ...run, pose: { x, y: 30, vx, vy: 0, angle: 0, angularVelocity: 0 } }, ZERO);
        assert.equal(explored.state, "flying");
        assert.equal(explored.completedSites, 0);
    }
    assert.equal(
        stepFlight({ ...run, fuel: 0, pose: { x: 140, y: 30, vx: 1, vy: 0, angle: 0, angularVelocity: 0 } }, COLLECTIVE)
            .state,
        "flying",
    );
    const ceiling = stepFlight(
        { ...run, reducedMotion: true, pose: { x: 12, y: 56, vx: 0, vy: 10, angle: 0, angularVelocity: 0 } },
        ZERO,
    );
    assert.equal(ceiling.state, "failed");
    assert.equal(ceiling.failureCause, "ceiling");
});

test("camera, bidirectional target cue, and deterministic bounded sky cover both directions", () => {
    assert.equal(cameraLeftForPose({ x: -20 }), -25);
    assert.equal(cameraLeftForPose({ x: 20 }), 0);
    assert.equal(cameraLeftForPose({ x: 80 }), 45);
    const target = { platformLeft: 120, platformRight: 129.6 };
    assert.equal(targetDirectionForViewport(target, 0), "right");
    assert.equal(skyProjectionIdentityForCamera(-1, 0), skyProjectionIdentityForCamera(0xffffffff, 0));
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
            const count = projection.chunks.filter((chunk) => (((chunk - offset) % 4) + 4) % 4 === 0).length;
            assert.ok(count >= 1 && count <= 2);
        }
    }
    const ringVectors = [
        { seed: 2, profile: [[28, 9]] },
        { seed: 1, profile: [[31, 10]] },
        {
            seed: 10,
            profile: [
                [28, 9],
                [34, 12],
            ],
        },
    ];
    for (const { seed, profile } of ringVectors) {
        const landmarks = skyProjectionForCamera(seed, -200).landmarksPath;
        assert.doesNotMatch(landmarks, /Q|A30 10/);
        const circle = landmarks.match(/M(-?[\d.]+) (-?[\d.]+)A16 16 0 1 0/);
        assert.ok(circle);
        const x = Number(circle[1]) + 16;
        const y = Number(circle[2]);
        for (const [radiusX, radiusY] of profile) {
            const cutX = Math.sqrt((16 ** 2 - radiusY ** 2) / (1 - radiusY ** 2 / radiusX ** 2));
            const cutY = Math.sqrt(16 ** 2 - cutX ** 2);
            const expected =
                `M${x - radiusX} ${y}` +
                `A${radiusX} ${radiusY} 0 0 1 ${x - cutX} ${y - cutY}` +
                `M${x + cutX} ${y - cutY}` +
                `A${radiusX} ${radiusY} 0 0 1 ${x + radiusX} ${y}` +
                `M${x + radiusX} ${y}` +
                `A${radiusX} ${radiusY} 0 0 1 ${x - radiusX} ${y}`;
            assert.ok(landmarks.includes(expected));
            assert.equal((landmarks.match(new RegExp(`A${radiusX} ${radiusY} 0 0 1`, "g")) ?? []).length, 3);
        }
        assert.equal((landmarks.match(/A(?:28 9|31 10|34 12) 0 0 1/g) ?? []).length, profile.length * 3);
    }
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
    controller.model = { ...controller.model, seed: 11, pose: { ...controller.model.pose, x: 20 } };
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
    const derived = JSON.parse(await readFile(new URL("tests/fixtures/lander-route-derived-v7.json", ROOT), "utf8"));
    for (const witness of derived.worldWitnesses.filter((_, index) => index % 101 === 0)) {
        const site = witness.descriptor.site;
        const descriptor = {
            seed: witness.descriptor.seed,
            platformLeft: site.closedFootprint[0],
            platformRight: site.closedFootprint[0] + 9.6,
            platformTop: site.platformTop,
            platformBottom: site.platformTop - 0.35,
            supportFeet: site.supportFeet,
        };
        const structure = siteStructure(descriptor);
        const members = siteScaffoldMembers(descriptor);
        assert.equal(structure.supportColumns.length, 3);
        assert.ok(members.length >= 20 && members.length <= 300);
        structure.supportColumns.forEach((column, index) => {
            assert.equal(column.left, descriptor.platformLeft + [0, 8.8, 17.6][index]);
            assert.equal(column.right, descriptor.platformLeft + [1, 9.8, 18.6][index]);
            assert.equal(column.leftFoot, descriptor.supportFeet[index * 2]);
            assert.equal(column.rightFoot, descriptor.supportFeet[index * 2 + 1]);
            assert.equal(column.collider.bottom, Math.min(column.leftFoot, column.rightFoot) - 0.1);
            assert.equal(column.collider.top, descriptor.platformBottom + 0.1);
            assert.equal(
                members
                    .slice(14)
                    .filter(
                        (member) =>
                            member.start[0] >= column.left &&
                            member.start[0] <= column.right &&
                            member.end[0] >= column.left &&
                            member.end[0] <= column.right,
                    ).length,
                2 * column.levels.length + 1,
            );
        });
        assert.equal(members.filter((member) => member.start[1] === descriptor.platformBottom).length >= 8, true);
    }
});

test("independent planar overlay derives every connected clear-face maximum and kills member mutations", async () => {
    const derived = JSON.parse(await readFile(new URL("tests/fixtures/lander-route-derived-v7.json", ROOT), "utf8"));
    for (const witness of derived.worldWitnesses.filter((_, index) => index % 101 === 0)) {
        const canonical = witness.descriptor.site;
        const descriptor = {
            seed: witness.descriptor.seed,
            platformLeft: canonical.closedFootprint[0],
            platformRight: canonical.closedFootprint[0] + 9.6,
            platformTop: canonical.platformTop,
            platformBottom: canonical.platformTop - 0.35,
            supportFeet: canonical.supportFeet,
        };
        const structure = siteStructure(descriptor);
        const scaffoldMembers = siteScaffoldMembers(descriptor);
        const fixture = { scaffoldMembers, supportColumns: structure.supportColumns, truss: structure.truss };
        assert.equal(independentMaximumClearFace(fixture).diameter, 3.1894356867634124);
        const removedDiagonal = scaffoldMembers.filter((_, index) => index !== 4);
        assert.ok(independentMaximumClearFace(fixture, removedDiagonal).diameter > 3.1894356867634124);
        const shiftedDiagonal = structuredClone(scaffoldMembers);
        shiftedDiagonal[4].end[0] += 0.2;
        assert.ok(independentMaximumClearFace(fixture, shiftedDiagonal).diameter > 3.1894356867634124);
    }
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
