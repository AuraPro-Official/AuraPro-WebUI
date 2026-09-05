import { afterEach, describe, expect, it, vi } from 'vitest';

import { createStreamingRenderScheduler, scheduleFrameWithFallback } from './render-scheduler';

afterEach(() => {
	vi.clearAllTimers();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('createStreamingRenderScheduler', () => {
	const setupFrames = () => {
		vi.useFakeTimers();
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
			setTimeout(() => callback(performance.now()), 16)
		);
		vi.stubGlobal('cancelAnimationFrame', clearTimeout);
	};

	it('keeps displayed text until the next update and renders the latest complete snapshot', () => {
		setupFrames();
		let content = 'First paragraph';
		let displayed = '';
		const render = vi.fn(() => (displayed = content));
		const scheduler = createStreamingRenderScheduler(render);
		scheduler.update(content.length, false);
		vi.advanceTimersByTime(16);
		expect(displayed).toBe(content);
		content += '\n\nSecond';
		scheduler.update(content.length, false);
		content += ' paragraph';
		scheduler.update(content.length, false);
		expect(displayed).toBe('First paragraph');
		vi.advanceTimersByTime(60);
		expect(displayed).toBe(content);
		expect(render).toHaveBeenCalledTimes(2);
	});

	it('does not postpone rendering indefinitely when chunks keep arriving', () => {
		setupFrames();
		const render = vi.fn();
		const scheduler = createStreamingRenderScheduler(render);
		for (let i = 0; i < 100; i++) {
			scheduler.update(50_000 + i, false);
			vi.advanceTimersByTime(10);
		}
		expect(render.mock.calls.length).toBeGreaterThanOrEqual(8);
		expect(render.mock.calls.length).toBeLessThanOrEqual(10);
		scheduler.cancel();
	});

	it('flushes immediately when done changes, even without another content update', () => {
		setupFrames();
		const render = vi.fn();
		const scheduler = createStreamingRenderScheduler(render);
		scheduler.update(50_000, false);
		vi.advanceTimersByTime(16);
		scheduler.update(50_100, false);
		scheduler.update(50_100, true);
		expect(render).toHaveBeenCalledTimes(2);
		vi.advanceTimersByTime(500);
		expect(render).toHaveBeenCalledTimes(2);
	});

	it('renders even when a hidden webview stops delivering animation frames', () => {
		vi.useFakeTimers();
		vi.stubGlobal('requestAnimationFrame', () => 1);
		vi.stubGlobal('cancelAnimationFrame', vi.fn());
		const render = vi.fn();
		const scheduler = createStreamingRenderScheduler(render);
		scheduler.update(50_000, false);
		vi.advanceTimersByTime(100);
		expect(render).toHaveBeenCalledTimes(1);
		scheduler.update(50_100, false);
		vi.advanceTimersByTime(200);
		expect(render).toHaveBeenCalledTimes(2);
	});

	it('cancels both the throttle timer and queued animation frame on teardown', () => {
		setupFrames();
		const render = vi.fn();
		const scheduler = createStreamingRenderScheduler(render);
		scheduler.update(50_000, false);
		vi.advanceTimersByTime(16);
		scheduler.update(50_100, false);
		scheduler.cancel();
		vi.advanceTimersByTime(500);
		expect(render).toHaveBeenCalledTimes(1);
		scheduler.update(50_200, false);
		scheduler.cancel();
		vi.advanceTimersByTime(500);
		expect(render).toHaveBeenCalledTimes(1);
	});
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
