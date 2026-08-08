import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	hasPendingCurrentAssistantResponses,
	isChatEventForCurrentConversation,
	isInactiveEventForQueueRun,
	waitForSocketSession
} from './chat-stream';

afterEach(() => {
	vi.useRealTimers();
});

const createSocket = () => {
	let connectListener: (() => void) | null = null;
	const socket = {
		id: undefined as string | undefined,
		connected: false,
		once: vi.fn((_event: 'connect', listener: () => void) => {
			connectListener = listener;
		}),
		off: vi.fn(),
		connect: vi.fn(),
		emitConnect(sessionId: string) {
			this.id = sessionId;
			this.connected = true;
			connectListener?.();
		}
	};

	return socket;
};

describe('waitForSocketSession', () => {
	it('returns an already connected session immediately', async () => {
		const socket = createSocket();
		socket.id = 'session-1';
		socket.connected = true;

		await expect(waitForSocketSession(socket)).resolves.toBe('session-1');
		expect(socket.once).not.toHaveBeenCalled();
	});

	it('waits for a connecting socket to receive its session id', async () => {
		const socket = createSocket();
		const session = waitForSocketSession(socket);

		socket.emitConnect('session-2');

		await expect(session).resolves.toBe('session-2');
		expect(socket.connect).toHaveBeenCalledTimes(1);
		expect(socket.off).toHaveBeenCalled();
	});

	it('falls back after the connection timeout', async () => {
		vi.useFakeTimers();
		const socket = createSocket();
		const session = waitForSocketSession(socket, 100);

		await vi.advanceTimersByTimeAsync(100);

		await expect(session).resolves.toBeNull();
	});
});

describe('isChatEventForCurrentConversation', () => {
	it('accepts events for the active chat', () => {
		expect(isChatEventForCurrentConversation('chat-1', 'chat-1', 'message-1', {})).toBe(true);
	});

	it('accepts an early first-message event before the backend returns the new chat id', () => {
		expect(
			isChatEventForCurrentConversation('chat-new', '', 'message-local', {
				'message-local': {}
			})
		).toBe(true);
	});

	it('rejects events from another chat', () => {
		expect(
			isChatEventForCurrentConversation('chat-2', 'chat-1', 'message-1', {
				'message-1': {}
			})
		).toBe(false);
	});

	it('rejects unrelated events while a new chat is pending', () => {
		expect(
			isChatEventForCurrentConversation('chat-other', '', 'message-other', {
				'message-local': {}
			})
		).toBe(false);
	});
});

describe('queue run guards', () => {
	const responseIds = new Set(['response-current']);

	it('accepts the inactive event that belongs to the current queue run', () => {
		expect(isInactiveEventForQueueRun(responseIds, 'response-current')).toBe(true);
	});

	it('rejects delayed inactive events from an older queue run', () => {
		expect(isInactiveEventForQueueRun(responseIds, 'response-previous')).toBe(false);
		expect(isInactiveEventForQueueRun(responseIds, null)).toBe(false);
	});
});

describe('hasPendingCurrentAssistantResponses', () => {
	it('waits for every assistant response under the current user message', () => {
		expect(
			hasPendingCurrentAssistantResponses({
				currentId: 'assistant-2',
				messages: {
					user: {
						role: 'user',
						parentId: null,
						childrenIds: ['assistant-1', 'assistant-2']
					},
					'assistant-1': { role: 'assistant', parentId: 'user', done: true },
					'assistant-2': { role: 'assistant', parentId: 'user', done: false }
				}
			})
		).toBe(true);
	});

	it('ignores unfinished placeholders on an unrelated old branch', () => {
		expect(
			hasPendingCurrentAssistantResponses({
				currentId: 'current-assistant',
				messages: {
					'old-user': { role: 'user', parentId: null, childrenIds: ['old-assistant'] },
					'old-assistant': { role: 'assistant', parentId: 'old-user', done: false },
					'current-user': {
						role: 'user',
						parentId: null,
						childrenIds: ['current-assistant']
					},
					'current-assistant': { role: 'assistant', parentId: 'current-user', done: true }
				}
			})
		).toBe(false);
	});
});
