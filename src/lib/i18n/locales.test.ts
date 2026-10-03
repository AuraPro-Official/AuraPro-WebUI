import { describe, expect, it } from 'vitest';
import en from './locales/en-US/translation.json';
import cn from './locales/zh-CN/translation.json';
import tw from './locales/zh-TW/translation.json';
import es from './locales/es-ES/translation.json';

const resources: Record<string, Record<string, string>> = {
	'en-US': en,
	'zh-CN': cn,
	'zh-TW': tw,
	'es-ES': es
};
const keys = [...new Set(Object.values(resources).flatMap(Object.keys))].sort();
const variables = (text: string) => (text.match(/\{\{[^}]+\}\}/g) ?? []).sort();

describe('primary language resources', () => {
	for (const [locale, messages] of Object.entries(resources)) {
		it(`${locale} includes all keys and preserves interpolation variables`, () => {
			expect(Object.keys(messages).sort()).toEqual(keys);
			for (const key of keys) {
				expect(typeof messages[key], key).toBe('string');
				expect(messages[key].trim(), key).not.toBe('');
				expect(variables(messages[key]), key).toEqual(variables(resources['en-US'][key]));
			}
		});
	}
	it('uses singular and plural forms for counted filters', () => {
		expect(en['{{count}} filters_one']).toBe('{{count}} filter');
		expect(en['{{count}} filters_other']).toBe('{{count}} filters');
		expect(es['{{count}} filters_one']).toBe('{{count}} filtro');
		expect(es['{{count}} filters_other']).toBe('{{count}} filtros');
		expect(es['{{count}} filters_many']).toBe('{{count}} filtros');
	});
});
