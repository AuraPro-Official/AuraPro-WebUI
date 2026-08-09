import { describe, expect, it } from 'vitest';

import { buildGlossaryLanguageSelectItems, normalizeGlossaryLanguage } from './glossaryLanguages';

describe('glossary language normalization', () => {
	it('uses one Chinese label for a language code and its Chinese name', () => {
		const items = buildGlossaryLanguageSelectItems(['pt', 'pt-BR', '葡萄牙语']);

		expect(items).toHaveLength(1);
		expect(items[0]).toMatchObject({ value: '葡萄牙语', label: '葡萄牙语' });
		expect(items[0]?.searchTerms).toEqual(expect.arrayContaining(['pt', 'pt-BR', '葡萄牙语']));
	});

	it('normalizes Chinese and regional Chinese codes to 中文', () => {
		expect(normalizeGlossaryLanguage('zh-CN')).toBe('中文');
		expect(normalizeGlossaryLanguage('繁體中文')).toBe('中文');
	});

	it('supports English names and legacy language codes as search aliases', () => {
		expect(normalizeGlossaryLanguage('Portuguese')).toBe('葡萄牙语');
		expect(normalizeGlossaryLanguage('tl')).toBe('菲律宾语');
	});

	it('keeps custom language names unchanged', () => {
		expect(normalizeGlossaryLanguage('自定义语言')).toBe('自定义语言');
	});
});
