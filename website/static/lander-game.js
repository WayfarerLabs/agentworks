import {
    advanceCue,
    advanceMissionSequence,
    advanceSimulation,
    clearSimulationInput,
    createCueState,
    createPreflightModel,
    createSimulationClock,
    enqueueInputEdge,
    mixDigitalInput,
    mixEngineRequests,
    plumeForThrust,
    pointerEngineRequests,
    removeQueuedInputEdges,
    resetSimulationAccumulator,
    settleCue,
    transitionMission,
} from "./lander-model.js";

const ACTIVE_KEYS = new Map([
    ["Space", "Space"],
    ["ArrowUp", "ArrowUp"],
    ["ArrowLeft", "ArrowLeft"],
    ["ArrowRight", "ArrowRight"],
    ["KeyH", "KeyH"],
    ["KeyL", "KeyL"],
]);
const ZERO_INPUT = { left: 0, right: 0 };

function eventTime(event) {
    return Number.isFinite(event.timeStamp) ? event.timeStamp : performance.now();
}

function isEditable(target) {
    return (
        target instanceof Element &&
        Boolean(target.closest("a, button, input, select, textarea, [contenteditable='true']"))
    );
}

function unmodified(event, allowShift = false) {
    return !event.ctrlKey && !event.altKey && !event.metaKey && (allowShift || !event.shiftKey);
}

function physicalControl(event) {
    if (["Space", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(event.code)) {
        return event.code;
    }
    const key = event.key.toLowerCase();
    if (key === "h") {
        return "KeyH";
    }
    if (key === "l") {
        return "KeyL";
    }
    return null;
}

export class LanderGameController {
    constructor(root = document.querySelector("#lander-game")) {
        if (!root) {
            throw new Error("Lunar deployment game root is missing");
        }
        this.root = root;
        this.shell = root.querySelector("#lander-scene-shell");
        this.scene = root.querySelector("#lander-scene");
        this.startButton = root.querySelector("#lander-start");
        this.controls = root.querySelector("#lander-controls");
        this.status = root.querySelector("#lander-status");
        this.model = createPreflightModel();
        this.clock = createSimulationClock();
        this.cue = createCueState(false);
        this.heldKeys = new Set();
        this.pointerInput = ZERO_INPUT;
        this.pointer = null;
        this.pointerToken = 0;
        this.pulseTimer = null;
        this.frameId = null;
        this.previousFrame = null;
        this.destroyed = false;
        this.paused = document.hidden;
        this.abortController = new AbortController();
        this.motion = matchMedia("(prefers-reduced-motion: reduce)");
        this.cue = createCueState(this.motion.matches);
        this.installListeners();
        this.startButton.hidden = false;
        this.render();
        if (!this.paused) {
            this.requestFrame();
        }
    }

    installListeners() {
        const options = { signal: this.abortController.signal };
        this.startButton.addEventListener("click", () => this.start(false, performance.now()), options);
        document.addEventListener("keydown", (event) => this.onKeyDown(event), options);
        document.addEventListener("keyup", (event) => this.onKeyUp(event), options);
        window.addEventListener("blur", () => this.clearAllInput(performance.now()), options);
        this.shell.addEventListener("focusout", () => this.clearAllInput(performance.now()), options);
        this.shell.addEventListener("pointerdown", (event) => this.onPointerDown(event), options);
        this.shell.addEventListener("pointermove", (event) => this.onPointerMove(event), options);
        this.shell.addEventListener("pointerup", (event) => this.onPointerUp(event), options);
        this.shell.addEventListener("pointercancel", (event) => this.onPointerAbort(event), options);
        this.shell.addEventListener("lostpointercapture", (event) => this.onLostCapture(event), options);
        document.addEventListener("visibilitychange", () => this.onVisibilityChange(), options);
        this.motion.addEventListener("change", (event) => this.onMotionChange(event), options);
    }

    eventUsesActiveShell(event) {
        return this.model.state !== "preflight" && event.composedPath().includes(this.shell);
    }

    queueInput(timestamp, token = null) {
        const keyboard = mixDigitalInput(Object.fromEntries([...this.heldKeys].map((key) => [key, true])));
        const request = mixEngineRequests(keyboard, this.pointerInput);
        this.clock = enqueueInputEdge(this.clock, { timestamp, ...request, token });
    }

    start(holdSpace, timestamp) {
        if (this.model.state !== "preflight") {
            return;
        }
        this.cue = settleCue();
        this.model = transitionMission(this.model, "START");
        this.clock = createSimulationClock();
        this.previousFrame = null;
        this.heldKeys.clear();
        this.pointerInput = ZERO_INPUT;
        if (holdSpace) {
            this.heldKeys.add("Space");
            this.queueInput(timestamp);
        }
        this.startButton.hidden = true;
        this.startButton.disabled = true;
        this.controls.hidden = false;
        this.shell.tabIndex = 0;
        this.shell.setAttribute("role", "application");
        this.shell.setAttribute("aria-label", "Lunar deployment game");
        this.shell.setAttribute("aria-describedby", "lander-controls lander-status");
        this.scene.setAttribute("aria-hidden", "true");
        this.status.textContent = "Mission underway.";
        this.shell.focus({ preventScroll: true });
        this.render();
        this.requestFrame();
    }

    exit() {
        if (this.model.state === "preflight") {
            return;
        }
        this.clearAllInput(performance.now());
        this.model = transitionMission(this.model, "EXIT");
        this.clock = createSimulationClock();
        this.previousFrame = null;
        this.cue = settleCue();
        this.shell.tabIndex = -1;
        this.shell.removeAttribute("role");
        this.shell.removeAttribute("aria-label");
        this.shell.removeAttribute("aria-describedby");
        this.scene.removeAttribute("aria-hidden");
        this.controls.hidden = true;
        this.startButton.disabled = false;
        this.startButton.hidden = false;
        this.status.textContent = "";
        this.render();
        this.startButton.focus({ preventScroll: true });
        this.requestFrame();
    }

    restart() {
        if (!["failed", "succeeded"].includes(this.model.state)) {
            return;
        }
        this.clearAllInput(performance.now());
        this.model = transitionMission(this.model, "RESTART");
        this.clock = createSimulationClock();
        this.previousFrame = null;
        this.status.textContent = "Mission underway.";
        this.render();
        this.shell.focus({ preventScroll: true });
        this.requestFrame();
    }

    onKeyDown(event) {
        if (this.model.state === "preflight") {
            const targets = [document.body, this.root, this.shell, this.scene];
            const accepted =
                event.code === "Space" &&
                !event.repeat &&
                unmodified(event) &&
                targets.includes(event.target) &&
                !isEditable(event.target);
            if (accepted) {
                event.preventDefault();
                this.start(true, eventTime(event));
            }
            return;
        }
        if (!this.eventUsesActiveShell(event)) {
            return;
        }
        if (event.key === "Escape" && unmodified(event)) {
            event.preventDefault();
            this.exit();
            return;
        }
        if (
            event.key.toLowerCase() === "r" &&
            unmodified(event) &&
            ["failed", "succeeded"].includes(this.model.state)
        ) {
            event.preventDefault();
            this.restart();
            return;
        }
        const control = physicalControl(event);
        if (this.model.state !== "flying" || !control || !unmodified(event, true)) {
            return;
        }
        event.preventDefault();
        if (event.repeat || this.heldKeys.has(control)) {
            return;
        }
        this.heldKeys.add(ACTIVE_KEYS.get(control));
        this.queueInput(eventTime(event));
    }

    onKeyUp(event) {
        const control = physicalControl(event);
        if (!control || !this.heldKeys.has(control)) {
            return;
        }
        this.heldKeys.delete(control);
        if (this.model.state === "flying") {
            this.queueInput(eventTime(event));
        }
    }

    onPointerDown(event) {
        if (this.model.state !== "flying" || !event.isPrimary || event.button !== 0 || this.pointer) {
            return;
        }
        event.preventDefault();
        this.pointerToken += 1;
        this.pointer = {
            id: event.pointerId,
            x: event.clientX,
            y: event.clientY,
            timestamp: eventTime(event),
            token: this.pointerToken,
            captured: true,
        };
        this.shell.setPointerCapture(event.pointerId);
        this.pointerInput = { left: 0.72, right: 0.72 };
        this.queueInput(eventTime(event), this.pointerToken);
    }

    onPointerMove(event) {
        if (!this.pointer || event.pointerId !== this.pointer.id || this.model.state !== "flying") {
            return;
        }
        event.preventDefault();
        const width = this.scene.getBoundingClientRect().width;
        this.pointerInput = pointerEngineRequests(event.clientX - this.pointer.x, width);
        this.queueInput(eventTime(event), this.pointer.token);
    }

    onPointerUp(event) {
        if (!this.pointer || event.pointerId !== this.pointer.id) {
            return;
        }
        event.preventDefault();
        const pointer = this.pointer;
        const elapsed = eventTime(event) - pointer.timestamp;
        const distance = Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y);
        pointer.captured = false;
        this.pointer = null;
        if (this.shell.hasPointerCapture(event.pointerId)) {
            this.shell.releasePointerCapture(event.pointerId);
        }
        if (elapsed <= 180 && distance <= 10 && elapsed < 140) {
            this.pulseTimer = window.setTimeout(() => {
                this.pulseTimer = null;
                this.finishPointerGesture(performance.now(), pointer.token);
            }, 140 - elapsed);
            this.pointer = { ...pointer, logicalOnly: true };
            return;
        }
        this.finishPointerGesture(eventTime(event), pointer.token);
    }

    onPointerAbort(event) {
        if (this.pointer && event.pointerId === this.pointer.id) {
            this.clearAllInput(eventTime(event));
        }
    }

    onLostCapture(event) {
        if (this.pointer && !this.pointer.logicalOnly && event.pointerId === this.pointer.id) {
            this.clearAllInput(eventTime(event));
        }
    }

    finishPointerGesture(timestamp, token) {
        if (this.pointer?.token === token) {
            this.pointer = null;
        }
        this.pointerInput = ZERO_INPUT;
        this.clock = removeQueuedInputEdges(this.clock, token);
        this.queueInput(timestamp);
    }

    teardownPointer(timestamp) {
        if (this.pulseTimer !== null) {
            clearTimeout(this.pulseTimer);
            this.pulseTimer = null;
        }
        const pointer = this.pointer;
        this.pointer = null;
        if (pointer?.captured && this.shell.hasPointerCapture(pointer.id)) {
            this.shell.releasePointerCapture(pointer.id);
        }
        this.pointerInput = ZERO_INPUT;
        if (pointer) {
            this.clock = removeQueuedInputEdges(this.clock, pointer.token);
        }
        if (this.model.state === "flying") {
            this.queueInput(timestamp);
        }
    }

    clearAllInput(timestamp) {
        this.heldKeys.clear();
        this.teardownPointer(timestamp);
        this.clock = clearSimulationInput(this.clock, timestamp);
        this.model = { ...this.model, commanded: { ...ZERO_INPUT } };
        this.render();
    }

    onVisibilityChange() {
        this.paused = document.hidden;
        this.root.dataset.paused = String(this.paused);
        if (this.paused) {
            if (this.frameId !== null) {
                cancelAnimationFrame(this.frameId);
                this.frameId = null;
            }
            this.clearAllInput(performance.now());
            this.clock = resetSimulationAccumulator(this.clock, null);
            this.previousFrame = null;
        } else {
            this.clock = createSimulationClock();
            this.previousFrame = null;
            this.requestFrame();
        }
    }

    onMotionChange(event) {
        if (event.matches) {
            this.cue = settleCue();
            this.model = advanceMissionSequence(this.model, 0, true);
            if (this.model.state === "succeeded") {
                this.clearAllInput(performance.now());
            }
            this.render();
        }
    }

    requestFrame() {
        if (this.frameId === null && !this.paused && !this.destroyed) {
            this.frameId = requestAnimationFrame((timestamp) => this.frame(timestamp));
        }
    }

    frame(timestamp) {
        this.frameId = null;
        const previousState = this.model.state;
        let elapsed = 0;
        if (this.previousFrame !== null) {
            const frameSeconds = (timestamp - this.previousFrame) / 1000;
            elapsed = frameSeconds >= 0 && frameSeconds <= 0.1 ? frameSeconds : 0;
        }
        this.previousFrame = timestamp;
        if (this.model.state === "preflight") {
            this.cue = advanceCue(this.cue, elapsed);
        } else if (this.model.state === "flying") {
            const result = advanceSimulation(this.clock, this.model, timestamp, {
                reducedMotion: this.motion.matches,
            });
            this.clock = result.clock;
            this.model = result.model;
            if (result.discarded) {
                this.clearAllInput(timestamp);
            }
        } else if (["landed", "deploying", "powering", "departing"].includes(this.model.state)) {
            this.model = advanceMissionSequence(this.model, elapsed, this.motion.matches);
        }
        if (previousState === "flying" && this.model.state !== "flying") {
            this.clearAllInput(timestamp);
        }
        this.render();
        const needsFrame =
            this.model.state === "flying" ||
            ["landed", "deploying", "powering", "departing"].includes(this.model.state) ||
            (this.model.state === "preflight" && this.cue.state === "running");
        if (needsFrame) {
            this.requestFrame();
        }
    }

    render() {
        const pose = this.model.pose;
        const leftPlume = plumeForThrust(this.model.commanded.left);
        const rightPlume = plumeForThrust(this.model.commanded.right);
        this.root.dataset.missionState = this.model.state;
        this.root.dataset.nocPower = this.model.nocPower ? "on" : "off";
        this.root.dataset.nocStage = String(this.model.nocStage);
        this.root.dataset.cue = this.cue.state;
        this.root.dataset.paused = String(this.paused);
        this.root.style.setProperty("--lander-x", `${pose.x * 10}px`);
        this.root.style.setProperty("--lander-y", `${548 - pose.y * 10}px`);
        this.root.style.setProperty("--lander-angle", `${pose.angle}deg`);
        this.root.style.setProperty("--left-plume-scale", String(leftPlume.scaleY));
        this.root.style.setProperty("--right-plume-scale", String(rightPlume.scaleY));
        this.root.style.setProperty("--left-plume-opacity", String(leftPlume.opacity));
        this.root.style.setProperty("--right-plume-opacity", String(rightPlume.opacity));
        if (this.model.agentPosition) {
            this.root.style.setProperty("--agent-x", `${this.model.agentPosition.x * 10}px`);
            this.root.style.setProperty("--agent-y", `${548 - this.model.agentPosition.y * 10}px`);
        }
        if (this.status.textContent !== this.model.status && this.model.status) {
            this.status.textContent = this.model.status;
        }
    }

    destroy() {
        if (this.destroyed) {
            return;
        }
        this.destroyed = true;
        if (this.frameId !== null) {
            cancelAnimationFrame(this.frameId);
            this.frameId = null;
        }
        this.abortController.abort();
        this.clearAllInput(performance.now());
        this.model = createPreflightModel();
        this.cue = settleCue();
        this.paused = false;
        this.shell.tabIndex = -1;
        this.shell.removeAttribute("role");
        this.shell.removeAttribute("aria-label");
        this.shell.removeAttribute("aria-describedby");
        this.scene.removeAttribute("aria-hidden");
        this.startButton.disabled = true;
        this.startButton.hidden = true;
        this.controls.hidden = true;
        this.status.textContent = "";
        this.render();
    }
}

export const landerGameController = new LanderGameController();
