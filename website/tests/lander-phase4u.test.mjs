import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

import {
    BASE_ROUTE_ALLOWANCE,
    ENGINE_ACCELERATION,
    ENGINE_FORCE_COEFFICIENT,
    GRAVITY,
    GRAVITY_FORCE_COEFFICIENT,
    TORQUE_ACCELERATION,
    TRANSLATIONAL_MASS,
    TRANSLATIONAL_MASS_DENOMINATOR,
    TRANSLATIONAL_MASS_NUMERATOR,
    createRun,
    predictedFuelAllowance,
    quantumCeil,
    refuelRatioForBase,
    stepFlight,
    updateRetention,
} from "../static/lander-model.js";
import {
    MAX_NORMALIZED_DECK,
    SITE_CANDIDATE_OFFSETS,
    SITE_CANDIDATE_ORDERS,
    SITE_SPACING,
    STATIC_WORLD_SEED,
    TERRAIN_GRADE_CHANGE_LIMIT,
    TERRAIN_GRADE_LIMIT,
    TERRAIN_PROFILES,
    WORLD_MAX_X,
    WORLD_MIN_X,
    classifySweptContact,
    createSiteForIndex,
    hullForPose,
    siteCandidateOrder,
    terrainHeightAt,
    terrainProfileForBlock,
} from "../static/lander-world.js";
import { OPENING_FLIGHT_ROWS, REPRESENTATIVE_FLIGHT_ROWS } from "./lander-phase4u-flight-vectors.mjs";

const ROOT = new URL("../", import.meta.url);
const FIXTURE_URL = new URL("fixtures/lander-route-geometry-v10.json", import.meta.url);
const IDS = Object.freeze(Array.from({ length: 8 }, (_, index) => `S${index}`));
const modulo = (value, divisor) => ((value % divisor) + divisor) % divisor;
const COMMANDS = Object.freeze([
    Object.freeze({ left: 0, right: 0 }),
    Object.freeze({ left: 0.72, right: 0.72 }),
    Object.freeze({ left: 0, right: 0.375 }),
    Object.freeze({ left: 0.375, right: 0 }),
    Object.freeze({ left: 0.2125, right: 0.5875 }),
    Object.freeze({ left: 0.5875, right: 0.2125 }),
]);

function commandRuns(encoded) {
    return encoded.split(",").map((pair) => pair.split(":").map(Number));
}

function replayFlight(model, encoded) {
    let steps = 0;
    let burn = 0;
    let maximumHullTop = -Infinity;
    for (const [commandIndex, count] of commandRuns(encoded)) {
        const command = COMMANDS[commandIndex];
        for (let index = 0; index < count; index += 1) {
            burn += (command.left + command.right) / 120;
            model = stepFlight(model, command);
            steps += 1;
            maximumHullTop = Math.max(maximumHullTop, ...hullForPose(model.pose).map((point) => point.y));
            if (model.state !== "flying" && model.state !== "launching") return { model, steps, burn, maximumHullTop };
        }
    }
    return { model, steps, burn, maximumHullTop };
}

function seedForOpening(profile, candidateOrder) {
    for (let seed = 1; seed < 10_000; seed += 1) {
        if (terrainProfileForBlock(seed, 0).id === profile && siteCandidateOrder(seed) === candidateOrder) return seed;
    }
    throw new Error(`No opening seed for ${profile}/${candidateOrder}`);
}

function integerPairKey(origin, target) {
    return `r:${Math.round((target.center - origin.center) * 1000)}:${Math.round(origin.platformTop * 1000)}:${Math.round(target.platformTop * 1000)}`;
}

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object")
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    return value;
}

function profileStats(samples) {
    const grades = samples.slice(1).map((sample, index) => Math.round((sample - samples[index]) * 400) / 100);
    const reversals = grades.filter((grade, index) => Math.sign(grade) !== Math.sign(grades[(index + 1) % 32]));
    const changes = grades.map((grade, index) => Math.round(Math.abs(grades[(index + 1) % 32] - grade) * 100) / 100);
    return { grades, reversals: reversals.length, changes };
}

function heightAt(geometry, assignment, x) {
    const block = Math.floor(x / 512);
    const profile = geometry.terrain.profiles[`S${block === assignment.leftBlock ? assignment.leftProfile : assignment.rightProfile}`];
    const local = x - block * 512;
    const segment = Math.min(31, Math.floor(local / 16));
    const fraction = (local - segment * 16) / 16;
    const normalized = profile[segment] + (profile[segment + 1] - profile[segment]) * fraction;
    return 64 * normalized - 9.2;
}

function independentSite(geometry, assignment, nominalCenter, candidateOrder) {
    for (let ordinal = 0; ordinal < 6; ordinal += 1) {
        const offsetIndex = geometry.site.candidateOrders[candidateOrder][ordinal];
        const center = nominalCenter + geometry.site.candidateOffsets[offsetIndex];
        const [left, right] = geometry.site.closedFootprint.map((offset) => center + offset);
        const xs = [left, right];
        for (let x = Math.ceil(left / 16) * 16; x <= right; x += 16) xs.push(x);
        const platformTop = Math.max(...xs.map((x) => heightAt(geometry, assignment, x))) + 2.5;
        if ((platformTop + 9.2) / 64 <= 0.5) return { center, ordinal, offsetIndex, platformTop };
    }
    throw new Error(`candidate exhaustion at ${nominalCenter}`);
}

function independentAssignments(geometry) {
    const keys = new Set();
    const distances = new Set();
    const ordinals = new Set();
    let count = 0;
    const phases = Array.from({ length: 8 }, (_, index) => modulo(36 + 192 * index, 512)).sort((a, b) => a - b);
    for (const phase of phases) {
        const leftBlock = Math.floor((phase - 4.8) / 512);
        const rightBlock = Math.floor((phase + 245.8) / 512);
        for (let leftProfile = 0; leftProfile < 8; leftProfile += 1) {
            const rightProfiles = leftBlock === rightBlock ? [leftProfile] : Array.from({ length: 8 }, (_, i) => i).filter((i) => i !== leftProfile);
            for (const rightProfile of rightProfiles) {
                const assignment = { leftBlock, rightBlock, leftProfile, rightProfile };
                for (let order = 0; order < 2; order += 1) {
                    const origin = independentSite(geometry, assignment, phase, order);
                    const target = independentSite(geometry, assignment, phase + 192, order);
                    const distance = target.center - origin.center;
                    distances.add(distance);
                    ordinals.add(origin.ordinal);
                    ordinals.add(target.ordinal);
                    keys.add(`r:${Math.round(distance * 1000)}:${Math.round(origin.platformTop * 1000)}:${Math.round(target.platformTop * 1000)}`);
                    count += 1;
                }
            }
        }
    }
    return { count, keys, distances, ordinals };
}

function contactTarget(model) {
    const target = model.retainedSites.find((site) => site.id === model.targetSiteId);
    return updateRetention(
        stepFlight(
            {
                ...model,
                state: "flying",
                launchStarted: true,
                launchCleared: true,
                pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1, angle: 0, angularVelocity: 0 },
            },
            { left: 0, right: 0 },
        ),
    );
}

test("Phase 4U geometry fixture is canonical, independently hashed, and geometry-only", async () => {
    const geometry = JSON.parse(await readFile(FIXTURE_URL, "utf8"));
    assert.deepEqual(Object.keys(geometry).sort(), ["collision", "deriverVersion", "geometryDigest", "physics", "schema", "site", "structure", "terrain", "world"]);
    const { geometryDigest, ...payload } = geometry;
    assert.equal(createHash("sha256").update(JSON.stringify(canonical(payload))).digest("hex"), geometryDigest);
    assert.equal(geometry.schema, "agw-lander-route-geometry/v10");
    assert.equal(geometry.deriverVersion, "agw-lander-geometry-deriver/v1");
    for (const forbidden of ["assignments", "records", "commands", "schedules", "openings", "pairKey", "proofDigest"])
        assert.equal(forbidden in geometry, false);
});

test("Phase 4U profiles retain reversals and exercise exact sharper bounds", async () => {
    const geometry = JSON.parse(await readFile(FIXTURE_URL, "utf8"));
    const stats = IDS.map((id) => profileStats(geometry.terrain.profiles[id]));
    assert.deepEqual(stats.map(({ reversals }) => reversals), [12, 12, 16, 16, 16, 12, 16, 12]);
    assert.deepEqual(TERRAIN_PROFILES, geometry.terrain.profiles);
    assert.equal(Math.max(...stats.flatMap(({ grades }) => grades).map(Math.abs)), TERRAIN_GRADE_LIMIT);
    assert.equal(Math.max(...stats.flatMap(({ changes }) => changes)), TERRAIN_GRADE_CHANGE_LIMIT);
    assert.deepEqual([TERRAIN_GRADE_LIMIT, TERRAIN_GRADE_CHANGE_LIMIT], [0.6, 1.2]);
    assert.deepEqual([Math.min(...Object.values(TERRAIN_PROFILES).flat()), Math.max(...Object.values(TERRAIN_PROFILES).flat())], [0.1, 0.6]);
    const mutated = structuredClone(geometry.terrain.profiles.S0);
    mutated[3] = 0.35;
    assert.notDeepEqual(profileStats(mutated), stats[0]);
});

test("Phase 4U independently closes 512 assignments and 250 geometry classes", async () => {
    const geometry = JSON.parse(await readFile(FIXTURE_URL, "utf8"));
    const closure = independentAssignments(geometry);
    assert.deepEqual([closure.count, closure.keys.size], [512, 250]);
    assert.deepEqual([...closure.distances].sort((a, b) => a - b), [152, 160, 168, 176, 184, 192, 200, 208, 216, 224, 232]);
    assert.deepEqual([...closure.ordinals].sort(), [0, 1, 2, 3, 4, 5]);
    const mutated = structuredClone(geometry);
    mutated.site.candidateOrders[1].reverse();
    assert.notDeepEqual(independentAssignments(mutated), closure);
});

test("Phase 4U direct allowance, rational mass, ratio, and terminal arithmetic are exact", () => {
    assert.deepEqual(
        [TRANSLATIONAL_MASS_NUMERATOR, TRANSLATIONAL_MASS_DENOMINATOR, TRANSLATIONAL_MASS],
        [7, 10, 0.7],
    );
    assert.deepEqual([ENGINE_FORCE_COEFFICIENT, GRAVITY_FORCE_COEFFICIENT, ENGINE_ACCELERATION, GRAVITY, TORQUE_ACCELERATION], [9, 3, 90 / 7, 30 / 7, 80]);
    assert.equal(ENGINE_ACCELERATION / GRAVITY, 3);
    assert.equal(BASE_ROUTE_ALLOWANCE, 22);
    assert.equal(quantumCeil(22 + 14.184000000000003 / 3), 26.75);
    assert.equal(predictedFuelAllowance({ platformTop: 20 }, { platformTop: 10 }), 22);
    assert.equal(predictedFuelAllowance({ platformTop: 10 }, { platformTop: 24.184000000000005 }), 26.75);
    assert.deepEqual([refuelRatioForBase(1), refuelRatioForBase(2), refuelRatioForBase(54)], [2, 1.5, 1]);
    let noBurnFuel = 15;
    for (let poweredBase = 1; poweredBase <= 4096; poweredBase += 1)
        noBurnFuel += 26.75 * refuelRatioForBase(poweredBase);
    assert.equal(noBurnFuel, 109636.5);
    assert.equal(Math.ceil(noBurnFuel), 109637);
});

test("Phase 4U replays all 16 non-exhaustive opening witnesses through production physics and collision", () => {
    assert.equal(OPENING_FLIGHT_ROWS.length, 16);
    const keys = new Set();
    for (const witness of OPENING_FLIGHT_ROWS) {
        const seed = seedForOpening(witness.profile, witness.candidateOrder);
        const opening = createRun({ seed, reducedMotion: false });
        const site = opening.retainedSites[0];
        assert.equal(site.center, witness.center);
        assert.equal(Number(site.platformTop.toFixed(12)), Number(witness.deck.toFixed(12)));
        const replayed = replayFlight(opening, witness.runs);
        assert.equal(replayed.model.state, "landed");
        assert.equal(replayed.model.completedSites, 1);
        assert.equal(replayed.steps, witness.contactStep);
        assert.equal(Number(replayed.burn.toFixed(12)), Number(witness.burn.toFixed(12)));
        assert.equal(Number((15 - replayed.burn).toFixed(6)), Number(witness.reserve.toFixed(6)));
        keys.add(`${witness.profile}/${witness.candidateOrder}`);
    }
    assert.equal(keys.size, 16);
});

test("Phase 4U replays closest, farthest, maximum-rise, and maximum-fall review witnesses", () => {
    assert.deepEqual(REPRESENTATIVE_FLIGHT_ROWS.map(({ label }) => label), ["closest", "farthest", "maximum-rise", "maximum-fall"]);
    for (const witness of REPRESENTATIVE_FLIGHT_ROWS) {
        const origin = createSiteForIndex(witness.seed, witness.index);
        const target = { ...createSiteForIndex(witness.seed, witness.index + 1), originSiteId: origin.id };
        assert.equal(integerPairKey(origin, target), witness.pairKey);
        const flight = {
            ...createRun({ seed: witness.seed, reducedMotion: false }),
            state: "launching",
            pose: { x: origin.center, y: origin.platformTop, vx: 0, vy: 0, angle: 0, angularVelocity: 0 },
            fuel: 30,
            fuelGaugeReference: 30,
            activeSiteId: origin.id,
            targetSiteId: target.id,
            retainedSites: [{ ...origin, powered: true }, target],
            generatorCursor: target.id + 1,
            launchStarted: false,
            launchCleared: false,
        };
        const replayed = replayFlight(flight, witness.runs);
        assert.equal(replayed.model.state, "landed");
        assert.equal(replayed.steps, witness.contactStep);
        assert.equal(Number(replayed.burn.toFixed(12)), Number(witness.burn.toFixed(12)));
        assert.equal(Number(replayed.maximumHullTop.toFixed(9)), Number(witness.maxHullTop.toFixed(9)));
        assert.ok(replayed.burn <= predictedFuelAllowance(origin, target));
    }
});

test("Phase 4U site generation is signed-total and centered at 192 meters", () => {
    assert.equal(SITE_SPACING, 192);
    assert.deepEqual([WORLD_MIN_X, WORLD_MAX_X], [-786432, 786432]);
    assert.deepEqual(SITE_CANDIDATE_OFFSETS, [0, 8, 16, 24, 32, 40]);
    assert.deepEqual(SITE_CANDIDATE_ORDERS, [[0, 1, 2, 3, 4, 5], [0, 5, 4, 3, 2, 1]]);
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let prior = createSiteForIndex(seed, -4095);
        let sum = 0;
        const order = siteCandidateOrder(seed);
        for (let index = -4094; index <= 4095; index += 1) {
            const current = createSiteForIndex(seed, index);
            assert.equal(current.candidateOrder, order);
            assert.ok(current.candidateOrdinal <= 5);
            assert.ok(current.normalizedDeck <= MAX_NORMALIZED_DECK);
            sum += current.center - prior.center;
            prior = current;
        }
        assert.ok(Math.abs(sum / 8190 - 192) <= 0.01);
        assert.ok(prior.center + 13.8 < WORLD_MAX_X);
        assert.equal(new Set(Array.from({ length: 128 }, (_, index) => terrainProfileForBlock(seed, index - 64).id)).size, 8);
    }
});

test("Phase 4U completes all four 4096-site missions without route state or replay", () => {
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let model = createRun({ seed, reducedMotion: true });
        let maximumSites = model.retainedSites.length;
        for (let index = 0; index < 4096; index += 1) {
            model = contactTarget(model);
            maximumSites = Math.max(maximumSites, model.retainedSites.length);
            assert.equal("targetRouteProof" in model, false);
        }
        assert.equal(model.completedSites, 4096);
        assert.equal(model.targetSiteId, null);
        assert.equal(model.generatorCursor, 4096);
        assert.equal(maximumSites, 3);
    }
});

test("Phase 4U runtime source graph has no proof catalog, replay, search, or route failure path", async () => {
    const sources = await Promise.all(["static/lander-model.js", "static/lander-world.js", "build.py"].map((path) => readFile(new URL(path, ROOT), "utf8")));
    for (const source of sources) {
        for (const symbol of ["targetRouteProof", "pairKey", "REFERENCE_PROOF", "proveRouteProof", "replayRouteProof", "generation-error", "lander-route-proofs"])
            assert.equal(source.includes(symbol), false);
    }
});

test("Phase 4U geometry CLI is deterministic, byte-verifying, and rejects old route flags", async () => {
    const directory = await mkdtemp(join(tmpdir(), "phase4u-geometry-"));
    const first = join(directory, "first.json");
    const second = join(directory, "second.json");
    const tool = new URL("tools/derive_lander_geometry.mjs", ROOT);
    assert.equal(spawnSync(process.execPath, [tool.pathname, "--output", first]).status, 0);
    assert.equal(spawnSync(process.execPath, [tool.pathname, "--output", second, "--verify", first]).status, 0);
    assert.equal(await readFile(first, "utf8"), await readFile(second, "utf8"));
    assert.equal(spawnSync(process.execPath, [tool.pathname, "--geometry", first]).status, 2);
});

test("Phase 4U procedural collision covers the expanded world beyond retired bounds", () => {
    const radius = Math.hypot(1.6, 6.5);
    for (const x of [-600000, 600000]) {
        const ground = terrainHeightAt(11, x);
        const pose = { x, y: ground - radius - 0.01, vx: 0, vy: 0, angle: (Math.atan2(1.6, 6.5) * 180) / Math.PI, angularVelocity: 0 };
        const contact = classifySweptContact({ seed: 11, retainedSites: [], targetSiteId: null }, pose, pose, { angularTravel: 0 });
        assert.deepEqual([contact.kind, contact.cause, contact.time], ["unsafe", "terrain", 0]);
    }
    assert.deepEqual([(WORLD_MAX_X - WORLD_MIN_X) / 16 + 1, (WORLD_MAX_X - WORLD_MIN_X) / 16], [98305, 98304]);
});
