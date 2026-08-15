import { canonicalBytes, fixtureDigest as digest } from "./lander_route_fixture.mjs";

export const PROFILE_ORDER = Object.freeze(["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]);
export const CANDIDATE_OFFSETS = Object.freeze([0, 8, 16, 24, 32, 40]);
export const CANDIDATE_ORDERS = Object.freeze([Object.freeze([0, 1, 2, 3, 4, 5]), Object.freeze([0, 5, 4, 3, 2, 1])]);
export const REVIEW_SEEDS = Object.freeze([11, 39, 41, 0x41475731]);

export const positiveModulo = (value, modulus) => ((value % modulus) + modulus) % modulus;

export function mixUint32(input) {
    let value = Number(input) >>> 0;
    value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
    value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
    return (value ^ (value >>> 16)) >>> 0;
}

export const normalizeSeed = (seed) => Number(seed) >>> 0 || 0x6d2b79f5;

export function sampleUnit(seed, stream, index) {
    const value =
        normalizeSeed(seed) ^ Math.imul(stream, 0x9e3779b9) ^ Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b);
    return mixUint32(value) / 2 ** 32;
}

export function validateGeometry(geometry, synthesizerVersion) {
    const expectedSite = {
        candidateOffsets: CANDIDATE_OFFSETS,
        candidateOrderStream: 17,
        candidateOrders: CANDIDATE_ORDERS,
        maxNormalizedDeck: 0.5,
        nominalOrigin: 36,
        nominalSpacing: 96,
        clearance: 2.5,
        closedFootprint: [-4.8, 13.8],
    };
    if (
        geometry.schema !== "agw-lander-route-geometry/v9" ||
        geometry.synthesizer?.recipe !== synthesizerVersion ||
        geometry.synthesizer?.beamWidth !== 6000 ||
        geometry.synthesizer?.maxLayers !== 269 ||
        geometry.synthesizer?.macroSteps !== 12 ||
        geometry.synthesizer?.maxContactStep !== 4332 ||
        geometry.terrain?.superblockWidth !== 512 ||
        geometry.terrain?.epochSuperblocks !== 8 ||
        geometry.terrain?.cadence !== 16 ||
        geometry.terrain?.gradeLimit !== 0.4 ||
        geometry.terrain?.gradeChangeLimit !== 0.8 ||
        geometry.siteGeometry?.platform?.clearance !== 2.5 ||
        canonicalBytes(geometry.site) !== canonicalBytes(expectedSite) ||
        canonicalBytes(Object.keys(geometry.terrain?.profiles ?? {}).sort()) !==
            canonicalBytes([...PROFILE_ORDER].sort())
    ) {
        throw new Error("Unsupported or incomplete geometry-v9 fixture");
    }
    for (const profile of Object.values(geometry.terrain.profiles)) {
        if (profile.length !== 33 || profile[0] !== 0.35 || profile.at(-1) !== 0.35) {
            throw new Error("Malformed Phase 4T terrain profile");
        }
    }
}

function reversalStats(profile) {
    const grades = Array.from(
        { length: 32 },
        (_, index) => Math.round((profile[index + 1] - profile[index]) * 400) / 100,
    ).filter((grade) => grade !== 0);
    const strengths = [];
    for (let index = 0; index < grades.length; index += 1) {
        const left = grades[index];
        const right = grades[(index + 1) % grades.length];
        if (Math.sign(left) !== Math.sign(right)) strengths.push(Math.round(Math.abs(right - left) * 100) / 100);
    }
    return { count: strengths.length, strengths };
}

export function validatePredecessor(predecessor, geometry) {
    if (predecessor.schema !== "agw-lander-route-geometry/v8") {
        throw new Error("Predecessor geometry schema mismatch");
    }
    const oldCounts = [];
    const newCounts = [];
    const oldStrengths = [];
    const newStrengths = [];
    for (const id of PROFILE_ORDER) {
        const oldStats = reversalStats(predecessor.terrain?.profiles?.[id] ?? []);
        const newStats = reversalStats(geometry.terrain.profiles[id]);
        oldCounts.push(oldStats.count);
        newCounts.push(newStats.count);
        oldStrengths.push(...oldStats.strengths);
        newStrengths.push(...newStats.strengths);
        if (newStats.count !== oldStats.count * 2) throw new Error(`Reversal-count mismatch ${id}`);
    }
    const median = (values) => {
        values.sort((a, b) => a - b);
        const middle = values.length / 2;
        return values.length % 2 ? values[Math.floor(middle)] : (values[middle - 1] + values[middle]) / 2;
    };
    if (
        canonicalBytes(oldCounts) !== canonicalBytes([6, 6, 8, 8, 8, 6, 8, 6]) ||
        canonicalBytes(newCounts) !== canonicalBytes([12, 12, 16, 16, 16, 12, 16, 12]) ||
        median(oldStrengths) !== 0.2 ||
        median(newStrengths) !== 0.6
    ) {
        throw new Error("Phase 4T reversal authority mismatch");
    }
}

export function validateNumberBound(quantumCeil, approvedBase) {
    const ratio = (baseNumber) => 1 + 0.5 ** (baseNumber - 1);
    const maximumAllowance = quantumCeil(approvedBase + 18.056 / 3);
    let worstFuel = 15;
    for (let baseNumber = 1; baseNumber <= 4096; baseNumber += 1) {
        worstFuel += maximumAllowance * ratio(baseNumber);
    }
    if (
        ratio(53) !== 1.0000000000000002 ||
        ratio(54) !== 1 ||
        maximumAllowance !== 19.450000000000003 ||
        worstFuel !== 79721.09999999384 ||
        Math.ceil(worstFuel) !== 79722
    )
        throw new Error("Phase 4T Number-domain authority mismatch");
}

export function worldY(geometry, normalized) {
    return geometry.terrain.mapping.worldScale * normalized + geometry.terrain.mapping.worldOffset;
}

export function profileForBlock(geometry, seed, blockIndex) {
    const epoch = Math.floor(blockIndex / 8);
    const slot = positiveModulo(blockIndex, 8);
    const first = positiveModulo(Math.floor(8 * sampleUnit(seed, 15, 0)) + epoch, 8);
    const last = positiveModulo(first + 2, 8);
    const middle = Array.from({ length: 8 }, (_, index) => index).filter((index) => index !== first && index !== last);
    for (let index = 5; index >= 1; index -= 1) {
        const sampleIndex = (Math.imul(epoch, 6) + 5 - index) >>> 0;
        const exchange = Math.floor((index + 1) * sampleUnit(seed, 16, sampleIndex));
        [middle[index], middle[exchange]] = [middle[exchange], middle[index]];
    }
    return { epoch, profile: [first, ...middle, last][slot], slot };
}

export function seededBlock(geometry, seed, blockIndex) {
    const selected = profileForBlock(geometry, seed, blockIndex);
    const samples = geometry.terrain.profiles[`S${selected.profile}`];
    return {
        epoch: selected.epoch,
        index: blockIndex,
        profile: selected.profile,
        slot: selected.slot,
        vertices: samples.map((height, index) => [blockIndex * 512 + index * 16, worldY(geometry, height)]),
    };
}

function interpolate(geometry, samples, localX) {
    const segment = Math.min(31, Math.floor(localX / 16));
    const fraction = (localX - segment * 16) / 16;
    return worldY(geometry, samples[segment] + (samples[segment + 1] - samples[segment]) * fraction);
}

export function seededHeight(geometry, seed, x) {
    const blockIndex = Math.floor(x / 512);
    const selected = profileForBlock(geometry, seed, blockIndex);
    return interpolate(geometry, geometry.terrain.profiles[`S${selected.profile}`], x - blockIndex * 512);
}

export function assignedHeight(geometry, assignment, x) {
    const blockIndex = Math.floor(x / 512);
    const profile = blockIndex === assignment.leftBlock ? assignment.leftProfile : assignment.rightProfile;
    if (profile === undefined) throw new RangeError(`Assignment ${assignment.assignmentId} misses block ${blockIndex}`);
    return interpolate(geometry, geometry.terrain.profiles[`S${profile}`], x - blockIndex * 512);
}

export function siteDescriptor(geometry, heightAt, index, nominalCenter, candidateOrder) {
    const order = CANDIDATE_ORDERS[candidateOrder];
    for (let candidateOrdinal = 0; candidateOrdinal < order.length; candidateOrdinal += 1) {
        const offsetIndex = order[candidateOrdinal];
        const center = nominalCenter + CANDIDATE_OFFSETS[offsetIndex];
        const closedFootprint = [center - 4.8, center + 13.8];
        const xs = [...closedFootprint];
        for (let x = Math.ceil(closedFootprint[0] / 16) * 16; x <= closedFootprint[1]; x += 16) xs.push(x);
        const localNativeMaximum = Math.max(...xs.map(heightAt));
        const platformTop = localNativeMaximum + 2.5;
        const normalizedDeck = (platformTop + 9.2) / 64;
        if (normalizedDeck > 0.5) continue;
        const supportXs = [
            closedFootprint[0],
            closedFootprint[0] + 1,
            closedFootprint[0] + 8.8,
            closedFootprint[0] + 9.8,
            closedFootprint[0] + 17.6,
            closedFootprint[0] + 18.6,
        ];
        return {
            index,
            nominalCenter,
            candidateOrder,
            candidateOrdinal,
            offsetIndex,
            center,
            closedFootprint,
            localNativeMaximum,
            platformTop,
            normalizedDeck,
            supportFeet: supportXs.map(heightAt),
        };
    }
    throw new Error(`Candidate exhaustion at ${nominalCenter}`);
}

function millimeters(value) {
    const result = Math.round(value * 1000);
    if (Math.abs(result / 1000 - value) > 1e-12) throw new Error(`Non-millimetre value ${value}`);
    return result;
}

export function assignmentsFor(geometry) {
    const phases = Array.from({ length: 16 }, (_, index) => positiveModulo(36 + 96 * index, 512)).sort(
        (left, right) => left - right,
    );
    const assignments = [];
    for (const phase of phases) {
        const leftBlock = Math.floor((phase - 4.8) / 512);
        const rightBlock = Math.floor((phase + 149.8) / 512);
        for (let leftProfile = 0; leftProfile < 8; leftProfile += 1) {
            const rightProfiles =
                leftBlock === rightBlock
                    ? [leftProfile]
                    : Array.from({ length: 8 }, (_, index) => index).filter((index) => index !== leftProfile);
            for (const rightProfile of rightProfiles) {
                for (let candidateOrder = 0; candidateOrder < 2; candidateOrder += 1) {
                    const assignmentId = `p${phase}-a${leftProfile}-b${rightProfile}-o${candidateOrder}`;
                    const partial = { assignmentId, phase, leftBlock, rightBlock, leftProfile, rightProfile };
                    const heightAt = (x) => assignedHeight(geometry, partial, x);
                    const origin = siteDescriptor(geometry, heightAt, 0, phase, candidateOrder);
                    const target = siteDescriptor(geometry, heightAt, 1, phase + 96, candidateOrder);
                    const distance = target.center - origin.center;
                    const distanceMillimeters = millimeters(distance);
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
                        distance,
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
    if (assignments.length !== 736) throw new Error(`Expected 736 assignments, got ${assignments.length}`);
    return assignments;
}

export function groupAssignments(assignments) {
    const groups = new Map();
    for (const assignment of assignments) {
        const group = groups.get(assignment.pairKey) ?? { assignmentIds: [], assignments: [] };
        group.assignmentIds.push(assignment.assignmentId);
        group.assignments.push(assignment);
        groups.set(assignment.pairKey, group);
    }
    const ordered = [...groups.values()].sort((left, right) => {
        const a = left.assignments[0];
        const b = right.assignments[0];
        return (
            a.distanceMillimeters - b.distanceMillimeters ||
            a.originMillimeters - b.originMillimeters ||
            a.targetMillimeters - b.targetMillimeters
        );
    });
    if (ordered.length !== 312) throw new Error(`Expected 312 pair keys, got ${ordered.length}`);
    return ordered;
}

export function envelopeHeight(originDeck, targetDeck, distance, x) {
    const origin = originDeck - 2.5;
    const target = targetDeck - 2.5;
    const barrier = distance - 4.8;
    if (x <= 13.8) return origin;
    if (x >= barrier) return target;
    return Math.min(29.2, origin + 0.4 * (x - 13.8), target + 0.4 * (barrier - x));
}

export function envelopeVertices(originDeck, targetDeck, distance) {
    const origin = originDeck - 2.5;
    const target = targetDeck - 2.5;
    const barrier = distance - 4.8;
    const xs = new Set([5, 13.8, barrier, distance + 11]);
    for (const x of [
        13.8 + (29.2 - origin) / 0.4,
        barrier - (29.2 - target) / 0.4,
        (target - origin + 0.4 * (barrier + 13.8)) / 0.8,
    ]) {
        if (x > 13.8 && x < barrier) xs.add(x);
    }
    return [...xs]
        .sort((left, right) => left - right)
        .map((x) => [x, envelopeHeight(originDeck, targetDeck, distance, x)])
        .filter((point, index, values) => index === 0 || canonicalBytes(point) !== canonicalBytes(values[index - 1]));
}

function siteForWorld(geometry, heightAt, index, nominalCenter, candidateOrder) {
    return siteDescriptor(geometry, heightAt, index, nominalCenter, candidateOrder);
}

export function envelopeWorld(geometry, originDeck, targetDeck, distance) {
    const relativeEnvelope = envelopeVertices(originDeck, targetDeck, distance);
    const at = (x) => envelopeHeight(originDeck, targetDeck, distance, x);
    const make = (index, center, deck) => {
        const left = center - 4.8;
        return {
            index,
            center,
            platformTop: deck,
            supportFeet: [left, left + 1, left + 8.8, left + 9.8, left + 17.6, left + 18.6].map(at),
        };
    };
    return {
        relativeEnvelope,
        originSiteId: 0,
        sites: [make(0, 0, originDeck), make(1, distance, targetDeck)],
        vertices: relativeEnvelope,
    };
}

export function assignmentWorld(geometry, assignment) {
    const heightAt = (x) => assignedHeight(geometry, assignment, x);
    const origin = siteForWorld(geometry, heightAt, 0, assignment.phase, assignment.candidateOrder);
    const target = siteForWorld(geometry, heightAt, 1, assignment.phase + 96, assignment.candidateOrder);
    const left = origin.center - 31;
    const right = target.center + 31;
    const xs = new Set([left, right]);
    for (let x = Math.ceil(left / 16) * 16; x <= right; x += 16) xs.add(x);
    for (const site of [origin, target]) {
        for (const x of [
            site.center - 4.8,
            site.center - 3.8,
            site.center + 4,
            site.center + 5,
            site.center + 12.8,
            site.center + 13.8,
        ])
            xs.add(x);
    }
    return {
        originSiteId: 0,
        sites: [origin, target],
        vertices: [...xs].sort((a, b) => a - b).map((x) => [x, heightAt(x)]),
    };
}

export function openingWorld(geometry, profile, candidateOrder) {
    const samples = geometry.terrain.profiles[`S${profile}`];
    const at = (x) => interpolate(geometry, samples, positiveModulo(x, 512));
    const site = siteDescriptor(geometry, at, 0, 36, candidateOrder);
    const xs = new Set([0, site.center + 31]);
    for (let x = 0; x <= site.center + 31; x += 16) xs.add(x);
    for (const x of [
        site.center - 4.8,
        site.center - 3.8,
        site.center + 4,
        site.center + 5,
        site.center + 12.8,
        site.center + 13.8,
    ])
        xs.add(x);
    return {
        originSiteId: null,
        sites: [site],
        vertices: [...xs].sort((a, b) => a - b).map((x) => [x, at(x)]),
    };
}

export function seededSite(geometry, seed, siteIndex) {
    const candidateOrder = sampleUnit(seed, 17, 0) < 0.5 ? 0 : 1;
    return siteDescriptor(
        geometry,
        (x) => seededHeight(geometry, seed, x),
        siteIndex,
        36 + 96 * siteIndex,
        candidateOrder,
    );
}

export function worldWitness(geometry, seed, siteIndex) {
    const site = seededSite(geometry, seed, siteIndex);
    const firstBlock = Math.floor(site.closedFootprint[0] / 512);
    const lastBlock = Math.floor(site.closedFootprint[1] / 512);
    const superblocks = [];
    for (let index = firstBlock; index <= lastBlock; index += 1) superblocks.push(seededBlock(geometry, seed, index));
    const descriptor = {
        seed: normalizeSeed(seed),
        siteIndex,
        directionlessPhase: positiveModulo(site.nominalCenter, 512),
        superblocks,
        site,
    };
    return { descriptor, digest: digest(descriptor) };
}
