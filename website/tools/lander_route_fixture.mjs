import { createHash } from "node:crypto";

export function canonical(value) {
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

export const canonicalBytes = (value) => JSON.stringify(canonical(value));
export const fixtureDigest = (value) => createHash("sha256").update(canonicalBytes(value), "utf8").digest("hex");

export function fixturePose(pose, decimals) {
    return Object.fromEntries(
        ["x", "y", "vx", "vy", "angle", "angularVelocity"].map((key) => [key, Number(pose[key].toFixed(decimals))]),
    );
}
