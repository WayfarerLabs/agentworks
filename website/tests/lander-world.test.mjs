import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    CHUNK_WIDTH,
    MAX_NORMALIZED_DECK,
    PLATFORM_CLEARANCE,
    SITE_CANDIDATE_OFFSETS,
    SITE_CANDIDATE_ORDERS,
    SITE_SPACING,
    STATIC_WORLD_SEED,
    TERRAIN_BLOCK_WIDTH,
    TERRAIN_GRADE_CHANGE_LIMIT,
    TERRAIN_GRADE_LIMIT,
    TERRAIN_PROFILES,
    TERRAIN_VERTEX_CADENCE,
    WORLD_MAX_X,
    cameraLeftForPose,
    createFirstSite,
    createSiteForIndex,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    routePairKey,
    selectRouteProof,
    siteScaffoldMembers,
    siteScaffoldPath,
    siteStructure,
    skyProjectionForCamera,
    targetDirectionForViewport,
    terrainHeightAt,
    terrainHeightFromVertices,
    terrainProfileForBlock,
    terrainSurfacePath,
    terrainVerticesForRange,
    terrainVerticesForWindow,
} from "../static/lander-world.js";

const GEOMETRY_URL = new URL("fixtures/lander-route-geometry-v9.json", import.meta.url);
const TEMPLATE_URL = new URL("../templates/lander-game.html", import.meta.url);

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
function close(actual, expected, tolerance = 1e-12) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

test("global terrain is a deterministic signed 16/512 metre straight polyline", () => {
    assert.equal(TERRAIN_VERTEX_CADENCE, 16);
    assert.equal(TERRAIN_BLOCK_WIDTH, 512);
    assert.equal(TERRAIN_GRADE_LIMIT, 0.4);
    assert.equal(TERRAIN_GRADE_CHANGE_LIMIT, 0.8);
    const expectedReversals = [12, 12, 16, 16, 16, 12, 16, 12];
    let maximumGrade = 0;
    let maximumGradeChange = 0;
    Object.values(TERRAIN_PROFILES).forEach((samples, profile) => {
        const grades = samples.slice(1).map((sample, index) => (sample - samples[index]) * 4);
        maximumGrade = Math.max(maximumGrade, ...grades.map(Math.abs));
        maximumGradeChange = Math.max(
            maximumGradeChange,
            ...grades.slice(1).map((grade, index) => Math.abs(grade - grades[index])),
        );
        const reversals = grades.filter((grade, index) => Math.sign(grade) !== Math.sign(grades[(index + 1) % 32]));
        assert.equal(reversals.length, expectedReversals[profile]);
        assert.ok(Math.min(...samples) >= 0.1 && Math.max(...samples) <= 0.6);
    });
    close(maximumGrade, TERRAIN_GRADE_LIMIT);
    close(maximumGradeChange, TERRAIN_GRADE_CHANGE_LIMIT);
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        const profileIds = new Set();
        for (let block = -100; block <= 100; block += 1) {
            const selected = terrainProfileForBlock(seed, block);
            profileIds.add(selected.id);
            const points = selected.samples.map((_, index) => [
                block * 512 + index * 16,
                terrainHeightAt(seed, block * 512 + index * 16),
            ]);
            const grades = points.slice(1).map((point, index) => (point[1] - points[index][1]) / 16);
            assert.ok(grades.every((grade) => Math.abs(grade) <= TERRAIN_GRADE_LIMIT + 1e-12));
            assert.ok(
                grades
                    .slice(1)
                    .every((grade, index) => Math.abs(grade - grades[index]) <= TERRAIN_GRADE_CHANGE_LIMIT + 1e-12),
            );
        }
        assert.deepEqual([...profileIds].sort(), Object.keys(TERRAIN_PROFILES).sort());
    }
});

test("centered terrain corpus has no short-period autocorrelation rhythm", () => {
    const pearson = (values, lag) => {
        const left = values.slice(0, -lag);
        const right = values.slice(lag);
        const leftMean = left.reduce((sum, value) => sum + value, 0) / left.length;
        const rightMean = right.reduce((sum, value) => sum + value, 0) / right.length;
        let numerator = 0;
        let leftSquare = 0;
        let rightSquare = 0;
        for (let index = 0; index < left.length; index += 1) {
            const a = left[index] - leftMean;
            const b = right[index] - rightMean;
            numerator += a * b;
            leftSquare += a * a;
            rightSquare += b * b;
        }
        return numerator / Math.sqrt(leftSquare * rightSquare);
    };
    const expected = [
        [0.08739957356836273, 32],
        [0.07807537753104245, 40],
        [0.07645001213865094, 40],
        [0.07000743972739065, 61],
    ];
    [11, 39, 41, STATIC_WORLD_SEED].forEach((seed, seedIndex) => {
        const normalized = Array.from({ length: 4096 }, (_, index) => {
            const vertex = index - 2048;
            const block = Math.floor(vertex / 32);
            return terrainProfileForBlock(seed, block).samples[vertex - block * 32];
        });
        const correlations = Array.from({ length: 49 }, (_, index) => {
            const lag = index + 16;
            return [Math.abs(pearson(normalized, lag)), lag];
        }).sort((left, right) => right[0] - left[0]);
        assert.deepEqual(correlations[0], expected[seedIndex]);
        assert.ok(correlations[0][0] < 0.09);
    });
});

test("route-only candidate order and closed native footprint produce capped exact local decks", () => {
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let expectedOrder = null;
        for (let index = -100; index <= 100; index += 1) {
            const site = createSiteForIndex(seed, index);
            assert.equal(site.nominalCenter, 36 + SITE_SPACING * index);
            expectedOrder ??= site.candidateOrder;
            assert.equal(site.candidateOrder, expectedOrder);
            assert.deepEqual(SITE_CANDIDATE_ORDERS[site.candidateOrder][site.candidateOrdinal], site.offsetIndex);
            assert.equal(site.center, site.nominalCenter + SITE_CANDIDATE_OFFSETS[site.offsetIndex]);
            assert.ok(site.candidateOrdinal <= 5);
            assert.ok(site.normalizedDeck <= MAX_NORMALIZED_DECK);
            assert.deepEqual(site.closedFootprint, [site.platformLeft, site.center + 13.8]);
            const vertices = terrainVerticesForWindow(seed, [site], site.platformLeft, site.platformLeft + 18.6);
            const localMaximum = Math.max(...vertices.map(([, y]) => y));
            close(site.localNativeMaximum, localMaximum);
            close(site.platformTop, localMaximum + PLATFORM_CLEARANCE);
            const columns = siteStructure(site).supportColumns;
            assert.deepEqual(
                columns.flatMap((column) => [column.leftFoot, column.rightFoot]),
                site.supportFeet,
            );
        }
    }
    const first = createFirstSite(STATIC_WORLD_SEED);
    close(first.platformTop, first.localNativeMaximum + PLATFORM_CLEARANCE);
});

test("signed candidate generation terminates through the final physical rail", () => {
    let minimumSpacing = Infinity;
    let maximumSpacing = -Infinity;
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let previous = null;
        for (let index = -4095; index <= 4095; index += 1) {
            const site = createSiteForIndex(seed, index);
            assert.ok(site.candidateOrdinal <= 5);
            assert.ok(site.normalizedDeck <= MAX_NORMALIZED_DECK);
            if (previous) {
                const spacing = site.center - previous.center;
                minimumSpacing = Math.min(minimumSpacing, spacing);
                maximumSpacing = Math.max(maximumSpacing, spacing);
                assert.ok(spacing >= 56 && spacing <= 136);
            }
            previous = site;
        }
    }
    assert.deepEqual([minimumSpacing, maximumSpacing], [56, 136]);
    const final = createSiteForIndex(STATIC_WORLD_SEED, 4095);
    close(WORLD_MAX_X - (final.center + 13.8), 46.2, 1e-9);
});

test("one exact keyed lookup terminates and is mutation sensitive", () => {
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED])
        for (let index = -100; index < 100; index += 1) {
            const origin = createSiteForIndex(seed, index);
            const target = createSiteForIndex(seed, index + 1);
            const key = routePairKey(origin, target);
            const reads = [];
            const record = { pairKey: key };
            const catalog = new Proxy(
                { [key]: record },
                {
                    get(object, property) {
                        reads.push(property);
                        return object[property];
                    },
                },
            );
            assert.equal(selectRouteProof(origin, target, catalog), record);
            assert.deepEqual(reads, [key]);
        }
    const origin = createFirstSite(11);
    assert.throws(() => selectRouteProof(origin, createSiteForIndex(11, 2), {}), Error);
});

test("strict-X terrain projection inserts all native feet and stays bounded", () => {
    const sites = [-1, 0, 1].map((index) => createSiteForIndex(39, index));
    const vertices = terrainVerticesForWindow(39, sites, -70, 170);
    assert.ok(vertices.length <= 48);
    assert.ok(vertices.every((point, index) => index === 0 || vertices[index - 1][0] < point[0]));
    for (const site of sites)
        for (const column of siteStructure(site).supportColumns) {
            close(terrainHeightFromVertices(vertices, column.left), column.leftFoot);
            close(terrainHeightFromVertices(vertices, column.right), column.rightFoot);
        }
    assert.deepEqual(terrainVerticesForWindow(39, sites, -70, 170), vertices);
    assert.equal(terrainVerticesForRange(vertices, -10, 10).at(-1)[0], 10);
});

test("terrain surface is one open mitered straight path", async () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const vertices = terrainVerticesForWindow(STATIC_WORLD_SEED, [site], 0, 100);
    const surface = terrainSurfacePath(vertices);
    assert.doesNotMatch(surface, /[ACQSTVZ]/);
    assert.equal(surface.match(/M/g)?.length, 1);
    const template = await readFile(TEMPLATE_URL, "utf8");
    const layer = template.match(/<g id="terrain-layer">([\s\S]*?)<\/g>/)?.[1] ?? "";
    assert.equal((layer.match(/<path/g) ?? []).length, 3);
    assert.match(layer, /class="terrain-surface"[\s\S]*?stroke-linejoin="miter"[\s\S]*?stroke-miterlimit="2"/);
});

test("integrated truss and three unbounded supports reach their native feet", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const structure = siteStructure(site);
    const members = siteScaffoldMembers(site);
    assert.equal(members.slice(2, 14).length, 12);
    assert.equal(structure.supportColumns.length, 3);
    for (const column of structure.supportColumns) {
        assert.equal(column.collider.bottom, Math.min(column.leftFoot, column.rightFoot) - 0.1);
        assert.ok(column.levels.slice(1).every((level, index) => column.levels[index] > level));
        assert.ok(column.levels.slice(1).every((level, index) => column.levels[index] - level <= 0.8 + 1e-12));
    }
    assert.equal(siteScaffoldPath(site).match(/M/g)?.length, members.length);
});

test("camera, retention, sky, and geometry-v9 remain bounded and deterministic", async () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    assert.deepEqual(
        [cameraLeftForPose({ x: 34 }), cameraLeftForPose({ x: 80 }), cameraLeftForPose({ x: -20 })],
        [0, 45, -25],
    );
    assert.equal(CHUNK_WIDTH, 50);
    assert.ok(retainedChunkIndexes(45).length <= 5);
    assert.ok(retainedSiteDescriptors([site], 0, 0).length <= 3);
    assert.equal(targetDirectionForViewport({ platformLeft: 200, platformRight: 210 }, 0), "right");
    assert.equal(targetDirectionForViewport({ platformLeft: -20, platformRight: -10 }, 0), "left");
    assert.equal(targetDirectionForViewport({ platformLeft: 99, platformRight: 110 }, 0), null);
    const sky = skyProjectionForCamera(STATIC_WORLD_SEED, 0);
    assert.equal(sky.chunks.length, 5);
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    assert.equal(geometry.schema, "agw-lander-route-geometry/v9");
    assert.equal(geometry.terrain.profiles.S4[5], 0.6);
    assert.equal(
        createHash("sha256")
            .update(JSON.stringify(canonical(geometry)))
            .digest("hex").length,
        64,
    );
});
