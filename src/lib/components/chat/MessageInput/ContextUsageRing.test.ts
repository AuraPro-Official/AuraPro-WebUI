import { describe, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';
import { writable } from 'svelte/store';

import ContextUsageRing from './ContextUsageRing.svelte';

vi.mock('$lib/stores', async () => {
	const { writable } = await import('svelte/store');
	return {
		models: writable([{ id: 'test-model', info: { params: { num_ctx: 4000 } } }]),
		settings: writable({})
	};
});

const createTranslations = (locale: string) => ({
	t: (key: string, variables?: Record<string, string | number>) => {
		const text = key.replace(/{{(\w+)}}/g, (_, name: string) => String(variables?.[name] ?? name));
		return `${locale}: ${text}`;
	}
});

describe('ContextUsageRing component', () => {
	it('initializes with the application i18n store before a model is selected', () => {
		const context = new Map([['i18n', writable(createTranslations('en'))]]);
		expect(() => render(ContextUsageRing, { props: { history: {} }, context }).body).not.toThrow();
	});

	it('renders model context usage without interrupting the input component', () => {
		const context = new Map([['i18n', writable(createTranslations('en'))]]);
		const { body } = render(ContextUsageRing, {
			context,
			props: {
				selectedModelIds: ['test-model'],
				history: {
					currentId: 'reply',
					messages: {
						reply: {
							role: 'assistant',
							contextUsage: { used_tokens: 1000, limit_tokens: 4000 }
						}
					}
				}
			}
		});
		expect(body).toContain('en: Context usage: 25%');
		expect(body).toContain('25%');
	});

	it('reads updated translations from the store', () => {
		const i18n = writable(createTranslations('en'));
		const context = new Map([['i18n', i18n]]);
		const props = { history: {}, selectedModelIds: ['test-model'] };
		expect(render(ContextUsageRing, { props, context }).body).toContain(
			'en: Context usage is unavailable'
		);
		i18n.set(createTranslations('es'));
		expect(render(ContextUsageRing, { props, context }).body).toContain(
			'es: Context usage is unavailable'
		);
	});
});
