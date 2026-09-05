import { afterEach, describe, expect, it, vi } from 'vitest';

import { scheduleFrameWithFallback } from './render-scheduler';

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('scheduleFrameWithFallback', () => {
	it('runs on the next animation frame and cancels the fallback', () => {
		vi.useFakeTimers();
		const frameCallbacks: FrameRequestCallback[] = [];
		const cancelAnimationFrame = vi.fn();
		vi.stubGlobal(
			'requestAnimationFrame',
			vi.fn((callback: FrameRequestCallback) => {
				frameCallbacks.push(callback);
				return 7;
			})
		);
		vi.stubGlobal('cancelAnimationFrame', cancelAnimationFrame);
		const callback = vi.fn();

		scheduleFrameWithFallback(callback);
		frameCallbacks[0]?.(0);
		vi.advanceTimersByTime(200);

		expect(callback).toHaveBeenCalledTimes(1);
		expect(cancelAnimationFrame).toHaveBeenCalledWith(7);
	});

	it('uses the timeout when the webview does not deliver animation frames', () => {
		vi.useFakeTimers();
		vi.stubGlobal(
			'requestAnimationFrame',
			vi.fn(() => 11)
		);
		vi.stubGlobal('cancelAnimationFrame', vi.fn());
		const callback = vi.fn();

		scheduleFrameWithFallback(callback, 80);
		vi.advanceTimersByTime(79);
		expect(callback).not.toHaveBeenCalled();

		vi.advanceTimersByTime(1);
		expect(callback).toHaveBeenCalledTimes(1);
	});

	it('does not run after cancellation', () => {
		vi.useFakeTimers();
		vi.stubGlobal(
			'requestAnimationFrame',
			vi.fn(() => 13)
		);
		vi.stubGlobal('cancelAnimationFrame', vi.fn());
		const callback = vi.fn();

		const cancel = scheduleFrameWithFallback(callback);
		cancel();
		vi.advanceTimersByTime(200);

		expect(callback).not.toHaveBeenCalled();
	});
});
