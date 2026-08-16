import assert from "node:assert/strict";
import test from "node:test";

import { readFile } from "node:fs/promises";

import { exactRootNumber, exactSegmentContact } from "../static/lander-collision.js";
import { createRun, stepFlight } from "../static/lander-model.js";
import {
    MAX_SITE_INDEX,
    MIN_SITE_INDEX,
    STATIC_WORLD_SEED,
    TERRAIN_PROFILES,
    WORLD_MAX_X,
    WORLD_MIN_X,
    cameraLeftForPose,
    classifySweptContact,
    createSiteForIndex,
    hullForPose,
    normalizeDegrees,
    terrainHeightAt,
    terrainProfileForBlock,
    worldTermini,
} from "../static/lander-world.js";

const ZERO = Object.freeze({ left: 0, right: 0 });
const ROOT = new URL("../", import.meta.url);

test("collision is one separately shipped leaf module rather than build-composed world bytes", async () => {
    const [collision, world, build] = await Promise.all(
        ["static/lander-collision.js", "static/lander-world.js", "build.py"].map((path) =>
            readFile(new URL(path, ROOT), "utf8"),
        ),
    );
    assert.doesNotMatch(collision, /^\s*import\s/m);
    assert.equal(world.match(/from "\.\/lander-collision\.js"/g)?.length, 1);
    assert.doesNotMatch(world, /COLLISION_DOUBLE_VIEW/);
    assert.match(build, /Path\("static\/lander-collision\.js"\)/);
    assert.doesNotMatch(build, /_compose_lander_world|lander-collision-source/);
});

test("production and independent exact roots preserve crossing, tangency, and collinear overlap", () => {
    const vectors = [
        {
            left: [
                { x: -2, y: 0 },
                { x: -1, y: 0 },
                { x: -1, y: 1 },
                { x: -2, y: 1 },
            ],
            right: [
                { x: 2, y: 0 },
                { x: 3, y: 0 },
                { x: 3, y: 1 },
                { x: 2, y: 1 },
            ],
            segment: [
                { x: 0, y: -1 },
                { x: 0, y: 2 },
            ],
            time: 0.25,
        },
        {
            left: [
                { x: 0, y: 0 },
                { x: 0, y: -1 },
            ],
            right: [
                { x: 1, y: 0 },
                { x: 0, y: 0 },
            ],
            segment: [
                { x: 0.25, y: -0.25 },
                { x: 0.26, y: -0.24 },
            ],
            time: 0.5,
        },
        {
            left: [
                { x: -2, y: 0 },
                { x: -1, y: 0 },
                { x: -1, y: 1 },
                { x: -2, y: 1 },
            ],
            right: [
                { x: 1, y: 0 },
                { x: 2, y: 0 },
                { x: 2, y: 1 },
                { x: 1, y: 1 },
            ],
            segment: [
                { x: 0, y: 0 },
                { x: 1, y: 0 },
            ],
            time: 1 / 3,
        },
    ];
    for (const vector of vectors) {
        assert.equal(exactRootNumber(exactSegmentContact(vector.left, vector.right, vector.segment)), vector.time);
    }
});

test("exact shared-root contact uses feature priority and margin remains detection-only", () => {
    const model = {
        ...createRun({ seed: 11 }),
        retainedSites: [],
        targetSiteId: null,
        terrainAuthority: "context",
        terrainVertices: [],
    };
    const previous = { x: -4, y: 10, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    const next = { ...previous, x: 4 };
    const shared = [
        { x: 0, y: 9 },
        { x: 0, y: 18 },
    ];
    const contact = classifySweptContact(model, previous, next, {
        angularTravel: 0,
        features: [
            { cause: "column", priority: 2, segment: shared },
            { cause: "noc", priority: 0, segment: shared },
        ],
    });
    assert.equal(contact.kind, "unsafe");
    assert.equal(contact.cause, "noc");
    assert.equal(contact.time, 0.3);

    const stationary = { x: 0, y: 10, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(
        classifySweptContact(model, stationary, stationary, {
            angularTravel: 0,
            features: [
                {
                    cause: "terrain",
                    priority: 4,
                    segment: [
                        { x: -2, y: 9.99 },
                        { x: 2, y: 9.99 },
                    ],
                },
            ],
        }),
        null,
    );
});

test("context terrain and midpoint streaming are authoritative", () => {
    const empty = {
        ...createRun({ seed: 11 }),
        retainedSites: [],
        targetSiteId: null,
        terrainAuthority: "context",
        terrainVertices: [
            [-10, 0],
            [10, 0],
        ],
    };
    const previous = { x: 0, y: 1, vx: 0, vy: -2, angle: 0, angularVelocity: 0 };
    const next = { ...previous, y: -1 };
    assert.equal(classifySweptContact(empty, previous, next, { angularTravel: 0 }).cause, "terrain");
    assert.equal(
        classifySweptContact(
            {
                ...empty,
                terrainVertices: [
                    [-10, -100],
                    [10, -100],
                ],
            },
            previous,
            next,
            { angularTravel: 0 },
        ),
        null,
    );

    const longNext = { ...previous, x: 2, y: previous.y };
    const instrumentation = {};
    assert.equal(
        classifySweptContact(empty, previous, longNext, {
            angularTravel: 0,
            instrumentation,
            features: [
                {
                    cause: "terrain",
                    priority: 4,
                    segment: [
                        { x: -2, y: 7.51 },
                        { x: 4, y: 7.51 },
                    ],
                },
            ],
        }),
        null,
    );
    assert.equal(instrumentation.maxStack, 8);
});

test("solid-below terrain presence is downward-unbounded at time zero", () => {
    const procedural = { ...createRun({ seed: 11 }), retainedSites: [], targetSiteId: null };
    const radius = Math.hypot(1.6, 6.5);
    const angle = (Math.atan2(1.6, 6.5) * 180) / Math.PI;
    const ground = terrainHeightAt(11, -80);
    const buried = { x: -80, y: ground - radius - 0.03, vx: 0, vy: 0, angle, angularVelocity: 0 };
    const proceduralContact = classifySweptContact(
        procedural,
        buried,
        { ...buried, y: buried.y + 0.04, angle: buried.angle + 10 },
        { angularTravel: 10 },
    );
    assert.equal(radius, 6.694027188471824);
    assert.equal(angle, 13.828650972280156);
    assert.equal(ground, 4.880000000000001);
    assert.equal(proceduralContact.kind, "unsafe");
    assert.equal(proceduralContact.cause, "terrain");
    assert.equal(proceduralContact.time, 0);
    assert.equal(proceduralContact.pose.y, -1.8440271884718233);
    assert.equal(proceduralContact.pose.angle, 13.828650972280116);

    const context = {
        ...procedural,
        terrainAuthority: "context",
        terrainVertices: [],
    };
    const segment = [
        { x: -1, y: radius + 0.03 },
        { x: 2, y: radius + 0.03 },
    ];
    assert.equal(segment[0].y, 6.724027188471824);
    const fixedPrevious = { ...buried, x: 0, y: 0 };
    const fixedNext = { ...fixedPrevious, x: 0.019 };
    const fixedContact = classifySweptContact(context, fixedPrevious, fixedNext, {
        angularTravel: 0,
        features: [{ cause: "terrain", priority: 4, segment, solidBelow: true }],
    });
    assert.equal(fixedContact.kind, "unsafe");
    assert.equal(fixedContact.cause, "terrain");
    assert.equal(fixedContact.time, 0);
    assert.equal(
        classifySweptContact(context, fixedPrevious, fixedNext, {
            angularTravel: 0,
            features: [{ cause: "terrain", priority: 4, segment }],
        }),
        null,
    );
});

test("seeded superprofiles preserve the exact closed band without a short silhouette period", () => {
    assert.deepEqual(Object.keys(TERRAIN_PROFILES), ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]);
    for (const samples of Object.values(TERRAIN_PROFILES)) {
        assert.equal(samples.length, 33);
        assert.equal(samples[0], 0.35);
        assert.equal(samples.at(-1), 0.35);
        assert.ok(samples.every((height) => height >= 0.1 && height <= 0.6));
        const grades = samples.slice(1).map((height, index) => (height - samples[index]) / 0.25);
        assert.ok(grades.every((grade) => Math.abs(grade) <= 0.6 + 1e-12));
        assert.ok(grades.slice(1).every((grade, index) => Math.abs(grade - grades[index]) <= 1.2 + 1e-12));
    }
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        const ids = Array.from({ length: 128 }, (_, offset) => terrainProfileForBlock(seed, offset - 64).profile);
        assert.equal(new Set(ids).size, 8);
        assert.ok(ids.slice(1).every((id, index) => id !== ids[index]));
        for (let period = 1; period <= 64; period += 1) {
            assert.ok(ids.some((id, index) => index < period || id !== ids[index - period]));
        }
    }
});

test("finite terrain, sites, camera, and physical rails share one closed world", () => {
    assert.equal(createSiteForIndex(11, MIN_SITE_INDEX).id, MIN_SITE_INDEX);
    assert.equal(createSiteForIndex(11, MAX_SITE_INDEX).id, MAX_SITE_INDEX);
    assert.throws(() => createSiteForIndex(11, MIN_SITE_INDEX - 1), RangeError);
    assert.throws(() => createSiteForIndex(11, MAX_SITE_INDEX + 1), RangeError);
    assert.throws(() => terrainHeightAt(11, WORLD_MIN_X - Number.EPSILON * Math.abs(WORLD_MIN_X)), RangeError);
    assert.throws(() => terrainHeightAt(11, WORLD_MAX_X + Number.EPSILON * Math.abs(WORLD_MAX_X)), RangeError);
    assert.deepEqual(worldTermini(11), {
        left: { foot: 13.2, x: WORLD_MIN_X },
        right: { foot: 13.2, x: WORLD_MAX_X },
        width: 0.2,
    });
    assert.equal(cameraLeftForPose({ x: WORLD_MIN_X - 20 }), WORLD_MIN_X - 0.2);
    assert.equal(cameraLeftForPose({ x: WORLD_MAX_X + 20 }), WORLD_MAX_X - 100 + 0.2);
});

test("former ceiling and speed thresholds stay ballistic until physical contact", () => {
    const run = createRun({ seed: 11, reducedMotion: true });
    const free = stepFlight(
        { ...run, fuel: 0, pose: { x: 140, y: 100, vx: 500, vy: 10, angle: 0, angularVelocity: 0 } },
        { left: 0.72, right: 0.72 },
    );
    assert.equal(free.state, "flying");
    assert.equal(free.failureCause, null);
    assert.equal(free.fuel, 0);

    let falling = { ...run, fuel: 0, pose: { x: 70, y: 35, vx: 0, vy: 0, angle: 0, angularVelocity: 0 } };
    for (let step = 0; step < 1000 && falling.state === "flying"; step += 1) falling = stepFlight(falling, ZERO);
    assert.equal(falling.state, "failed");
    assert.equal(falling.failureCause, "terrain");
});

test("launch top authority lasts until both feet clear even during an early descent", () => {
    const run = createRun({ seed: 11 });
    const active = run.retainedSites[0];
    const descending = stepFlight(
        {
            ...run,
            state: "launching",
            activeSiteId: active.id,
            targetSiteId: null,
            launchStarted: true,
            launchCleared: false,
            pose: {
                x: active.center,
                y: active.platformTop + 0.001,
                vx: 0,
                vy: -1,
                angle: 0,
                angularVelocity: 0,
            },
        },
        ZERO,
    );
    assert.equal(descending.state, "launching");
    assert.equal(descending.failureCause, null);
    assert.equal(descending.launchCleared, false);
});

test("terminus and final service are physical and terminal without inventing a site", () => {
    const run = createRun({ seed: 39, reducedMotion: true });
    const railPose = { x: WORLD_MAX_X - 2, y: 30, vx: 600, vy: 0, angle: 0, angularVelocity: 0 };
    const rail = stepFlight({ ...run, pose: railPose }, ZERO);
    assert.equal(rail.state, "failed");
    assert.equal(rail.failureCause, "terminus");

    const target = createSiteForIndex(39, MAX_SITE_INDEX);
    const terminal = stepFlight(
        {
            ...run,
            completedSites: MAX_SITE_INDEX,
            refuelRatio: 1,
            generatorCursor: MAX_SITE_INDEX + 1,
            retainedSites: [target],
            targetSiteId: MAX_SITE_INDEX,
            pose: {
                x: target.center,
                y: target.platformTop + 0.001,
                vx: 0,
                vy: -1,
                angle: 0,
                angularVelocity: 0,
            },
        },
        ZERO,
    );
    assert.equal(terminal.state, "launching");
    assert.equal(terminal.completedSites, 4096);
    assert.equal(terminal.generatorCursor, 4096);
    assert.equal(terminal.activeSiteId, MAX_SITE_INDEX);
    assert.equal(terminal.targetSiteId, null);
    assert.equal("targetRouteProof" in terminal, false);
    assert.equal(terminal.fuel, run.fuel + 22);
});

test("maximum reachable signed angular sweeps find final-slab contact with bounded retention", () => {
    const model = {
        ...createRun({ seed: 41 }),
        retainedSites: [],
        targetSiteId: null,
        terrainAuthority: "context",
        terrainVertices: [],
    };
    const pose = { x: 0, y: 35.8, vx: 0, vy: 0, angle: 0.9, angularVelocity: 0 };
    assert.throws(() => classifySweptContact(model, pose, pose), TypeError);
    const fullRotation = {};
    assert.equal(classifySweptContact(model, pose, pose, { angularTravel: 360, instrumentation: fullRotation }), null);
    assert.equal(fullRotation.visitedKnots, 362);
    for (const direction of [-1, 1]) {
        const angularTravel = direction * 73091.33333333333;
        const previous = { ...pose, angle: direction * 0.9 };
        const next = {
            ...previous,
            x: direction * 11746.828095238097,
            angle: normalizeDegrees(previous.angle + angularTravel),
        };
        const corner = hullForPose({ ...next, angle: previous.angle + angularTravel }).reduce((selected, candidate) =>
            direction * candidate.x > direction * selected.x ? candidate : selected,
        );
        const edgeX = corner.x - direction * 0.001;
        const edge = [
            { x: edgeX, y: 20 },
            { x: edgeX, y: 50 },
        ];
        const staleInstrumentation = {};
        assert.equal(
            classifySweptContact(model, previous, next, {
                angularTravel: direction * 53148,
                instrumentation: staleInstrumentation,
                features: [{ cause: "terminus", priority: 3, segment: edge }],
            }),
            null,
        );
        assert.equal(staleInstrumentation.visitedKnots, 53150);
        const instrumentation = {};
        const contact = classifySweptContact(model, previous, next, {
            angularTravel,
            instrumentation,
            features: [{ cause: "terminus", priority: 3, segment: edge }],
        });
        assert.equal(contact.kind, "unsafe");
        assert.equal(contact.cause, "terminus");
        assert.ok(contact.time > 0.9999);
        assert.equal(instrumentation.visitedKnots, 73094);
        assert.equal(instrumentation.visitedKnots - staleInstrumentation.visitedKnots, 19944);
        assert.ok(instrumentation.maxKnotHulls <= 2);
        assert.ok(instrumentation.maxStack <= 20);
        assert.ok(instrumentation.prunedSlabs > 50000);
        assert.ok(instrumentation.constructedKnotHulls < 256);
    }
});
