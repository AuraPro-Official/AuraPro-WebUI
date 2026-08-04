import { get, type Readable } from 'svelte/store';
import type { Socket } from 'socket.io-client';

export const isSocketReady = (socket: Socket | null, authenticated: boolean) =>
	Boolean(socket?.connected && socket.id && authenticated);

export const authenticateSocket = async (
	socket: Socket | null,
	token: string | null,
	timeoutMs = 8000
): Promise<boolean> => {
	if (!socket?.connected || !token) {
		return false;
	}

	return await new Promise((resolve) => {
		let settled = false;
		const finish = (ready: boolean) => {
			if (settled) return;
			settled = true;
			clearTimeout(timeoutId);
			resolve(ready);
		};

		const timeoutId = setTimeout(() => finish(false), timeoutMs);
		socket.emit(
			'user-join',
			{ auth: { token } },
			(response: { id?: string } | null | undefined) => {
				finish(Boolean(response?.id));
			}
		);
	});
};

export const waitForSocketReady = async (
	socketStore: Readable<Socket | null>,
	readyStore: Readable<boolean>,
	timeoutMs = 10000
): Promise<Socket | null> => {
	const currentSocket = get(socketStore);
	if (isSocketReady(currentSocket, get(readyStore))) {
		return currentSocket;
	}

	return await new Promise((resolve) => {
		let settled = false;
		let unsubscribe = () => {};

		const finish = (socket: Socket | null) => {
			if (settled) return;
			settled = true;
			clearTimeout(timeoutId);
			unsubscribe();
			resolve(socket);
		};

		const timeoutId = setTimeout(() => finish(null), timeoutMs);
		unsubscribe = readyStore.subscribe((authenticated) => {
			const socket = get(socketStore);
			if (isSocketReady(socket, authenticated)) {
				finish(socket);
			}
		});

		if (settled) {
			unsubscribe();
		}
	});
};
