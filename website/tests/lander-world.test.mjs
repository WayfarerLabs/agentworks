import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    CHUNK_WIDTH,
    DECK_LEVELS,
    STATIC_WORLD_SEED,
    TERRAIN_GRADE_CHANGE_LIMIT,
    TERRAIN_GRADE_LIMIT,
    TERRAIN_SUBDIVISIONS,
    cameraLeftForPose,
    createFirstSite,
    createSiteForIndex,
    instantiateTemplateSite,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    selectTemplate,
    siteScaffoldMembers,
    siteScaffoldPath,
    siteStructure,
    skyProjectionForCamera,
    targetDirectionForViewport,
    terrainCycleForSeed,
    terrainFillPath,
    terrainHeightAt,
    terrainHeightFromVertices,
    terrainSiteForIndex,
    terrainSurfacePath,
    terrainVerticesForRange,
    terrainVerticesForWindow,
} from "../static/lander-world.js";

const GEOMETRY_URL = new URL("fixtures/lander-route-geometry-v6.json", import.meta.url);
const TEMPLATE_URL = new URL("../templates/lander-game.html", import.meta.url);

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    }
    return value;
}

function close(actual, expected, tolerance = 1e-12) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

test("seeded cycle lookup is direct, signed, and repeats exactly in both directions", () => {
    const expected = new Map([
        [11, ["A", 2, 261, [[67,"route-96-fall"],[59,"route-81-rise"],[75,"route-84-fall"]]]],
        [39, ["A", 0, 261, [[59,"route-81-rise"],[75,"route-84-fall"],[67,"route-96-fall"]]]],
        [41, ["B", 1, 276, [[67,"route-99-rise"],[75,"route-90-fall"],[59,"route-87-rise"]]]],
        [STATIC_WORLD_SEED, ["B", 2, 276, [[75,"route-90-fall"],[59,"route-87-rise"],[67,"route-99-rise"]]]],
    ]);
    for (const [seed, [family, phase, blockWidth, slots]] of expected) {
        const cycle = terrainCycleForSeed(seed);
        assert.deepEqual([cycle.family, cycle.phase, cycle.blockWidth], [family, phase, blockWidth]);
        assert.deepEqual(cycle.slots.map((slot) => [slot.terrainLevel, slot.templateId]), slots);
        for (let index = -100; index <= 100; index += 1) {
            const site = terrainSiteForIndex(seed, index);
            const repeated = terrainSiteForIndex(seed, index + 3);
            assert.deepEqual([repeated.center - site.center, repeated.terrainLevel, repeated.deckLevel,
                repeated.templateId], [blockWidth, site.terrainLevel, site.deckLevel, site.templateId]);
        }
    }
});

test("native terrain alternates smooth summits and valleys within hard chord bounds", () => {
    assert.equal(TERRAIN_SUBDIVISIONS, 8);
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        for (let index = -9; index <= 9; index += 1) {
            const site = terrainSiteForIndex(seed, index);
            const next = terrainSiteForIndex(seed, index + 1);
            const middle = (site.center + next.center) / 2;
            close(terrainHeightAt(seed, site.center), site.terrainLevel / 10);
            close(terrainHeightAt(seed, middle), site.valleyLevel / 10);
            close(terrainHeightAt(seed, next.center), next.terrainLevel / 10);
            const vertices = terrainVerticesForWindow(seed, [], site.center, next.center);
            assert.equal(vertices.length, 17);
            const grades = vertices.slice(1).map(([x, y], vertexIndex) =>
                (y - vertices[vertexIndex][1]) / (x - vertices[vertexIndex][0]));
            assert.ok(grades.slice(0, 8).every((grade) => grade <= 1e-12));
            assert.ok(grades.slice(8).every((grade) => grade >= -1e-12));
            assert.ok(grades.every((grade) => Math.abs(grade) <= TERRAIN_GRADE_LIMIT + 1e-12));
            assert.ok(grades.slice(1).every((grade, gradeIndex) =>
                Math.abs(grade - grades[gradeIndex]) <= TERRAIN_GRADE_CHANGE_LIMIT + 1e-12));
        }
    }
});

test("every deck is exactly 2.4 metres above its own unchanged native summit", () => {
    assert.deepEqual(DECK_LEVELS, [83, 91, 99]);
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        for (let index = -12; index <= 100; index += 1) {
            const site = createSiteForIndex(seed, index);
            const vertices = terrainVerticesForWindow(seed, [site], site.platformLeft, site.platformLeft + 18.6);
            const localMaximum = Math.max(...vertices.map(([, y]) => y));
            close(localMaximum, site.terrainLevel / 10);
            close(site.platformTop, localMaximum + 2.4);
            assert.equal(site.deckLevel, site.terrainLevel + 24);
        }
    }
});

test("one exact route lookup terminates and rejects cycle or catalog drift", async () => {
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let origin = createFirstSite(seed);
        for (let siteIndex = 1; siteIndex <= 100; siteIndex += 1) {
            const template = selectTemplate(seed, siteIndex, origin, geometry.templates);
            const target = instantiateTemplateSite(seed, siteIndex, origin, template);
            assert.equal(template.templateId, terrainSiteForIndex(seed, siteIndex - 1).templateId);
            origin = target;
        }
        assert.equal(origin.id, 100);
    }
    const origin = createFirstSite(11);
    assert.throws(() => selectTemplate(11, 1, { ...origin, center: 37 }, geometry.templates), /direct terrain cycle/);
    assert.throws(() => selectTemplate(11, 1, origin,
        geometry.templates.filter(({ templateId }) => templateId !== "route-96-fall")), /Missing exact route/);
});

test("strict-x projection inserts native feet and reaches the exact 72-vertex ceiling", () => {
    const sites = [-19, -18, -17].map((index) => createSiteForIndex(39, index));
    assert.deepEqual(sites.map(({ center }) => center), [-1626, -1530, -1449]);
    const maximum = terrainVerticesForWindow(39, sites, -1650, -1400);
    assert.equal(maximum.length, 72);
    assert.ok(maximum.every((point, index) => index === 0 || maximum[index - 1][0] < point[0]));
    for (const site of sites) {
        for (const column of siteStructure(site).supportColumns) {
            close(terrainHeightFromVertices(maximum, column.left), column.leftFoot);
            close(terrainHeightFromVertices(maximum, column.right), column.rightFoot);
        }
    }
    assert.deepEqual(terrainVerticesForWindow(39, sites, -1650, -1400), maximum);
});

test("terrain renders as one closed unstroked fill and one open strict surface", async () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const vertices = terrainVerticesForWindow(STATIC_WORLD_SEED, [site], 0, 100);
    const fill = terrainFillPath(vertices);
    const surface = terrainSurfacePath(vertices);
    assert.match(fill, /Z$/);
    assert.doesNotMatch(surface, /[ZV]/);
    assert.equal(surface.match(/M/g)?.length, 1);
    const template = await readFile(TEMPLATE_URL, "utf8");
    const layer = template.match(/<g id="terrain-layer">([\s\S]*?)<\/g>/)?.[1] ?? "";
    assert.equal((layer.match(/<path/g) ?? []).length, 2);
    assert.match(layer, /class="terrain-fill"[^>]+fill="#d7d2c4"[^>]+stroke="none"/);
    assert.match(layer, /class="terrain-surface"[^>]+fill="none"[^>]+stroke="#4b4e55"/);
    assert.equal(terrainVerticesForRange(vertices, 0, 10).at(-1)[0], 10);
});

test("site has one twelve-bay truss and three independently footed lattice columns", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const structure = siteStructure(site);
    const members = siteScaffoldMembers(site);
    assert.ok(members.length >= 41 && members.length <= 95);
    assert.equal(members.slice(2, 14).length, 12);
    assert.deepEqual(structure.supportColumns.map(({ left, right }) => [left, right]), [
        [site.platformLeft, site.platformLeft + 1],
        [site.platformLeft + 8.8, site.platformLeft + 9.8],
        [site.platformLeft + 17.6, structure.buildingRight],
    ]);
    assert.deepEqual(structure.truss, {
        bottom: site.platformBottom - 0.85,
        left: site.platformLeft - 0.1,
        right: structure.buildingRight + 0.1,
        top: site.platformBottom + 0.1,
    });
    for (const column of structure.supportColumns) {
        assert.equal(column.collider.bottom, Math.min(column.leftFoot, column.rightFoot) - 0.1);
        assert.ok(column.levels.slice(1).every((level, index) => column.levels[index] > level));
        assert.ok(column.levels.slice(1).every((level, index) => column.levels[index] - level <= 0.8 + 1e-12));
    }
    assert.equal(siteScaffoldPath(site).match(/M/g)?.length, members.length);
});

test("camera, retention, sky, and v6 geometry remain bounded and deterministic", async () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    assert.deepEqual([cameraLeftForPose({ x: 34 }), cameraLeftForPose({ x: 80 }),
        cameraLeftForPose({ x: -20 })], [0, 45, -25]);
    assert.equal(CHUNK_WIDTH, 50);
    assert.ok(retainedChunkIndexes(45).length <= 5);
    assert.ok(retainedSiteDescriptors([site], 0, 0).length <= 3);
    assert.equal(targetDirectionForViewport({ platformLeft: 200, platformRight: 210 }, 0), "right");
    assert.equal(targetDirectionForViewport({ platformLeft: -20, platformRight: -10 }, 0), "left");
    assert.equal(targetDirectionForViewport({ platformLeft: 99, platformRight: 110 }, 0), null);
    const sky = skyProjectionForCamera(STATIC_WORLD_SEED, 0);
    assert.equal(sky.chunks.length, 5);
    assert.equal((sky.starsPath.match(/h2/g) ?? []).length, 20);

    const text = await readFile(GEOMETRY_URL, "utf8");
    const geometry = JSON.parse(text);
    assert.equal(geometry.schema, "agw-lander-route-geometry/v6");
    assert.equal(geometry.templates.length, 6);
    assert.equal(geometry.terrain.subdivisionsPerHalfLeg, 8);
    assert.equal(createHash("sha256").update(JSON.stringify(canonical(geometry))).digest("hex"),
        "d0393f958f4a8b2657dbf21ef5184f40527be2c8c8d5cf860e6e81e3d2971fa2");
});
