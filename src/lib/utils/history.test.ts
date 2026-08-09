import { describe, expect, it } from 'vitest';

import { sanitizeHistory } from './index';

describe('sanitizeHistory', () => {
	it('turns an unrecoverable missing parent into a root message', () => {
		const history: any = {
			currentId: 'assistant',
			messages: {
				assistant: {
					id: 'assistant',
					role: 'assistant',
					parentId: 'missing',
					childrenIds: [],
					content: '',
					done: false,
					timestamp: 2
				}
			}
		};

		sanitizeHistory(history);

		expect(history.currentId).toBe('assistant');
		expect(history.messages.assistant.parentId).toBeNull();
	});

	it('recovers a parent from the surviving child link', () => {
		const history: any = {
			currentId: 'child',
			messages: {
				root: {
					id: 'root',
					role: 'user',
					parentId: null,
					childrenIds: ['child'],
					timestamp: 1
				},
				child: {
					id: 'child',
					role: 'assistant',
					parentId: 'missing',
					childrenIds: [],
					timestamp: 2
				}
			}
		};

		sanitizeHistory(history);

		expect(history.messages.child.parentId).toBe('root');
		expect(history.messages.root.childrenIds).toEqual(['child']);
	});

	it('breaks parent cycles and rebuilds consistent child links', () => {
		const history: any = {
			currentId: 'a',
			messages: {
				a: {
					id: 'a',
					role: 'assistant',
					parentId: 'b',
					childrenIds: ['b'],
					timestamp: 2
				},
				b: {
					id: 'b',
					role: 'user',
					parentId: 'a',
					childrenIds: ['a'],
					timestamp: 1
				}
			}
		};

		sanitizeHistory(history);

		const visited = new Set<string>();
		let message = history.messages[history.currentId];
		while (message) {
			expect(visited.has(message.id)).toBe(false);
			visited.add(message.id);
			message = message.parentId ? history.messages[message.parentId] : null;
		}
		expect(visited.size).toBeGreaterThan(0);
	});
});
