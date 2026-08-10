export const STATIC_WORLD_SEED = 0x41475731;
export const CHUNK_WIDTH = 20;
export const TERRAIN_SAMPLE_SPACING = 4;
export const PLATFORM_WIDTH = 9.6;
export const PLATFORM_THICKNESS = 0.35;
export const PLATFORM_CLEARANCE = 0.8;
export const TARGET_DECK_BAND = Object.freeze([1.55, 8.3]);

const MOTIF = Object.freeze([0, 1.2, -0.8, 1, -0.6, 0]);
const TEMPLATE_SLOT_ORDER = Object.freeze([78, 81, 84, 87, 90, 93, 96, 99, 102]);

function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
}

function freeze(value) {
    if (Array.isArray(value)) {
        value.forEach(freeze);
    } else if (value && typeof value === "object") {
        Object.values(value).forEach(freeze);
    }
    return Object.freeze(value);
}

export function normalizeSeed(value) {
    const normalized = Number(value) >>> 0;
    return normalized === 0 ? 0x6d2b79f5 : normalized;
}

export function mixUint32(input) {
    let value = Number(input) >>> 0;
    value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
    value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
    return (value ^ (value >>> 16)) >>> 0;
}

export function sampleUnit(seed, stream, index) {
    const value =
        normalizeSeed(seed) ^
        Math.imul(stream, 0x9e3779b9) ^
        Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b);
    return mixUint32(value) / 2 ** 32;
}

function boundaryHeight(seed, boundaryIndex) {
    return 2 + 3 * sampleUnit(seed, 1, boundaryIndex >>> 0);
}

export function terrainSample(seed, sampleIndex) {
    const chunkIndex = Math.floor(sampleIndex / 5);
    const localIndex = sampleIndex - chunkIndex * 5;
    if (localIndex === 0) {
        return boundaryHeight(seed, chunkIndex);
    }
    const start = boundaryHeight(seed, chunkIndex);
    const end = boundaryHeight(seed, chunkIndex + 1);
    const base = start + (end - start) * (localIndex / 5);
    const sign = sampleUnit(seed, 2, chunkIndex >>> 0) >= 0.5 ? 1 : -1;
    return clamp(base + sign * MOTIF[localIndex], 0.75, 7.5);
}

export function terrainHeightAt(seed, x) {
    const leftIndex = Math.floor(x / TERRAIN_SAMPLE_SPACING);
    const leftX = leftIndex * TERRAIN_SAMPLE_SPACING;
    const fraction = (x - leftX) / TERRAIN_SAMPLE_SPACING;
    const left = terrainSample(seed, leftIndex);
    const right = terrainSample(seed, leftIndex + 1);
    return left + (right - left) * fraction;
}

export function terrainHeightFromVertices(vertices, x) {
    for (let index = 1; index < vertices.length; index += 1) {
        const left = vertices[index - 1]; const right = vertices[index];
        if (x < left[0] || x > right[0]) continue;
        if (right[0] === left[0]) return Math.min(left[1], right[1]);
        return left[1] + (right[1] - left[1]) * ((x - left[0]) / (right[0] - left[0]));
    }
    throw new RangeError(`Terrain vertices do not cover ${x}`);
}

export function siteFoundationBottom(vertices, site) {
    const left = site.platformRight + 2;
    return Math.min(terrainHeightFromVertices(vertices, left), terrainHeightFromVertices(vertices, left + 7));
}

export function nativeTerrainVertices(seed, left, right) {
    const first = Math.floor(left / TERRAIN_SAMPLE_SPACING);
    const last = Math.ceil(right / TERRAIN_SAMPLE_SPACING);
    const vertices = [];
    if (left > first * TERRAIN_SAMPLE_SPACING) {
        vertices.push([left, terrainHeightAt(seed, left)]);
    }
    for (let index = first; index <= last; index += 1) {
        const x = index * TERRAIN_SAMPLE_SPACING;
        if (x >= left && x <= right) {
            vertices.push([x, terrainSample(seed, index)]);
        }
    }
    if (right < last * TERRAIN_SAMPLE_SPACING) {
        vertices.push([right, terrainHeightAt(seed, right)]);
    }
    return vertices;
}

function maximumTerrain(seed, left, right) {
    return Math.max(...nativeTerrainVertices(seed, left, right).map((point) => point[1]));
}

export function createFirstSite(seed) {
    const normalized = normalizeSeed(seed);
    const center = 36;
    const platformLeft = center - PLATFORM_WIDTH / 2;
    const platformRight = center + PLATFORM_WIDTH / 2;
    const platformTop = maximumTerrain(normalized, platformLeft, platformRight) + PLATFORM_CLEARANCE;
    return freeze({
        id: 0,
        center,
        platformLeft,
        platformRight,
        platformTop,
        platformBottom: platformTop - PLATFORM_THICKNESS,
        canCollected: false,
        powered: false,
        nocStage: 0,
        templateId: "initial",
    });
}

export function templatePreference(seed, siteIndex) {
    const base = Math.floor(9 * sampleUnit(seed, 3, siteIndex));
    const preferred = [];
    for (let count = 0; count < 9; count += 1) {
        preferred.push(TEMPLATE_SLOT_ORDER[(base + 4 * count) % 9]);
    }
    return preferred;
}

export function selectTemplate(seed, siteIndex, originSite, templates) {
    const byDistance = new Map(templates.map((template) => [template.centerDelta, template]));
    for (const distance of templatePreference(seed, siteIndex)) {
        const template = byDistance.get(distance);
        if (!template) {
            throw new Error(`Missing route template for ${distance} metres`);
        }
        const top = originSite.platformTop + template.deckDelta;
        if (top >= TARGET_DECK_BAND[0] && top <= TARGET_DECK_BAND[1]) {
            return template;
        }
    }
    throw new Error("No eligible route template");
}

function interpolateKnots(knots, x) {
    if (x <= knots[0][0]) {
        return knots[0][1];
    }
    for (let index = 1; index < knots.length; index += 1) {
        const right = knots[index];
        const left = knots[index - 1];
        if (x <= right[0]) {
            const fraction = (x - left[0]) / (right[0] - left[0]);
            return left[1] + (right[1] - left[1]) * fraction;
        }
    }
    return knots.at(-1)[1];
}

export function instantiateTemplateSite(seed, siteIndex, originSite, template) {
    const center = originSite.center + template.centerDelta;
    const platformTop = originSite.platformTop + template.deckDelta;
    return freeze({
        id: siteIndex,
        center,
        platformLeft: center - PLATFORM_WIDTH / 2,
        platformRight: center + PLATFORM_WIDTH / 2,
        platformTop,
        platformBottom: platformTop - PLATFORM_THICKNESS,
        canCollected: false,
        powered: false,
        nocStage: 0,
        templateId: template.templateId,
        originSiteId: originSite.id,
        clearanceKnots: template.clearanceKnots.map((point) => [...point]),
        seed: normalizeSeed(seed),
    });
}

export function corridorVertices(seed, originSite, targetSite) {
    const originRight = originSite.platformRight;
    const targetLeft = targetSite.platformLeft;
    const vertices = [[originRight, originSite.platformTop - PLATFORM_CLEARANCE]];
    const firstIndex = Math.floor(originRight / TERRAIN_SAMPLE_SPACING) + 1;
    const lastIndex = Math.ceil(targetLeft / TERRAIN_SAMPLE_SPACING) - 1;
    for (let index = firstIndex; index <= lastIndex; index += 1) {
        const x = index * TERRAIN_SAMPLE_SPACING;
        const raw = terrainSample(seed, index);
        const relativeX = x - originSite.center;
        const cap = originSite.platformTop + interpolateKnots(targetSite.clearanceKnots, relativeX);
        const y = raw > cap ? Math.max(0.75, cap - 0.15 * sampleUnit(seed, 4, index >>> 0)) : raw;
        vertices.push([x, y]);
    }
    vertices.push([targetLeft, targetSite.platformTop - PLATFORM_CLEARANCE]);
    vertices.push([targetSite.platformRight, targetSite.platformTop - PLATFORM_CLEARANCE]);
    return vertices;
}

function deduplicateVertices(vertices) {
    const result = [];
    for (const point of vertices) {
        const previous = result.at(-1);
        if (previous && previous[0] === point[0]) {
            previous[1] = Math.min(previous[1], point[1]);
        } else {
            result.push([...point]);
        }
    }
    return result;
}

export function terrainVerticesForWindow(seed, sites, left, right) {
    let vertices = nativeTerrainVertices(seed, left, right);
    const ordered = [...sites].sort((a, b) => a.center - b.center);
    for (const site of ordered) {
        vertices = vertices.filter(([x]) => x < site.platformLeft || x > site.platformRight);
        vertices.push(
            [site.platformLeft, site.platformTop - PLATFORM_CLEARANCE],
            [site.platformRight, site.platformTop - PLATFORM_CLEARANCE],
        );
    }
    for (let index = 1; index < ordered.length; index += 1) {
        const origin = ordered[index - 1];
        const target = ordered[index];
        if (target.clearanceKnots) {
            vertices = vertices.filter(([x]) => x < origin.platformRight || x > target.platformRight);
            vertices.push(...corridorVertices(seed, origin, target));
            const resume = Math.floor(target.platformRight / TERRAIN_SAMPLE_SPACING) + 1;
            vertices.push([resume * TERRAIN_SAMPLE_SPACING, terrainSample(seed, resume)]);
        }
    }
    return freeze(deduplicateVertices(vertices.filter(([x]) => x >= left && x <= right).sort((a, b) => a[0] - b[0])));
}

export function retainedChunkIndexes(cameraLeft) {
    const left = cameraLeft - 40;
    const right = cameraLeft + 140;
    const first = Math.floor(left / CHUNK_WIDTH);
    const last = Math.ceil(right / CHUNK_WIDTH) - 1;
    return freeze(Array.from({ length: last - first + 1 }, (_, offset) => first + offset));
}

export function retainedSiteDescriptors(sites, activeSiteId, targetSiteId) {
    const byId = new Map(sites.map((site) => [site.id, site]));
    const ids = new Set([activeSiteId, targetSiteId]);
    if (activeSiteId !== null) {
        ids.add(activeSiteId - 1);
    }
    return freeze(
        [...ids]
            .filter((id) => id !== null && byId.has(id))
            .sort((a, b) => a - b)
            .map((id) => byId.get(id)),
    );
}

export function cameraLeftForPose(pose) {
    return Math.max(0, pose.x - 35);
}

export function targetIsOffscreen(targetSite, cameraLeft) {
    return Boolean(targetSite && targetSite.platformLeft > cameraLeft + 100);
}

export function terrainPath(vertices) {
    if (vertices.length === 0) {
        return "";
    }
    const points = vertices.map(([x, y]) => `${x * 10} ${548 - y * 10}`).join("L");
    return `M${points}L${vertices.at(-1)[0] * 10} 648L${vertices[0][0] * 10} 648Z`;
}
