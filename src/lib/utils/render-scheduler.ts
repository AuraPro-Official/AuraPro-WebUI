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

export const createStreamingRenderScheduler = (callback: () => void) => {
	let pendingCancel: (() => void) | null = null;
	let lastRenderAt: number | null = null;

	const cancel = () => {
		pendingCancel?.();
		pendingCancel = null;
	};
	const render = () => {
		pendingCancel = null;
		callback();
		lastRenderAt = performance.now();
	};

	return {
		cancel,
		update(contentLength: number, done: boolean) {
			if (done) {
				cancel();
				render();
				return;
			}
			if (pendingCancel) return;

			// Keep existing tokens visible while coalescing updates; never debounce indefinitely.
			const interval = contentLength < 8_000 ? 32 : contentLength < 32_000 ? 50 : 100;
			const delay =
				lastRenderAt === null ? 0 : Math.max(0, interval - (performance.now() - lastRenderAt));
			if (delay === 0) {
				pendingCancel = scheduleFrameWithFallback(render);
			} else {
				const timer = setTimeout(() => {
					pendingCancel = scheduleFrameWithFallback(render);
				}, delay);
				pendingCancel = () => clearTimeout(timer);
			}
		}
	};
};
