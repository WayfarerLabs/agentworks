import {
    affineHullEnclosure,
    boundsOverlap,
    compareExactRoots,
    exactRootNumber,
    exactSegmentContact,
    exactZeroRoot,
    hasInteriorAngleKnot,
    polygonDistanceSquared,
    polygonSegmentDistanceSquared,
} from "./lander-collision.js";

export const STATIC_WORLD_SEED = 0x41475731,
    CHUNK_WIDTH = 50;
export const PLATFORM_WIDTH = 9.6,
    PLATFORM_THICKNESS = 0.35,
    PLATFORM_CLEARANCE = 2.5;
export const SCAFFOLD_MEMBER_WIDTH = 0.2;
export const SCAFFOLD_MEMBER_HALF = SCAFFOLD_MEMBER_WIDTH / 2;
export const TRUSS_BAY_COUNT = 12,
    TRUSS_BAY_HEIGHT = 0.75,
    TRUSS_BAY_WIDTH = 1.55;
export const COLUMN_WIDTH = 1,
    COLUMN_BAY_HEIGHT = 0.8;
export const NOC_CONNECTOR_WIDTH = 2,
    NOC_WIDTH = 7,
    NOC_ROOF_OFFSET = 7.2,
    NOC_MAST_WIDTH = 0.5,
    NOC_MAST_HEIGHT = 3.2;

export const TERRAIN_VERTEX_CADENCE = 16,
    TERRAIN_BLOCK_WIDTH = 512,
    TERRAIN_EPOCH_BLOCKS = 8,
    TERRAIN_GRADE_LIMIT = 0.4,
    TERRAIN_GRADE_CHANGE_LIMIT = 0.8,
    TERRAIN_NORMALIZED_MINIMUM = 0.1,
    TERRAIN_NORMALIZED_MAXIMUM = 0.6,
    SITE_SPACING = 96;
export const SITE_CANDIDATE_OFFSETS = Object.freeze([0, 8, 16, 24, 32, 40]);
export const SITE_CANDIDATE_ORDERS = Object.freeze([
    Object.freeze([0, 1, 2, 3, 4, 5]),
    Object.freeze([0, 5, 4, 3, 2, 1]),
]);
export const MAX_NORMALIZED_DECK = 0.5;
export const WORLD_MIN_X = -393216,
    WORLD_MAX_X = 393216,
    MIN_SITE_INDEX = -4095,
    MAX_SITE_INDEX = 4095,
    TERMINUS_WIDTH = 0.2;

const TERRAIN_PROFILE_ROWS = [
    ".35 .29 .19 .11 .21 .30 .39 .47 .42 .41 .36 .28 .38 .45 .40 .30 .23 .28 .33 .26 .20 .30 .25 .19 .29 .35 .29 .22 .16 .10 .17 .26 .35",
    ".35 .43 .52 .42 .35 .34 .27 .36 .41 .49 .41 .36 .31 .26 .34 .43 .52 .47 .37 .43 .33 .29 .28 .23 .33 .40 .45 .38 .36 .28 .36 .44 .35",
    ".35 .30 .25 .35 .43 .36 .28 .33 .43 .33 .24 .29 .20 .19 .14 .23 .32 .37 .32 .38 .48 .57 .49 .42 .47 .52 .42 .36 .46 .39 .37 .28 .35",
    ".35 .45 .52 .46 .38 .43 .49 .54 .48 .38 .43 .49 .44 .39 .48 .53 .45 .36 .42 .50 .44 .36 .27 .35 .27 .18 .26 .36 .30 .25 .35 .44 .35",
    ".35 .26 .33 .42 .50 .60 .50 .41 .48 .39 .38 .29 .39 .29 .23 .31 .38 .28 .19 .28 .23 .18 .25 .32 .42 .37 .32 .40 .35 .28 .19 .28 .35",
    ".35 .41 .51 .41 .33 .39 .48 .38 .30 .28 .22 .27 .36 .42 .34 .31 .28 .20 .28 .37 .43 .51 .41 .33 .43 .48 .38 .31 .41 .43 .50 .43 .35",
    ".35 .28 .33 .43 .35 .30 .38 .31 .22 .32 .37 .30 .36 .38 .44 .34 .28 .35 .45 .40 .32 .39 .34 .25 .32 .34 .35 .41 .31 .29 .24 .29 .35",
    ".35 .29 .21 .15 .23 .32 .42 .35 .33 .28 .36 .38 .48 .42 .34 .43 .48 .39 .33 .25 .34 .40 .33 .23 .32 .35 .40 .31 .24 .16 .26 .27 .35",
];
export const TERRAIN_PROFILES = freeze(
    Object.fromEntries(TERRAIN_PROFILE_ROWS.map((row, index) => [`S${index}`, row.split(" ").map(Number)])),
);

function freeze(value) {
    if (value && typeof value === "object") Object.values(value).forEach(freeze);
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

export function terrainProfileForBlock(seed, blockIndex) {
    if (!Number.isSafeInteger(blockIndex)) throw new TypeError("Terrain block index must be a safe integer");
    const epoch = Math.floor(blockIndex / TERRAIN_EPOCH_BLOCKS);
    const slot = positiveModulo(blockIndex, TERRAIN_EPOCH_BLOCKS);
    const offset = Math.floor(8 * sampleUnit(seed, 15, 0));
    const first = positiveModulo(offset + epoch, 8);
    const last = positiveModulo(first + 2, 8);
    const middle = Array.from({ length: 8 }, (_, index) => index).filter((index) => index !== first && index !== last);
    for (let index = 5; index >= 1; index -= 1) {
        const sampleIndex = (Math.imul(epoch, 6) + (5 - index)) >>> 0;
        const exchange = Math.floor((index + 1) * sampleUnit(seed, 16, sampleIndex));
        [middle[index], middle[exchange]] = [middle[exchange], middle[index]];
    }
    const profile = [first, ...middle, last][slot];
    const id = `S${profile}`;
    return freeze({ blockIndex, epoch, id, profile, samples: TERRAIN_PROFILES[id], slot });
}

export function terrainHeightAt(seed, x) {
    if (!Number.isFinite(x)) throw new TypeError("Terrain X must be finite");
    if (x < WORLD_MIN_X || x > WORLD_MAX_X) throw new RangeError(`Terrain X ${x} is outside the generated world`);
    const blockIndex = Math.floor(x / TERRAIN_BLOCK_WIDTH);
    const localX = x - blockIndex * TERRAIN_BLOCK_WIDTH;
    const segment = Math.min(31, Math.floor(localX / TERRAIN_VERTEX_CADENCE));
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

export function siteCandidateOrder(seed) {
    return sampleUnit(seed, 17, 0) < 0.5 ? 0 : 1;
}

export function createSiteForIndex(seed, siteIndex, state = {}) {
    if (!Number.isSafeInteger(siteIndex)) throw new TypeError("Site index must be a safe integer");
    if (siteIndex < MIN_SITE_INDEX || siteIndex > MAX_SITE_INDEX) {
        throw new RangeError(`Site index ${siteIndex} is outside the generated world`);
    }
    const normalized = normalizeSeed(seed);
    const nominalCenter = 36 + SITE_SPACING * siteIndex;
    const candidateOrder = siteCandidateOrder(normalized);
    const order = SITE_CANDIDATE_ORDERS[candidateOrder];
    for (let candidateOrdinal = 0; candidateOrdinal < order.length; candidateOrdinal += 1) {
        const offsetIndex = order[candidateOrdinal];
        const center = nominalCenter + SITE_CANDIDATE_OFFSETS[offsetIndex];
        const platformLeft = center - PLATFORM_WIDTH / 2;
        const platformRight = center + PLATFORM_WIDTH / 2;
        const closedFootprint = [platformLeft, center + 13.8];
        const candidates = [...closedFootprint];
        for (
            let x = Math.ceil(closedFootprint[0] / TERRAIN_VERTEX_CADENCE) * TERRAIN_VERTEX_CADENCE;
            x <= closedFootprint[1];
            x += TERRAIN_VERTEX_CADENCE
        )
            candidates.push(x);
        const localNativeMaximum = Math.max(...candidates.map((x) => terrainHeightAt(normalized, x)));
        const platformTop = localNativeMaximum + PLATFORM_CLEARANCE;
        const normalizedDeck = (platformTop + 9.2) / 64;
        if (normalizedDeck > MAX_NORMALIZED_DECK) continue;
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
            nominalCenter,
            candidateOrder,
            candidateOrdinal,
            offsetIndex,
            center,
            closedFootprint,
            localNativeMaximum,
            normalizedDeck,
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
    throw new Error(`No eligible site candidate for ${siteIndex}`);
}

export function createFirstSite(seed) {
    return createSiteForIndex(seed, 0);
}

function millimeters(value) {
    const result = Math.round(value * 1000);
    if (!Number.isSafeInteger(result)) throw new RangeError(`Value ${value} has no safe millimetre key`);
    return result;
}

export function routePairKey(originSite, targetSite) {
    return `r:${millimeters(targetSite.center - originSite.center)}:${millimeters(originSite.platformTop)}:${millimeters(targetSite.platformTop)}`;
}

export function selectRouteProof(originSite, targetSite, proofCatalog) {
    if (targetSite.id !== originSite.id + 1) {
        throw new Error(`Sites ${originSite.id}/${targetSite.id} are not one forward route leg`);
    }
    const key = routePairKey(originSite, targetSite);
    const proof = proofCatalog[key];
    if (!proof || proof.pairKey !== key) throw new Error(`Missing exact route proof ${key}`);
    return proof;
}

export function terrainVerticesForWindow(seed, sites, left, right) {
    if (!Number.isFinite(left) || !Number.isFinite(right) || right < left) {
        throw new RangeError("Terrain range must be finite and ordered");
    }
    left = Math.max(WORLD_MIN_X, left);
    right = Math.min(WORLD_MAX_X, right);
    if (right < left) return freeze([]);
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
    const visibleLeft = Math.max(WORLD_MIN_X, cameraLeft);
    const visibleRight = Math.min(WORLD_MAX_X, cameraLeft + 100);
    const left = Math.max(WORLD_MIN_X, visibleLeft - 40);
    const right = Math.min(WORLD_MAX_X, visibleRight + 40);
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
    const raw = pose.x < 5 ? pose.x - 5 : pose.x > 35 ? pose.x - 35 : 0;
    return collisionClamp(raw, WORLD_MIN_X - TERMINUS_WIDTH, WORLD_MAX_X - 100 + TERMINUS_WIDTH);
}

export function worldTermini(seed) {
    return freeze({
        left: { foot: terrainHeightAt(seed, WORLD_MIN_X), x: WORLD_MIN_X },
        right: { foot: terrainHeightAt(seed, WORLD_MAX_X), x: WORLD_MAX_X },
        width: TERMINUS_WIDTH,
    });
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

export const MAX_LANDING_HORIZONTAL_SPEED = 2.2;
export const MAX_LANDING_DESCENT_SPEED = 3.6;
export const MAX_LANDING_ANGLE = 18;
export const MAX_LANDING_ANGULAR_SPEED = 26;
export const COLLISION_MARGIN = 0.02;
export const COLLISION_ANGLE_KNOT_DEGREES = 1;

export const LANDER_HULL = Object.freeze([
    [-1.6, 0],
    [1.6, 0],
    [1.6, 6.5],
    [-1.6, 6.5],
]);
const HULL_RADIUS = Math.hypot(1.6, 6.5);
const collisionClamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));
const collisionSiteById = (model, id) => model.retainedSites.find((site) => site.id === id) ?? null;

export const normalizeDegrees = (degrees) => ((((degrees + 180) % 360) + 360) % 360) - 180;

export function transformLocalPoint(pose, localX, localY) {
    const radians = (pose.angle * Math.PI) / 180;
    return {
        x: pose.x + localX * Math.cos(radians) + localY * Math.sin(radians),
        y: pose.y - localX * Math.sin(radians) + localY * Math.cos(radians),
    };
}
function interpolatePose(left, right, fraction) {
    const lerp = (a, b) => a + (b - a) * fraction;
    return {
        x: lerp(left.x, right.x),
        y: lerp(left.y, right.y),
        vx: lerp(left.vx, right.vx),
        vy: lerp(left.vy, right.vy),
        angle: normalizeDegrees(left.angle + normalizeDegrees(right.angle - left.angle) * fraction),
        angularVelocity: lerp(left.angularVelocity, right.angularVelocity),
    };
}

export function hullForPose(pose) {
    return LANDER_HULL.map(([x, y]) => transformLocalPoint(pose, x, y));
}

export function hullBounds(pose) {
    const radians = (pose.angle * Math.PI) / 180;
    const sine = Math.sin(radians);
    const cosine = Math.cos(radians);
    let left = Infinity;
    let right = -Infinity;
    let bottom = Infinity;
    let top = -Infinity;
    for (const [localX, localY] of LANDER_HULL) {
        const x = pose.x + localX * cosine + localY * sine;
        const y = pose.y - localX * sine + localY * cosine;
        left = Math.min(left, x);
        right = Math.max(right, x);
        bottom = Math.min(bottom, y);
        top = Math.max(top, y);
    }
    return { left, right, bottom, top };
}

function rectangle(left, right, bottom, top) {
    return [
        { x: left, y: bottom },
        { x: right, y: bottom },
        { x: right, y: top },
        { x: left, y: top },
    ];
}

const boxedFeature = (cause, priority, box) => ({
    cause,
    priority,
    polygon: rectangle(box.left, box.right, box.bottom, box.top),
});

function belowTerrain(hull, segment) {
    const [left, right] = segment;
    return hull.some((point) => {
        if (point.x < left.x || point.x > right.x) return false;
        const y = left.y + (right.y - left.y) * ((point.x - left.x) / (right.x - left.x));
        return point.y <= y;
    });
}

export function unsafeFeatures(model, pose, target, ignoredTopSiteId = null) {
    const features = [];
    for (const site of model.retainedSites) {
        const topLeft = { x: site.platformLeft, y: site.platformTop };
        const topRight = { x: site.platformRight, y: site.platformTop };
        const bottomLeft = { x: site.platformLeft, y: site.platformBottom };
        const bottomRight = { x: site.platformRight, y: site.platformBottom };
        if (site.id === target?.id || site.id === ignoredTopSiteId) {
            features.push({ cause: "platform", priority: 1, segment: [topLeft, bottomLeft] });
            features.push({ cause: "platform", priority: 1, segment: [bottomLeft, bottomRight] });
            features.push({ cause: "platform", priority: 1, segment: [bottomRight, topRight] });
        } else {
            const platform = {
                left: site.platformLeft,
                right: site.platformRight,
                bottom: site.platformBottom,
                top: site.platformTop,
            };
            features.push(boxedFeature("platform", 1, platform));
        }
        const structure = siteStructure(site);
        features.push(boxedFeature("truss", 2, structure.truss));
        for (const column of structure.supportColumns) {
            features.push(boxedFeature("column", 2, column.collider));
        }
        features.push(boxedFeature("noc", 0, structure.noc), boxedFeature("mast", 0, structure.mast));
    }
    return features.map((feature) => ({
        ...feature,
        bounds: collisionBounds(feature.polygon ?? feature.segment),
    }));
}

function terrainCandidates(seed, bounds) {
    const left = Math.max(WORLD_MIN_X, bounds.left - COLLISION_MARGIN);
    const right = Math.min(WORLD_MAX_X, bounds.right + COLLISION_MARGIN);
    if (right < left) return [];
    const first = Math.floor(left / TERRAIN_VERTEX_CADENCE);
    const last = Math.ceil(right / TERRAIN_VERTEX_CADENCE);
    const result = [];
    for (let index = first; index < last; index += 1) {
        const x = index * TERRAIN_VERTEX_CADENCE;
        if (x < WORLD_MIN_X || x + TERRAIN_VERTEX_CADENCE > WORLD_MAX_X) continue;
        const segment = [
            { x, y: terrainHeightAt(seed, x) },
            { x: x + TERRAIN_VERTEX_CADENCE, y: terrainHeightAt(seed, x + TERRAIN_VERTEX_CADENCE) },
        ];
        const top = Math.max(segment[0].y, segment[1].y);
        if (top >= bounds.bottom - COLLISION_MARGIN) {
            result.push({ cause: "terrain", priority: 4, segment, solidBelow: true });
        }
    }
    return result;
}

function contextTerrainCandidates(vertices, bounds) {
    return vertices.slice(1).flatMap((_, offset) => {
        const index = offset + 1;
        const segment = [
                { x: vertices[index - 1][0], y: vertices[index - 1][1] },
                { x: vertices[index][0], y: vertices[index][1] },
            ],
            candidate = { cause: "terrain", priority: 4, segment, solidBelow: true };
        candidate.bounds = candidateBounds(candidate);
        return boundsOverlap(candidate.bounds, bounds, COLLISION_MARGIN, candidate.solidBelow) ? [candidate] : [];
    });
}
const contextTerrainCache = new WeakMap();

function collisionBounds(...groups) {
    const points = groups.flat();
    return {
        left: Math.min(...points.map(({ x }) => x)),
        right: Math.max(...points.map(({ x }) => x)),
        bottom: Math.min(...points.map(({ y }) => y)),
        top: Math.max(...points.map(({ y }) => y)),
    };
}

function expandedEnclosure(left, right, expansion = 0) {
    const bounds = collisionBounds(left, right);
    return Object.fromEntries(
        Object.entries(bounds).map(([key, value]) => [
            key,
            value + (key === "left" || key === "bottom" ? -expansion : expansion),
        ]),
    );
}

const candidateBounds = (candidate) => candidate.bounds ?? collisionBounds(candidate.polygon ?? candidate.segment);

function candidateDistanceSquared(hull, candidate) {
    if (candidate.solidBelow && belowTerrain(hull, candidate.segment)) return 0;
    return candidate.polygon
        ? polygonDistanceSquared(hull, candidate.polygon)
        : polygonSegmentDistanceSquared(hull, candidate.segment[0], candidate.segment[1]);
}

function mixHull(left, right, amount) {
    return left.map((point, index) => ({
        x: point.x + (right[index].x - point.x) * amount,
        y: point.y + (right[index].y - point.y) * amount,
    }));
}

const maximumHullDisplacement = (left, right) =>
    Math.max(...left.map((point, index) => Math.hypot(right[index].x - point.x, right[index].y - point.y)));

function contactForCandidate(leftHull, rightHull, candidate) {
    if (candidateDistanceSquared(leftHull, candidate) === 0) return { hull: leftHull, root: exactZeroRoot(), time: 0 };
    const segments = candidate.polygon
        ? candidate.polygon.map((point, index) => [point, candidate.polygon[(index + 1) % candidate.polygon.length]])
        : [candidate.segment];
    let earliest = null;
    for (const segment of segments) {
        const root = exactSegmentContact(leftHull, rightHull, segment);
        if (root && (!earliest || compareExactRoots(root, earliest.root) < 0)) {
            const time = exactRootNumber(root);
            earliest = { hull: mixHull(leftHull, rightHull, time), root, time };
        }
    }
    return earliest;
}

function candidatesForBounds(model, bounds, fixed, target, terrainIsFixed) {
    const candidates = fixed.filter((candidate) =>
        boundsOverlap(candidateBounds(candidate), bounds, COLLISION_MARGIN, candidate.solidBelow),
    );
    if (!terrainIsFixed) candidates.push(...terrainCandidates(model.seed, bounds));
    if (
        target &&
        target.platformTop >= bounds.bottom - COLLISION_MARGIN &&
        target.platformTop <= bounds.top + COLLISION_MARGIN
    ) {
        candidates.push({
            cause: "target",
            priority: 5,
            segment: horizontalSegment(target.platformLeft, target.platformRight, target.platformTop),
            target: true,
        });
    }
    return candidates;
}

function hasCandidateForBounds(model, bounds, fixed, target, terrainIsFixed) {
    if (
        fixed.some((candidate) =>
            boundsOverlap(candidateBounds(candidate), bounds, COLLISION_MARGIN, candidate.solidBelow),
        )
    )
        return true;
    if (!terrainIsFixed && terrainCandidates(model.seed, bounds).length) return true;
    return Boolean(
        target &&
        target.platformTop >= bounds.bottom - COLLISION_MARGIN &&
        target.platformTop <= bounds.top + COLLISION_MARGIN &&
        target.platformRight >= bounds.left - COLLISION_MARGIN &&
        target.platformLeft <= bounds.right + COLLISION_MARGIN,
    );
}

const horizontalSegment = (left, right, y) => [
    { x: left, y },
    { x: right, y },
];

function terminusCandidate(x, foot) {
    return {
        cause: "terminus",
        priority: 3,
        segment: [
            { x, y: foot },
            { x, y: 2 ** 38 },
        ],
    };
}

function contactPose(previous, next, angularTravel, time) {
    const pose = interpolatePose(previous, next, time);
    pose.angle = normalizeDegrees(previous.angle + angularTravel * time);
    return pose;
}

function classifyContact(contact, target, previous, next, angularTravel) {
    const pose = contactPose(previous, next, angularTravel, contact.time);
    if (!contact.candidate.target) {
        return { kind: "unsafe", cause: contact.candidate.cause, pose, time: contact.time };
    }
    const feet = contact.hull.slice(0, 2);
    const safe =
        pose.vy <= 0 &&
        feet.every((foot) => foot.x >= target.platformLeft && foot.x <= target.platformRight) &&
        Math.abs(pose.vx) <= MAX_LANDING_HORIZONTAL_SPEED &&
        Math.abs(pose.vy) <= MAX_LANDING_DESCENT_SPEED &&
        Math.abs(normalizeDegrees(pose.angle)) <= MAX_LANDING_ANGLE &&
        Math.abs(pose.angularVelocity) <= MAX_LANDING_ANGULAR_SPEED;
    return { kind: safe ? "safe" : "unsafe", cause: safe ? "target" : "target-envelope", pose, time: contact.time };
}

function sweepSlab(model, slab, fixed, target, previous, next, angularTravel, instrumentation, terrainIsFixed) {
    const stack = [
        { left: slab.left, right: slab.right, leftHull: slab.leftHull, rightHull: slab.rightHull, depth: 0 },
    ];
    while (stack.length) {
        instrumentation.maxStack = Math.max(instrumentation.maxStack, stack.length);
        const interval = stack.pop();
        const displacement = maximumHullDisplacement(interval.leftHull, interval.rightHull);
        const bounds = expandedEnclosure(interval.leftHull, interval.rightHull, displacement);
        const candidates = candidatesForBounds(model, bounds, fixed, target, terrainIsFixed);
        if (!candidates.length) continue;
        if (displacement > COLLISION_MARGIN) {
            const middle = (interval.left + interval.right) / 2;
            if (middle === interval.left || middle === interval.right)
                throw new Error("Collision midpoint did not progress");
            const middleHull = mixHull(interval.leftHull, interval.rightHull, 0.5);
            const depth = interval.depth + 1;
            if (depth > 20) throw new Error("Collision displacement invariant exceeded derived depth");
            stack.push(
                { left: middle, right: interval.right, leftHull: middleHull, rightHull: interval.rightHull, depth },
                { left: interval.left, right: middle, leftHull: interval.leftHull, rightHull: middleHull, depth },
            );
            continue;
        }
        const middleHull = mixHull(interval.leftHull, interval.rightHull, 0.5);
        const detected = candidates.filter((candidate) =>
            [interval.leftHull, middleHull, interval.rightHull].some(
                (hull) => candidateDistanceSquared(hull, candidate) <= COLLISION_MARGIN ** 2,
            ),
        );
        const contacts = [];
        for (const candidate of detected) {
            const local = contactForCandidate(interval.leftHull, interval.rightHull, candidate);
            if (!local) continue;
            contacts.push({
                candidate,
                hull: local.hull,
                root: local.root,
                time: interval.left + (interval.right - interval.left) * local.time,
            });
        }
        contacts.sort((a, b) => compareExactRoots(a.root, b.root) || a.candidate.priority - b.candidate.priority);
        if (contacts.length) return classifyContact(contacts[0], target, previous, next, angularTravel);
    }
    return null;
}

function* collisionKnots(previous, angularTravel) {
    yield { angle: previous.angle, time: 0 };
    const endpoint = previous.angle + angularTravel;
    if (angularTravel > 0) {
        for (let angle = Math.floor(previous.angle) + 1; angle <= Math.ceil(endpoint) - 1; angle += 1) {
            yield { angle, time: (angle - previous.angle) / angularTravel };
        }
    } else if (angularTravel < 0) {
        for (let angle = Math.ceil(previous.angle) - 1; angle >= Math.floor(endpoint) + 1; angle -= 1) {
            yield { angle, time: (angle - previous.angle) / angularTravel };
        }
    }
    yield { angle: endpoint, time: 1 };
}

export function classifySweptContactFromBounds(model, previous, next, previousBounds, nextBounds, options = {}) {
    const target = collisionSiteById(model, model.targetSiteId);
    if (!Number.isFinite(options.angularTravel)) throw new TypeError("Swept contact requires explicit angularTravel");
    const angularTravel = options.angularTravel;
    const swept = hasInteriorAngleKnot(previous.angle, angularTravel)
        ? {
              left: Math.min(previous.x, next.x) - HULL_RADIUS,
              right: Math.max(previous.x, next.x) + HULL_RADIUS,
              bottom: Math.min(previous.y, next.y) - HULL_RADIUS,
              top: Math.max(previous.y, next.y) + HULL_RADIUS,
          }
        : {
              left: Math.min(previousBounds.left, nextBounds.left),
              right: Math.max(previousBounds.right, nextBounds.right),
              bottom: Math.min(previousBounds.bottom, nextBounds.bottom),
              top: Math.max(previousBounds.top, nextBounds.top),
          };
    const features = options.features ?? unsafeFeatures(model, previous, target, options.ignoreTopSiteId);
    const reachesTerminus = swept.left <= WORLD_MIN_X || swept.right >= WORLD_MAX_X;
    const maximumTop =
        model.collisionMaximumTop ??
        Math.max(29.2, target?.platformTop ?? -Infinity, ...features.map((feature) => candidateBounds(feature).top));
    if (!reachesTerminus && swept.bottom > maximumTop + COLLISION_MARGIN) return null;
    const fixed = features.map((feature) =>
        feature.bounds ? feature : { ...feature, bounds: candidateBounds(feature) },
    );
    const terrainSegmentCount = Math.ceil((swept.right - swept.left) / TERRAIN_VERTEX_CADENCE);
    const contextTerrain = model.terrainAuthority === "context";
    const terrainIsFixed = contextTerrain || terrainSegmentCount <= 64;
    if (contextTerrain) {
        let terrainFeatures = contextTerrainCache.get(model);
        if (!terrainFeatures) {
            terrainFeatures = contextTerrainCandidates(model.terrainVertices, {
                left: -Infinity,
                right: Infinity,
                bottom: -Infinity,
                top: Infinity,
            });
            contextTerrainCache.set(model, terrainFeatures);
        }
        fixed.push(
            ...terrainFeatures.filter((candidate) =>
                boundsOverlap(candidate.bounds, swept, COLLISION_MARGIN, candidate.solidBelow),
            ),
        );
    } else if (terrainIsFixed) fixed.push(...terrainCandidates(model.seed, swept));
    let termini;
    if (swept.left <= WORLD_MIN_X + COLLISION_MARGIN) {
        termini = worldTermini(model.seed);
        fixed.push(terminusCandidate(WORLD_MIN_X, termini.left.foot));
    }
    if (swept.right >= WORLD_MAX_X - COLLISION_MARGIN) {
        termini ??= worldTermini(model.seed);
        fixed.push(terminusCandidate(WORLD_MAX_X, termini.right.foot));
    }
    const instrumentation = options.instrumentation ?? {};
    instrumentation.visitedKnots = 0;
    instrumentation.maxKnotHulls = 0;
    instrumentation.maxStack = 0;
    instrumentation.constructedKnotHulls = 0;
    instrumentation.prunedSlabs = 0;
    let prior = null;
    for (const knot of collisionKnots(previous, angularTravel)) {
        instrumentation.visitedKnots += 1;
        const center = {
            x: previous.x + (next.x - previous.x) * knot.time,
            y: previous.y + (next.y - previous.y) * knot.time,
            angle: knot.angle,
        };
        const current = { ...knot, center };
        if (prior) {
            // Every canonical affine-slab vertex coordinate stays between its two endpoint
            // coordinates. The center enclosure expanded by the hull radius is therefore a strict
            // superset of the complete slab; the ordinary detection margin remains in the overlap.
            const coarseBounds = affineHullEnclosure(prior.center, center, HULL_RADIUS);
            if (!hasCandidateForBounds(model, coarseBounds, fixed, target, terrainIsFixed)) {
                instrumentation.prunedSlabs += 1;
                prior = current;
                continue;
            }
            const leftHull = prior.hull ?? hullForPose(prior.center);
            if (!prior.hull) instrumentation.constructedKnotHulls += 1;
            const rightHull = hullForPose(center);
            instrumentation.constructedKnotHulls += 1;
            current.hull = rightHull;
            instrumentation.maxKnotHulls = Math.max(instrumentation.maxKnotHulls, 2);
            const contact = sweepSlab(
                model,
                { left: prior.time, right: current.time, leftHull, rightHull },
                fixed,
                target,
                previous,
                next,
                angularTravel,
                instrumentation,
                terrainIsFixed,
            );
            if (contact) return contact;
        }
        prior = current;
    }
    return null;
}

export function classifySweptContact(model, previous, next, options = {}) {
    return classifySweptContactFromBounds(model, previous, next, hullBounds(previous), hullBounds(next), options);
}
