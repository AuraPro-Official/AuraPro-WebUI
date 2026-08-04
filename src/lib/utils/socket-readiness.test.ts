import { describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import type { Socket } from 'socket.io-client';

import { authenticateSocket, isSocketReady, waitForSocketReady } from './socket-readiness';

const asSocket = (socket: Partial<Socket>) => socket as Socket;

describe('socket readiness', () => {
	it('requires a connected and authenticated socket', () => {
		expect(isSocketReady(asSocket({ connected: true, id: 'session-1' }), true)).toBe(true);
		expect(isSocketReady(asSocket({ connected: true, id: 'session-1' }), false)).toBe(false);
		expect(isSocketReady(asSocket({ connected: false, id: 'session-1' }), true)).toBe(false);
	});

	it('marks authentication ready only after the server acknowledges user-join', async () => {
		const emit = vi.fn((_event, _payload, callback) => callback({ id: 'user-1' }));
		const socket = asSocket({ connected: true, id: 'session-1', emit });

		await expect(authenticateSocket(socket, 'token')).resolves.toBe(true);
		expect(emit).toHaveBeenCalledWith(
			'user-join',
			{ auth: { token: 'token' } },
			expect.any(Function)
		);
	});

	it('waits for the authenticated readiness signal', async () => {
		const socket = asSocket({ connected: true, id: 'session-1' });
		const socketStore = writable<Socket | null>(socket);
		const readyStore = writable(false);
		const ready = waitForSocketReady(socketStore, readyStore, 1000);

		readyStore.set(true);
		await expect(ready).resolves.toBe(socket);
	});
});
