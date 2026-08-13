export const STATIC_WORLD_SEED = 0x41475731;
export const CHUNK_WIDTH = 50;
export const PLATFORM_WIDTH = 9.6;
export const PLATFORM_THICKNESS = 0.35;
export const PLATFORM_CLEARANCE = 2.5;
export const SCAFFOLD_MEMBER_WIDTH = 0.2;
export const SCAFFOLD_MEMBER_HALF = SCAFFOLD_MEMBER_WIDTH / 2;
export const TRUSS_BAY_COUNT = 12;
export const TRUSS_BAY_HEIGHT = 0.75;
export const TRUSS_BAY_WIDTH = 1.55;
export const COLUMN_WIDTH = 1;
export const COLUMN_BAY_HEIGHT = 0.8;
export const NOC_CONNECTOR_WIDTH = 2;
export const NOC_WIDTH = 7;
export const NOC_ROOF_OFFSET = 7.2;
export const NOC_MAST_WIDTH = 0.5;
export const NOC_MAST_HEIGHT = 3.2;

export const TERRAIN_VERTEX_CADENCE = 16;
export const TERRAIN_BLOCK_WIDTH = 128;
export const TERRAIN_GRADE_LIMIT = 0.36;
export const TERRAIN_GRADE_CHANGE_LIMIT = 0.4;
export const TERRAIN_NORMALIZED_MINIMUM = 0.1;
export const TERRAIN_NORMALIZED_MAXIMUM = 0.6;
export const SITE_SPACING = 96;

export const TERRAIN_PROFILES = freeze({
    H0: [0.35, 0.4, 0.48, 0.55, 0.58, 0.54, 0.46, 0.38, 0.35],
    H1: [0.35, 0.42, 0.5, 0.56, 0.59, 0.53, 0.44, 0.37, 0.35],
    H2: [0.35, 0.39, 0.47, 0.54, 0.57, 0.52, 0.45, 0.39, 0.35],
    H3: [0.35, 0.41, 0.49, 0.57, 0.6, 0.55, 0.47, 0.39, 0.35],
    L0: [0.35, 0.29, 0.21, 0.15, 0.12, 0.17, 0.25, 0.32, 0.35],
    L1: [0.35, 0.28, 0.2, 0.14, 0.1, 0.16, 0.24, 0.31, 0.35],
    L2: [0.35, 0.3, 0.23, 0.16, 0.13, 0.18, 0.26, 0.33, 0.35],
    L3: [0.35, 0.27, 0.19, 0.13, 0.11, 0.15, 0.23, 0.3, 0.35],
});

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
        normalizeSeed(seed) ^ Math.imul(stream, 0x9e3779b9) ^ Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b);
    return mixUint32(value) / 2 ** 32;
}

export function positiveModulo(value, modulus) {
    return ((value % modulus) + modulus) % modulus;
}

export function terrainParityForSeed(seed) {
    return Math.floor(2 * sampleUnit(seed, 13, 0));
}

export function terrainCycleForSeed(seed) {
    return freeze({ blockWidth: TERRAIN_BLOCK_WIDTH, parity: terrainParityForSeed(seed), phases: [4, 36, 68, 100] });
}

export function terrainProfileForBlock(seed, blockIndex) {
    if (!Number.isSafeInteger(blockIndex)) throw new TypeError("Terrain block index must be a safe integer");
    const family = positiveModulo(blockIndex + terrainParityForSeed(seed), 2) === 0 ? "H" : "L";
    const variant = Math.floor(4 * sampleUnit(seed, 14, blockIndex >>> 0));
    const id = `${family}${variant}`;
    return freeze({ blockIndex, family, id, samples: TERRAIN_PROFILES[id], variant });
}

export function terrainHeightAt(seed, x) {
    if (!Number.isFinite(x)) throw new TypeError("Terrain X must be finite");
    const blockIndex = Math.floor(x / TERRAIN_BLOCK_WIDTH);
    const localX = x - blockIndex * TERRAIN_BLOCK_WIDTH;
    const segment = Math.min(7, Math.floor(localX / TERRAIN_VERTEX_CADENCE));
    const fraction = (localX - segment * TERRAIN_VERTEX_CADENCE) / TERRAIN_VERTEX_CADENCE;
    const samples = terrainProfileForBlock(seed, blockIndex).samples;
    const normalized = samples[segment] + (samples[segment + 1] - samples[segment]) * fraction;
    return 64 * normalized - 9.2;
}

export function terrainHeightFromVertices(vertices, x) {
    for (let index = 1; index < vertices.length; index += 1) {
        const left = vertices[index - 1];
        const right = vertices[index];
        if (x < left[0] || x > right[0]) continue;
        if (right[0] === left[0]) return Math.min(left[1], right[1]);
        return left[1] + (right[1] - left[1]) * ((x - left[0]) / (right[0] - left[0]));
    }
    throw new RangeError(`Terrain vertices do not cover ${x}`);
}

export function terrainVerticesForRange(vertices, left, right) {
    if (vertices.length === 0 || right < left || right < vertices[0][0] || left > vertices.at(-1)[0]) {
        return freeze([]);
    }
    const clippedLeft = Math.max(left, vertices[0][0]);
    const clippedRight = Math.min(right, vertices.at(-1)[0]);
    if (clippedLeft === clippedRight) {
        const point = vertices.find(([x]) => x === clippedLeft);
        return freeze([[clippedLeft, point?.[1] ?? terrainHeightFromVertices(vertices, clippedLeft)]]);
    }
    const interior = vertices.filter(([x]) => x > clippedLeft && x < clippedRight);
    return freeze([
        [clippedLeft, terrainHeightFromVertices(vertices, clippedLeft)],
        ...interior.map((point) => [...point]),
        [clippedRight, terrainHeightFromVertices(vertices, clippedRight)],
    ]);
}

export function siteStructure(site) {
    const buildingLeft = site.platformRight + NOC_CONNECTOR_WIDTH;
    const buildingRight = buildingLeft + NOC_WIDTH;
    const roof = site.platformTop + NOC_ROOF_OFFSET;
    const trussBottom = site.platformBottom - TRUSS_BAY_HEIGHT;
    const supportColumns = [
        [0, 1],
        [8.8, 9.8],
        [17.6, 18.6],
    ].map(([leftOffset, rightOffset], index) => {
        const left = site.platformLeft + leftOffset;
        const right = site.platformLeft + rightOffset;
        const leftFoot = site.supportFeet?.[index * 2] ?? terrainHeightAt(site.seed, left);
        const rightFoot = site.supportFeet?.[index * 2 + 1] ?? terrainHeightAt(site.seed, right);
        const latticeFloor = Math.max(leftFoot, rightFoot);
        const bayCount = Math.ceil((site.platformBottom - latticeFloor) / COLUMN_BAY_HEIGHT);
        const levels = Array.from({ length: bayCount + 1 }, (_, index) =>
            Math.max(latticeFloor, site.platformBottom - COLUMN_BAY_HEIGHT * index),
        );
        return {
            bayCount,
            index,
            latticeFloor,
            left,
            leftFoot,
            levels: levels.filter((level, index) => index === 0 || level !== levels[index - 1]),
            right,
            rightFoot,
            collider: {
                bottom: Math.min(leftFoot, rightFoot) - SCAFFOLD_MEMBER_HALF,
                left: left - SCAFFOLD_MEMBER_HALF,
                right: right + SCAFFOLD_MEMBER_HALF,
                top: site.platformBottom + SCAFFOLD_MEMBER_HALF,
            },
        };
    });
    return freeze({
        buildingLeft,
        buildingRight,
        mast: {
            bottom: roof,
            left: buildingLeft + (NOC_WIDTH - NOC_MAST_WIDTH) / 2,
            right: buildingLeft + (NOC_WIDTH + NOC_MAST_WIDTH) / 2,
            top: roof + NOC_MAST_HEIGHT,
        },
        noc: { bottom: site.platformBottom, left: buildingLeft, right: buildingRight, top: roof },
        supportColumns,
        roof,
        truss: {
            bottom: trussBottom - SCAFFOLD_MEMBER_HALF,
            left: site.platformLeft - SCAFFOLD_MEMBER_HALF,
            right: buildingRight + SCAFFOLD_MEMBER_HALF,
            top: site.platformBottom + SCAFFOLD_MEMBER_HALF,
        },
        trussBottom,
    });
}

export function siteScaffoldMembers(site) {
    const structure = siteStructure(site);
    const bayWidth = (structure.buildingRight - site.platformLeft) / TRUSS_BAY_COUNT;
    const members = [
        { start: [site.platformLeft, site.platformBottom], end: [structure.buildingRight, site.platformBottom] },
        { start: [site.platformLeft, structure.trussBottom], end: [structure.buildingRight, structure.trussBottom] },
    ];
    for (let bay = 0; bay < TRUSS_BAY_COUNT; bay += 1) {
        const left = site.platformLeft + bayWidth * bay;
        const right = left + bayWidth;
        members.push(
            bay % 2 === 0
                ? { start: [left, site.platformBottom], end: [right, structure.trussBottom] }
                : { start: [left, structure.trussBottom], end: [right, site.platformBottom] },
        );
    }
    for (const column of structure.supportColumns) {
        members.push(
            { start: [column.left, site.platformBottom], end: [column.left, column.leftFoot] },
            { start: [column.right, site.platformBottom], end: [column.right, column.rightFoot] },
        );
        for (const level of column.levels) {
            members.push({ start: [column.left, level], end: [column.right, level] });
        }
        for (let bay = 0; bay < column.levels.length - 1; bay += 1) {
            const top = column.levels[bay];
            const bottom = column.levels[bay + 1];
            members.push(
                bay % 2 === 0
                    ? { start: [column.left, top], end: [column.right, bottom] }
                    : { start: [column.right, top], end: [column.left, bottom] },
            );
        }
    }
    return freeze(members.map((member) => ({ cap: "butt", join: "round", ...member })));
}

export function siteScaffoldPath(site) {
    const projectX = (x) => Number((x * 10).toFixed(12));
    const projectY = (y) => 548 - y * 10;
    return siteScaffoldMembers(site)
        .map(({ start, end }, index) => {
            const [startX, startY] = start;
            const [endX, endY] = end;
            if (index < 2) return `M${projectX(startX)} ${projectY(startY)}H${projectX(endX)}`;
            return `M${projectX(startX)} ${projectY(startY)}L${projectX(endX)} ${projectY(endY)}`;
        })
        .join("");
}

export function createSiteForIndex(seed, siteIndex, state = {}) {
    if (!Number.isSafeInteger(siteIndex)) throw new TypeError("Site index must be a safe integer");
    const normalized = normalizeSeed(seed);
    const center = 36 + SITE_SPACING * siteIndex;
    const platformLeft = center - PLATFORM_WIDTH / 2;
    const platformRight = center + PLATFORM_WIDTH / 2;
    const buildingRight = center + 13.8;
    const candidates = [platformLeft, buildingRight];
    for (
        let x = Math.ceil(platformLeft / TERRAIN_VERTEX_CADENCE) * TERRAIN_VERTEX_CADENCE;
        x <= buildingRight;
        x += TERRAIN_VERTEX_CADENCE
    )
        candidates.push(x);
    const localNativeMaximum = Math.max(...candidates.map((x) => terrainHeightAt(normalized, x)));
    const platformTop = localNativeMaximum + PLATFORM_CLEARANCE;
    const supportXs = [
        platformLeft,
        platformLeft + 1,
        platformLeft + 8.8,
        platformLeft + 9.8,
        platformLeft + 17.6,
        platformLeft + 18.6,
    ];
    return freeze({
        id: siteIndex,
        seed: normalized,
        center,
        deckLevel: platformTop * 10,
        localNativeMaximum,
        supportFeet: supportXs.map((x) => terrainHeightAt(normalized, x)),
        platformLeft,
        platformRight,
        platformTop,
        platformBottom: platformTop - PLATFORM_THICKNESS,
        canCollected: state.canCollected ?? false,
        powered: state.powered ?? false,
        nocStage: state.nocStage ?? 0,
        pairKey: state.pairKey ?? null,
        originSiteId: state.originSiteId ?? null,
    });
}

export function createFirstSite(seed) {
    return createSiteForIndex(seed, 0);
}

function deckMillimeters(deck) {
    const result = Math.round(deck * 1000);
    if (Math.abs(result / 1000 - deck) > 1e-12) throw new Error(`Deck ${deck} is not millimetre exact`);
    return result;
}

export function routePairKey(originSite, targetSite) {
    return `d:${deckMillimeters(originSite.platformTop)}:${deckMillimeters(targetSite.platformTop)}`;
}

export function selectRouteProof(originSite, targetSite, proofCatalog) {
    if (targetSite.id !== originSite.id + 1 || targetSite.center - originSite.center !== SITE_SPACING) {
        throw new Error(`Sites ${originSite.id}/${targetSite.id} are not one forward route leg`);
    }
    const key = routePairKey(originSite, targetSite);
    const proof = proofCatalog[key];
    if (!proof || proof.pairKey !== key) throw new Error(`Missing exact route proof ${key}`);
    return proof;
}

export function selectTemplate(seed, siteIndex, originSite, proofCatalog) {
    const targetSite = createSiteForIndex(seed, siteIndex);
    return selectRouteProof(originSite, targetSite, proofCatalog);
}

export function instantiateTemplateSite(seed, siteIndex, originSite, proof) {
    const targetSite = createSiteForIndex(seed, siteIndex);
    if (proof !== selectRouteProof(originSite, targetSite, { [proof?.pairKey]: proof })) {
        throw new RangeError(`Route proof ${proof?.pairKey} does not match the direct site pair`);
    }
    return createSiteForIndex(seed, siteIndex, { pairKey: proof.pairKey, originSiteId: originSite.id });
}

export function terrainSiteForIndex(seed, siteIndex) {
    const site = createSiteForIndex(seed, siteIndex);
    return freeze({
        index: siteIndex,
        center: site.center,
        deckLevel: site.deckLevel,
        platformTop: site.platformTop,
        localNativeMaximum: site.localNativeMaximum,
        centerDelta: SITE_SPACING,
        phase: positiveModulo(site.center, TERRAIN_BLOCK_WIDTH),
    });
}

export function terrainVerticesForWindow(seed, sites, left, right) {
    if (!Number.isFinite(left) || !Number.isFinite(right) || right < left) {
        throw new RangeError("Terrain range must be finite and ordered");
    }
    const points = new Set([left, right]);
    for (
        let x = Math.ceil(left / TERRAIN_VERTEX_CADENCE) * TERRAIN_VERTEX_CADENCE;
        x <= right;
        x += TERRAIN_VERTEX_CADENCE
    )
        points.add(x);
    for (const site of sites) {
        [
            site.platformLeft,
            site.platformLeft + 1,
            site.platformLeft + 8.8,
            site.platformLeft + 9.6,
            site.platformLeft + 9.8,
            site.platformLeft + 11.6,
            site.platformLeft + 17.6,
            site.platformLeft + 18.6,
        ].forEach((x) => {
            if (x >= left && x <= right) points.add(x);
        });
    }
    const vertices = [...points].sort((a, b) => a - b).map((x) => [x, terrainHeightAt(seed, x)]);
    if (vertices.some((point, index) => index > 0 && vertices[index - 1][0] >= point[0])) {
        throw new Error("Terrain vertices must have strictly increasing X coordinates");
    }
    if (vertices.length > 48) throw new Error(`Terrain projection exceeds 48 vertices: ${vertices.length}`);
    return freeze(vertices);
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
    if (pose.x < 5) return pose.x - 5;
    if (pose.x > 35) return pose.x - 35;
    return 0;
}

export function targetDirectionForViewport(targetSite, cameraLeft) {
    if (!targetSite) return null;
    if (targetSite.platformLeft > cameraLeft + 100) return "right";
    if (targetSite.platformRight < cameraLeft) return "left";
    return null;
}

function skyChunkKeyForCamera(cameraLeft) {
    const firstChunk = Math.floor((cameraLeft * 0.24) / 50) - 1;
    return Array.from({ length: 5 }, (_, index) => firstChunk + index).join(":");
}

const PLANET_RADIUS = 16;
const PLANET_RING_PROFILES = Object.freeze([
    Object.freeze([[28, 9]]),
    Object.freeze([[31, 10]]),
    Object.freeze([
        [28, 9],
        [34, 12],
    ]),
]);

function occludedRingPath(x, y, radiusX, radiusY) {
    const cutX = Math.sqrt((PLANET_RADIUS ** 2 - radiusY ** 2) / (1 - radiusY ** 2 / radiusX ** 2));
    const cutY = Math.sqrt(PLANET_RADIUS ** 2 - cutX ** 2);
    return (
        `M${x - radiusX} ${y}A${radiusX} ${radiusY} 0 0 1 ${x - cutX} ${y - cutY}` +
        `M${x + cutX} ${y - cutY}A${radiusX} ${radiusY} 0 0 1 ${x + radiusX} ${y}` +
        `M${x + radiusX} ${y}A${radiusX} ${radiusY} 0 0 1 ${x - radiusX} ${y}`
    );
}

export function skyProjectionIdentityForCamera(seed, cameraLeft) {
    return `${normalizeSeed(seed)}|${skyChunkKeyForCamera(cameraLeft)}`;
}

export function skyProjectionForCamera(seed, cameraLeft) {
    const key = skyChunkKeyForCamera(cameraLeft);
    const chunks = key.split(":").map(Number);
    const stars = [];
    const landmarks = [];
    const landmarkOffset = Math.floor(4 * sampleUnit(seed, 8, 0));
    for (const chunk of chunks) {
        for (let index = 0; index < 4; index += 1) {
            const key = (Math.imul(chunk, 4) + index) >>> 0;
            const x = (chunk * 50 + 4 + 42 * sampleUnit(seed, 6, key)) * 10;
            const y = 50 + 190 * sampleUnit(seed, 7, key);
            stars.push(`M${x} ${y}h2`);
        }
        if (positiveModulo(chunk - landmarkOffset, 4) !== 0) continue;
        const key = chunk >>> 0;
        const x = 10 * (chunk * 50 + 10 + 30 * sampleUnit(seed, 9, key));
        const y = 90 + 110 * sampleUnit(seed, 10, key);
        if (sampleUnit(seed, 11, key) < 0.5) {
            landmarks.push(`M${x} ${y - 18}A18 18 0 1 0 ${x} ${y + 18}A13 18 0 0 1 ${x} ${y - 18}`);
        } else {
            const profile = PLANET_RING_PROFILES[Math.floor(3 * sampleUnit(seed, 12, key))];
            landmarks.push(
                `M${x - PLANET_RADIUS} ${y}A${PLANET_RADIUS} ${PLANET_RADIUS} 0 1 0 ` +
                    `${x + PLANET_RADIUS} ${y}A${PLANET_RADIUS} ${PLANET_RADIUS} 0 1 0 ` +
                    `${x - PLANET_RADIUS} ${y}Z` +
                    profile.map(([radiusX, radiusY]) => occludedRingPath(x, y, radiusX, radiusY)).join(""),
            );
        }
    }
    return freeze({ key, chunks, starsPath: stars.join(""), landmarksPath: landmarks.join("") });
}

export function terrainSurfacePath(vertices) {
    if (vertices.length === 0) {
        return "";
    }
    const points = vertices.map(([x, y]) => `${x * 10} ${548 - y * 10}`).join("L");
    return `M${points}`;
}

export function terrainFillPath(vertices) {
    if (vertices.length === 0) {
        return "";
    }
    return `${terrainSurfacePath(vertices)}L${vertices.at(-1)[0] * 10} 648L${vertices[0][0] * 10} 648Z`;
}
