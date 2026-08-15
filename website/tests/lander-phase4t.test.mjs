import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createRun, stepFlight, transitionMission, updateRetention } from "../static/lander-model.js";
import { REFERENCE_PROOF_CATALOG } from "../static/lander-route-proofs.generated.js";
import {
    MAX_NORMALIZED_DECK,
    SITE_CANDIDATE_OFFSETS,
    SITE_CANDIDATE_ORDERS,
    STATIC_WORLD_SEED,
    TERRAIN_PROFILES,
    WORLD_MAX_X,
    createSiteForIndex,
    routePairKey,
    selectRouteProof,
    terrainProfileForBlock,
} from "../static/lander-world.js";

const FIXTURE_ROOT = new URL("fixtures/", import.meta.url);
const PROFILE_IDS = Object.freeze(["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]);

function close(actual, expected, tolerance = 1e-10) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

function positiveModulo(value, modulus) {
    return ((value % modulus) + modulus) % modulus;
}

function median(values) {
    const ordered = values.toSorted((left, right) => left - right);
    const middle = ordered.length / 2;
    return ordered.length % 2
        ? ordered[Math.floor(middle)]
        : (ordered[middle - 1] + ordered[middle]) / 2;
}

function profileStats(profile) {
    const grades = Array.from({ length: profile.length - 1 }, (_, index) =>
        Math.round((profile[index + 1] - profile[index]) * 400) / 100,
    );
    const nonzero = grades.flatMap((grade, index) => (grade === 0 ? [] : [{ grade, index }]));
    const reversals = nonzero.flatMap((entry, index) => {
        const next = nonzero[(index + 1) % nonzero.length];
        return Math.sign(entry.grade) === Math.sign(next.grade)
            ? []
            : [{ index: next.index, strength: Math.round(Math.abs(next.grade - entry.grade) * 100) / 100 }];
    });
    const spacings = reversals.map(
        (reversal, index) => positiveModulo(reversals[(index + 1) % reversals.length].index - reversal.index, 32) || 32,
    );
    return { grades, reversals, spacings };
}

function heightAt(geometry, assignment, x) {
    const blockIndex = Math.floor(x / geometry.terrain.superblockWidth);
    const profileIndex = blockIndex === assignment.leftBlock ? assignment.leftProfile : assignment.rightProfile;
    if (profileIndex === undefined) throw new RangeError(`Independent assignment misses block ${blockIndex}`);
    const profile = geometry.terrain.profiles[`S${profileIndex}`];
    const localX = x - blockIndex * geometry.terrain.superblockWidth;
    const segment = Math.min(profile.length - 2, Math.floor(localX / geometry.terrain.cadence));
    const fraction = (localX - segment * geometry.terrain.cadence) / geometry.terrain.cadence;
    const normalized = profile[segment] + (profile[segment + 1] - profile[segment]) * fraction;
    return geometry.terrain.mapping.worldScale * normalized + geometry.terrain.mapping.worldOffset;
}

function independentSite(geometry, assignment, index, nominalCenter, candidateOrder) {
    const order = geometry.site.candidateOrders[candidateOrder];
    for (let candidateOrdinal = 0; candidateOrdinal < order.length; candidateOrdinal += 1) {
        const offsetIndex = order[candidateOrdinal];
        const center = nominalCenter + geometry.site.candidateOffsets[offsetIndex];
        const closedFootprint = geometry.site.closedFootprint.map((offset) => center + offset);
        const samples = [...closedFootprint];
        for (
            let x = Math.ceil(closedFootprint[0] / geometry.terrain.cadence) * geometry.terrain.cadence;
            x <= closedFootprint[1];
            x += geometry.terrain.cadence
        )
            samples.push(x);
        const localNativeMaximum = Math.max(...samples.map((x) => heightAt(geometry, assignment, x)));
        const platformTop = localNativeMaximum + geometry.site.clearance;
        const normalizedDeck =
            (platformTop - geometry.terrain.mapping.worldOffset) / geometry.terrain.mapping.worldScale;
        if (normalizedDeck > geometry.site.maxNormalizedDeck) continue;
        return {
            index,
            nominalCenter,
            candidateOrder,
            candidateOrdinal,
            offsetIndex,
            center,
            platformTop,
        };
    }
    throw new Error(`Independent candidate exhaustion at ${nominalCenter}`);
}

function millimeters(value) {
    return Math.round(value * 1000);
}

function independentAssignments(geometry) {
    const phases = Array.from({ length: 16 }, (_, index) => positiveModulo(36 + 96 * index, 512)).sort(
        (left, right) => left - right,
    );
    const assignments = [];
    for (const phase of phases) {
        const leftBlock = Math.floor((phase + geometry.site.closedFootprint[0]) / 512);
        const rightBlock = Math.floor((phase + 96 + 40 + geometry.site.closedFootprint[1]) / 512);
        for (let leftProfile = 0; leftProfile < 8; leftProfile += 1) {
            const rightProfiles =
                leftBlock === rightBlock
                    ? [leftProfile]
                    : Array.from({ length: 8 }, (_, index) => index).filter((index) => index !== leftProfile);
            for (const rightProfile of rightProfiles) {
                for (let candidateOrder = 0; candidateOrder < 2; candidateOrder += 1) {
                    const partial = {
                        assignmentId: `p${phase}-a${leftProfile}-b${rightProfile}-o${candidateOrder}`,
                        phase,
                        leftBlock,
                        rightBlock,
                        leftProfile,
                        rightProfile,
                    };
                    const origin = independentSite(geometry, partial, 0, phase, candidateOrder);
                    const target = independentSite(geometry, partial, 1, phase + 96, candidateOrder);
                    const distanceMillimeters = millimeters(target.center - origin.center);
                    const originMillimeters = millimeters(origin.platformTop);
                    const targetMillimeters = millimeters(target.platformTop);
                    assignments.push({
                        ...partial,
                        candidateOrder,
                        originNominalCenter: origin.nominalCenter,
                        targetNominalCenter: target.nominalCenter,
                        originCandidateOrdinal: origin.candidateOrdinal,
                        targetCandidateOrdinal: target.candidateOrdinal,
                        originOffsetIndex: origin.offsetIndex,
                        targetOffsetIndex: target.offsetIndex,
                        originCenter: origin.center,
                        targetCenter: target.center,
                        distance: target.center - origin.center,
                        distanceMillimeters,
                        originDeck: origin.platformTop,
                        targetDeck: target.platformTop,
                        originMillimeters,
                        targetMillimeters,
                        deckDelta: target.platformTop - origin.platformTop,
                        pairKey: `r:${distanceMillimeters}:${originMillimeters}:${targetMillimeters}`,
                    });
                }
            }
        }
    }
    return assignments;
}

test("Phase 4T profiles independently double predecessor reversals and exercise sharp bounds", async () => {
    const predecessor = JSON.parse(await readFile(new URL("lander-route-geometry-v8.json", FIXTURE_ROOT), "utf8"));
    const geometry = JSON.parse(await readFile(new URL("lander-route-geometry-v9.json", FIXTURE_ROOT), "utf8"));
    const oldStats = PROFILE_IDS.map((id) => profileStats(predecessor.terrain.profiles[id]));
    const newStats = PROFILE_IDS.map((id) => profileStats(geometry.terrain.profiles[id]));
    assert.deepEqual(oldStats.map(({ reversals }) => reversals.length), [6, 6, 8, 8, 8, 6, 8, 6]);
    assert.deepEqual(newStats.map(({ reversals }) => reversals.length), [12, 12, 16, 16, 16, 12, 16, 12]);
    for (let index = 0; index < PROFILE_IDS.length; index += 1) {
        assert.equal(newStats[index].reversals.length, oldStats[index].reversals.length * 2);
        assert.equal(512 / newStats[index].reversals.length, 512 / oldStats[index].reversals.length / 2);
        assert.ok(
            newStats[index].spacings.every(
                (spacing, spacingIndex, all) => spacing !== 1 || all[(spacingIndex + 1) % all.length] !== 1,
            ),
        );
    }
    const normalized = PROFILE_IDS.flatMap((id) => geometry.terrain.profiles[id]);
    const grades = newStats.flatMap(({ grades: values }) => values);
    const gradeChanges = newStats.flatMap(({ grades: values }) =>
        values.map((grade, index) => Math.abs(values[(index + 1) % values.length] - grade)),
    );
    assert.deepEqual([Math.min(...normalized), Math.max(...normalized)], [0.1, 0.6]);
    assert.equal(Math.max(...grades.map(Math.abs)), 0.4);
    assert.equal(Math.max(...gradeChanges), 0.8);
    assert.equal(median(oldStats.flatMap(({ reversals }) => reversals.map(({ strength }) => strength))), 0.2);
    assert.equal(median(newStats.flatMap(({ reversals }) => reversals.map(({ strength }) => strength))), 0.6);
    const mutated = geometry.terrain.profiles.S0.map(() => 0.35);
    assert.notEqual(profileStats(mutated).reversals.length, newStats[0].reversals.length);
});

test("Phase 4T assignments independently enumerate every profile pair, rejection, and exact key", async () => {
    const geometry = JSON.parse(await readFile(new URL("lander-route-geometry-v9.json", FIXTURE_ROOT), "utf8"));
    const derived = JSON.parse(await readFile(new URL("lander-route-derived-v9.json", FIXTURE_ROOT), "utf8"));
    const assignments = independentAssignments(geometry);
    assert.deepEqual(assignments, derived.assignments);
    assert.equal(assignments.length, 736);
    assert.equal(new Set(assignments.map(({ pairKey }) => pairKey)).size, 312);
    assert.equal(assignments.filter(({ leftBlock, rightBlock }) => leftBlock === rightBlock).length, 176);
    assert.equal(assignments.filter(({ leftBlock, rightBlock }) => leftBlock !== rightBlock).length, 560);
    assert.deepEqual(
        [
            ...new Set(
                assignments.flatMap(({ originCandidateOrdinal, targetCandidateOrdinal }) => [
                    originCandidateOrdinal,
                    targetCandidateOrdinal,
                ]),
            ),
        ].sort((left, right) => left - right),
        [0, 1, 2, 3, 4, 5],
    );
    const mutated = structuredClone(geometry);
    mutated.site.maxNormalizedDeck = 0.49;
    assert.throws(() => independentAssignments(mutated), Error);
});

test("Phase 4T runtime selector, candidates, and final rail match independent fixed authority", async () => {
    const geometry = JSON.parse(await readFile(new URL("lander-route-geometry-v9.json", FIXTURE_ROOT), "utf8"));
    assert.deepEqual(TERRAIN_PROFILES, geometry.terrain.profiles);
    assert.deepEqual(SITE_CANDIDATE_OFFSETS, geometry.site.candidateOffsets);
    assert.deepEqual(SITE_CANDIDATE_ORDERS, geometry.site.candidateOrders);
    assert.equal(MAX_NORMALIZED_DECK, geometry.site.maxNormalizedDeck);
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        const selected = Array.from({ length: 128 }, (_, offset) => terrainProfileForBlock(seed, offset - 64).profile);
        assert.equal(new Set(selected).size, 8);
        assert.ok(selected.every((profile, index) => index === 0 || profile !== selected[index - 1]));
        let candidateOrder = null;
        for (let index = -100; index <= 100; index += 1) {
            const site = createSiteForIndex(seed, index);
            candidateOrder ??= site.candidateOrder;
            assert.equal(site.candidateOrder, candidateOrder);
            assert.ok(site.candidateOrdinal <= 5);
            assert.ok(site.normalizedDeck <= 0.5);
        }
    }
    const final = createSiteForIndex(STATIC_WORLD_SEED, 4095);
    close(WORLD_MAX_X - (final.center + 13.8), 46.2, 1e-9);
});

test("Phase 4T full signed generation and every positive mission leg remain total", () => {
    const durations = [];
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let signedOrigin = createSiteForIndex(seed, -4095);
        for (let index = -4095; index < 4095; index += 1) {
            const signedTarget = createSiteForIndex(seed, index + 1);
            const key = routePairKey(signedOrigin, signedTarget);
            assert.equal(selectRouteProof(signedOrigin, signedTarget, REFERENCE_PROOF_CATALOG).pairKey, key);
            signedOrigin = signedTarget;
        }
        const penultimate = createSiteForIndex(seed, 4094);
        const final = createSiteForIndex(seed, 4095);
        assert.equal(
            selectRouteProof(penultimate, final, REFERENCE_PROOF_CATALOG).pairKey,
            routePairKey(penultimate, final),
        );
        assert.ok(final.center + 13.8 < WORLD_MAX_X);

        const started = performance.now();
        let model = updateRetention(createRun({ seed, reducedMotion: true }));
        let proofPaths = 0;
        let maximumSites = model.retainedSites.length;
        let maximumChunks = model.retainedChunks.length;
        for (let completed = 0; completed < 4096; completed += 1) {
            if (model.state === "launching") {
                for (let step = 0; step < 90 && model.state === "launching"; step += 1) {
                    model = updateRetention(stepFlight(model, { left: 0.72, right: 0.72 }));
                }
            }
            const target = model.retainedSites.find((site) => site.id === model.targetSiteId);
            assert.ok(target);
            model = updateRetention(
                stepFlight(
                    {
                        ...model,
                        pose: {
                            x: target.center,
                            y: target.platformTop + 0.001,
                            vx: 0,
                            vy: -1,
                            angle: 0,
                            angularVelocity: 0,
                        },
                    },
                    { left: 0, right: 0 },
                ),
            );
            assert.notEqual(model.state, "generation-error");
            maximumSites = Math.max(maximumSites, model.retainedSites.length);
            maximumChunks = Math.max(maximumChunks, model.retainedChunks.length);
            if (completed < 4095) {
                const origin = model.retainedSites.find((site) => site.id === model.activeSiteId);
                const next = model.retainedSites.find((site) => site.id === model.targetSiteId);
                const key = routePairKey(origin, next);
                assert.deepEqual(model.targetRouteProof, REFERENCE_PROOF_CATALOG[key]);
                assert.equal(next.pairKey, key);
                proofPaths += 1;
            }
            if (completed === 2047) {
                const restored = transitionMission({ ...model, state: "failed" }, "RESTART");
                assert.equal(restored.state, "launching");
                assert.deepEqual(restored.targetRouteProof, model.targetRouteProof);
                model = updateRetention(restored);
            }
        }
        durations.push(performance.now() - started);
        assert.equal(proofPaths, 4095);
        assert.equal(model.completedSites, 4096);
        assert.equal(model.state, "launching");
        assert.equal(model.targetSiteId, null);
        assert.equal(model.targetRouteProof, null);
        assert.equal(model.generatorCursor, 4096);
        assert.equal(maximumSites, 3);
        assert.ok(maximumChunks <= 5);
    }
    assert.ok(durations.every((duration) => duration < 90_000));
});
