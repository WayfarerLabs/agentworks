const COLLISION_DOUBLE_VIEW = new DataView(new ArrayBuffer(8));
const collisionAbsBigInt = (value) => (value < 0n ? -value : value);

function collisionGcd(left, right) {
    left = collisionAbsBigInt(left);
    right = collisionAbsBigInt(right);
    while (right) [left, right] = [right, left % right];
    return left;
}

function collisionRational(numerator, denominator = 1n) {
    if (denominator < 0n) {
        numerator = -numerator;
        denominator = -denominator;
    }
    const divisor = collisionGcd(numerator, denominator) || 1n;
    return { numerator: numerator / divisor, denominator: denominator / divisor };
}

const COLLISION_ZERO = collisionRational(0n);
const COLLISION_ONE = collisionRational(1n);

export const affineHullEnclosure = (left, right, radius) => ({
    left: Math.min(left.x, right.x) - radius,
    right: Math.max(left.x, right.x) + radius,
    bottom: Math.min(left.y, right.y) - radius,
    top: Math.max(left.y, right.y) + radius,
});
export function boundsOverlap(left, right, margin = 0, downward = false) {
    return (
        left.right >= right.left - margin &&
        left.left <= right.right + margin &&
        left.top >= right.bottom - margin &&
        (downward || left.bottom <= right.top + margin)
    );
}

function segmentDistanceSquared(a, b, c, d) {
    const orientation = (p, q, r) => (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);
    const onSegment = (p, q, r) =>
        q.x >= Math.min(p.x, r.x) &&
        q.x <= Math.max(p.x, r.x) &&
        q.y >= Math.min(p.y, r.y) &&
        q.y <= Math.max(p.y, r.y);
    const signs = [orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)];
    if (
        (signs[0] === 0 && onSegment(a, c, b)) ||
        (signs[1] === 0 && onSegment(a, d, b)) ||
        (signs[2] === 0 && onSegment(c, a, d)) ||
        (signs[3] === 0 && onSegment(c, b, d)) ||
        (signs[0] > 0 !== signs[1] > 0 && signs[2] > 0 !== signs[3] > 0)
    )
        return 0;
    const pointDistanceSquared = (point, start, end) => {
        const dx = end.x - start.x,
            dy = end.y - start.y,
            lengthSquared = dx * dx + dy * dy,
            projection =
                lengthSquared === 0
                    ? 0
                    : Math.min(1, Math.max(0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared)),
            x = start.x + projection * dx,
            y = start.y + projection * dy;
        return (point.x - x) ** 2 + (point.y - y) ** 2;
    };
    return Math.min(
        pointDistanceSquared(a, c, d),
        pointDistanceSquared(b, c, d),
        pointDistanceSquared(c, a, b),
        pointDistanceSquared(d, a, b),
    );
}

export function polygonSegmentDistanceSquared(polygon, start, end) {
    let minimum = Infinity;
    for (let index = 0; index < polygon.length; index += 1) {
        minimum = Math.min(
            minimum,
            segmentDistanceSquared(polygon[index], polygon[(index + 1) % polygon.length], start, end),
        );
    }
    return minimum;
}

function pointInPolygon(point, polygon) {
    let inside = false;
    for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index, index += 1) {
        const a = polygon[index],
            b = polygon[previous];
        if (a.y > point.y !== b.y > point.y && point.x <= ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x)
            inside = !inside;
    }
    return inside;
}

export function polygonDistanceSquared(left, right) {
    if (left.some((point) => pointInPolygon(point, right)) || right.some((point) => pointInPolygon(point, left)))
        return 0;
    let minimum = Infinity;
    for (let index = 0; index < right.length; index += 1) {
        minimum = Math.min(
            minimum,
            polygonSegmentDistanceSquared(left, right[index], right[(index + 1) % right.length]),
        );
    }
    return minimum;
}
export function hasInteriorAngleKnot(start, travel) {
    const end = start + travel;
    if (travel > 0) return Math.floor(start) + 1 <= Math.ceil(end) - 1;
    if (travel < 0) return Math.ceil(start) - 1 >= Math.floor(end) + 1;
    return false;
}

const compareRationals = (left, right) =>
    left.numerator * right.denominator < right.numerator * left.denominator
        ? -1
        : left.numerator * right.denominator > right.numerator * left.denominator
          ? 1
          : 0;
const rationalMidpoint = (left, right) =>
    collisionRational(
        left.numerator * right.denominator + right.numerator * left.denominator,
        2n * left.denominator * right.denominator,
    );

function collisionDyadicInteger(value) {
    if (value === 0) return { exponent: 0, integer: 0n };
    COLLISION_DOUBLE_VIEW.setFloat64(0, value, false);
    const high = COLLISION_DOUBLE_VIEW.getUint32(0, false),
        low = COLLISION_DOUBLE_VIEW.getUint32(4, false),
        sign = high >>> 31 ? -1n : 1n,
        exponentBits = (high >>> 20) & 0x7ff,
        fraction = (BigInt(high & 0xfffff) << 32n) | BigInt(low);
    return exponentBits === 0
        ? { exponent: -1074, integer: sign * fraction }
        : { exponent: exponentBits - 1075, integer: sign * ((1n << 52n) | fraction) };
}

function collisionPolynomial(coefficients) {
    const parts = coefficients.map(collisionDyadicInteger),
        exponent = Math.min(...parts.map((part) => part.exponent)),
        result = parts.map((part) => part.integer << BigInt(part.exponent - exponent));
    while (result.length > 1 && result.at(-1) === 0n) result.pop();
    const divisor = result.reduce((value, coefficient) => collisionGcd(value, coefficient), 0n) || 1n;
    const sign = result.at(-1) < 0n ? -1n : 1n;
    return result.map((coefficient) => (coefficient / divisor) * sign);
}

function polynomialSignAt(polynomial, value) {
    const [constant = 0n, linear = 0n, quadratic = 0n] = polynomial,
        numerator = value.numerator,
        denominator = value.denominator,
        result =
            constant * denominator * denominator + linear * numerator * denominator + quadratic * numerator * numerator;
    return result < 0n ? -1 : result > 0n ? 1 : 0;
}

function integerSquareRoot(value) {
    if (value < 2n) return value;
    let left = 1n,
        right = 1n << BigInt(Math.ceil(value.toString(2).length / 2));
    while (left < right) {
        const middle = (left + right + 1n) >> 1n;
        if (middle * middle <= value) left = middle;
        else right = middle - 1n;
    }
    return left;
}

function rationalRoot(value) {
    return { exact: value, left: value, right: value, polynomial: [value.numerator, -value.denominator], ordinal: 0 };
}

function rootsInUnit(coefficients) {
    const polynomial = collisionPolynomial(coefficients),
        [constant = 0n, linear = 0n, quadratic = 0n] = polynomial;
    if (quadratic === 0n) {
        if (linear === 0n) return [];
        const value = collisionRational(-constant, linear);
        return compareRationals(value, COLLISION_ZERO) >= 0 && compareRationals(value, COLLISION_ONE) <= 0
            ? [rationalRoot(value)]
            : [];
    }
    const discriminant = linear * linear - 4n * quadratic * constant;
    if (discriminant < 0n) return [];
    const squareRoot = integerSquareRoot(discriminant);
    if (squareRoot * squareRoot === discriminant) {
        const roots = [collisionRational(-linear - squareRoot, 2n * quadratic)];
        if (squareRoot !== 0n) roots.push(collisionRational(-linear + squareRoot, 2n * quadratic));
        return roots
            .filter((root) => compareRationals(root, COLLISION_ZERO) >= 0 && compareRationals(root, COLLISION_ONE) <= 0)
            .sort(compareRationals)
            .map(rationalRoot);
    }
    const vertex = collisionRational(-linear, 2n * quadratic),
        points = [COLLISION_ZERO];
    if (compareRationals(vertex, COLLISION_ZERO) > 0 && compareRationals(vertex, COLLISION_ONE) < 0)
        points.push(vertex);
    points.push(COLLISION_ONE);
    const roots = [];
    for (let index = 1; index < points.length; index += 1) {
        const left = points[index - 1],
            right = points[index],
            leftSign = polynomialSignAt(polynomial, left),
            rightSign = polynomialSignAt(polynomial, right);
        if (leftSign * rightSign < 0) roots.push({ exact: null, left, right, polynomial, ordinal: roots.length });
    }
    return roots;
}

function refineRoot(root) {
    if (root.exact) return;
    const middle = rationalMidpoint(root.left, root.right),
        sign = polynomialSignAt(root.polynomial, middle);
    if (sign === 0) {
        root.exact = middle;
        root.left = middle;
        root.right = middle;
    } else if (sign === polynomialSignAt(root.polynomial, root.left)) root.left = middle;
    else root.right = middle;
}

function proportionalPolynomials(left, right) {
    const length = Math.max(left.length, right.length);
    let leftFactor = 0n,
        rightFactor = 0n;
    for (let index = 0; index < length; index += 1) {
        const a = left[index] ?? 0n,
            b = right[index] ?? 0n;
        if (a === 0n && b === 0n) continue;
        if (a === 0n || b === 0n) return false;
        if (leftFactor === 0n) [leftFactor, rightFactor] = [a, b];
        else if (a * rightFactor !== b * leftFactor) return false;
    }
    return true;
}

export function compareExactRoots(left, right) {
    if (left.exact && right.exact) return compareRationals(left.exact, right.exact);
    if (!left.exact && !right.exact && proportionalPolynomials(left.polynomial, right.polynomial))
        return left.ordinal - right.ordinal;
    for (;;) {
        if (compareRationals(left.right, right.left) < 0) return -1;
        if (compareRationals(right.right, left.left) < 0) return 1;
        if (left.exact) refineRoot(right);
        else if (right.exact) refineRoot(left);
        else {
            refineRoot(left);
            refineRoot(right);
        }
    }
}

function signAtRoot(coefficients, root) {
    const polynomial = collisionPolynomial(coefficients);
    if (polynomial.every((coefficient) => coefficient === 0n) || proportionalPolynomials(polynomial, root.polynomial))
        return 0;
    if (root.exact) return polynomialSignAt(polynomial, root.exact);
    const vertex = polynomial.length === 3 ? collisionRational(-polynomial[1], 2n * polynomial[2]) : null;
    for (;;) {
        const left = polynomialSignAt(polynomial, root.left),
            right = polynomialSignAt(polynomial, root.right),
            vertexOutside =
                !vertex || compareRationals(vertex, root.left) <= 0 || compareRationals(vertex, root.right) >= 0;
        if (left !== 0 && left === right && vertexOutside) return left;
        refineRoot(root);
    }
}

export function exactRootNumber(root) {
    if (root.exact) return Number(root.exact.numerator) / Number(root.exact.denominator);
    for (;;) {
        const left = Number(root.left.numerator) / Number(root.left.denominator),
            right = Number(root.right.numerator) / Number(root.right.denominator);
        if (Object.is(left, right)) return left;
        refineRoot(root);
    }
}

function movingOrientation(startA, endA, startB, endB, point) {
    const ax = endA.x - startA.x,
        ay = endA.y - startA.y,
        bx = endB.x - startB.x,
        by = endB.y - startB.y,
        dx0 = startB.x - startA.x,
        dx1 = bx - ax,
        dy0 = startB.y - startA.y,
        dy1 = by - ay,
        ex0 = point.x - startA.x,
        ex1 = -ax,
        ey0 = point.y - startA.y,
        ey1 = -ay;
    return [dx0 * ey0 - dy0 * ex0, dx0 * ey1 + dx1 * ey0 - dy0 * ex1 - dy1 * ex0, dx1 * ey1 - dy1 * ex1];
}

function fixedOrientation(left, right, start, end) {
    const dx = right.x - left.x,
        dy = right.y - left.y;
    return [dx * (start.y - left.y) - dy * (start.x - left.x), dx * (end.y - start.y) - dy * (end.x - start.x), 0];
}

function affineDot(leftStart, leftEnd, rightStart, rightEnd) {
    const lx = leftStart.x,
        ly = leftStart.y,
        ldx = leftEnd.x - lx,
        ldy = leftEnd.y - ly,
        rx = rightStart.x,
        ry = rightStart.y,
        rdx = rightEnd.x - rx,
        rdy = rightEnd.y - ry;
    return [lx * rx + ly * ry, lx * rdx + ldx * rx + ly * rdy + ldy * ry, ldx * rdx + ldy * rdy];
}

const subtractCollisionPoint = (left, right) => ({ x: left.x - right.x, y: left.y - right.y });
function projectionPolynomials(startA, endA, startB, endB, left, right) {
    const fixed = subtractCollisionPoint(right, left),
        fixedReverse = subtractCollisionPoint(left, right),
        edgeStart = subtractCollisionPoint(startB, startA),
        edgeEnd = subtractCollisionPoint(endB, endA),
        edgeReverseStart = subtractCollisionPoint(startA, startB),
        edgeReverseEnd = subtractCollisionPoint(endA, endB);
    return [
        affineDot(subtractCollisionPoint(left, startA), subtractCollisionPoint(left, endA), edgeStart, edgeEnd),
        affineDot(
            subtractCollisionPoint(left, startB),
            subtractCollisionPoint(left, endB),
            edgeReverseStart,
            edgeReverseEnd,
        ),
        affineDot(subtractCollisionPoint(right, startA), subtractCollisionPoint(right, endA), edgeStart, edgeEnd),
        affineDot(
            subtractCollisionPoint(right, startB),
            subtractCollisionPoint(right, endB),
            edgeReverseStart,
            edgeReverseEnd,
        ),
        affineDot(subtractCollisionPoint(startA, left), subtractCollisionPoint(endA, left), fixed, fixed),
        affineDot(
            subtractCollisionPoint(startA, right),
            subtractCollisionPoint(endA, right),
            fixedReverse,
            fixedReverse,
        ),
        affineDot(subtractCollisionPoint(startB, left), subtractCollisionPoint(endB, left), fixed, fixed),
        affineDot(
            subtractCollisionPoint(startB, right),
            subtractCollisionPoint(endB, right),
            fixedReverse,
            fixedReverse,
        ),
    ];
}

function segmentsIntersectAtRoot(startA, endA, startB, endB, segment, root) {
    const signs = [
            movingOrientation(startA, endA, startB, endB, segment[0]),
            movingOrientation(startA, endA, startB, endB, segment[1]),
            fixedOrientation(segment[0], segment[1], startA, endA),
            fixedOrientation(segment[0], segment[1], startB, endB),
        ].map((polynomial) => signAtRoot(polynomial, root)),
        projections = projectionPolynomials(startA, endA, startB, endB, segment[0], segment[1]),
        on = (first, second) => signAtRoot(projections[first], root) >= 0 && signAtRoot(projections[second], root) >= 0;
    return (
        (signs[0] * signs[1] < 0 && signs[2] * signs[3] < 0) ||
        (signs[0] === 0 && on(0, 1)) ||
        (signs[1] === 0 && on(2, 3)) ||
        (signs[2] === 0 && on(4, 5)) ||
        (signs[3] === 0 && on(6, 7))
    );
}

export function exactSegmentContact(leftHull, rightHull, segment) {
    let earliest = null;
    for (let index = 0; index < leftHull.length; index += 1) {
        const next = (index + 1) % leftHull.length,
            polynomials = [
                movingOrientation(leftHull[index], rightHull[index], leftHull[next], rightHull[next], segment[0]),
                movingOrientation(leftHull[index], rightHull[index], leftHull[next], rightHull[next], segment[1]),
                fixedOrientation(segment[0], segment[1], leftHull[index], rightHull[index]),
                fixedOrientation(segment[0], segment[1], leftHull[next], rightHull[next]),
                ...projectionPolynomials(
                    leftHull[index],
                    rightHull[index],
                    leftHull[next],
                    rightHull[next],
                    segment[0],
                    segment[1],
                ),
            ],
            roots = [rationalRoot(COLLISION_ZERO), rationalRoot(COLLISION_ONE)];
        for (const polynomial of polynomials) roots.push(...rootsInUnit(polynomial));
        roots.sort(compareExactRoots);
        for (const root of roots)
            if (
                segmentsIntersectAtRoot(
                    leftHull[index],
                    rightHull[index],
                    leftHull[next],
                    rightHull[next],
                    segment,
                    root,
                ) &&
                (!earliest || compareExactRoots(root, earliest) < 0)
            )
                earliest = root;
    }
    return earliest;
}

export const exactZeroRoot = () => rationalRoot(COLLISION_ZERO);
