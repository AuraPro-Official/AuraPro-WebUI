export const scheduleFrameWithFallback = (
	callback: () => void,
	fallbackDelayMs = 100
): (() => void) => {
	let active = true;
	let frameId: number | null = null;
	let timerId: ReturnType<typeof setTimeout> | null = null;

	const cancel = () => {
		if (!active) return;
		active = false;
		if (frameId !== null && typeof globalThis.cancelAnimationFrame === 'function') {
			globalThis.cancelAnimationFrame(frameId);
		}
		if (timerId !== null) clearTimeout(timerId);
	};

	const run = () => {
		if (!active) return;
		active = false;
		if (frameId !== null && typeof globalThis.cancelAnimationFrame === 'function') {
			globalThis.cancelAnimationFrame(frameId);
		}
		if (timerId !== null) clearTimeout(timerId);
		callback();
	};

	if (typeof globalThis.requestAnimationFrame === 'function') {
		frameId = globalThis.requestAnimationFrame(run);
	}
	timerId = setTimeout(run, fallbackDelayMs);

	return cancel;
};
