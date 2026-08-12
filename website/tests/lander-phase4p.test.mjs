import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { createRun } from "../static/lander-model.js";
import {
    cameraForPose,
    STATIC_WORLD_SEED,
    terrainHeightAt,
    terrainNormalizedHeightAt,
    worldViewportX,
    worldViewportY,
} from "../static/lander-world.js";

const ROOT = new URL("../", import.meta.url).pathname;
const TOOL = join(ROOT, "tools/derive_lander_routes.mjs");
const GEOMETRY = join(ROOT, "tests/fixtures/lander-route-geometry-v5.json");
const AUTHORITY = join(ROOT, "tests/fixtures/lander-route-derived-v5.json");

function run(tool, output) {
    return spawnSync(process.execPath,
        [tool, "--geometry", GEOMETRY, "--output", output, "--verify", AUTHORITY],
        { encoding: "utf8" });
}

test("Phase 4P authority regenerates byte-for-byte and terrain mutations fail closed", async () => {
    const directory = await mkdtemp(join(tmpdir(), "agw-phase4p-"));
    const output = join(directory, "derived.json");
    await writeFile(join(directory, "lander_clear_faces.mjs"),
        await readFile(join(ROOT, "tools/lander_clear_faces.mjs"), "utf8"), "utf8");
    const ordinary = run(TOOL, output);
    assert.equal(ordinary.status, 0, ordinary.stderr);
    assert.equal(await readFile(output, "utf8"), await readFile(AUTHORITY, "utf8"));

    const source = await readFile(TOOL, "utf8");
    const mutations = [
        ["anchor-stream.mjs", "sampleUnit(seed, 13, span >>> 0)", "sampleUnit(seed, 15, span >>> 0)"],
        ["sample-spacing.mjs", "const TERRAIN_SAMPLE_SPACING = 10;", "const TERRAIN_SAMPLE_SPACING = 11;"],
        ["deck-datum.mjs", "const DECK_LEVEL = 116;", "const DECK_LEVEL = 115;"],
        ["projection.mjs", "return 64 * terrainNormalizedHeightAt(seed, x) - 29.2;",
            "return 63 * terrainNormalizedHeightAt(seed, x) - 29.2;"],
    ];
    for (const [name, before, after] of mutations) {
        const changed = source.replace(before, after);
        assert.notEqual(changed, source);
        const path = join(directory, name);
        await writeFile(path, changed, "utf8");
        assert.equal(run(path, output).status, 1, name);
    }
});

test("Phase 4P exact extrema and opening camera vectors survive production projection", () => {
    assert.equal(terrainNormalizedHeightAt(STATIC_WORLD_SEED, 0), 0.4085759765235707);
    assert.equal(terrainNormalizedHeightAt(STATIC_WORLD_SEED, 100), 0.4399995140650219);
    assert.equal(terrainNormalizedHeightAt(11, -640), 0.10337466620840133);
    assert.equal(terrainNormalizedHeightAt(41, 1920), 0.5986480843508616);
    for (const seed of [1, 11, 39, 41, 0xffffffff]) {
        for (let x = -2000; x <= 2000; x += 5) {
            const normalized = terrainNormalizedHeightAt(seed, x);
            assert.ok(normalized >= 0.1 && normalized <= 0.6);
            assert.equal(terrainHeightAt(seed, x), 64 * normalized - 29.2);
        }
    }
    const pose = createRun({ seed: 1 }).pose;
    const camera = cameraForPose(pose);
    assert.deepEqual(camera, { left: 0, down: 79 });
    assert.equal(worldViewportX(pose.x, camera), 300);
    assert.equal(worldViewportY(pose.y, camera), 107);
});
