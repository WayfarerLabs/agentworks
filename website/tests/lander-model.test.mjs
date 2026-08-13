import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { FakeElement, controllerClasses, controllerFixture, descendantCount, focusable } from "./lander-test-dom.mjs";

import {
    ENGINE_ACCELERATION,
    MAX_THRUST_VECTOR,
    MAX_PLAYABLE_Y,
    FAILURE_STATUS,
    FUEL_QUANTUM,
    REFERENCE_TEMPLATE_CATALOG,
    REFERENCE_TEMPLATES,
    ROUTE_DIGESTS,
    STEP_SECONDS,
    TURN_DIFFERENTIAL,
    TURNING_TOTAL,
    advanceMissionSequence,
    advanceSimulation,
    classifySweptContact,
    checkpointPoseForContact,
    collectiveRequestForSteer,
    createCueState,
    createPreflightModel,
    createRun,
    createSimulationClock,
    effectiveThrust,
    enqueueInputEdge,
    fuelGaugeLevel,
    integratePose,
    mixDigitalInput,
    mixEngineRequests,
    refuelRatioForBase,
    plumeForThrust,
    pointerEngineRequests,
    proveTemplate,
    removeQueuedInputEdges,
    stepFlight,
    transitionMission,
    updateRetention,
} from "../static/lander-model.js";
import {
    STATIC_WORLD_SEED,
    createSiteForIndex,
    instantiateTemplateSite,
    selectTemplate,
    siteScaffoldMembers,
    siteStructure,
    terrainCycleForSeed,
    terrainHeightAt,
    terrainFillPath,
    terrainSurfacePath as terrainPath,
    terrainVerticesForRange,
    terrainVerticesForWindow,
    terrainSiteForIndex,
} from "../static/lander-world.js";

const ROOT = new URL("../", import.meta.url).pathname;

function close(actual, expected, tolerance = 1e-10) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    }
    return value;
}

function digest(value) {
    return createHash("sha256").update(JSON.stringify(canonical(value)), "utf8").digest("hex");
}

function heightFromVertices(vertices, x) {
    for (let index = 1; index < vertices.length; index += 1) {
        const [leftX, leftY] = vertices[index - 1]; const [rightX, rightY] = vertices[index];
        if (x < leftX || x > rightX) continue;
        return leftY + (rightY - leftY) * (x - leftX) / (rightX - leftX);
    }
    throw new RangeError(`Witness vertices do not cover ${x}`);
}

function stepMany(pose, request, fuel, count) {
    let current = pose;
    let reserve = fuel;
    for (let index = 0; index < count; index += 1) {
        const result = integratePose(current, request, reserve);
        current = result.pose;
        reserve = result.thrust.fuel;
    }
    return { pose: current, fuel: reserve };
}

test("9.0 engine physics, true gimbal, assist, input arbitration, and plumes match fixed vectors", () => {
    assert.equal(ENGINE_ACCELERATION, 9);
    assert.equal(MAX_THRUST_VECTOR, 30);
    assert.equal(TURNING_TOTAL, 0.8);
    assert.equal(TURN_DIFFERENTIAL, 0.375);
    const pose = { x: 10, y: 30, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    const gravity = stepMany(pose, { left: 0, right: 0 }, 30, 120);
    close(gravity.pose.y, 28.4875); close(gravity.pose.vy, -3); close(gravity.fuel, 30);
    const collective = stepMany(pose, { left: 0.72, right: 0.72 }, 30, 120);
    close(collective.pose.y, 35.0215); close(collective.pose.vy, 9.96); close(collective.fuel, 28.56);
    const turn = integratePose(pose, { left: 0, right: 0.375 }, 30);
    close((turn.pose.vx - pose.vx) / STEP_SECONDS, -1.6875);
    close((turn.pose.vy - pose.vy) / STEP_SECONDS, -0.0771642622275195);
    close(turn.pose.angularVelocity, -0.25); close(turn.pose.angle, -0.00208333333333);
    close(turn.thrust.vectorAngle, -30); close(turn.thrust.fuel, 29.996875);
    const assisted = integratePose({ ...pose, angularVelocity: 15 }, { left: 0.72, right: 0.72 }, 30);
    close(assisted.thrust.left, 0.66); close(assisted.thrust.right, 0.78);
    close(assisted.pose.angularVelocity, 14.92); close(assisted.pose.angle, 0.124333333333);
    const coasting = integratePose({ ...pose, angularVelocity: 15 }, { left: 0, right: 0 }, 30);
    assert.deepEqual({ left: coasting.thrust.left, right: coasting.thrust.right,
        vectorAngle: coasting.thrust.vectorAngle }, { left: 0, right: 0, vectorAngle: 0 });
    close(coasting.pose.angularVelocity, 15); close(coasting.pose.angle, 0.125);
    const exhausted = effectiveThrust({ left: 1, right: 1 }, 0.005);
    close(exhausted.left, 0.3); close(exhausted.right, 0.3); assert.equal(exhausted.fuel, 0);
    assert.equal(exhausted.vectorAngle, 0);
    const digitalRows = [
        [{}, { left: 0, right: 0 }],
        [{ Space: true }, { left: 0.72, right: 0.72 }],
        [{ ArrowLeft: true }, { left: 0, right: 0.375 }],
        [{ ArrowRight: true }, { left: 0.375, right: 0 }],
        [{ Space: true, ArrowLeft: true }, { left: 0.21250000000000002, right: 0.5875 }],
        [{ Space: true, ArrowRight: true }, { left: 0.5875, right: 0.21250000000000002 }],
        [{ ArrowLeft: true, ArrowRight: true }, { left: 0, right: 0 }],
        [{ Space: true, ArrowLeft: true, ArrowRight: true }, { left: 0.72, right: 0.72 }],
    ];
    for (const [input, expected] of digitalRows) assert.deepEqual(mixDigitalInput(input), expected);
    const pointer = pointerEngineRequests(1000, 1000);
    assert.deepEqual(pointer, { left: 0.5875, right: 0.21250000000000002 });
    assert.deepEqual(pointerEngineRequests(-1000, 1000), { left: 0.21250000000000002, right: 0.5875 });
    assert.deepEqual(pointerEngineRequests(0, 1000), { left: 0.72, right: 0.72 });
    assert.deepEqual(mixEngineRequests(mixDigitalInput({ Space: true }), pointer), pointer);
    assert.deepEqual(mixEngineRequests(mixDigitalInput({ ArrowLeft: true }), pointer),
        { left: 0.21250000000000002, right: 0.5875 });
    for (const command of [digitalRows[4][1], digitalRows[5][1], pointer,
        pointerEngineRequests(-1000, 1000)]) {
        close(command.left + command.right, TURNING_TOTAL);
        close(Math.abs(command.left - command.right), TURN_DIFFERENTIAL);
        assert.ok(command.left + command.right <= 1.44);
    }
    const halfSteer = pointerEngineRequests(95, 1000);
    close(halfSteer.left + halfSteer.right, (1.44 + TURNING_TOTAL) / 2);
    close(halfSteer.left - halfSteer.right, TURN_DIFFERENTIAL / 2);
    const alternateAuthority = collectiveRequestForSteer(1, 1, 0.2);
    close(alternateAuthority.left + alternateAuthority.right, 1);
    close(alternateAuthority.left - alternateAuthority.right, 0.2);
    assert.deepEqual(plumeForThrust(0.5), { scaleY: 0.54, opacity: 0.625 });
});

test("one-indexed refuel ratio is direct, exact, and stable through base one hundred", () => {
    assert.deepEqual([1, 2, 3, 52, 53, 54, 100].map(refuelRatioForBase),
        [2, 1.5, 1.25, 1 + 2 ** -51, 1 + 2 ** -52, 1, 1]);
    assert.throws(() => refuelRatioForBase(0), /positive integer/);
    assert.throws(() => refuelRatioForBase(1.5), /positive integer/);
});

test("all six retained route literals begin at the reachable launch edge and pass defensive replays", () => {
    assert.deepEqual(REFERENCE_TEMPLATES.map(({ templateId }) => templateId), [
        "route-81-rise", "route-84-fall", "route-96-fall",
        "route-87-rise", "route-99-rise", "route-90-fall",
    ]);
    for (const template of REFERENCE_TEMPLATES) {
        assert.deepEqual(template.runs[0], [1, 90]);
        assert.ok(template.runs.length <= 64);
        assert.equal(template.combinationsEvaluated, 4);
        assert.ok(template.runs.reduce((total, run) => total + run[1], 0) <= 2880);
        close(template.demonstratedMinimum / FUEL_QUANTUM,
            Math.round(template.demonstratedMinimum / FUEL_QUANTUM));
        const proof = proveTemplate(template);
        assert.equal(proof.success.classification, "safe");
        assert.ok(proof.smallerFailure.exhaustionStep < proof.success.contactStep);
    }
    assert.match(ROUTE_DIGESTS.outputDigest, /^[0-9a-f]{64}$/);
});

test("every accepted touchdown margin settles to the same canonical cycle checkpoint", () => {
    for (const template of REFERENCE_TEMPLATES) {
        let context = null;
        for (const seed of [11, 41]) {
            for (let index = 0; index < 3; index += 1) {
                const originSite = createSiteForIndex(seed, index,
                    { canCollected: true, powered: true, nocStage: 7 });
                try {
                    const targetSite = instantiateTemplateSite(seed, index + 1, originSite, template);
                    context = { seed, originSite, targetSite };
                } catch {
                    // The finite cycles intentionally expose each template in one family only.
                }
            }
        }
        assert.ok(context, template.templateId);
        const { seed, originSite, targetSite } = context;
        for (const x of [originSite.platformLeft + 1.621, originSite.center,
            originSite.platformRight - 1.621]) {
            const contact = { x, y: originSite.platformTop, vx: 0, vy: -1,
                angle: 0, angularVelocity: 0 };
            const pose = checkpointPoseForContact(originSite, contact);
            assert.equal(pose.x, originSite.center);
            assert.equal(pose.y, originSite.platformTop);
            const proof = proveTemplate(template, { seed, originSite, targetSite, pose });
            assert.equal(proof.success.classification, "safe");
            assert.equal(proof.smallerFailure.allowance,
                template.demonstratedMinimum - FUEL_QUANTUM);
        }
    }
});

test("independent v7 derivation reproduces v6 bytes and all 48 selected replays", async () => {
    const directory = await mkdtemp(join(tmpdir(), "agw-route-test-"));
    const output = join(directory, "routes.json");
    const tool = join(ROOT, "tools/derive_lander_routes.mjs");
    await writeFile(join(directory, "lander_clear_faces.mjs"),
        await readFile(join(ROOT, "tools/lander_clear_faces.mjs"), "utf8"), "utf8");
    const geometry = join(ROOT, "tests/fixtures/lander-route-geometry-v6.json");
    const fixture = join(ROOT, "tests/fixtures/lander-route-derived-v6.json");
    const derivationStarted = performance.now();
    execFileSync(process.execPath, [tool, "--geometry", geometry, "--output", output,
        "--verify", fixture]);
    const derivationMilliseconds = performance.now() - derivationStarted;
    assert.ok(derivationMilliseconds < 10_000, `derivation took ${derivationMilliseconds} ms`);
    assert.equal(await readFile(output, "utf8"), await readFile(fixture, "utf8"));

    const derived = JSON.parse(await readFile(fixture, "utf8"));
    assert.equal(derived.schema, "agw-lander-route-derived/v6");
    assert.equal(derived.deriverVersion, "agw-lander-route-deriver/v7");
    assert.equal(derived.recipeVersion, "agw-lander-route-recipes/v3");
    assert.equal(derived.canonicalPoseDecimals, 9);
    assert.deepEqual(REFERENCE_TEMPLATES, derived.routes);
    assert.deepEqual(ROUTE_DIGESTS, {
        geometryDigest: derived.geometryDigest,
        outputDigest: derived.outputDigest,
        physicsDigest: derived.physicsDigest,
        worldDigest: derived.worldDigest,
    });
    assert.deepEqual(ROUTE_DIGESTS, {
        geometryDigest: "d0393f958f4a8b2657dbf21ef5184f40527be2c8c8d5cf860e6e81e3d2971fa2",
        outputDigest: "ac653b9bbb909ca39a2175c8b26065a81701d375dab14468f928b558638cea93",
        physicsDigest: "e08f8260b723dd245db88de9ae2cdbac54bf9a97cb0bed1b6f170eda362c48dc",
        worldDigest: "502d34c5c1a447b50eebcb458b40bbf8169f7efc7774edc1322af76ab9d0f215",
    });
    const { outputDigest, ...unsignedDerived } = derived;
    assert.equal(digest(unsignedDerived), outputDigest);
    assert.equal(derived.routes.reduce((total, route) =>
        total + route.combinationsEvaluated, 0), 24);
    assert.equal(derived.worldWitnesses.length, 24);
    assert.equal(derived.worldWitnesses.length * 2, 48);
    assert.deepEqual([...new Set(derived.worldWitnesses.map(({ descriptor }) =>
        descriptor.seed))], [11, 39, 41, STATIC_WORLD_SEED]);
    assert.deepEqual(derived.worldWitnesses.map(({ descriptor }) =>
        descriptor.legIndex), [0,1,2,3,4,5,0,1,2,3,4,5,0,1,2,3,4,5,0,1,2,3,4,5]);
    assert.equal(digest(derived.worldWitnesses), derived.worldDigest);

    for (const witness of derived.worldWitnesses) {
        const { descriptor } = witness;
        const template = REFERENCE_TEMPLATES.find((candidate) =>
            candidate.templateId === descriptor.templateId);
        const originSite = createSiteForIndex(descriptor.seed, descriptor.legIndex,
            { canCollected: true, powered: true, nocStage: 7 });
        const targetSite = instantiateTemplateSite(descriptor.seed,
            descriptor.legIndex + 1, originSite, template);
        assert.deepEqual({
            center: originSite.center,
            deckLevel: originSite.deckLevel,
            index: originSite.id,
            localMaximum: originSite.terrainLevel / 10,
            minimumDeckTop: originSite.platformTop,
            terrainLevel: originSite.terrainLevel,
            top: originSite.platformTop,
        }, descriptor.origin);
        assert.deepEqual({
            center: targetSite.center,
            deckLevel: targetSite.deckLevel,
            index: targetSite.id,
            localMaximum: targetSite.terrainLevel / 10,
            minimumDeckTop: targetSite.platformTop,
            terrainLevel: targetSite.terrainLevel,
            top: targetSite.platformTop,
        }, descriptor.target);
        assert.deepEqual(terrainVerticesForWindow(descriptor.seed, [originSite, targetSite],
            descriptor.terrainRange[0], descriptor.terrainRange[1]), descriptor.vertices);
        assert.equal(digest(descriptor), witness.digest);
        for (const feet of descriptor.terrainFeet) {
            for (const [x, y] of feet) assert.equal(y, terrainHeightAt(descriptor.seed, x));
        }
        for (const site of descriptor.sites) {
            close(site.platform.right - site.platform.left, 9.6);
            close(site.truss.right - site.truss.left, 18.8);
            assert.equal(site.supportColumns.length, 3);
            assert.ok(site.scaffoldMembers.length >= 41 && site.scaffoldMembers.length <= 95);
            assert.deepEqual(siteScaffoldMembers({
                seed: descriptor.seed,
                platformLeft: site.platform.left,
                platformRight: site.platform.right,
                platformTop: site.platform.top,
                platformBottom: site.platform.bottom,
            }), site.scaffoldMembers);
            close(site.clearApertures.terrainWedge.diameter,
                Math.hypot(1, 0.32407407407407407));
        }
    }
    assert.equal(spawnSync(process.execPath, [tool, "--bogus"]).status, 2);

    const authorityCopy = join(directory, "authority.json");
    const authorityBytes = await readFile(fixture, "utf8");
    await writeFile(authorityCopy, authorityBytes, "utf8");
    const samePath = spawnSync(process.execPath,
        [tool, "--geometry", geometry, "--output", authorityCopy, "--verify", authorityCopy],
        { encoding: "utf8" });
    assert.equal(samePath.status, 2);
    assert.match(samePath.stderr, /must resolve to different paths/);
    assert.equal(await readFile(authorityCopy, "utf8"), authorityBytes);

    const mutateTool = async (name, before, after) => {
        const changedTool = join(directory, name);
        const source = await readFile(tool, "utf8");
        const changed = source.replace(before, after);
        assert.notEqual(changed, source);
        await writeFile(changedTool, changed, "utf8");
        return spawnSync(process.execPath,
            [changedTool, "--geometry", geometry, "--output", output, "--verify", fixture],
            { encoding: "utf8" });
    };
    assert.equal((await mutateTool("seven-subdivisions.mjs",
        "const TERRAIN_SUBDIVISIONS = 8;", "const TERRAIN_SUBDIVISIONS = 7;")).status, 1);
    assert.equal((await mutateTool("wrong-deck.mjs",
        "deckLevel: slot.terrainLevel + 24", "deckLevel: slot.terrainLevel + 23")).status, 1);
    assert.equal((await mutateTool("weak-prefix.mjs",
        "[[1,90],[0,1],[1,20]", "[[2,90],[0,1],[1,20]")).status, 1);
    assert.equal((await mutateTool("open-face.mjs",
        "for (let bay = 0; bay < 12; bay += 1) {",
        "for (let bay = 0; bay < 12; bay += 1) { if (bay === 2) continue;")).status, 1);
    assert.equal((await mutateTool("changed-precision.mjs",
        "const REPLAY_POSE_DECIMAL_PLACES = 9;",
        "const REPLAY_POSE_DECIMAL_PLACES = 10;")).status, 1);

    const blockedGeometry = join(directory, "blocked-geometry.json");
    const blocked = JSON.parse(await readFile(geometry, "utf8"));
    blocked.terrain.localDeck.levelOffset = 23;
    await writeFile(blockedGeometry, `${JSON.stringify(blocked)}\n`, "utf8");
    assert.equal(spawnSync(process.execPath,
        [tool, "--geometry", blockedGeometry, "--output", output]).status, 1);
});
test("production route proof rejects a weak or inexact launch prefix before collision replay", () => {
    const template = REFERENCE_TEMPLATES[0];
    assert.throws(() => proveTemplate({ ...template, runs: [[2, 90], ...template.runs.slice(1)] }),
        /must begin with exact \[1,90\] launch request/);
    assert.throws(() => proveTemplate({ ...template, runs: [[1, 89], ...template.runs.slice(1)] }),
        /must begin with exact \[1,90\] launch request/);
});

test("safe target top is inclusive and epsilon excess is unsafe", () => {
    const model = createRun({ seed: 1 });
    const target = model.retainedSites[0];
    const previous = { x: target.center, y: target.platformTop + 0.5, vx: 2.2, vy: -3.6,
        angle: -18, angularVelocity: 26 };
    const next = { ...previous, y: target.platformTop + 0.2 };
    assert.equal(classifySweptContact(model, previous, next).kind, "safe");
    const limits = [
        ["vx", 2.2], ["vy", -3.6], ["angle", -18], ["angularVelocity", 26],
    ];
    for (const [field, limit] of limits) {
        const excess = limit + Math.sign(limit) * 1e-9;
        assert.equal(classifySweptContact(model, { ...previous, [field]: excess },
            { ...next, [field]: excess }).kind, "unsafe", `${field} beyond the inclusive limit must crash`);
    }
    for (const [field, value] of [["vx", -2.2], ["angle", 18], ["angularVelocity", -26]]) {
        assert.equal(classifySweptContact(model, { ...previous, [field]: value },
            { ...next, [field]: value }).kind, "safe", `${field} mirrors through absolute value`);
    }
    assert.equal(classifySweptContact(model, { ...previous, vy: 1e-9 }, { ...next, vy: 1e-9 }).kind,
        "unsafe", "upward contact is unsafe");
    const tangent = { x: target.center, y: target.platformTop, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, tangent, { ...tangent, x: tangent.x + 0.01 }).cause, "grazing");

    const isolated = { ...model, terrainVertices: [[-100, -20], [200, -20]] };
    for (const x of [target.platformLeft - 20, target.platformRight + 20]) {
        const clear = { x, y: target.platformTop + 0.1, vx: 0, vy: -1, angle: 0, angularVelocity: 0 };
        assert.equal(classifySweptContact(isolated, clear, { ...clear, y: target.platformTop - 0.1 }), null);
    }
});

test("closed unsafe geometry catches slopes, platform equality, scaffold, mast, and precedence", () => {
    const base = createRun({ seed: 1 });
    const site = base.retainedSites[0];
    const slopeModel = { ...base, retainedSites: [], targetSiteId: null, terrainVertices: [[0, 0], [10, 10]] };
    const slopePose = { x: 5, y: 6.62, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(slopeModel, slopePose, slopePose).cause, "terrain");
    const sidePose = { x: site.platformLeft - 1.6 - 0.02, y: site.platformTop - 0.1,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(base, sidePose, sidePose).cause, "platform");
    const riserPose = { x: site.center, y: site.platformTop - 0.6,
        vx: 0, vy: 0, angle: 180, angularVelocity: 0 };
    assert.equal(classifySweptContact(base, riserPose, riserPose).cause, "truss");
    const buildingLeft = site.platformRight + 2;
    const roof = site.platformTop + 7.2;
    const mastPose = { x: buildingLeft + 3.5, y: roof + 0.1,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(base, mastPose, mastPose).cause, "mast");
    const earlierUnsafe = { x: site.platformRight - 0.8, y: site.platformTop + 0.7,
        vx: 0, vy: -1, angle: 20, angularVelocity: 0 };
    const laterTop = { ...earlierUnsafe, y: site.platformTop + 0.1 };
    assert.equal(classifySweptContact(base, earlierUnsafe, laterTop).cause, "noc");
});

test("collision uses the exact retained terrain chain and rejects an absent authority", () => {
    const model = createRun({ seed: 1 });
    const [x, y] = model.terrainVertices[0];
    assert.equal(y, terrainHeightAt(model.seed, x));
    const cornerPose = { x, y: y - 0.25, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, cornerPose, cornerPose).cause, "terrain");
    assert.throws(() => classifySweptContact({ ...model, terrainVertices: null }, cornerPose, cornerPose),
        /requires retained terrain vertices/);
    const chain = Array.from({ length: 72 }, (_, index) => [index * 2, 3 + (index % 5) / 10]);
    const chainModel = { ...model, retainedSites: [], targetSiteId: null, terrainVertices: chain };
    for (let index = 1; index < chain.length; index += 1) {
        const x = (chain[index - 1][0] + chain[index][0]) / 2;
        const y = (chain[index - 1][1] + chain[index][1]) / 2;
        const pose = { x, y: y - 0.25, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
        assert.equal(classifySweptContact(chainModel, pose, pose).cause, "terrain");
    }
});

test("safe landing creates next target, adds uncapped award, and begins service", () => {
    let model = createRun({ seed: 1 });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
        angle: 0, angularVelocity: 0 }, fuel: 100 };
    const landed = stepFlight(model, { left: 0, right: 0 });
    assert.equal(landed.state, "landed");
    assert.equal(landed.completedSites, 1);
    assert.equal(landed.retainedSites.length, 2);
    assert.equal(landed.retainedSites[0].canCollected, true);
    assert.ok(landed.fuel > 100);
    close(landed.refuelRatio, 1.5);
    assert.equal(landed.targetSiteId, 1);
    assert.ok(landed.targetRouteProof);
});

test("accepted touchdown margins award from the centered immutable checkpoint", () => {
    for (const side of ["left", "right"]) {
        let model = createRun({ seed: 1, reducedMotion: true });
        const target = model.retainedSites[0];
        const x = side === "left" ? target.platformLeft + 1.721 : target.platformRight - 1.721;
        model = { ...model, pose: { x, y: target.platformTop + 0.001,
            vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
        model = stepFlight(model, { left: 0, right: 0 });
        assert.equal(model.state, "launching"); assert.equal(model.completedSites, 1);
        assert.equal(model.pose.x, target.center); assert.equal(model.checkpoint.pose.x, target.center);
        assert.equal(model.targetRouteProof.success.classification, "safe");
        assert.ok(model.targetRouteProof.smallerFailure.exhaustionStep < model.targetRouteProof.success.contactStep);
    }
});

test("service powers the NOC, freezes a checkpoint, and launches with actual fuel", () => {
    let model = createRun({ seed: 1, reducedMotion: true });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
        angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "launching");
    assert.equal(model.retainedSites[0].powered, true);
    assert.ok(Object.isFrozen(model.checkpoint));
    const fuel = model.fuel;
    for (let index = 0; index < 90; index += 1) model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "launching"); assert.equal(model.fuel, fuel);
    for (let index = 0; index < 90 && model.state === "launching"; index += 1) {
        model = stepFlight(model, { left: 0.72, right: 0.72 });
    }
    assert.equal(model.state, "flying");
    assert.ok(model.fuel < fuel);
});

test("manual launch ignores only its rising start top and keeps other collisions", () => {
    let model = createRun({ seed: 1, reducedMotion: true });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
        angle: 0, angularVelocity: 0 } };
    const launching = stepFlight(model, { left: 0, right: 0 });
    assert.equal(launching.state, "launching");
    assert.deepEqual(stepFlight({ ...launching, fuel: 0 }, { left: 0, right: 0 }),
        { ...launching, fuel: 0, commanded: { left: 0, right: 0, vectorAngle: 0 } });
    const active = launching.retainedSites.find((site) => site.id === launching.activeSiteId);
    const side = { ...launching, pose: { ...launching.pose, x: active.platformLeft + 1.6, vy: 1 } };
    assert.equal(stepFlight({ ...side, launchStarted: true }, { left: 0.72, right: 0.72 }).failureCause, "platform");
    const riser = { ...launching, pose: { ...launching.pose, x: active.center,
        y: active.platformTop - 0.6, vy: 1, angle: 180 } };
    assert.equal(stepFlight({ ...riser, launchStarted: true }, { left: 0.72, right: 0.72 }).failureCause, "truss");
    const buildingLeft = active.platformRight + 2;
    const noc = { ...launching, pose: { ...launching.pose, x: buildingLeft + 3.5,
        y: active.platformTop + 1, vy: 1 } };
    assert.equal(stepFlight({ ...noc, launchStarted: true }, { left: 0.72, right: 0.72 }).failureCause, "noc");
});

test("destroy restores the pristine static DOM from active and failed controllers", async () => {
    const { LanderGameController } = await controllerClasses();
    for (const terminal of [false, true]) {
        const fixture = controllerFixture();
        const controller = new LanderGameController(fixture.root);
        const elements = fixture.elements;
        elements["lander-scene-shell"].setAttribute("role", "application");
        elements["lander-outcome"].hidden = false; elements["lander-controls-rail"].hidden = false;
        elements["lander-exit"].disabled = false;
        elements["lander-status"].textContent = terminal ? FAILURE_STATUS : "Mission underway.";
        if (terminal) { elements["lander-restart"].hidden = false; elements["lander-restart"].disabled = false; }
        fixture.root.style.setProperty("--lander-x", "999px");
        assert.equal(focusable(elements["lander-exit"]), true);
        controller.destroy();
        const restored = fixture.root.replacement;
        assert.equal(controller.model.state, "preflight");
        assert.ok(restored); assert.equal(restored.style.properties.size, 0);
        assert.equal(restored.elements["lander-start"].hidden, true);
        assert.equal(restored.elements["lander-start"].disabled, true);
        assert.equal(restored.elements["lander-outcome"].hidden, true);
        assert.equal(restored.elements["lander-exit"].disabled, true);
        assert.equal(restored.elements["lander-restart"].hidden, true);
        assert.equal(restored.elements["lander-restart"].disabled, true);
        assert.equal(focusable(restored.elements["lander-exit"]), false);
        assert.equal(restored.elements["lander-status"].textContent, "");
    }
});

test("render clears stale live status on crash entry", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture();
    const controller = new LanderGameController(fixture.root);
    fixture.elements["lander-status"].textContent = "Mission underway.";
    controller.model = { ...createPreflightModel(), state: "crashing", status: "", crash: null };
    controller.render();
    assert.equal(fixture.elements["lander-status"].textContent, "");
    controller.destroy();
});

test("short pointer tap survives automatic lost capture for its full pulse", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = createRun({ seed: 1 });
    const stage = fixture.elements["lander-scene-stage"];
    stage.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 100, clientY: 50, timeStamp: 0 });
    stage.dispatchEvent({ type: "pointerup", pointerId: 7, isPrimary: true, button: 0,
        clientX: 103, clientY: 52, timeStamp: 50 });
    assert.equal(stage.hasPointerCapture(7), false);
    assert.equal(controller.pointer, null); assert.deepEqual(controller.collectivePulse,
        { active: true, token: 1, deadline: 140 });
    assert.notEqual(controller.pulseTimer, null);
    assert.ok(controller.clock.queue.some((edge) => edge.token === 1 && edge.left === 0.72 && edge.right === 0.72));
    assert.equal(controller.clock.queue.at(-1).physical.collectivePulse.token, 1);
    controller.endCollectivePulse(140);
    assert.deepEqual(controller.collectivePulse,
        { active: false, token: null, deadline: null });
    assert.equal(controller.pulseTimer, null);
    assert.equal(controller.clock.queue.at(-1).timestamp, 140);
    assert.equal(controller.clock.queue.at(-1).left, 0);
    assert.equal(controller.clock.queue.at(-1).physical.collectivePulse.active, false);
    controller.destroy();
});

test("a rapid reused pointer supersedes the old pulse and ignores its stale deadline", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = createRun({ seed: 1 });
    const stage = fixture.elements["lander-scene-stage"];
    stage.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 100, clientY: 50, timeStamp: 0 });
    stage.dispatchEvent({ type: "pointerup", pointerId: 7, isPrimary: true, button: 0,
        clientX: 102, clientY: 50, timeStamp: 20 });
    assert.equal(controller.collectivePulse.token, 1);
    const firstTimer = controller.pulseTimer;
    stage.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 200, clientY: 50, timeStamp: 60 });
    assert.equal(controller.collectivePulse.active, false); assert.equal(controller.pulseTimer, null);
    assert.equal(controller.pointer.token, 2); assert.notEqual(firstTimer, controller.pulseTimer);
    assert.deepEqual(controller.clock.queue.slice(-2).map(({ timestamp, left, right, token }) =>
        ({ timestamp, left, right, token })), [
        { timestamp: 60, left: 0, right: 0, token: 1 },
        { timestamp: 60, left: 0.72, right: 0.72, token: 2 },
    ]);
    assert.equal(controller.pointer.token, 2);
    assert.deepEqual(controller.pointerInput, { left: 0.72, right: 0.72 });
    stage.dispatchEvent({ type: "pointerup", pointerId: 7, isPrimary: true, button: 0,
        clientX: 202, clientY: 50, timeStamp: 160 });
    assert.equal(controller.collectivePulse.token, 2);
    assert.notEqual(controller.pulseTimer, null);
    controller.endCollectivePulse(200);
    assert.equal(controller.collectivePulse.active, false); assert.equal(controller.pulseTimer, null);
    assert.deepEqual([controller.clock.queue.at(-1).timestamp, controller.clock.queue.at(-1).left,
        controller.clock.queue.at(-1).right], [200, 0, 0]);
    assert.equal(controller.clock.queue.at(-1).physical.collectivePulse.active, false);
    controller.destroy();
});

test("an overdue pulse ends at its deadline before a reused pointer starts", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = createRun({ seed: 1 });
    const stage = fixture.elements["lander-scene-stage"];
    stage.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 100, clientY: 50, timeStamp: 0 });
    stage.dispatchEvent({ type: "pointerup", pointerId: 7, isPrimary: true, button: 0,
        clientX: 102, clientY: 50, timeStamp: 20 });
    const delayedTimer = controller.pulseTimer;
    stage.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 200, clientY: 50, timeStamp: 200 });
    assert.deepEqual(controller.clock.queue.slice(-2).map(({ timestamp, left, right, token }) =>
        ({ timestamp, left, right, token })), [
        { timestamp: 140, left: 0, right: 0, token: 1 },
        { timestamp: 200, left: 0.72, right: 0.72, token: 2 },
    ]);
    assert.equal(controller.pointer.token, 2); assert.equal(controller.pulseTimer, null);
    assert.notEqual(delayedTimer, controller.pulseTimer);
    assert.equal(controller.pointer.token, 2);
    assert.deepEqual(controller.pointerInput, { left: 0.72, right: 0.72 });
    controller.destroy();
});

test("a tap released after its minimum completes immediately without leaving thrust active", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = createRun({ seed: 1 });
    const stage = fixture.elements["lander-scene-stage"];
    stage.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 100, clientY: 50, timeStamp: 0 });
    stage.dispatchEvent({ type: "pointerup", pointerId: 7, isPrimary: true, button: 0,
        clientX: 102, clientY: 50, timeStamp: 160 });
    assert.equal(controller.collectivePulse.active, false); assert.equal(controller.pulseTimer, null);
    assert.deepEqual(controller.clock.queue.slice(-2).map(({ timestamp, left, right, token }) =>
        ({ timestamp, left, right, token })), [
        { timestamp: 160, left: 0.72, right: 0.72, token: 1 },
        { timestamp: 160, left: 0, right: 0, token: 1 },
    ]);
    controller.destroy();
});

test("live reduced-motion changes persist into crash behavior in both directions", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = createRun({ seed: 7, reducedMotion: false });
    controller.onMotionChange({ matches: true });
    assert.equal(controller.model.reducedMotion, true);
    const impact = { ...controller.model, pose: { x: 12, y: MAX_PLAYABLE_Y, vx: 0, vy: 10,
        angle: 0, angularVelocity: 0 } };
    assert.equal(stepFlight(impact, { left: 0, right: 0 }).state, "failed");
    controller.model = createRun({ seed: 7, reducedMotion: true });
    controller.onMotionChange({ matches: false });
    assert.equal(controller.model.reducedMotion, false);
    const animated = { ...controller.model, pose: impact.pose };
    assert.equal(stepFlight(animated, { left: 0, right: 0 }).state, "crashing");
    controller.destroy();
});

test("visibility teardown restores the complete zero command and neutral plume vector", async () => {
    const { LanderGameController } = await controllerClasses();
    document.hidden = false;
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = { ...createRun({ seed: 1 }),
        commanded: { left: 0.2125, right: 0.5875, vectorAngle: -30 } };
    controller.render();
    assert.equal(controller.root.style.properties.get("--thrust-vector-angle"), "-30deg");
    document.hidden = true;
    controller.onVisibilityChange(); controller.render();
    assert.deepEqual(controller.model.commanded, { left: 0, right: 0, vectorAngle: 0 });
    assert.equal(controller.root.style.properties.get("--thrust-vector-angle"), "0deg");
    controller.destroy();
});

test("intermediate NOC battery stages project from model to the retained site DOM", async () => {
    let model = createRun({ seed: 1 });
    const site = model.retainedSites[0];
    model = { ...model, pose: { x: site.center, y: site.platformTop + 0.001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    model = advanceMissionSequence(model, 0.3);
    model = advanceMissionSequence(model, 0.9);
    assert.equal(advanceMissionSequence(model, 0.199).nocStage, 0);
    assert.equal(advanceMissionSequence(model, 0.2).nocStage, 1);
    assert.equal(advanceMissionSequence(model, 0.4).nocStage, 2);
    assert.equal(advanceMissionSequence(model, 0.6).nocStage, 3);
    assert.equal(advanceMissionSequence(model, 0.8).nocStage, 4);
    assert.equal(advanceMissionSequence(model, 0.999).state, "powering");
    assert.equal(advanceMissionSequence(model, 1).nocStage, 5);
    assert.equal(advanceMissionSequence(model, 1.2).nocStage, 6);
    assert.equal(advanceMissionSequence(model, 1.4).state, "launching");
    assert.equal(advanceMissionSequence(model, 1.4).retainedSites[0].powered, true);
    model = updateRetention(advanceMissionSequence(model, 0.41));
    const active = model.retainedSites.find((candidate) => candidate.id === model.activeSiteId);
    assert.equal(model.state, "powering"); assert.equal(model.nocStage, 2); assert.equal(active.nocStage, 2);
    close(siteStructure(active).noc.bottom, active.platformBottom);
    const buildingLeft = active.platformRight + 2;
    const foundationPose = { x: buildingLeft + 1.6, y: active.platformBottom + 0.2,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, foundationPose, foundationPose).cause, "noc");

    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = model; controller.render();
    const group = fixture.elements["site-layer"].querySelector(`[data-site-id="${active.id}"]`);
    assert.equal(group.dataset.nocStage, "2");
    const structure = siteStructure(active);
    assert.equal(group.querySelector(".noc-building").attributes.get("d"),
        `M${structure.buildingLeft * 10} ${548 - active.platformBottom * 10}` +
        `V${548 - structure.roof * 10}h70V${548 - active.platformBottom * 10}Z`);
    controller.model = updateRetention(advanceMissionSequence(model, 0.2)); controller.render();
    assert.equal(group.dataset.nocStage, "3");
    controller.destroy();
});

test("static and dynamic scaffold, battery, signal, and collider geometry stay identical", async () => {
    const model = updateRetention(createRun({ seed: STATIC_WORLD_SEED }));
    const active = model.retainedSites[0];
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = model; controller.render();
    const group = fixture.elements["site-layer"].querySelector(`[data-site-id="${active.id}"]`);
    const template = await readFile(join(ROOT, "templates/lander-game.html"), "utf8");
    const staticSupport = template.match(/class="site-scaffold"[^>]+d="([^"]+)"/);
    assert.ok(staticSupport);
    const support = group.querySelector(".site-scaffold");
    assert.equal(support.attributes.get("d"), staticSupport[1]);
    const left = active.platformLeft * 10; const right = active.platformRight * 10;
    const top = 548 - active.platformTop * 10; const bottom = 548 - active.platformBottom * 10;
    const structure = siteStructure(active);
    assert.equal(support.attributes.get("d").match(/M/g)?.length, siteScaffoldMembers(active).length);
    assert.doesNotMatch(support.attributes.get("d"), /Z/);
    const riserPose = { x: active.platformLeft, y: active.platformBottom - 0.4,
        vx: 0, vy: 0, angle: 180, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, riserPose, riserPose).cause, "truss");

    const battery = group.querySelector(".noc-battery");
    assert.deepEqual(battery.children.map((node) => node.className ?? null),
        [null, "battery-bar battery-bar-1", "battery-bar battery-bar-2",
            "battery-bar battery-bar-3", "battery-bar battery-bar-4"]);
    const buildingLeft = right + 20; const roof = 548 - structure.roof * 10;
    const rectangle = battery.children[0];
    assert.deepEqual(["x","y","width","height"].map((name) => rectangle.attributes.get(name)),
        [buildingLeft + 24, roof + 16, 22, 40].map(String));
    assert.equal(rectangle.attributes.has("rx"), false);
    const barTops = [46, 38, 30, 22];
    for (let index = 1; index <= 4; index += 1) {
        assert.equal(battery.children[index].attributes.get("d"),
            `M${buildingLeft + 29} ${roof + barTops[index - 1]}h12v5h-12Z`);
    }
    assert.equal(group.children.filter((node) => node.className?.includes("antenna-signal")).length, 3);
    controller.destroy();
});

test("vacuum crash has exactly eight deterministic fragments and finite duration", () => {
    let model = createRun({ seed: 7 });
    model = { ...model, pose: { x: 30, y: 55.99, vx: 0, vy: 10, angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "crashing");
    assert.equal(model.crash.fragments.length, 8);
    assert.equal(model.crashOrdinal, 1);
    model = advanceMissionSequence(model, 0.6);
    assert.equal(model.state, "failed");
    assert.equal(model.status, FAILURE_STATUS);
});

test("reduced motion crashes atomically with zero debris", () => {
    let model = createRun({ seed: 7, reducedMotion: true });
    model = { ...model, pose: { x: 30, y: 55.99, vx: 0, vy: 10, angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "failed");
    assert.equal(model.crash, null);
});

test("checkpoint restart restores post-award fuel without duplicating progress", () => {
    const initial = createRun({ seed: 1, reducedMotion: true });
    const initialRetry = transitionMission({ ...initial, state: "failed", fuel: 0, pose: { ...initial.pose, x: 9 },
        crashOrdinal: 3 }, "RESTART");
    assert.deepEqual({ ...initialRetry, crashOrdinal: initial.crashOrdinal }, initial);
    let model = createRun({ seed: 1, reducedMotion: true });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
        angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    const expected = structuredClone(model.checkpoint);
    model = { ...model, state: "failed", fuel: 0 };
    const restarted = transitionMission(model, "RESTART");
    assert.equal(restarted.state, "launching");
    for (const [field, value] of Object.entries(expected)) assert.deepEqual(restarted[field], value, field);
    assert.equal(restarted.retainedSites[0].canCollected, true);
});

test("fixed scheduler is frame-rate equivalent and bounds its input queue", () => {
    for (const rate of [30, 60, 120]) {
        let clock = createSimulationClock(0);
        let model = createRun({ seed: 1 });
        for (let frame = 1; frame <= rate; frame += 1) {
            const result = advanceSimulation(clock, model, (frame * 1000) / rate);
            clock = result.clock; model = result.model;
        }
        close(model.pose.x, 30.8); close(model.pose.y, 30.0875); close(model.pose.vy, -3.4);
    }
    let clock = createSimulationClock(0);
    for (let index = 0; index < 64; index += 1) clock = enqueueInputEdge(clock, { timestamp: 1, left: index % 2, right: 0 });
    const physical = Object.freeze({ heldCodes: Object.freeze(["Space"]),
        pointer: Object.freeze({ active: true, id: 7, anchorX: 10, currentX: 90, pulseDeadline: null }) });
    clock = enqueueInputEdge(clock, { timestamp: 1, left: 1, right: 0.72, token: 7, physical });
    assert.equal(clock.queue.length, 1);
    assert.equal(clock.queue[0].snapshot, true);
    assert.equal(clock.queue[0].token, 7); assert.equal(clock.queue[0].physical, physical);
    clock = removeQueuedInputEdges(clock, 7);
    clock = enqueueInputEdge(clock, { timestamp: 2, left: 0.72, right: 0.72,
        physical: { heldCodes: ["Space"], pointer: { active: false } } });
    const continued = advanceSimulation(clock, createRun({ seed: 1 }), 1000 / 120);
    assert.deepEqual(continued.clock.input, { left: 0.72, right: 0.72 });
});

test("cue, preflight transition, and retention remain bounded", () => {
    assert.deepEqual(createCueState(true), { state: "settled", elapsed: 0 });
    const run = updateRetention(transitionMission(createPreflightModel(), "START", { seed: 1 }));
    assert.equal(run.state, "flying");
    assert.ok(run.retainedChunks.length <= 10);
    assert.ok(run.retainedSites.length <= 3);
    assert.ok(run.terrainVertices.length > 0);
    close(STEP_SECONDS, 1 / 120);
});

test("retention and DOM reconciliation change only at bounded window keys", async () => {
    let model = updateRetention(createRun({ seed: 1 }));
    const vertices = model.terrainVertices; const key = model.retentionKey;
    model = updateRetention({ ...model, pose: { ...model.pose, x: model.pose.x + 1 } });
    assert.equal(model.retentionKey, key); assert.equal(model.terrainVertices, vertices);
    const changed = updateRetention({ ...model, pose: { ...model.pose, x: model.pose.x + 25 } });
    assert.notEqual(changed.retentionKey, key); assert.notEqual(changed.terrainVertices, vertices);

    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = model; controller.render();
    const terrainNode = fixture.elements["terrain-layer"].children[0];
    const writes = terrainNode.setCount;
    controller.model = updateRetention({ ...model, pose: { ...model.pose, x: model.pose.x + 1 } });
    controller.render();
    assert.equal(terrainNode.setCount, writes);
    const crossing = Object.freeze([[0, 2], [40, 4], [64, 4], [100, 3]].map(Object.freeze));
    controller.model = { ...model, terrainVertices: crossing, retainedChunks: [0, 1], retainedSites: [],
        retentionKey: "crossing-shelf" };
    controller.render();
    assert.deepEqual(fixture.elements["terrain-layer"].children.map((node) => node.attributes.get("d")), [
        terrainFillPath(terrainVerticesForRange(crossing, 0, 100)),
        terrainPath(terrainVerticesForRange(crossing, 0, 100)),
    ]);
    controller.model = changed; controller.render();
    assert.notEqual(controller.worldWindowKey, key);
    controller.root.style.setProperty("--crash-x", "900px");
    controller.exit();
    assert.equal(controller.model.state, "preflight"); assert.equal(controller.worldWindowKey, null);
    assert.equal(fixture.elements["terrain-layer"].children.length, 0);
    assert.equal(fixture.elements["site-layer"].children.length, 0);
    assert.equal(controller.root.style.properties.has("--crash-x"), false);
    assert.equal(fixture.elements["lander-start"].hidden, false);
    controller.destroy();
});

test("worst-case world projection stays within eighty descendants during a crash", async () => {
    let model = createRun({ seed: 1 });
    const sites = [model.retainedSites[0]];
    for (let id = 1; id < 3; id += 1) {
        const template = selectTemplate(model.seed, id, sites.at(-1), REFERENCE_TEMPLATE_CATALOG);
        sites.push(instantiateTemplateSite(model.seed, id, sites.at(-1), template));
    }
    model = updateRetention({ ...model, pose: { ...model.pose, x: sites[1].center }, retainedSites: sites,
        activeSiteId: 1, targetSiteId: 2 });
    assert.equal(model.retainedChunks.length, 5); assert.equal(model.retainedSites.length, 3);
    const fragments = Array.from({ length: 8 }, (_, id) => ({ id, x: model.pose.x, y: model.pose.y,
        vx: id / 10, vy: id / 20, angularVelocity: id, color: "#292b30" }));
    model = { ...model, state: "crashing", crash: { pose: model.pose, fragments }, sequenceSeconds: 0.1 };

    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    const world = new FakeElement(); const missionLander = new FakeElement();
    missionLander.append(...Array.from({ length: 4 }, () => new FakeElement()));
    fixture.elements["mission-agent"].append(new FakeElement(), new FakeElement());
    world.append(fixture.elements["terrain-layer"], fixture.elements["site-layer"],
        fixture.elements["debris-layer"], missionLander, fixture.elements["mission-agent"]);
    controller.model = model; controller.render();
    assert.equal(fixture.elements["terrain-layer"].children.length, 2);
    assert.equal(fixture.elements["site-layer"].children.length, 3);
    assert.equal(fixture.elements["debris-layer"].children.length, 8);
    assert.equal(descendantCount(world), 75);
    assert.ok(descendantCount(world) <= 80);
    controller.destroy();
});

test("four seeded 100-site powered missions keep lifecycle and generation timing bounded", () => {
    const durations = [];
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let model = updateRetention(createRun({ seed, reducedMotion: true }));
        const cycle = terrainCycleForSeed(seed);
        for (let completed = 0; completed < 100; completed += 1) {
            if (model.state === "launching") {
                for (let step = 0; step < 90 && model.state === "launching"; step += 1) {
                    model = updateRetention(stepFlight(model, { left: 0.72, right: 0.72 }));
                }
            }
            const target = model.retainedSites.find((site) => site.id === model.targetSiteId);
            model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
                angle: 0, angularVelocity: 0 } };
            const started = performance.now();
            model = updateRetention(stepFlight(model, { left: 0, right: 0 }));
            durations.push(performance.now() - started);
            assert.equal(model.state, "launching");
            const powered = model.retainedSites.find((site) => site.id === model.activeSiteId);
            assert.deepEqual([powered.powered, powered.nocStage], [true, 7]);
            assert.ok(model.checkpoint);
            assert.ok(model.retainedSites.length <= 3);
            assert.ok(model.retainedChunks.length <= 5);
            assert.ok(model.terrainVertices.length <= 72);
            assert.equal(terrainSiteForIndex(seed, completed + 3).center -
                terrainSiteForIndex(seed, completed).center, cycle.blockWidth);
        }
        assert.equal(model.completedSites, 100);
        assert.ok(model.refuelRatio >= 1);
    }
    durations.sort((left, right) => left - right);
    const p95 = durations[Math.ceil(durations.length * 0.95) - 1];
    assert.ok(p95 < 25, `generation p95 was ${p95} ms`);
    assert.ok(durations.at(-1) < 50, `generation maximum was ${durations.at(-1)} ms`);
});
