import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    CHUNK_WIDTH,
    PLATFORM_CLEARANCE,
    SITE_SPACING,
    STATIC_WORLD_SEED,
    TERRAIN_BLOCK_WIDTH,
    TERRAIN_GRADE_CHANGE_LIMIT,
    TERRAIN_GRADE_LIMIT,
    TERRAIN_PROFILES,
    TERRAIN_VERTEX_CADENCE,
    cameraLeftForPose,
    createFirstSite,
    createSiteForIndex,
    instantiateTemplateSite,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    routePairKey,
    selectRouteProof,
    selectTemplate,
    siteScaffoldMembers,
    siteScaffoldPath,
    siteStructure,
    skyProjectionForCamera,
    targetDirectionForViewport,
    terrainHeightAt,
    terrainHeightFromVertices,
    terrainParityForSeed,
    terrainProfileForBlock,
    terrainSurfacePath,
    terrainVerticesForRange,
    terrainVerticesForWindow,
} from "../static/lander-world.js";

const GEOMETRY_URL = new URL("fixtures/lander-route-geometry-v7.json", import.meta.url);
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

test("global terrain is a deterministic signed 16/128 metre straight polyline", () => {
    assert.equal(TERRAIN_VERTEX_CADENCE, 16);
    assert.equal(TERRAIN_BLOCK_WIDTH, 128);
    assert.equal(TERRAIN_GRADE_LIMIT, 0.36);
    assert.equal(TERRAIN_GRADE_CHANGE_LIMIT, 0.4);
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        assert.ok([0, 1].includes(terrainParityForSeed(seed)));
        const profileIds = new Set();
        for (let block = -100; block <= 100; block += 1) {
            const selected = terrainProfileForBlock(seed, block);
            profileIds.add(selected.id);
            assert.equal(selected.id[0], (((block + terrainParityForSeed(seed)) % 2) + 2) % 2 === 0 ? "H" : "L");
            const points = selected.samples.map((_, index) => [
                block * 128 + index * 16,
                terrainHeightAt(seed, block * 128 + index * 16),
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

test("route-only site X and closed native footprint produce exact local decks", () => {
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        for (let index = -100; index <= 100; index += 1) {
            const site = createSiteForIndex(seed, index);
            assert.equal(site.center, 36 + SITE_SPACING * index);
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
    close(createFirstSite(STATIC_WORLD_SEED).platformTop, 6.356);
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
            assert.equal(selectTemplate(seed, index + 1, origin, catalog), record);
            assert.equal(instantiateTemplateSite(seed, index + 1, origin, record).pairKey, key);
        }
    const origin = createFirstSite(11);
    assert.throws(() => selectRouteProof(origin, createSiteForIndex(11, 2), {}), /not one forward route leg/);
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
    assert.equal((layer.match(/<path/g) ?? []).length, 2);
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

test("camera, retention, sky, and geometry-v7 remain bounded and deterministic", async () => {
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
    assert.equal(geometry.schema, "agw-lander-route-geometry/v7");
    assert.equal(geometry.terrain.profiles.L1[4], 0.1);
    assert.equal(
        createHash("sha256")
            .update(JSON.stringify(canonical(geometry)))
            .digest("hex").length,
        64,
    );
});
