import { describe, expect, it } from 'vitest';
import { getThinkingFilter, toggleThinkingFilter } from './thinking-filter';

describe('Thinking shortcut', () => {
	it('uses the available plugin ID instead of inventing a request flag', () => {
		expect(getThinkingFilter([{ id: 'custom_thinking', name: 'Thinking' }])?.id).toBe(
			'custom_thinking'
		);
	});
	it('prefers the stable ID and avoids ambiguous name matches', () => {
		expect(getThinkingFilter([{ id: 'thinking', name: '思考' }])?.id).toBe('thinking');
		expect(
			getThinkingFilter([
				{ id: 'a', name: 'Thinking' },
				{ id: 'b', name: 'Thinking' }
			])
		).toBeNull();
	});
	it('does not show a shortcut for unavailable or unrelated filters', () => {
		expect(getThinkingFilter([])).toBeNull();
		expect(getThinkingFilter([{ id: 'other', name: 'Thinking tools' }])).toBeNull();
	});
	it('toggles the shared selection without changing other filters or mutating state', () => {
		const selected = ['other'];
		const enabled = toggleThinkingFilter(selected, 'thinking');
		expect(enabled).toEqual(['other', 'thinking']);
		expect(selected).toEqual(['other']);
		expect(toggleThinkingFilter(enabled, 'thinking')).toEqual(['other']);
	});
});
