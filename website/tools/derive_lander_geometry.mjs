import { createHash } from "node:crypto";
import { mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

const PROFILES = Object.freeze({
    S0: [0.35, 0.29, 0.25, 0.1, 0.25, 0.3, 0.39, 0.47, 0.42, 0.41, 0.36, 0.28, 0.38, 0.45, 0.4, 0.3, 0.23, 0.28, 0.33, 0.26, 0.2, 0.3, 0.25, 0.19, 0.29, 0.35, 0.29, 0.22, 0.16, 0.1, 0.17, 0.26, 0.35],
    S1: [0.35, 0.45, 0.6, 0.45, 0.35, 0.34, 0.27, 0.36, 0.41, 0.49, 0.41, 0.36, 0.31, 0.26, 0.34, 0.43, 0.52, 0.47, 0.37, 0.43, 0.33, 0.29, 0.28, 0.23, 0.33, 0.4, 0.45, 0.38, 0.36, 0.28, 0.36, 0.44, 0.35],
    S2: [0.35, 0.3, 0.25, 0.35, 0.43, 0.36, 0.28, 0.33, 0.43, 0.33, 0.24, 0.29, 0.2, 0.19, 0.14, 0.23, 0.32, 0.37, 0.32, 0.38, 0.45, 0.6, 0.45, 0.42, 0.47, 0.52, 0.42, 0.36, 0.46, 0.39, 0.37, 0.28, 0.35],
    S3: [0.35, 0.45, 0.52, 0.46, 0.38, 0.43, 0.49, 0.54, 0.48, 0.38, 0.43, 0.49, 0.44, 0.39, 0.45, 0.6, 0.45, 0.36, 0.42, 0.5, 0.44, 0.36, 0.27, 0.35, 0.27, 0.18, 0.26, 0.36, 0.3, 0.25, 0.35, 0.44, 0.35],
    S4: [0.35, 0.26, 0.33, 0.42, 0.45, 0.6, 0.45, 0.41, 0.48, 0.39, 0.38, 0.29, 0.39, 0.29, 0.23, 0.31, 0.38, 0.28, 0.19, 0.28, 0.23, 0.18, 0.25, 0.32, 0.42, 0.37, 0.32, 0.4, 0.35, 0.28, 0.19, 0.28, 0.35],
    S5: [0.35, 0.45, 0.6, 0.45, 0.33, 0.39, 0.48, 0.38, 0.3, 0.28, 0.22, 0.27, 0.36, 0.42, 0.34, 0.31, 0.28, 0.2, 0.28, 0.37, 0.43, 0.51, 0.41, 0.33, 0.43, 0.48, 0.38, 0.31, 0.41, 0.43, 0.5, 0.43, 0.35],
    S6: [0.35, 0.28, 0.33, 0.43, 0.35, 0.3, 0.38, 0.31, 0.22, 0.32, 0.37, 0.3, 0.36, 0.38, 0.44, 0.34, 0.28, 0.35, 0.45, 0.4, 0.32, 0.39, 0.34, 0.25, 0.32, 0.34, 0.35, 0.41, 0.31, 0.25, 0.1, 0.25, 0.35],
    S7: [0.35, 0.29, 0.21, 0.15, 0.23, 0.32, 0.42, 0.35, 0.33, 0.28, 0.36, 0.38, 0.48, 0.42, 0.34, 0.43, 0.48, 0.39, 0.33, 0.25, 0.34, 0.4, 0.33, 0.23, 0.32, 0.35, 0.4, 0.31, 0.25, 0.1, 0.25, 0.27, 0.35],
});
const OFFSETS = Object.freeze([0, 8, 16, 24, 32, 40]);
const ORDERS = Object.freeze([Object.freeze([0, 1, 2, 3, 4, 5]), Object.freeze([0, 5, 4, 3, 2, 1])]);
const SEEDS = Object.freeze([11, 39, 41, 0x41475731]);

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object")
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    return value;
}

function canonicalBytes(value) {
    return JSON.stringify(canonical(value));
}

function prettyJson(value, indent = 0, prefixWidth = indent) {
    if (!value || typeof value !== "object") return JSON.stringify(value);
    const padding = " ".repeat(indent);
    const childPadding = " ".repeat(indent + 2);
    if (Array.isArray(value)) {
        if (value.length === 0) return "[]";
        if (value.every((item) => !item || typeof item !== "object")) {
            const compact = `[${value.map((item) => JSON.stringify(item)).join(", ")}]`;
            if (prefixWidth + compact.length <= 120) return compact;
            const lines = [];
            let line = childPadding;
            for (let index = 0; index < value.length; index += 1) {
                const token = `${JSON.stringify(value[index])}${index + 1 < value.length ? "," : ""}`;
                const separated = line === childPadding ? token : ` ${token}`;
                if (line.length + separated.length > 120) {
                    lines.push(line);
                    line = `${childPadding}${token}`;
                } else line += separated;
            }
            lines.push(line);
            return `[\n${lines.join("\n")}\n${padding}]`;
        }
        const lines = value.map((item, index) => {
            const suffix = index + 1 < value.length ? "," : "";
            return `${childPadding}${prettyJson(item, indent + 2, indent + 2)}${suffix}`;
        });
        return `[\n${lines.join("\n")}\n${padding}]`;
    }
    const entries = Object.entries(value);
    if (entries.length === 0) return "{}";
    const lines = entries.map(([key, item], index) => {
        const prefix = `${childPadding}${JSON.stringify(key)}: `;
        const suffix = index + 1 < entries.length ? "," : "";
        return `${prefix}${prettyJson(item, indent + 2, prefix.length)}${suffix}`;
    });
    return `{\n${lines.join("\n")}\n${padding}}`;
}

function mixUint32(input) {
    let value = Number(input) >>> 0;
    value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
    value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
    return (value ^ (value >>> 16)) >>> 0;
}

const normalizeSeed = (seed) => Number(seed) >>> 0 || 0x6d2b79f5;
const positiveModulo = (value, modulus) => ((value % modulus) + modulus) % modulus;
const sampleUnit = (seed, stream, index) =>
    mixUint32(normalizeSeed(seed) ^ Math.imul(stream, 0x9e3779b9) ^ Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b)) /
    2 ** 32;

function profileForBlock(seed, blockIndex) {
    const epoch = Math.floor(blockIndex / 8);
    const slot = positiveModulo(blockIndex, 8);
    const first = positiveModulo(Math.floor(8 * sampleUnit(seed, 15, 0)) + epoch, 8);
    const last = positiveModulo(first + 2, 8);
    const middle = Array.from({ length: 8 }, (_, index) => index).filter((index) => index !== first && index !== last);
    for (let index = 5; index >= 1; index -= 1) {
        const exchange = Math.floor((index + 1) * sampleUnit(seed, 16, (Math.imul(epoch, 6) + 5 - index) >>> 0));
        [middle[index], middle[exchange]] = [middle[exchange], middle[index]];
    }
    return [first, ...middle, last][slot];
}

function interpolate(samples, localX) {
    const segment = Math.min(31, Math.floor(localX / 16));
    const fraction = (localX - segment * 16) / 16;
    return 64 * (samples[segment] + (samples[segment + 1] - samples[segment]) * fraction) - 9.2;
}

function seededHeight(seed, x) {
    const block = Math.floor(x / 512);
    return interpolate(PROFILES[`S${profileForBlock(seed, block)}`], x - block * 512);
}

function site(heightAt, index, nominalCenter, orderIndex) {
    for (let ordinal = 0; ordinal < 6; ordinal += 1) {
        const offsetIndex = ORDERS[orderIndex][ordinal];
        const center = nominalCenter + OFFSETS[offsetIndex];
        const left = center - 4.8;
        const right = center + 13.8;
        const samples = [left, right];
        for (let x = Math.ceil(left / 16) * 16; x <= right; x += 16) samples.push(x);
        const platformTop = Math.max(...samples.map(heightAt)) + 2.5;
        if ((platformTop + 9.2) / 64 > 0.5) continue;
        return { center, index, ordinal, platformTop };
    }
    throw new Error(`candidate exhaustion at site ${index}`);
}

function assignmentClosure() {
    const keys = new Set();
    let assignments = 0;
    const phases = Array.from({ length: 8 }, (_, index) => positiveModulo(36 + 192 * index, 512)).sort((a, b) => a - b);
    for (const phase of phases) {
        const leftBlock = Math.floor((phase - 4.8) / 512);
        const rightBlock = Math.floor((phase + 245.8) / 512);
        for (let leftProfile = 0; leftProfile < 8; leftProfile += 1) {
            const rightProfiles = leftBlock === rightBlock ? [leftProfile] : Array.from({ length: 8 }, (_, i) => i).filter((i) => i !== leftProfile);
            for (const rightProfile of rightProfiles) {
                const heightAt = (x) => {
                    const block = Math.floor(x / 512);
                    const profile = block === leftBlock ? leftProfile : rightProfile;
                    return interpolate(PROFILES[`S${profile}`], x - block * 512);
                };
                for (let order = 0; order < 2; order += 1) {
                    const origin = site(heightAt, 0, phase, order);
                    const target = site(heightAt, 1, phase + 192, order);
                    keys.add(`r:${Math.round((target.center - origin.center) * 1000)}:${Math.round(origin.platformTop * 1000)}:${Math.round(target.platformTop * 1000)}`);
                    assignments += 1;
                }
            }
        }
    }
    if (assignments !== 512 || keys.size !== 250) throw new Error(`geometry census ${assignments}/${keys.size}`);
}

function validateClosure() {
    const counts = [];
    for (const profile of Object.values(PROFILES)) {
        const grades = profile.slice(1).map((value, index) => Math.round((value - profile[index]) * 400) / 100);
        counts.push(grades.filter((grade, index) => grade * grades[(index + 1) % 32] < 0).length);
        if (Math.max(...grades.map(Math.abs)) !== 0.6) throw new Error("grade limit is not exercised");
        if (Math.max(...grades.map((grade, index) => Math.abs(grades[(index + 1) % 32] - grade))) !== 1.2)
            throw new Error("grade-change limit is not exercised");
    }
    if (JSON.stringify(counts) !== JSON.stringify([12, 12, 16, 16, 16, 12, 16, 12]))
        throw new Error(`reversal census ${counts}`);
    assignmentClosure();
    for (const seed of SEEDS) {
        const order = sampleUnit(seed, 17, 0) < 0.5 ? 0 : 1;
        let previous = site((x) => seededHeight(seed, x), -4095, 36 - 192 * 4095, order);
        let distance = 0;
        for (let index = -4094; index <= 4095; index += 1) {
            const next = site((x) => seededHeight(seed, x), index, 36 + 192 * index, order);
            if (next.ordinal > 5 || (next.platformTop + 9.2) / 64 > 0.5) throw new Error(`invalid site ${seed}/${index}`);
            distance += next.center - previous.center;
            previous = next;
        }
        if (Math.abs(distance / 8190 - 192) > 0.01) throw new Error(`signed spacing mean drift for ${seed}`);
        if (previous.center + 13.8 >= 786432) throw new Error(`final site misses rail clearance for ${seed}`);
    }
    if (49152 - -49152 + 1 !== 98305 || 49152 - -49152 !== 98304) throw new Error("world census mismatch");
}

function payload() {
    validateClosure();
    const value = {
        schema: "agw-lander-route-geometry/v10",
        deriverVersion: "agw-lander-geometry-deriver/v1",
        terrain: {
            cadence: 16,
            epochSuperblocks: 8,
            gradeChangeLimit: 1.2,
            gradeLimit: 0.6,
            mapping: { normalizedMaximum: 0.6, normalizedMinimum: 0.1, worldOffset: -9.2, worldScale: 64 },
            profiles: PROFILES,
            selection: { firstAdvancePerEpoch: 1, lastOffset: 2, offsetStream: 15, shuffleOrder: [5, 4, 3, 2, 1], shuffleStream: 16 },
            superblockWidth: 512,
        },
        site: { candidateOffsets: OFFSETS, candidateOrderStream: 17, candidateOrders: ORDERS, maxNormalizedDeck: 0.5, nominalOrigin: 36, nominalSpacing: 192, clearance: 2.5, closedFootprint: [-4.8, 13.8] },
        structure: {
            member: { cap: "butt", join: "round", width: 0.2 },
            noc: { mastHeight: 3.2, mastWidth: 0.5, roofOffset: 7.2, width: 7 },
            platform: { clearance: 2.5, thickness: 0.35, width: 9.6 },
            supportColumns: { bayHeight: 0.8, count: 3, railPairOffsets: [[0, 1], [8.8, 9.8], [17.6, 18.6]], width: 1 },
            truss: { bayCount: 12, bayHeight: 0.75, bayWidth: 1.55, span: 18.6 },
        },
        world: { maxSiteIndex: 4095, maxX: 786432, minSiteIndex: -4095, minX: -786432, terminusWidth: 0.2 },
        collision: { angleKnotDegrees: 1, margin: 0.02, recipe: "agw-lander-swept-collision/v2" },
        physics: {
            engineAcceleration: 90 / 7,
            engineForceCoefficient: 9,
            gravity: 30 / 7,
            gravityForceCoefficient: 3,
            massDenominator: 10,
            massNumerator: 7,
            stepSeconds: 1 / 120,
            torqueAcceleration: 80,
        },
    };
    const geometryDigest = createHash("sha256").update(canonicalBytes(value)).digest("hex");
    return `${prettyJson(canonical({ ...value, geometryDigest }))}\n`;
}

function parseArgs(args) {
    if (args.length !== 2 && args.length !== 4) throw new TypeError("usage");
    if (args[0] !== "--output" || !args[1] || (args.length === 4 && (args[2] !== "--verify" || !args[3])))
        throw new TypeError("usage");
    return { output: args[1], verify: args[3] };
}

async function atomicWrite(path, bytes) {
    const directory = await mkdtemp(join(dirname(path), ".lander-geometry-"));
    const temporary = join(directory, "fixture.json");
    try {
        await writeFile(temporary, bytes, "utf8");
        await rename(temporary, path);
    } finally {
        await rm(directory, { recursive: true, force: true });
    }
}

async function main() {
    let options;
    try {
        options = parseArgs(process.argv.slice(2));
    } catch {
        console.error("Usage: derive_lander_geometry.mjs --output PATH [--verify PATH]");
        process.exitCode = 2;
        return;
    }
    try {
        const bytes = payload();
        await atomicWrite(options.output, bytes);
        if (options.verify && (await readFile(options.verify, "utf8")) !== bytes) throw new Error("verification differs");
    } catch (error) {
        console.error(error instanceof Error ? error.message : error);
        process.exitCode = 1;
    }
}

await main();
