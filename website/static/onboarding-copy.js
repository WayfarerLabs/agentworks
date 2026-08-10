const SUCCESS_MESSAGE = "Prompt copied.";
const FAILURE_MESSAGE = "Copy failed. Select the prompt and copy it manually.";
const MANUAL_MESSAGE = "Select the prompt and copy it manually.";

export async function copyPrompt(prompt, status, clipboard) {
    try {
        await clipboard.writeText(prompt.textContent);
        status.textContent = SUCCESS_MESSAGE;
    } catch {
        status.textContent = FAILURE_MESSAGE;
    }
}

export function initializeCopy(documentObject, clipboard) {
    const prompt = documentObject.getElementById("onboarding-prompt");
    const button = documentObject.getElementById("copy-onboarding-prompt");
    const status = documentObject.getElementById("copy-status");
    if (!prompt || !button || !status) {
        return;
    }
    if (!clipboard || typeof clipboard.writeText !== "function") {
        status.textContent = MANUAL_MESSAGE;
        return;
    }
    button.hidden = false;
    button.addEventListener("click", () => copyPrompt(prompt, status, clipboard));
}

if (typeof document !== "undefined") {
    initializeCopy(document, typeof navigator === "undefined" ? undefined : navigator.clipboard);
}
