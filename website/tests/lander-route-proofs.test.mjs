import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
    BASE_ROUTE_ALLOWANCE,
    FUEL_QUANTUM,
    checkpointPoseForContact,
    createRun,
    proveRouteProof,
    stepFlight,
    updateRetention,
} from "../static/lander-model.js";
import { REFERENCE_PROOF_CATALOG, REFERENCE_PROOFS, ROUTE_DIGESTS } from "../static/lander-route-proofs.generated.js";
import { STATIC_WORLD_SEED, createSiteForIndex, terrainProfileForBlock } from "../static/lander-world.js";
import { quantumCeil } from "../tools/derive_lander_routes.mjs";

const ROOT = new URL("../", import.meta.url).pathname;

function close(actual, expected, tolerance = 1e-10) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

test("route arithmetic uses exact quantum ceiling and integer-millimeter delta keys", () => {
    assert.equal(quantumCeil(17.450000000000003), 17.5);
    assert.equal(quantumCeil(17.45), 17.45);
    assert.equal(BASE_ROUTE_ALLOWANCE, 13.4);
    assert.equal(
        new Set(
            [
            { originMillimeters: 5_716, targetMillimeters: 20_116 },
            { originMillimeters: 6_356, targetMillimeters: 20_756 },
            { originMillimeters: 5_716, targetMillimeters: 19_476 },
            ].map(({ originMillimeters, targetMillimeters }) => targetMillimeters - originMillimeters),
        ).size,
        2,
    );
    const ratio = (baseNumber) => 1 + 0.5 ** (baseNumber - 1);
    assert.equal(ratio(53), 1.0000000000000002);
    assert.equal(ratio(54), 1);
    const maximumAllowance = quantumCeil(BASE_ROUTE_ALLOWANCE + 18.056 / 3);
    assert.equal(maximumAllowance, 19.450000000000003);
    let worstFuel = 15;
    for (let baseNumber = 1; baseNumber <= 4096; baseNumber += 1) {
        worstFuel += maximumAllowance * ratio(baseNumber);
    }
    assert.equal(worstFuel, 79721.09999999384);
    assert.equal(Math.ceil(worstFuel), 79722);
});

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
        return Object.fromEntries(
            Object.keys(value)
                .sort()
                .map((key) => [key, canonical(value[key])]),
        );
    }
    return value;
}

function digest(value) {
    return createHash("sha256")
        .update(JSON.stringify(canonical(value)), "utf8")
        .digest("hex");
}

function heightFromVertices(vertices, x) {
    for (let index = 1; index < vertices.length; index += 1) {
        const [leftX, leftY] = vertices[index - 1];
        const [rightX, rightY] = vertices[index];
        if (x < leftX || x > rightX) continue;
        return leftY + ((rightY - leftY) * (x - leftX)) / (rightX - leftX);
    }
    throw new RangeError(`Witness vertices do not cover ${x}`);
}

function assignedHeight(geometry, assignment, x) {
    const blockIndex = Math.floor(x / geometry.terrain.superblockWidth);
    const profileIndex = blockIndex === assignment.leftBlock ? assignment.leftProfile : assignment.rightProfile;
    if (profileIndex === undefined) throw new RangeError(`Assignment misses block ${blockIndex}`);
    const samples = geometry.terrain.profiles[`S${profileIndex}`];
    const localX = x - blockIndex * geometry.terrain.superblockWidth;
    const segment = Math.min(samples.length - 2, Math.floor(localX / geometry.terrain.cadence));
    const fraction = (localX - segment * geometry.terrain.cadence) / geometry.terrain.cadence;
    const normalized = samples[segment] + (samples[segment + 1] - samples[segment]) * fraction;
    return geometry.terrain.mapping.worldScale * normalized + geometry.terrain.mapping.worldOffset;
}

function assignmentContext(geometry, assignment) {
    const heightAt = (x) => assignedHeight(geometry, assignment, x);
    const site = (id, center, platformTop) => {
        const platformLeft = center - 4.8;
        const supportXs = [
            platformLeft,
            platformLeft + 1,
            platformLeft + 8.8,
            platformLeft + 9.8,
            platformLeft + 17.6,
            platformLeft + 18.6,
        ];
        return Object.freeze({
            id,
            seed: 1,
            center,
            platformLeft,
            platformRight: center + 4.8,
            platformTop,
            platformBottom: platformTop - 0.35,
            supportFeet: supportXs.map(heightAt),
            canCollected: id === 0,
            powered: id === 0,
            nocStage: id === 0 ? 7 : 0,
        });
    };
    const originSite = site(0, assignment.originCenter, assignment.originDeck);
    const targetSite = site(1, assignment.targetCenter, assignment.targetDeck);
    const left = assignment.originCenter - 31;
    const right = assignment.targetCenter + 31;
    const xs = new Set([left, right]);
    for (let x = Math.ceil(left / 16) * 16; x <= right; x += 16) xs.add(x);
    for (const candidate of [originSite, targetSite]) {
        [
            candidate.platformLeft,
            candidate.platformLeft + 1,
            candidate.platformLeft + 8.8,
            candidate.platformLeft + 9.6,
            candidate.platformLeft + 9.8,
            candidate.platformLeft + 11.6,
            candidate.platformLeft + 17.6,
            candidate.platformLeft + 18.6,
        ].forEach((x) => xs.add(x));
    }
    return {
        seed: 1,
        originSite,
        targetSite,
        terrainVertices: [...xs].sort((leftX, rightX) => leftX - rightX).map((x) => [x, heightAt(x)]),
    };
}

test("all 312 keyed proof records replay every concrete terrain assignment", async () => {
    const fixture = JSON.parse(await readFile(join(ROOT, "tests/fixtures/lander-route-derived-v9.json"), "utf8"));
    const geometry = JSON.parse(await readFile(join(ROOT, "tests/fixtures/lander-route-geometry-v9.json"), "utf8"));
    assert.equal(REFERENCE_PROOFS.length, 312);
    assert.equal(Object.keys(REFERENCE_PROOF_CATALOG).length, 312);
    assert.equal(fixture.assignments.length, 736);
    assert.equal(fixture.records.length, 312);
    assert.equal(new Set(fixture.assignments.map(({ pairKey }) => pairKey)).size, 312);
    assert.ok(
        fixture.assignments.every(
            ({ deckDelta, originMillimeters, targetMillimeters }) =>
                Math.round(deckDelta * 1000) === targetMillimeters - originMillimeters,
        ),
    );
    for (const record of fixture.records) {
        const proof = REFERENCE_PROOF_CATALOG[record.pairKey];
        assert.ok(proof);
        assert.deepEqual(proof, {
            pairKey: record.pairKey,
            envelope: record.envelope,
            assignmentMembershipDigest: record.assignmentMembershipDigest,
            runs: record.runs,
            scheduleDigest: record.scheduleDigest,
            search: record.search,
            success: record.success,
            controllerBurn: record.controllerBurn,
            baseBurn: record.baseBurn,
            climbSurcharge: record.climbSurcharge,
            allowance: record.allowance,
            maxHullTop: record.maxHullTop,
        });
        assert.deepEqual(proof.runs[0], [1, 90]);
        assert.ok(proof.success.contactStep <= 4332);
        assert.ok(Number.isFinite(proof.maxHullTop));
        assert.equal(
            proof.allowance,
            Math.ceil((BASE_ROUTE_ALLOWANCE + proof.climbSurcharge) / FUEL_QUANTUM) * FUEL_QUANTUM,
        );
    }
    for (const assignment of fixture.assignments) {
        const proof = REFERENCE_PROOF_CATALOG[assignment.pairKey];
        const context = assignmentContext(geometry, assignment);
        assert.equal(proveRouteProof(proof, context), proof, assignment.assignmentId);
    }
    const branchCounts = Object.fromEntries(
        [
            ["deep", (assignment) => assignment.targetMillimeters - assignment.originMillimeters < -10_000],
            [
                "shallow",
                (assignment) => {
                    const delta = assignment.targetMillimeters - assignment.originMillimeters;
                    return delta >= -10_000 && delta < 0;
                },
            ],
            ["rising", (assignment) => assignment.targetMillimeters - assignment.originMillimeters >= 0],
        ].map(([key, predicate]) => [
            key,
            new Set(fixture.assignments.filter(predicate).map(({ pairKey }) => pairKey)).size,
        ]),
    );
    assert.deepEqual(branchCounts, { deep: 8, shallow: 121, rising: 183 });
    close(
        Math.ceil(Math.max(...REFERENCE_PROOFS.map(({ baseBurn }) => baseBurn)) / FUEL_QUANTUM) * FUEL_QUANTUM,
        BASE_ROUTE_ALLOWANCE,
    );
    assert.deepEqual(ROUTE_DIGESTS, {
        assignmentDigest: fixture.assignmentDigest,
        bootstrapDigest: fixture.bootstrapDigest,
        geometryDigest: fixture.geometryDigest,
        outputDigest: fixture.outputDigest,
        physicsDigest: fixture.physicsDigest,
        predecessorGeometryDigest: fixture.predecessorGeometryDigest,
        proofDigest: fixture.proofDigest,
        worldDigest: fixture.worldDigest,
    });
});

test("accepted touchdown margins settle to one centered immutable checkpoint", async () => {
    const fixture = JSON.parse(await readFile(join(ROOT, "tests/fixtures/lander-route-derived-v9.json"), "utf8"));
    const geometry = JSON.parse(await readFile(join(ROOT, "tests/fixtures/lander-route-geometry-v9.json"), "utf8"));
    for (const assignment of fixture.assignments.filter((_, index) => index % 257 === 0)) {
        const context = assignmentContext(geometry, assignment);
        for (const x of [
            context.originSite.platformLeft + 1.621,
            context.originSite.center,
            context.originSite.platformRight - 1.621,
        ]) {
            const contact = { x, y: context.originSite.platformTop, vx: 0, vy: -1, angle: 0, angularVelocity: 0 };
            const pose = checkpointPoseForContact(context.originSite, contact);
            assert.equal(pose.x, context.originSite.center);
            assert.equal(pose.y, context.originSite.platformTop);
            assert.equal(
                proveRouteProof(REFERENCE_PROOF_CATALOG[assignment.pairKey], { ...context, pose }).success
                    .classification,
                "safe",
            );
        }
    }
});

test("checked v9 authority has exact schemas, ordering, digests, and CLI rejection", async () => {
    const geometryPath = join(ROOT, "tests/fixtures/lander-route-geometry-v9.json");
    const fixturePath = join(ROOT, "tests/fixtures/lander-route-derived-v9.json");
    const geometry = JSON.parse(await readFile(geometryPath, "utf8"));
    const derived = JSON.parse(await readFile(fixturePath, "utf8"));
    assert.equal(derived.schema, "agw-lander-route-derived/v9");
    assert.equal(derived.deriverVersion, "agw-lander-route-deriver/v10");
    assert.equal(derived.synthesizerVersion, "agw-lander-corridor-synthesizer/v2");
    assert.equal(derived.collisionVersion, "agw-lander-swept-collision/v2");
    assert.equal(derived.canonicalPoseDecimals, 9);
    assert.equal(digest(geometry), derived.geometryDigest);
    assert.equal(digest(derived.assignments), derived.assignmentDigest);
    assert.equal(
        digest({ records: derived.records, openings: derived.openings, terminal: derived.terminal }),
        derived.proofDigest,
    );
    assert.equal(digest(derived.worldWitnesses), derived.worldDigest);
    const { outputDigest, ...unsignedDerived } = derived;
    assert.equal(digest(unsignedDerived), outputDigest);
    assert.equal(derived.openings.length, 16);
    const rawMinimumOpeningReserve = Math.min(...derived.openings.map(({ reserve }) => reserve));
    assert.equal(rawMinimumOpeningReserve, 6.385999999999723);
    assert.ok(rawMinimumOpeningReserve < 6.386);
    assert.equal(
        Math.min(...derived.openings.map(({ reserve }) => Number(reserve.toFixed(6)))),
        6.386,
    );
    assert.equal(derived.worldWitnesses.length, 808);
    assert.equal(derived.records.filter((record) => record.search.macroExpansions === 0).length, 111);
    assert.equal(derived.records.filter((record) => record.search.macroExpansions !== 0).length, 201);
    assert.ok(
        Math.max(
            ...derived.records
                .filter((record) => record.search.macroExpansions === 0)
                .map((record) => record.search.terminalReplays),
        ) <= 7,
    );
    assert.deepEqual(derived.terminal, {
        siteIndex: 4095,
        deckDelta: 0,
        allowance: 13.4,
        ratio: 1,
        award: 13.4,
        completedSites: 4096,
        generatorCursor: 4096,
        activeSiteId: 4095,
        targetSiteId: null,
        targetRouteProof: null,
        cueDirection: null,
    });
    assert.deepEqual(
        [...new Set(derived.worldWitnesses.map(({ descriptor }) => descriptor.seed))],
        [11, 39, 41, STATIC_WORLD_SEED],
    );
    for (const { descriptor, digest: descriptorDigest } of derived.worldWitnesses) {
        const runtimeSite = createSiteForIndex(descriptor.seed, descriptor.siteIndex);
        assert.deepEqual(descriptor.site, {
            index: runtimeSite.id,
            nominalCenter: runtimeSite.nominalCenter,
            candidateOrder: runtimeSite.candidateOrder,
            candidateOrdinal: runtimeSite.candidateOrdinal,
            offsetIndex: runtimeSite.offsetIndex,
            center: runtimeSite.center,
            closedFootprint: runtimeSite.closedFootprint,
            localNativeMaximum: runtimeSite.localNativeMaximum,
            platformTop: runtimeSite.platformTop,
            normalizedDeck: runtimeSite.normalizedDeck,
            supportFeet: runtimeSite.supportFeet,
        });
        for (const block of descriptor.superblocks) {
            const runtimeBlock = terrainProfileForBlock(descriptor.seed, block.index);
            assert.deepEqual(block, {
                epoch: runtimeBlock.epoch,
                index: block.index,
                profile: runtimeBlock.profile,
                slot: runtimeBlock.slot,
                vertices: runtimeBlock.samples.map((height, index) => [
                    block.index * 512 + index * 16,
                    64 * height - 9.2,
                ]),
            });
        }
        assert.equal(digest(descriptor), descriptorDigest);
    }
    assert.equal(spawnSync(process.execPath, [join(ROOT, "tools/derive_lander_routes.mjs"), "--bogus"]).status, 2);

    const directory = await mkdtemp(join(tmpdir(), "agw-route-test-"));
    const changedGeometry = join(directory, "geometry.json");
    const predecessorPath = join(ROOT, "tests/fixtures/lander-route-geometry-v8.json");
    const bootstrapPath = join(ROOT, "tests/fixtures/lander-route-derived-v8.json");
    geometry.site.nominalSpacing = 95;
    await writeFile(changedGeometry, JSON.stringify(geometry) + "\n", "utf8");
    assert.equal(
        spawnSync(process.execPath, [
            join(ROOT, "tools/derive_lander_routes.mjs"),
            "--geometry",
            changedGeometry,
            "--predecessor-geometry",
            predecessorPath,
            "--bootstrap",
            bootstrapPath,
            "--output",
            join(directory, "output.json"),
        ]).status,
        1,
    );

    const projected = join(directory, "lander-route-proofs.generated.js");
    assert.equal(
        spawnSync(process.execPath, [
            join(ROOT, "tools/project_lander_route_proofs.mjs"),
            "--fixture",
            fixturePath,
            "--output",
            projected,
        ]).status,
        0,
    );
    assert.equal(
        await readFile(projected, "utf8"),
        await readFile(join(ROOT, "static/lander-route-proofs.generated.js"), "utf8"),
    );
});

test("production route proof rejects a weak or inexact launch prefix before collision replay", async () => {
    const fixture = JSON.parse(await readFile(join(ROOT, "tests/fixtures/lander-route-derived-v9.json"), "utf8"));
    const geometry = JSON.parse(await readFile(join(ROOT, "tests/fixtures/lander-route-geometry-v9.json"), "utf8"));
    const proof = REFERENCE_PROOFS[0];
    const context = assignmentContext(
        geometry,
        fixture.assignments.find(({ pairKey }) => pairKey === proof.pairKey),
    );
    assert.throws(
        () => proveRouteProof({ ...proof, runs: [[2, 90], ...proof.runs.slice(1)] }, context),
        Error,
    );
    assert.throws(
        () => proveRouteProof({ ...proof, runs: [[1, 89], ...proof.runs.slice(1)] }, context),
        Error,
    );
});

test("four seeded 100-site powered missions keep lifecycle and generation timing bounded", () => {
    const durations = [];
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let model = updateRetention(createRun({ seed, reducedMotion: true }));
        for (let completed = 0; completed < 100; completed += 1) {
            if (model.state === "launching") {
                for (let step = 0; step < 90 && model.state === "launching"; step += 1) {
                    model = updateRetention(stepFlight(model, { left: 0.72, right: 0.72 }));
                }
            }
            const target = model.retainedSites.find((site) => site.id === model.targetSiteId);
            model = {
                ...model,
                pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1, angle: 0, angularVelocity: 0 },
            };
            const started = performance.now();
            model = updateRetention(stepFlight(model, { left: 0, right: 0 }));
            durations.push(performance.now() - started);
            assert.equal(model.state, "launching");
            const powered = model.retainedSites.find((site) => site.id === model.activeSiteId);
            assert.deepEqual([powered.powered, powered.nocStage], [true, 7]);
            assert.ok(model.checkpoint);
            assert.ok(model.retainedSites.length <= 3);
            assert.ok(model.retainedChunks.length <= 5);
            assert.ok(model.terrainVertices.length <= 48);
            assert.equal(
                (((36 + 96 * (completed + 16)) % 512) + 512) % 512,
                (((36 + 96 * completed) % 512) + 512) % 512,
            );
        }
        assert.equal(model.completedSites, 100);
        assert.ok(model.refuelRatio >= 1);
    }
    durations.sort((left, right) => left - right);
    const p95 = durations[Math.ceil(durations.length * 0.95) - 1];
    assert.ok(p95 < 25, `generation p95 was ${p95} ms`);
    assert.ok(durations.at(-1) < 50, `generation maximum was ${durations.at(-1)} ms`);
});
